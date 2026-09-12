#!/usr/bin/env python3
"""Public workflow CLI contracts over the real on-disk SQLite ledger."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib.repo_identity import resolve_repo_identity
from hooks.tests.support import build_no_change_document, record_context_forge, wait_for_trace_writes

WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.rstrip("\n")


class WorkflowLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-ledger-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Workflow Harness")
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        git(self.repo, "add", "app.py")
        git(self.repo, "commit", "-q", "-m", "base")
        self.state_root = self.tmp / "state"
        self.previous_state_root = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.state_root)
        self.env = os.environ.copy()
        self.design_declaration = self.tmp / "design-absent.json"
        self.design_declaration.write_text(json.dumps({
            "schemaVersion": 1,
            "status": "absent",
            "reason": "workflow ledger test has no governing design",
        }), encoding="utf-8")

    def tearDown(self) -> None:
        if self.previous_state_root is None:
            os.environ.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
        else:
            os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = self.previous_state_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = list(args)
        if "advisor-result" in command and "--design-declaration" not in command:
            command.extend(("--design-declaration", str(self.design_declaration)))
        return subprocess.run(
            [sys.executable, str(WORKFLOW), *command], cwd=self.repo, env=self.env,
            text=True, encoding="utf-8", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )


    @property
    def identity(self):
        return resolve_repo_identity(self.repo)

    @property
    def slot(self) -> Path:
        return self.state_root / self.identity.key

    @property
    def database(self) -> Path:
        return self.slot / "workflow.sqlite3"

    def ledger_counts(self) -> dict[str, int]:
        tables = ("metadata", "workflows", "evidence", "review_manifests", "workflow_events",
                  "event_evidence", "event_manifests", "active_projection", "migration_records")
        if not self.database.exists(): return {table: 0 for table in tables}
        with sqlite3.connect(self.database) as connection:
            existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    if table in existing else 0 for table in tables}

    def begin(self, slug: str = "ledger") -> dict[str, object]:
        result = self.cli("begin", "--repo", str(self.repo), "--slug", slug)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def prepare_preflight_ready(self, slug: str = "atomic") -> tuple[dict[str, object], Path]:
        state = self.begin(slug)
        record_context_forge(self.repo, self.tmp)
        workflow_id = str(state["workflowId"])
        for command in (
            ("advisor-result", "--slug", slug, "--workflow-id", workflow_id, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", workflow_id, "--stage", "preflight", "--findings", "none"),
        ):
            result = self.cli(*command, "--repo", str(self.repo))
            self.assertEqual(result.returncode, 0, result.stderr)
        document = self.tmp / "preflight.json"
        document.write_text(json.dumps(build_no_change_document("ledger proof")), encoding="utf-8")
        return state, document

    def write_preflight_evidence(self, path: Path, workflow_id: str) -> None:
        """The envelope the preflight recorder writes: identity plus the document."""
        path.write_text(json.dumps({
            "schemaVersion": 1,
            "slug": "legacy",
            "workflowId": workflow_id,
            "document": build_no_change_document("legacy"),
            "recordedAt": "2026-08-01T00:00:00+00:00",
        }), encoding="utf-8")

    def legacy_state(self, *, evidence: bool = True) -> tuple[dict[str, object], Path]:
        self.slot.mkdir(parents=True, mode=0o700)
        workflow_id = uuid.uuid4().hex
        evidence_path = self.slot / "preflight-legacy.json"
        if evidence:
            self.write_preflight_evidence(evidence_path, workflow_id)
        state: dict[str, object] = {
            "schemaVersion": 1,
            "repo": self.identity.as_dict(),
            "slug": "legacy",
            "workflowId": workflow_id,
            "intent": "survive migration",
            "phase": "preflight",
            "nextAction": "tdd",
            "repoContextForge": "passed",
            "gitnexus": "passed",
            "advisorPreflight": {"source": "codex-advisor", "status": "completed", "findings": "none", "reason": None},
            "preflight": "passed",
            "preflightEvidence": str(evidence_path),
            "tdd": "pending",
            "productionCode": "pending",
            "implementation": "pending",
            "verification": "pending",
            "codeReview": {"status": "pending", "findings": "pending"},
            "finalReview": {"source": None, "status": "pending", "findings": "pending"},
            "createdAt": "2026-08-01T00:00:00+00:00",
            "updatedAt": "2026-08-01T00:00:00+00:00",
        }
        (self.slot / "workflow.json").write_text(json.dumps(state), encoding="utf-8")
        return state, evidence_path

    def test_status_without_database_or_legacy_state_is_missing_not_success(self) -> None:
        status = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(status.returncode, 2)
        self.assertIn("no active workflow", status.stderr)
        self.assertFalse(self.state_root.exists(),
                         "a read-only status created a permanent empty state slot")
        history = self.cli("history", "--repo", str(self.repo))
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertEqual(json.loads(history.stdout)["events"], [])
        self.assertFalse(self.state_root.exists(),
                         "read-only history created a permanent empty state slot")

    def test_begin_secures_a_new_default_state_root(self) -> None:
        estate_home = self.tmp / "claude-home"
        env = self.env.copy()
        env.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
        env["DEVIN_ESTATE_HOME"] = str(estate_home)
        begun = subprocess.run(
            [sys.executable, str(WORKFLOW), "begin", "--repo", str(self.repo), "--slug", "private-root"],
            cwd=self.repo, env=env, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(begun.returncode, 0, begun.stderr)
        self.assertEqual(stat.S_IMODE((estate_home / "state").stat().st_mode), 0o700)

    def test_begin_and_status_use_private_sqlite_without_a_json_snapshot(self) -> None:
        begun = self.cli("begin", "--repo", str(self.repo), "--slug", "ledger", "--intent", "test")
        self.assertEqual(begun.returncode, 0, begun.stderr)
        state = json.loads(begun.stdout)
        self.assertEqual(state["slug"], "ledger")
        self.assertEqual(state["nextAction"], "repo-context-forge")

        slot = self.state_root / resolve_repo_identity(self.repo).key
        database = slot / "workflow.sqlite3"
        self.assertTrue(database.is_file())
        self.assertFalse((slot / "workflow.json").exists())
        self.assertEqual(stat.S_IMODE(slot.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

        status = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(status.returncode, 0, status.stderr)
        projection = json.loads(status.stdout)
        self.assertEqual(projection, state)
        self.assertEqual(projection["schemaVersion"], 1)
        stable = {
            "schemaVersion", "repo", "slug", "workflowId", "phase", "nextAction",
            "repoContextForge", "gitnexus", "advisorPreflight", "preflight",
            "tdd", "productionCode", "implementation", "verification",
            "codeReview", "finalReview",
        }
        self.assertTrue(stable <= set(projection), stable - set(projection))
        rendered = json.dumps(projection, sort_keys=True)
        for private_detail in (
            "workflow.sqlite3", "workflow_events", "active_projection",
            "event_evidence", "event_manifests", "review_manifests",
        ):
            self.assertNotIn(private_detail, rendered)
        for private_key in ("databasePath", "sqlitePath", "table", "tables", "storage"):
            self.assertNotIn(private_key, projection)

    def test_authoritative_database_refuses_a_different_repository_identity(self) -> None:
        self.begin("identity")
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q")
        git(other, "config", "user.email", "test@example.invalid")
        git(other, "config", "user.name", "Workflow Harness")
        (other / "app.py").write_text("value = 2\n", encoding="utf-8")
        git(other, "add", "app.py")
        git(other, "commit", "-q", "-m", "base")
        other_identity = resolve_repo_identity(other)
        other_slot = self.state_root / other_identity.key
        other_slot.mkdir(parents=True, mode=0o700)
        shutil.copy2(self.database, other_slot / "workflow.sqlite3")

        result = subprocess.run(
            [sys.executable, str(WORKFLOW), "status", "--repo", str(other)],
            cwd=other, env=self.env, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("repository identity", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_history_retains_superseded_passes_in_event_order(self) -> None:
        first = json.loads(self.cli("begin", "--repo", str(self.repo), "--slug", "first").stdout)
        second = json.loads(self.cli("begin", "--repo", str(self.repo), "--slug", "second").stdout)

        status = json.loads(self.cli("status", "--repo", str(self.repo)).stdout)
        self.assertEqual(status["workflowId"], second["workflowId"])
        history = self.cli("history", "--repo", str(self.repo))
        self.assertEqual(history.returncode, 0, history.stderr)
        events = json.loads(history.stdout)["events"]
        self.assertEqual([event["kind"] for event in events], ["begin", "begin"])
        self.assertEqual([event["workflowId"] for event in events], [first["workflowId"], second["workflowId"]])
        self.assertEqual([event["eventId"] for event in events], sorted(event["eventId"] for event in events))

    def test_status_repairs_a_missing_or_stale_active_pointer(self) -> None:
        first = json.loads(self.cli("begin", "--repo", str(self.repo), "--slug", "first").stdout)
        second = json.loads(self.cli("begin", "--repo", str(self.repo), "--slug", "second").stdout)
        slot = self.state_root / resolve_repo_identity(self.repo).key
        database = slot / "workflow.sqlite3"

        import sqlite3
        connection = sqlite3.connect(database)
        try:
            first_event = connection.execute(
                "SELECT event_id FROM workflow_events WHERE workflow_id = ? ORDER BY event_id DESC LIMIT 1",
                (first["workflowId"],),
            ).fetchone()[0]
            connection.execute("DELETE FROM active_projection")
            connection.execute(
                "INSERT INTO active_projection(slot, workflow_id, event_id) VALUES (1, ?, ?)",
                (first["workflowId"], first_event),
            )
            connection.commit()
        finally:
            connection.close()

        repaired = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(json.loads(repaired.stdout)["workflowId"], second["workflowId"])
        connection = sqlite3.connect(database)
        try:
            pointer = connection.execute(
                "SELECT workflow_id, event_id FROM active_projection WHERE slot = 1"
            ).fetchone()
            latest = connection.execute(
                "SELECT event_id FROM workflow_events WHERE workflow_id = ? ORDER BY event_id DESC LIMIT 1",
                (second["workflowId"],),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(pointer, (second["workflowId"], latest))

        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE active_projection SET event_id = 999999 WHERE slot = 1")
            connection.commit()
        finally:
            connection.close()
        dangling = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(dangling.returncode, 0, dangling.stderr)
        connection = sqlite3.connect(database)
        try:
            repaired_pointer = connection.execute(
                "SELECT workflow_id, event_id FROM active_projection WHERE slot = 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(repaired_pointer, (second["workflowId"], latest))

    def test_invalid_transition_appends_no_event_or_evidence(self) -> None:
        self.begin("invalid")
        before = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        refused = self.cli(
            "set-phase", "--repo", str(self.repo),
            "--phase", "implementation", "--status", "passed",
        )
        self.assertEqual(refused.returncode, 2)
        after = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertEqual(after, before)
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)
        finally:
            connection.close()

    def test_later_statement_abort_rolls_back_evidence_event_and_projection(self) -> None:
        state, document = self.prepare_preflight_ready()
        connection = sqlite3.connect(self.database)
        try:
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("evidence", "workflow_events", "event_evidence")
            }
            pointer = connection.execute(
                "SELECT workflow_id, event_id FROM active_projection WHERE slot = 1"
            ).fetchone()
            connection.execute("""
                CREATE TRIGGER abort_projection BEFORE UPDATE ON active_projection
                BEGIN SELECT RAISE(ABORT, 'forced later-statement abort'); END
            """)
            connection.commit()
        finally:
            connection.close()

        result = self.cli(
            "record-preflight", "--repo", str(self.repo),
            "--slug", "atomic", "--workflow-id", str(state["workflowId"]),
            "--input", str(document),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("forced later-statement abort", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        connection = sqlite3.connect(self.database)
        try:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("evidence", "workflow_events", "event_evidence")
            }
            current_pointer = connection.execute(
                "SELECT workflow_id, event_id FROM active_projection WHERE slot = 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(after, before)
        self.assertEqual(current_pointer, pointer)

    def test_valid_legacy_import_is_atomic_logical_and_idempotent(self) -> None:
        legacy, evidence_path = self.legacy_state()
        legacy["phase"] = "tdd"
        legacy["nextAction"] = "tdd"
        legacy["tdd"] = "in-progress"
        (self.slot / "workflow.json").write_text(json.dumps(legacy), encoding="utf-8")
        tdd_path = self.slot / "tdd-legacy.json"
        tdd_command = [
            sys.executable,
            "-c",
            "from pathlib import Path; ready = Path('legacy-green').exists(); "
            "print('ready' if ready else 'not ready'); raise SystemExit(0 if ready else 1)",
        ]
        tdd_document = {
            "schemaVersion": 1,
            "slug": "legacy",
            "workflowId": legacy["workflowId"],
            "status": "pending",
            "behavior": "legacy RED survives migration",
            "seam": "workflow CLI",
            "command": shlex.join(tdd_command),
            "runs": [{
                "phase": "red", "expectedFailure": "AssertionError",
                "exitCode": 1, "timedOut": False, "outputTail": "AssertionError",
                "valid": True,
            }],
            "updatedAt": "2026-08-01T00:00:01+00:00",
        }
        tdd_path.write_text(json.dumps(tdd_document), encoding="utf-8")
        review_path = self.slot / "review-legacy.json"
        review_document = {
            "schemaVersion": 1,
            "slug": "legacy",
            "workflowId": legacy["workflowId"],
            "status": "passed",
            "resolvedModel": "legacy-reviewer",
            "reviewContextId": "legacy-context",
            "findings": [],
            "dispositions": [],
            "recordedAt": "2026-08-01T00:00:02+00:00",
        }
        review_path.write_text(json.dumps(review_document), encoding="utf-8")
        legacy_bytes = (self.slot / "workflow.json").read_bytes()
        evidence_bytes = evidence_path.read_bytes()
        tdd_bytes = tdd_path.read_bytes()
        review_bytes = review_path.read_bytes()

        imported = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        state = json.loads(imported.stdout)
        self.assertTrue(str(state["preflightEvidence"]).startswith("evidence-"))
        self.assertTrue(str(state["tddEvidence"]).startswith("evidence-"))
        expected = dict(legacy)
        expected["preflightEvidence"] = state["preflightEvidence"]
        expected["preflightLatestEvidence"] = state["preflightLatestEvidence"]
        expected["tddEvidence"] = state["tddEvidence"]
        # The legacy snapshot stores both steps passed, but neither is a readiness
        # source without producer evidence this bare pass never recorded: the retired
        # gitnexus field is derived from Repo Context Forge's evidence, and the Repo
        # Context Forge claim itself publishes as pending until the bootstrap reruns.
        self.assertEqual((legacy["gitnexus"], legacy["repoContextForge"]), ("passed", "passed"))
        expected["gitnexus"] = expected["repoContextForge"] = "pending"
        expected["activeCandidateTree"] = state["activeCandidateTree"]
        self.assertRegex(str(state["activeCandidateTree"]), r"^[0-9a-f]{40}$")
        self.assertEqual(state, expected)
        self.assertEqual((self.slot / "workflow.json").read_bytes(), legacy_bytes)
        self.assertEqual(evidence_path.read_bytes(), evidence_bytes)
        self.assertEqual(tdd_path.read_bytes(), tdd_bytes)
        self.assertEqual(review_path.read_bytes(), review_bytes)

        history = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertEqual([event["kind"] for event in history], ["legacy-imported"])
        imported_records = [
            json.loads(self.cli(
                "evidence", "--repo", str(self.repo), "--evidence-id", evidence_id,
            ).stdout)
            for evidence_id in history[0]["evidenceIds"]
        ]
        self.assertEqual(
            {record["kind"] for record in imported_records},
            {"preflight", "tdd", "code-review"},
            "instance-bound TDD/review evidence remained outside the ledger",
        )
        logical = self.cli(
            "evidence", "--repo", str(self.repo),
            "--evidence-id", str(state["preflightEvidence"]),
        )
        self.assertEqual(logical.returncode, 0, logical.stderr)
        self.assertEqual(json.loads(logical.stdout)["document"], json.loads(evidence_bytes))

        second = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(json.loads(second.stdout), state)
        again = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertEqual(again, history)

        (self.repo / "legacy-green").write_text("ready\n", encoding="utf-8")
        green = self.cli(
            "tdd", "--repo", str(self.repo), "--slug", "legacy",
            "--phase", "green", "--behavior", "legacy RED survives migration",
            "--seam", "workflow CLI", "--", *tdd_command,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        self.assertTrue(json.loads(green.stdout.splitlines()[-1])["valid"])
        self.assertEqual(json.loads(self.cli("status", "--repo", str(self.repo)).stdout)["tdd"], "passed")

    def test_mapless_import_uses_preflight_ownership_without_inventing_proof(self) -> None:
        from hooks.lib._workflow_db import evidence_write
        from hooks.lib.workflow_state import WorkflowError, annotate_tdd_evidence
        from hooks.tests.support import pending_behavior

        legacy, preflight_path = self.legacy_state()
        wid = str(legacy["workflowId"])
        review = {"schemaVersion": 1, "workflowId": wid, "slug": "legacy",
                  "recordedAt": "2026-08-01T00:00:01+00:00",
                  "findings": [{"id": "LEGACY-1", "kind": "behavioral"}]}
        intake = evidence_write(wid, "code-review", review).evidence_id
        (self.slot / "review-legacy.json").write_text(json.dumps(review))
        preflight = json.loads(preflight_path.read_text())
        owner = {**pending_behavior("BM_OWNER"), "kind": "preservation",
                 "status": "already-satisfied", "evidence": "legacy prose settlement",
                 "sourceRefs": [{"type": "finding", "evidenceId": intake, "id": "LEGACY-1"}]}
        preflight["document"]["behaviorMap"] = [owner]
        preflight_path.write_text(json.dumps(preflight))
        legacy["findingStates"] = [{"stage": "code-review", "kind": "behavioral",
                                    "status": "report-only", "findingId": "LEGACY-1",
                                    "intakeEvidenceId": intake}]
        (self.slot / "workflow.json").write_text(json.dumps(legacy))
        imported = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        history = self.cli("history", "--repo", str(self.repo))
        self.assertEqual(history.returncode, 0, history.stderr)
        json.loads(history.stdout)["events"]
        for mapped in (None, [owner], "corrupt"):
            document = {"schemaVersion": 1, "workflowId": wid, "runs": []}
            if mapped is not None:
                document["behaviorMap"] = mapped
            with self.subTest(map=mapped), self.assertRaises((WorkflowError, ValueError)) as error:
                annotate_tdd_evidence(self.identity, "legacy", wid, document)
            if mapped != "corrupt":
                self.assertIn("producer proved", str(error.exception), "IMPORTED_MAP_OWNER_WAS_LOST")
            else:
                self.assertIn("behaviorMap", str(error.exception))
            for action, before in (("status", imported), ("history", history)):
                after = self.cli(action, "--repo", str(self.repo))
                self.assertEqual(after.returncode, 0, after.stderr)
                self.assertEqual(json.loads(after.stdout), json.loads(before.stdout))
        self.assertEqual(json.loads(preflight_path.read_text()), preflight)

    def test_candidate_refused_legacy_begin_rolls_back_authority_import(self) -> None:
        marker = "GUARDED_MUTATION_PRECOMMITTED_AUTHORITY_ROWS"
        self.legacy_state(); before = self.ledger_counts(); self.slot.mkdir(parents=True, exist_ok=True)
        lock = sqlite3.connect(self.database); lock.execute("BEGIN IMMEDIATE")
        trace = self.tmp / "legacy-begin-git-trace.json"
        process = subprocess.Popen([sys.executable, str(WORKFLOW), "begin", "--repo", str(self.repo), "--slug", "replacement"],
            cwd=self.repo, env={**self.env, "GIT_TRACE2_EVENT": str(trace)}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertGreaterEqual(wait_for_trace_writes(trace), 2, marker)
            (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8"); lock.commit()
            stdout, stderr = process.communicate(timeout=10)
        finally:
            lock.close()
            if process.poll() is None: process.kill(); process.wait()
        self.assertEqual((process.returncode, "active candidate changed" in stderr.lower(), self.ledger_counts(),
                          (self.slot / "workflow.json").exists()), (2, True, before, True), marker + stdout + stderr)

    def test_incomplete_legacy_complete_commits_no_authority_rows(self) -> None:
        marker = "COMPLETE_REFUSAL_PRECOMMITTED_LEGACY_ROWS"
        self.legacy_state(); before = self.ledger_counts(); result = self.cli("complete", "--repo", str(self.repo))
        self.assertEqual((result.returncode, "workflow incomplete" in result.stderr,
                          self.ledger_counts(), (self.slot / "workflow.json").exists()),
                         (2, True, before, True), marker + result.stdout + result.stderr)

    def test_legacy_migration_uses_evidence_time_for_the_latest_tdd_candidate(self) -> None:
        legacy, _ = self.legacy_state()
        legacy["phase"] = "tdd"
        legacy["nextAction"] = "tdd"
        legacy["tdd"] = "in-progress"
        (self.slot / "workflow.json").write_text(json.dumps(legacy), encoding="utf-8")
        command = [sys.executable, "-c", "print('green')"]

        def tdd_document(behavior: str, updated_at: str) -> dict[str, object]:
            return {
                "schemaVersion": 1,
                "slug": "legacy",
                "workflowId": legacy["workflowId"],
                "status": "pending",
                "behavior": behavior,
                "seam": "workflow CLI",
                "command": shlex.join(command),
                "runs": [{
                    "phase": "red", "expectedFailure": "AssertionError",
                    "exitCode": 1, "timedOut": False, "outputTail": "AssertionError",
                    "valid": True,
                }],
                "updatedAt": updated_at,
            }

        # Filename order is deliberately the opposite of evidence time.
        (self.slot / "tdd-a-new.json").write_text(
            json.dumps(tdd_document("newest candidate", "2026-08-01T00:00:02+00:00")),
            encoding="utf-8",
        )
        (self.slot / "tdd-z-old.json").write_text(
            json.dumps(tdd_document("older candidate", "2026-08-01T00:00:01+00:00")),
            encoding="utf-8",
        )

        imported = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        state = json.loads(imported.stdout)
        evidence = self.cli(
            "evidence", "--repo", str(self.repo),
            "--evidence-id", str(state["tddEvidence"]),
        )
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertEqual(json.loads(evidence.stdout)["document"]["behavior"], "newest candidate")

        green = self.cli(
            "tdd", "--repo", str(self.repo), "--slug", "legacy",
            "--phase", "green", "--behavior", "newest candidate",
            "--seam", "workflow CLI", "--", *command,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)

    def test_legacy_schema_version_must_be_an_exact_integer(self) -> None:
        for invalid in (True, 1.0):
            with self.subTest(schemaVersion=invalid):
                shutil.rmtree(self.slot, ignore_errors=True)
                legacy, _ = self.legacy_state()
                legacy["schemaVersion"] = invalid
                (self.slot / "workflow.json").write_text(json.dumps(legacy), encoding="utf-8")
                result = self.cli("status", "--repo", str(self.repo))
                self.assertEqual(result.returncode, 2)
                self.assertIn("schema is unsupported", result.stderr)
                connection = sqlite3.connect(self.database)
                try:
                    self.assertIsNone(connection.execute(
                        "SELECT value FROM metadata WHERE key = 'authority'"
                    ).fetchone())
                finally:
                    connection.close()

    def test_history_refuses_an_invalid_older_event_instead_of_publishing_it(self) -> None:
        # history must fail closed on rows _event_state refuses, exactly like
        # every other authoritative read path — never publish them raw.
        self.begin("first")
        self.begin("second")
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE workflow_events SET state_json = '{}' "
                "WHERE event_id = (SELECT MIN(event_id) FROM workflow_events)"
            )
            connection.commit()
        finally:
            connection.close()

        status = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(status.returncode, 0, status.stderr)
        history = self.cli("history", "--repo", str(self.repo))
        self.assertEqual(history.returncode, 2, history.stdout)
        self.assertIn("invalid state schema", history.stderr)

    def test_future_event_schema_or_policy_fails_closed(self) -> None:
        for column in ("state_schema_version", "policy_version"):
            with self.subTest(column=column):
                shutil.rmtree(self.state_root, ignore_errors=True)
                self.begin(f"future-{column}")
                connection = sqlite3.connect(self.database)
                try:
                    connection.execute(
                        f"UPDATE workflow_events SET {column} = 999 "
                        "WHERE event_id = (SELECT MAX(event_id) FROM workflow_events)"
                    )
                    connection.commit()
                finally:
                    connection.close()
                result = self.cli("status", "--repo", str(self.repo))
                self.assertEqual(result.returncode, 2)
                self.assertIn("event schema or policy", result.stderr)

    def test_failed_legacy_import_leaves_json_authoritative_and_retries_once(self) -> None:
        legacy, evidence_path = self.legacy_state(evidence=False)
        legacy_bytes = (self.slot / "workflow.json").read_bytes()
        failed = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("legacy document is missing or malformed", failed.stderr)
        self.assertEqual((self.slot / "workflow.json").read_bytes(), legacy_bytes)

        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(connection.execute(
                "SELECT value FROM metadata WHERE key = 'authority'"
            ).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0], 0)
        finally:
            connection.close()

        self.write_preflight_evidence(evidence_path, str(legacy["workflowId"]))
        recovered = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        history = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertEqual(len(history), 1)

    def test_incomplete_legacy_preflight_evidence_is_rejected_and_rolled_back(self) -> None:
        legacy, evidence_path = self.legacy_state()
        evidence_path.write_text(json.dumps({
            "schemaVersion": 1,
            "workflowId": legacy["workflowId"],
            "recordedAt": "2026-08-01T00:00:00+00:00",
        }), encoding="utf-8")
        legacy_bytes = (self.slot / "workflow.json").read_bytes()

        rejected = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(rejected.returncode, 2, rejected.stdout)
        self.assertIn("legacy preflight evidence is incomplete", rejected.stderr)
        self.assertEqual((self.slot / "workflow.json").read_bytes(), legacy_bytes)
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(connection.execute(
                "SELECT value FROM metadata WHERE key = 'authority'"
            ).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0], 0)
        finally:
            connection.close()

        self.write_preflight_evidence(evidence_path, str(legacy["workflowId"]))
        recovered = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        state = json.loads(recovered.stdout)
        evidence = self.cli(
            "evidence", "--repo", str(self.repo),
            "--evidence-id", str(state["preflightEvidence"]),
        )
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertEqual(json.loads(evidence.stdout)["document"]["document"], build_no_change_document("legacy"))

    def test_referenced_legacy_evidence_without_an_owner_is_rejected(self) -> None:
        legacy, evidence_path = self.legacy_state()
        envelope = json.loads(evidence_path.read_text(encoding="utf-8"))
        del envelope["workflowId"]
        evidence_path.write_text(json.dumps(envelope), encoding="utf-8")
        legacy_bytes = (self.slot / "workflow.json").read_bytes()

        rejected = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(rejected.returncode, 2, rejected.stdout)
        self.assertIn("has no workflowId", rejected.stderr)
        self.assertEqual((self.slot / "workflow.json").read_bytes(), legacy_bytes)
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(connection.execute(
                "SELECT value FROM metadata WHERE key = 'authority'"
            ).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0], 0)
        finally:
            connection.close()

        self.write_preflight_evidence(evidence_path, str(legacy["workflowId"]))
        recovered = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_legacy_public_status_mismatch_aborts_authority_and_is_retryable(self) -> None:
        legacy, evidence_path = self.legacy_state(evidence=False)
        failed = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(failed.returncode, 2)
        self.write_preflight_evidence(evidence_path, str(legacy["workflowId"]))

        connection = sqlite3.connect(self.database)
        connection.execute("""
            CREATE TRIGGER corrupt_imported_status AFTER INSERT ON workflow_events
            BEGIN UPDATE workflow_events SET state_json = '{}' WHERE event_id = NEW.event_id; END
        """)
        connection.commit()
        connection.close()

        mismatched = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(mismatched.returncode, 2)
        self.assertIn("public status mismatch", mismatched.stderr)
        connection = sqlite3.connect(self.database)
        try:
            self.assertIsNone(connection.execute(
                "SELECT value FROM metadata WHERE key = 'authority'"
            ).fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0], 0)
            connection.execute("DROP TRIGGER corrupt_imported_status")
            connection.commit()
        finally:
            connection.close()
        recovered = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_concurrent_first_use_import_commits_exactly_once(self) -> None:
        self.legacy_state()
        commands = [
            subprocess.Popen(
                [sys.executable, str(WORKFLOW), "status", "--repo", str(self.repo)],
                cwd=self.repo, env=self.env, text=True, encoding="utf-8",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=20) + (process.returncode,) for process in commands]
        self.assertTrue(all(code == 0 for _, _, code in results), results)
        self.assertEqual(json.loads(results[0][0]), json.loads(results[1][0]))
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM migration_records").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 1)
        finally:
            connection.close()

    def test_concurrent_begins_retain_both_and_stale_producer_refuses(self) -> None:
        command = [
            sys.executable, str(WORKFLOW), "begin", "--repo", str(self.repo),
            "--slug", "concurrent",
        ]
        processes = [
            subprocess.Popen(
                command, cwd=self.repo, env=self.env, text=True, encoding="utf-8",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=20) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _, _, code in results), results)
        workflows = [json.loads(stdout) for stdout, _, _ in results]
        history = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertEqual({event["workflowId"] for event in history}, {item["workflowId"] for item in workflows})
        active = json.loads(self.cli("status", "--repo", str(self.repo)).stdout)
        inactive = next(item for item in workflows if item["workflowId"] != active["workflowId"])
        refused = self.cli(
            "advisor-result", "--repo", str(self.repo),
            "--slug", "concurrent", "--workflow-id", str(inactive["workflowId"]),
            "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed",
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("workflow instance", refused.stderr)
        after = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertEqual(after, history)

    def test_corrupt_authoritative_database_never_falls_back_to_stale_json(self) -> None:
        self.legacy_state()
        first = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.database.write_bytes(b"not a sqlite database")
        refused = self.cli("status", "--repo", str(self.repo))
        self.assertEqual(refused.returncode, 2)
        self.assertNotIn("Traceback", refused.stderr)
        self.assertIn("workflow database", refused.stderr)



if __name__ == "__main__":
    unittest.main(verbosity=2)
