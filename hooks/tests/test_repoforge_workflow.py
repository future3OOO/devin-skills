#!/usr/bin/env python3
"""Real Repo Context Forge bootstrap integration with workflow state."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"
BOOTSTRAP = ROOT / "skills" / "repo-context-forge" / "scripts" / "bootstrap.py"
QUALITY_GATE = ROOT / "skills" / "production-code" / "scripts" / "code_quality_gate.py"
CANONICAL_BOOTSTRAP = Path("/home/prop_/.local/share/repo-context-forge/current/scripts/codex_context_bootstrap.py")
GITNEXUS = shutil.which("gitnexus")
OWNER_RULES = ("QG54-OWNER-COMPETITION-PRODUCTION", "QG54-OWNER-COMPETITION-TEST")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib.workflow_documents import graph_evidence_document  # noqa: E402
from hooks.tests.support import build_no_change_document, fixture_env, graph_packet  # noqa: E402


@unittest.skipUnless(CANONICAL_BOOTSTRAP.is_file(), "real Repo Context Forge source is unavailable")
class RepoForgeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-repoforge-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.intent = "record the real rendered intake packet"
        self.slug = "repoforge-workflow"
        # The shared fixture environment, not a second copy of it: it also isolates
        # HOME, which is what keeps these real intakes out of the caller's GitNexus
        # registry, analysis cache and machine-wide intake lock.
        self.env = fixture_env(self.tmp / "state")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Workflow Harness")
        self.git("remote", "add", "origin", "https://example.invalid/workflow-fixture.git")
        # A callable symbol and a dependent, so the producer's graph plan has real
        # context and impact to resolve rather than an empty single-file surface.
        (self.repo / "app.py").write_text("def compute(value):\n    return value + 1\n", encoding="utf-8")
        (self.repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(1)\n", encoding="utf-8"
        )
        self.git("add", "app.py", "caller.py")
        self.git("commit", "-q", "-m", "base")
        begun = self.pass_state("begin", "--slug", self.slug, "--intent", self.intent)
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args: str) -> None:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def pass_state(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKFLOW), *args, "--repo", str(self.repo)],
            cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def bootstrap_command(
        self,
        *,
        intent: str | None = None,
        out: Path | None = None,
        mode: str = "intent",
        gitnexus_mode: str = "off",
        map_build: str = "never",
        base: str | None = None,
    ) -> list[str]:
        described = self.intent if intent is None else intent
        command = [
            sys.executable, str(BOOTSTRAP), "--repo", str(self.repo),
            "--workflow-slug", self.slug, "--mode", mode,
            "--map-build", map_build, "--gitnexus-mode", gitnexus_mode, "--top", "5",
        ]
        command += ["--base", base] if base else []
        # An empty intent is passed as no intent at all, which is what leaves a clean
        # local checkout with no target surface for the producer to block on.
        command += ["--intent", described] if described else []
        return command + (["--out", str(out)] if out is not None else [])

    def bootstrap(self, *, timeout: int = 120, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.bootstrap_command(**kwargs), cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout,
        )

    def graph_command(self, **kwargs: object) -> list[str]:
        """The same public Adapter over a real graph plan instead of `--gitnexus-mode off`.

        Local mode against a dirty dependent, because that is what gives the producer a
        target to resolve: with no target the packet plans no checks, and a resolved
        result over an empty plan carries no graph facts to record.

        The producer is real and GitNexus still indexes; only the SoulForge map is
        skipped, because nothing these tests assert reads it. Measured on this
        fixture: 11.7s per run with the map, 1.8s without, across 30 runs.
        """
        (self.repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(2)\n", encoding="utf-8"
        )
        return self.bootstrap_command(mode="local", gitnexus_mode="auto", map_build="never", **kwargs)

    def graph_bootstrap(self, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.graph_command(**kwargs), cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600,
        )

    def status(self) -> dict[str, object]:
        result = self.pass_state("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def evidence(self, evidence_id: str) -> dict[str, object]:
        result = self.pass_state("evidence", "--evidence-id", evidence_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_sha256_repo_records_projection_and_reaches_advisor_checkpoint(self) -> None:
        marker = "SHA256_WORKFLOW_NOT_READY"
        repo = self.tmp / "sha256-repo"
        repo.mkdir()
        env = self.env | {"DEVIN_WORKFLOW_STATE_ROOT": str(self.tmp / "sha256-state")}

        def git(*args: str) -> str:
            result = subprocess.run(
                ["git", *args], cwd=repo, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return result.stdout.strip()

        git("init", "-q", "--object-format=sha256")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "Workflow Harness")
        git("remote", "add", "origin", "https://example.invalid/workflow-sha256.git")
        (repo / "app.py").write_text(
            "def compute(value):\n    return value + 1\n", encoding="utf-8"
        )
        (repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(1)\n", encoding="utf-8"
        )
        git("add", "app.py", "caller.py")
        git("commit", "-q", "-m", "base")
        head = git("rev-parse", "HEAD")
        self.assertEqual(len(head), 64)

        slug = "repoforge-sha256"
        intent = "record the real SHA-256 compute projection"
        begun = subprocess.run(
            [sys.executable, str(WORKFLOW), "begin", "--repo", str(repo),
             "--slug", slug, "--intent", intent],
            cwd=repo, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        (repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(2)\n", encoding="utf-8"
        )

        forged = subprocess.run(
            [sys.executable, str(BOOTSTRAP), "--repo", str(repo),
             "--workflow-slug", slug, "--mode", "local", "--map-build", "auto",
             "--gitnexus-mode", "auto", "--top", "5", "--intent", intent],
            cwd=repo, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600,
        )
        self.assertEqual(forged.returncode, 0, marker + "\n" + forged.stdout + forged.stderr)

        checkpoint = subprocess.run(
            [sys.executable, str(WORKFLOW), "checkpoint", "--repo", str(repo),
             "--phase", "preflight-advice"],
            cwd=repo, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(
            checkpoint.returncode, 0, marker + "\n" + checkpoint.stdout + checkpoint.stderr,
        )
        payload = json.loads(checkpoint.stdout)
        self.assertTrue(payload["ready"], marker)
        self.assertEqual(payload["passStartOid"], head, marker)
        self.assertEqual(len(payload["activeCandidateTree"]), 64, marker)

    def test_corrupt_authoritative_ledger_refuses_before_the_bootstrap_runs(self) -> None:
        state = self.status()
        database = (Path(self.env["DEVIN_WORKFLOW_STATE_ROOT"])
                    / str(state["repo"]["key"]) / "workflow.sqlite3")
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'repo_key'",
                ("different-repository",),
            )
            connection.commit()
        finally:
            connection.close()
        before = database.read_bytes()

        refused = self.bootstrap()

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(refused.stdout, "", "the external bootstrap ran for corrupt state")
        self.assertEqual(
            refused.stderr,
            "<blocker>cannot bind Repo Context Forge to the active workflow: "
            "workflow database repository identity does not match this checkout</blocker>\n",
        )
        self.assertEqual(database.read_bytes(), before)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_real_bootstrap_advances_workflow_without_extra_persisted_records(self) -> None:
        direct = self.graph_bootstrap()
        self.assertEqual(direct.returncode, 0, direct.stdout + direct.stderr)
        self.assertIn("REPO_CONTEXT_FORGE_REQUIRED_INTAKE", direct.stdout)
        state = self.status()
        self.assertEqual(state["repoContextForge"], "passed")
        self.assertEqual(state["phase"], "repo-context-forge")
        state_dir = Path(self.env["DEVIN_WORKFLOW_STATE_ROOT"])
        self.assertFalse(any(path.name in {"packets", "repoforge"} for path in state_dir.rglob("*")))

        output = self.tmp / "packet.txt"
        redirected = self.graph_bootstrap(out=output)
        self.assertEqual(redirected.returncode, 0, redirected.stdout + redirected.stderr)
        self.assertEqual(redirected.stdout, "")
        self.assertIn("REPO_CONTEXT_FORGE_REQUIRED_INTAKE", output.read_text(encoding="utf-8"))
        self.assertEqual(self.status()["repoContextForge"], "passed")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_no_remote_source_gap_reaches_advisor_checkpoint(self) -> None:
        marker = "NO_REMOTE_SOURCE_PROVENANCE_BLOCKED"
        self.git("remote", "remove", "origin")
        self.git("branch", "-M", "main")

        forged = self.graph_bootstrap(base="main")

        self.assertEqual(forged.returncode, 0, marker + "\n" + forged.stdout + forged.stderr)
        state = self.status()
        self.assertEqual((state["repoContextForge"], state["gitnexus"]), ("passed", "passed"), marker)
        evidence = self.evidence(str(state["repoContextForgeEvidence"]))["document"]
        projection = evidence["advisorProjection"]
        self.assertEqual(projection["sourceRepo"], {"gap": "source_repo_unavailable"}, marker)
        self.assertEqual(projection["expectedCandidateTree"], projection["indexedCandidateTree"], marker)
        checkpoint = self.pass_state("checkpoint", "--phase", "preflight-advice")
        self.assertEqual(checkpoint.returncode, 0, marker + "\n" + checkpoint.stdout + checkpoint.stderr)
        self.assertTrue(json.loads(checkpoint.stdout)["ready"], marker)

    def governed_bootstrap(self, *, dirty: bool, mode: str | None = None) -> subprocess.CompletedProcess[str]:
        """The public wrapper on a branch one commit past main, with or without an
        uncommitted candidate on top: the producer's own mode choice, or the caller's."""
        # A tracked .gitignore: without one the producer's pr-mode receipt never
        # publishes (its SoulForge ignore-line cleanup cannot check out an untracked file).
        (self.repo / ".gitignore").write_text(".gitnexus/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "ignore the graph index")
        self.git("branch", "-M", "main")
        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "app.py").write_text("def compute(value):\n    return value + 2\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "feature")
        if dirty:
            (self.repo / "probe.py").write_text(
                "from app import compute\n\n\ndef probe():\n    return compute(3)\n", encoding="utf-8"
            )
        return subprocess.run(
            [sys.executable, str(BOOTSTRAP), "--repo", str(self.repo), "--workflow-slug", self.slug,
             "--map-build", "auto", "--gitnexus-mode", "auto", "--top", "5", "--base", "main",
             "--intent", self.intent, *(["--mode", mode] if mode else [])],
            cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600,
        )

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_clean_governed_branch_keeps_the_producers_mode(self) -> None:
        marker = "CLEAN_GOVERNED_MODE_CHANGED"
        forged = self.governed_bootstrap(dirty=False)
        self.assertEqual(forged.returncode, 0, marker + "\n" + forged.stdout + forged.stderr)
        self.assertIn("mode: pr", forged.stdout, marker)
        checkpoint = self.pass_state("checkpoint", "--phase", "preflight-advice")
        self.assertTrue(json.loads(checkpoint.stdout)["ready"], marker + ": " + checkpoint.stdout)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_dirty_governed_branch_reaches_the_advisor_checkpoint(self) -> None:
        marker = "DIRTY_GOVERNED_CHECKOUT_BLOCKS_ADVISOR_CHECKPOINT"
        forged = self.governed_bootstrap(dirty=True)
        self.assertEqual(forged.returncode, 0, marker + "\n" + forged.stdout + forged.stderr)
        checkpoint = self.pass_state("checkpoint", "--phase", "preflight-advice")
        self.assertEqual(checkpoint.returncode, 0, marker + "\n" + checkpoint.stdout + checkpoint.stderr)
        self.assertTrue(json.loads(checkpoint.stdout)["ready"], marker + ": " + checkpoint.stdout)
        self.assertIn("mode: local", forged.stdout, marker)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_dirty_governed_branch_overrides_an_explicit_pr_mode(self) -> None:
        """The producer honors its last --mode: a caller's pr on a dirty governed
        checkout would bind the projection to HEAD, so the wrapper's local wins."""
        marker = "EXPLICIT_PR_MODE_BINDS_A_DIRTY_GOVERNED_CHECKOUT_TO_HEAD"
        forged = self.governed_bootstrap(dirty=True, mode="pr")
        self.assertEqual(forged.returncode, 0, marker + "\n" + forged.stdout + forged.stderr)
        self.assertIn("mode: local", forged.stdout, marker)
        checkpoint = self.pass_state("checkpoint", "--phase", "preflight-advice")
        self.assertTrue(json.loads(checkpoint.stdout)["ready"], marker + ": " + checkpoint.stdout)

    def ledger_bytes(self, state: dict[str, object]) -> bytes:
        return (Path(self.env["DEVIN_WORKFLOW_STATE_ROOT"])
                / str(state["repo"]["key"]) / "workflow.sqlite3").read_bytes()

    def test_an_unresolved_producer_result_refuses_and_mutates_nothing(self) -> None:
        """A planned graph the producer could not resolve is not evidence."""
        (self.repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(2)\n", encoding="utf-8"
        )
        before = self.status()
        ledger = self.ledger_bytes(before)

        # A real two-check plan with the graph engine disabled: the producer reports
        # the analysis blocked rather than resolved, and still exits zero.
        refused = self.bootstrap(mode="local", map_build="auto", gitnexus_mode="off")

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("REPO_CONTEXT_FORGE_REQUIRED_INTAKE", refused.stdout)
        self.assertIn("no resolved graph result", refused.stderr)
        self.assertIn("rerun the bootstrap", refused.stderr)
        self.assertEqual(self.status(), before)
        self.assertEqual(self.ledger_bytes(before), ledger, "a refused producer changed the ledger")

    def test_a_blocked_packet_never_reaches_workflow_state(self) -> None:
        """The producer's own blocker exits non-zero, so nothing is recorded from it."""
        before = self.status()
        ledger = self.ledger_bytes(before)

        blocked = self.bootstrap(mode="local", intent="")

        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("blocker", blocked.stdout)
        self.assertEqual(self.status(), before)
        self.assertEqual(self.ledger_bytes(before), ledger, "a blocked packet changed the ledger")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_packet_that_planned_no_checks_still_records_its_resolved_result(self) -> None:
        """How many checks a packet plans is the producer's call, not a refusal here."""
        # The producer plans a file_context check for any content-bearing file and
        # blocks an intent that matches no symbol, so a zero-check packet needs a
        # .gitignore-only tree in repo mode. Committing the .gitnexus/ ignore rule
        # keeps GitNexus's own ignore write from mutating the analysis candidate.
        (self.repo / ".gitignore").write_text(".gitnexus/\n", encoding="utf-8")
        self.git("rm", "-q", "app.py", "caller.py")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "zero-checkable surface")

        recorded = self.bootstrap(mode="repo", intent="", gitnexus_mode="auto", timeout=600)

        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        state = self.status()
        self.assertEqual(state["repoContextForge"], "passed")
        graph = self.evidence(str(state["repoContextForgeEvidence"]))["document"]["graph"]
        self.assertEqual((graph["status"], graph["entries"]), ("resolved", []))

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_same_slug_replacement_rejects_the_stale_producer(self) -> None:
        """A pass replaced while the producer runs never receives its graph result."""
        process = subprocess.Popen(
            self.graph_command(), cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            # Popen returning only means the child was forked, and the bootstrap resolves
            # repository identity — git, realpath and cksum, three subprocesses of its own —
            # before it captures the workflow id. Only the producer starting proves the
            # capture already happened, so match the child's command line instead of
            # accepting any child; otherwise the replacement below can still land first and
            # become the instance the child captures, which fails on exit 0.
            children = Path(f"/proc/{process.pid}/task/{process.pid}/children")
            producer = ""
            deadline = time.monotonic() + 300
            while not producer and time.monotonic() < deadline:
                for pid in children.read_text().split():
                    try:
                        command = Path(f"/proc/{pid}/cmdline").read_bytes()
                    except OSError:
                        continue  # an identity subprocess that exited between the two reads
                    if str(CANONICAL_BOOTSTRAP).encode("utf-8") in command:
                        producer = pid
                        break
                if not producer:
                    time.sleep(0.001)
            self.assertTrue(producer, "the real producer never started, so no capture was observed")

            replaced = self.pass_state("begin", "--slug", self.slug, "--intent", "replacement pass")
            self.assertEqual(replaced.returncode, 0, replaced.stdout + replaced.stderr)
            self.assertIsNone(
                process.poll(),
                "the producer finished before the replacement landed; the stale path was not exercised",
            )
            stdout, stderr = process.communicate(timeout=600)
        finally:
            process.kill()

        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn("cannot record Repo Context Forge graph evidence", stderr)
        # The specific cause, not just the adapter's wrapper: any WorkflowError produces
        # the line above, so only this one proves the stale instance was what refused.
        self.assertIn("--workflow-id does not match the active workflow instance", stderr)
        state = self.status()
        self.assertEqual(state["workflowId"], json.loads(replaced.stdout)["workflowId"])
        self.assertEqual(state["repoContextForge"], "pending")
        self.assertNotIn("repoContextForgeEvidence", state)

    def advance_to_tdd(self) -> None:
        """The real recorders between recorded context evidence and the TDD gate."""
        state = self.status()
        slug, wid = str(state["slug"]), str(state["workflowId"])
        declaration = self.tmp / "design-absent.json"
        declaration.write_text(json.dumps({"schemaVersion": 1, "status": "absent", "reason": "test pass has no governing design"}), encoding="utf-8")
        for step in (
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
             "--source", "codex-advisor", "--verdict", "completed", "--design-declaration", str(declaration)),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid,
             "--stage", "preflight", "--findings", "none"),
        ):
            result = self.pass_state(*step)
            self.assertEqual(result.returncode, 0, " ".join(step) + "\n" + result.stdout + result.stderr)
        # This suite proves growth-per-cycle accounting, not candidate policy;
        # its free-form tdd() plumbing rides the legacy path, so the fixture
        # commits a map-less pre-Behavior-Map preflight - a setup shortcut
        # producing the imported-legacy document shape (the real importer path
        # is proven by LegacyImportFreeFormTests) - inside the suite's own
        # state-root environment. Setup only.
        document = build_no_change_document("issue-106 typed verification fixture")
        document.pop("behaviorMap", None)
        doc_path = self.tmp / "legacy-preflight.json"
        doc_path.write_text(json.dumps(document), encoding="utf-8")
        committed = subprocess.run(
            [sys.executable, "-c",
             "import json, sys; sys.path.insert(0, sys.argv[1]); "
             "from hooks.lib.repo_identity import resolve_repo_identity; "
             "from hooks.lib import workflow_state as w; "
             "w.commit_evidence_phase(resolve_repo_identity(sys.argv[2]), sys.argv[3], sys.argv[4], "
             "'preflight', json.load(open(sys.argv[5])))",
             str(ROOT), str(self.repo), slug, wid, str(doc_path)],
            cwd=str(ROOT), env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(committed.returncode, 0, committed.stdout + committed.stderr)

    def tdd(self, phase: str, behavior: str, result_value: int,
            *, expected: str | None = None) -> subprocess.CompletedProcess[str]:
        """One real RED or GREEN through the recorder CLI, over the fixture's own Seam."""
        args = [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(self.repo), "--slug", self.slug,
                "--phase", phase, "--behavior", behavior, "--seam", "app.compute import Interface"]
        if expected:
            args += ["--expected-failure", expected]
        args += ["--", sys.executable, "-c",
                 f"import app; assert app.compute(1) == {result_value}, 'AssertionError: {behavior}'"]
        return subprocess.run(
            args, cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def compute_returns(self, offset: int) -> None:
        self.repo.joinpath("app.py").write_text(
            f"def compute(value):\n    return value + {offset}\n", encoding="utf-8"
        )

    def advance_to_typed_verification(self) -> None:
        """The real recorders between recorded context evidence and typed verification."""
        self.advance_to_tdd()
        state = self.status()
        slug, wid = str(state["slug"]), str(state["workflowId"])
        gate = subprocess.run(
            [sys.executable, str(QUALITY_GATE), "check", "--repo", str(self.repo), "--json"],
            cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        baseline = self.tmp / "baseline-gate.json"
        baseline.write_text(gate.stdout, encoding="utf-8")
        for step in (
            ("tdd", "--slug", slug, "--not-required",
             "fixture pass proves evidence wiring, not a fixture behavior change"),
            ("record-production-code", "--slug", slug, "--workflow-id", wid, "--input", str(baseline)),
            ("set-phase", "--phase", "implementation", "--status", "passed"),
        ):
            result = self.pass_state(*step)
            self.assertEqual(result.returncode, 0, " ".join(step) + "\n" + result.stdout + result.stderr)

    def typed_quality_gate_run(self, base_ref: str) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        """One typed quality-gate verification and the run entry it recorded."""
        verified = self.pass_state(
            "verify", "--slug", self.slug, "--kind", "quality-gate", "--base-ref", base_ref,
        )
        state = self.status()
        document = self.evidence(str(state["verificationLatestEvidence"]))["document"]
        runs = document["runs"]
        self.assertTrue(runs, "typed verification recorded no run")
        return verified, runs[-1]

    def owner_states(self, gate_payload: dict[str, object]) -> dict[str, dict[str, object]]:
        """Each owner rule's per-evaluation state finding from the gate verdict."""
        states = {
            str(item["ruleId"]): item
            for item in gate_payload["findings"]
            if str(item["ruleId"]) in OWNER_RULES and item["region"]["scope"] == "evaluation"
        }
        self.assertEqual(sorted(states), sorted(OWNER_RULES), gate_payload["findings"])
        return states

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_typed_verification_hands_recorded_graph_evidence_to_the_owner_rules(self) -> None:
        """A governed pass with uncommitted edits reaches a complete owner-rule verdict.

        The whole chain is real: the producer analyzes the dirty candidate, the
        bootstrap records the evidence, and typed verification must hand that
        recorded evidence to the gate so both owner-competition rules evaluate
        instead of reporting the unestablished-scope gap.
        """
        self.git("branch", "-M", "main")
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.advance_to_typed_verification()

        verified, run = self.typed_quality_gate_run("main")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIsNone(run["bindingError"], run["bindingError"])
        for rule_id, finding in sorted(self.owner_states(run["gate"]).items()):
            gaps = finding["completeness"]["gaps"]
            self.assertNotEqual(finding["status"], "incomplete", f"{rule_id} could not evaluate: {gaps}")
            self.assertTrue(finding["completeness"]["complete"], f"{rule_id} gaps: {gaps}")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_evidence_bound_to_a_different_snapshot_keeps_the_owner_rules_incomplete(self) -> None:
        """Falsification: an edit after the recorded analysis is named as staleness.

        The gate captures the moved tree, the recorded evidence still names the
        analyzed one, and its own binding check must report the stale gap —
        never silently accept, never rebind.
        """
        self.git("branch", "-M", "main")
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.advance_to_typed_verification()
        (self.repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(3)\n", encoding="utf-8"
        )

        verified, run = self.typed_quality_gate_run("main")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        for rule_id, finding in sorted(self.owner_states(run["gate"]).items()):
            self.assertEqual(finding["status"], "incomplete", f"{rule_id}: {finding}")
            self.assertIn(
                "external graph evidence is stale: it does not name the evaluated snapshot",
                finding["completeness"]["gaps"],
                f"{rule_id} did not name the stale binding",
            )

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_bootstrap_records_the_producer_graph_result_as_workflow_evidence(self) -> None:
        """One public bootstrap binds the producer's own resolved graph result to this pass."""
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.assertIn("REPO_CONTEXT_FORGE_REQUIRED_INTAKE", forged.stdout)

        state = self.status()
        self.assertEqual(state["repoContextForge"], "passed")
        evidence_id = state.get("repoContextForgeEvidence")
        self.assertIsInstance(evidence_id, str, f"no producer evidence was recorded: {state}")

        record = self.evidence(str(evidence_id))
        self.assertEqual(record["kind"], "repo-context-forge")
        self.assertEqual(record["workflowId"], state["workflowId"])
        graph = record["document"]["graph"]
        self.assertEqual(graph["status"], "resolved")
        self.assertEqual(graph["unresolved_checks"], [])
        self.assertTrue(graph["entries"], "the recorded graph result carries no entries")
        self.assertTrue(
            all(entry["status"] == "resolved" and entry["resolved_identity"] for entry in graph["entries"])
        )
        self.assertEqual(
            graph["authority"]["source_repository"],
            str(Path(self.repo).resolve()),
            "the evidence is not bound to this source checkout",
        )
        self.assertTrue(graph["producer_revision"]["commit"])
        projection = record["document"]["advisorProjection"]
        self.assertEqual(projection["schemaVersion"], 1)
        self.assertEqual(
            (projection["expectedCandidateTree"], projection["indexedCandidateTree"]),
            (state["activeCandidateTree"], state["activeCandidateTree"]),
        )
        checkpoint_result = self.pass_state("checkpoint", "--phase", "preflight-advice")
        self.assertEqual(
            checkpoint_result.returncode, 0,
            checkpoint_result.stdout + checkpoint_result.stderr,
        )
        checkpoint = json.loads(checkpoint_result.stdout)
        self.assertEqual(checkpoint["advisorProjectionEvidence"], evidence_id)
        self.assertEqual(checkpoint["advisorProjection"], projection)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_mutation_and_status_responses_share_graph_candidate_readiness(self) -> None:
        marker = "MUTATION_STATUS_GRAPH_READINESS_DIVERGED"
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        analyzed = self.status()
        workflow_id = str(analyzed["workflowId"])
        (self.repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(3)\n", encoding="utf-8"
        )

        paused = self.pass_state(
            "pause", "--slug", self.slug, "--workflow-id", workflow_id,
            "--reason", "measure candidate readiness",
        )
        self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
        mutation, status = json.loads(paused.stdout), self.status()
        self.assertEqual(
            (
                mutation["activeCandidateTree"], mutation["repoContextForge"], mutation["gitnexus"],
                status["activeCandidateTree"], status["repoContextForge"], status["gitnexus"],
            ),
            (
                status["activeCandidateTree"], "pending", "pending",
                mutation["activeCandidateTree"], "pending", "pending",
            ),
            marker + json.dumps({"mutation": mutation, "status": status}, sort_keys=True),
        )

    def git_out(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout.strip()

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_rerun_keeps_the_first_recorded_base_and_reports_the_conflict(self) -> None:
        """The recorded base is immutable for the pass: a rerun that resolves a
        different commit keeps the original and says so, because a moving base
        would make successive per-edit measurements incoherent."""
        fork = self.git_out("rev-parse", "HEAD")
        self.git("branch", "base-main")
        forged = self.graph_bootstrap(base="base-main")
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.assertEqual(self.status().get("baseOid"), fork)

        (self.repo / "feature.py").write_text("def grown():\n    return 1\n", encoding="utf-8")
        self.git("add", "feature.py")
        self.git("commit", "-q", "-m", "advance the branch")
        self.git("branch", "-f", "base-main")
        moved = self.git_out("rev-parse", "base-main")
        self.assertNotEqual(moved, fork)

        rerun = self.graph_bootstrap(base="base-main")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertEqual(self.status().get("baseOid"), fork, "a rerun replaced the immutable base")
        self.assertIn(f"pass base already recorded as {fork}", rerun.stderr)
        self.assertIn(f"this bootstrap resolved {moved}", rerun.stderr)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_pass_without_a_resolvable_base_records_no_base_oid(self) -> None:
        """Honest absence: when the producer resolves no base, nothing is recorded."""
        self.git("branch", "-m", "feature-work")
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.assertNotIn("baseOid", self.status())

    def stack(self) -> str:
        """A two-branch stack: feat1 is the lower PR, feat2 the leaf whose PR base
        is feat1 (gh's per-branch `gh-merge-base` config). Returns feat1's tip,
        the merge-base the leaf's pass must record."""
        self.git("checkout", "-q", "-b", "feat1")
        (self.repo / "lower.py").write_text("def lower():\n    return 1\n", encoding="utf-8")
        self.git("add", "lower.py")
        self.git("commit", "-q", "-m", "feat1 carries an escape")
        self.git("checkout", "-q", "-b", "feat2")
        (self.repo / "leaf.py").write_text("def leaf():\n    return 2\n", encoding="utf-8")
        self.git("add", "leaf.py")
        self.git("commit", "-q", "-m", "feat2 is clean")
        self.git("config", "branch.feat2.gh-merge-base", "feat1")
        return self.git_out("rev-parse", "feat1")

    def gate(self, base: str) -> dict[str, object]:
        result = subprocess.run(
            [sys.executable, str(QUALITY_GATE), "check", "--repo", str(self.repo), "--base-ref", base, "--json"],
            cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        # A failed gate must fail here, not as a parse error or an empty verdict.
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_run_cancelled_after_its_packet_records_no_base(self) -> None:
        """Cancellation, not refusal: the packet is already rendered when the run
        is killed, which is the window the recording sits in."""
        feat1 = self.stack()
        # stderr goes to the void rather than an undrained pipe, and the marker
        # wait has a deadline, so a stalled producer fails this test rather than
        # holding the suite open.
        cancelled = subprocess.Popen(
            self.graph_command(), cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        # The watchdog, not the loop, bounds the wait: a child that emits nothing
        # at all would otherwise block the iterator before any check.
        watchdog = threading.Timer(600, cancelled.kill)
        watchdog.start()
        emitted = False
        try:
            for line in cancelled.stdout:
                if "END_REPO_CONTEXT_FORGE_REQUIRED_INTAKE" in line:
                    emitted = True
                    break
        finally:
            watchdog.cancel()
            cancelled.kill()
            cancelled.wait(timeout=120)
            cancelled.stdout.close()
        self.assertTrue(emitted, "CANCELLED_RUN_LEFT_A_BASE: the producer never emitted its packet")
        self.assertNotIn("baseOid", self.status(), "CANCELLED_RUN_LEFT_A_BASE")

        # The other way a run can end before recording: the producer refuses its
        # arguments, so no packet exists at all.
        refused = subprocess.run(
            self.graph_command() + ["--top", "not-a-number"], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600,
        )
        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
        self.assertNotIn("baseOid", self.status(), "INTERRUPTED_RUN_LEFT_A_BASE")

        retry = self.graph_bootstrap()
        self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
        self.assertEqual(self.status().get("baseOid"), feat1, "CANCELLED_RUN_LEFT_A_BASE")
        self.assertNotIn("pass base already recorded", retry.stderr, "CANCELLED_RUN_LEFT_A_BASE")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_an_abbreviated_base_option_is_not_overridden(self) -> None:
        """The producer accepts every unambiguous abbreviation of `--base`, so an
        explicit base spelled `--ba` is still the caller's choice."""
        root = self.git_out("rev-parse", "HEAD")
        self.git("branch", "caller-base", root)
        self.stack()
        forged = subprocess.run(
            self.graph_command() + ["--ba", "caller-base"], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600,
        )
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.assertEqual(self.status().get("baseOid"), root, "ABBREVIATED_BASE_WAS_OVERRIDDEN")

    def resolve_base(self) -> str | None:
        """The adapter over this checkout, seeing only the `gh` the case placed:
        the search path is the case's own directory, so an absent `gh` is really
        absent and no installed one reaches the network."""
        binaries = self.tmp / "bin"
        binaries.mkdir(exist_ok=True)
        return self.adapter_module()._pr_base_ref(self.repo, env={"PATH": str(binaries)})

    def test_a_base_name_this_checkout_lacks_resolves_to_nothing(self) -> None:
        """The fall-through turns on this: a name no ref carries must resolve to
        nothing, so a caller holding it can still reach its next signal. Real
        refs in a real checkout; the name a pull request would supply is a
        string either way."""
        marker = "UNFETCHED_BASE_NAME_DID_NOT_RESOLVE_TO_NOTHING"
        bootstrap = self.adapter_module()
        self.git("branch", "lower")
        self.assertIsNone(bootstrap._branch_ref(self.repo, "never-fetched", ("origin",)), marker)
        self.assertEqual(
            bootstrap._branch_ref(self.repo, "lower", ("origin",)), "refs/heads/lower",
            "A_LOCAL_BRANCH_STOPPED_RESOLVING",
        )

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_stacked_branch_records_the_merge_base_with_its_pr_base_branch(self) -> None:
        """Measured on GitNexus #18: the producer's fallback base is the fork point
        from main, so a stacked pass measured the whole stack. The leaf's pass
        records the merge-base with the branch its PR merges into."""
        feat1 = self.stack()
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.assertEqual(self.status().get("baseOid"), feat1, "STACKED_BASE_NOT_RECORDED")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_the_typed_gate_on_a_stacked_branch_measures_only_its_own_delta(self) -> None:
        """The gate is given the base the pass recorded, never one chosen by
        hand, and it never sees the lower PR's files."""
        self.stack()
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        verdict = self.gate(str(self.status().get("baseOid")))
        self.assertNotIn("lower.py", verdict.get("changedFilesSample"), "STACKED_GATE_MEASURED_OTHER_PR")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_revalidation_on_a_stacked_branch_resolves_the_same_pr_base(self) -> None:
        feat1 = self.stack()
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        command = self.bootstrap_command(mode="local", gitnexus_mode="auto", map_build="never") + ["--revalidate"]
        rerun = subprocess.run(
            command, cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=600,
        )
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertNotIn("pass base already recorded", rerun.stderr, "STACKED_REVALIDATE_MOVED_BASE")
        self.assertIn("<base_ref>refs/heads/feat1</base_ref>", rerun.stdout, "STACKED_REVALIDATE_MOVED_BASE")
        self.assertEqual(self.status().get("baseOid"), feat1, "STACKED_REVALIDATE_MOVED_BASE")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_nothing_the_lookup_later_says_replaces_the_first_recorded_base(self) -> None:
        """One pass, every perturbation that could move the answer: the config
        repointed, the origin repointed, and the base branch itself advanced.
        The first recorded OID wins each time and the rerun says so."""
        marker = "REPOINTED_LOOKUP_REPLACED_FIRST_BASE"
        feat1 = self.stack()
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.assertEqual(self.status().get("baseOid"), feat1, marker)

        perturbations = (
            ("config", lambda: self.git("config", "branch.feat2.gh-merge-base", "main")),
            ("origin", lambda: self.git("remote", "set-url", "origin", "https://example.invalid/moved.git")),
            ("advance", lambda: self.git("branch", "-f", "feat1", "HEAD")),
        )
        for name, perturb in perturbations:
            perturb()
            rerun = self.graph_bootstrap()
            self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
            self.assertEqual(self.status().get("baseOid"), feat1, f"{marker}: after the {name} change")
            self.assertIn(f"pass base already recorded as {feat1}", rerun.stderr, f"{marker}: {name}")

    def test_an_upstream_targeted_branch_keeps_the_producers_upstream_base(self) -> None:
        """The producer prefers upstream/main over origin/main. A branch whose PR
        goes to the parent project has no PR in the origin fork, so only its
        `gh-merge-base` config names a base; that config must not flip the pass
        onto the fork's diverged main."""
        upstream = self.git_out("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/upstream/main", upstream)
        (self.repo / "fork.py").write_text("def forked():\n    return 1\n", encoding="utf-8")
        self.git("add", "fork.py")
        self.git("commit", "-q", "-m", "the fork's main moved on")
        self.git("update-ref", "refs/remotes/origin/main", self.git_out("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "work.py").write_text("def work():\n    return 2\n", encoding="utf-8")
        self.git("add", "work.py")
        self.git("commit", "-q", "-m", "work for the parent project")
        self.git("config", "branch.feature.gh-merge-base", "main")

        # Which ref the adapter picks is decided in _pr_base_ref; recording that
        # ref as the base is proved once, by the stacked-branch test below.
        self.assertEqual(self.resolve_base(), "refs/remotes/upstream/main", "UPSTREAM_MAIN_PREFERENCE_LOST")

    def test_a_tag_coexisting_with_the_base_branch_is_not_measured(self) -> None:
        """git resolves a bare name through refs/tags before refs/heads, so the
        name handed to the producer must carry the namespace the adapter checked."""
        self.git("checkout", "-q", "-b", "lower")
        (self.repo / "lower.py").write_text("def lower():\n    return 1\n", encoding="utf-8")
        self.git("add", "lower.py")
        self.git("commit", "-q", "-m", "the lower branch")
        branch = self.git_out("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "leaf")
        (self.repo / "leaf.py").write_text("def leaf():\n    return 2\n", encoding="utf-8")
        self.git("add", "leaf.py")
        self.git("commit", "-q", "-m", "the leaf branch")
        # A tag of the same name, on a different commit: git prefers it for a bare name.
        self.git("tag", "lower", "HEAD")
        self.git("config", "branch.leaf.gh-merge-base", "lower")

        self.assertEqual(self.resolve_base(), "refs/heads/lower", "TAG_SHADOWED_THE_BASE_BRANCH")
        self.assertEqual(self.git_out("rev-parse", "refs/heads/lower"), branch, "TAG_SHADOWED_THE_BASE_BRANCH")

    def test_an_impostor_host_supplies_no_github_slug(self) -> None:
        """The slug pattern names GitHub itself, not any host whose name ends in
        it: a lookup bound to the wrong project asks GitHub about a repository
        this checkout does not have."""
        bootstrap = self.adapter_module()
        self.assertTrue(hasattr(bootstrap, "github_slug"), "NON_GITHUB_HOST_TREATED_AS_GITHUB")
        self.assertIsNone(
            bootstrap.github_slug("https://notgithub.com/owner/repo"),
            "NON_GITHUB_HOST_TREATED_AS_GITHUB",
        )
        for origin in ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git",
                       "ssh://git@github.com/owner/repo.git"):
            self.assertEqual(bootstrap.github_slug(origin), "owner/repo", "NON_GITHUB_HOST_TREATED_AS_GITHUB")

    def test_a_github_looking_path_supplies_no_slug(self) -> None:
        """The slug comes from the origin's host, not from a segment of its path."""
        bootstrap = self.adapter_module()
        self.assertTrue(hasattr(bootstrap, "github_slug"), "ORIGIN_PATH_TREATED_AS_GITHUB_HOST")
        for origin in ("https://example.invalid/mirror/@github.com/owner/repo",
                       "https://example.invalid/github.com/owner/repo",
                       "git@example.invalid:mirror/github.com/owner/repo.git"):
            self.assertIsNone(bootstrap.github_slug(origin), "ORIGIN_PATH_TREATED_AS_GITHUB_HOST")

    def test_a_tag_sharing_the_base_name_is_not_the_base_branch(self) -> None:
        """The recorded base names the branch the PR merges into; a tag that
        happens to carry that name resolves to a commit but is not that branch."""
        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "work.py").write_text("def work():\n    return 2\n", encoding="utf-8")
        self.git("add", "work.py")
        self.git("commit", "-q", "-m", "work")
        self.git("tag", "release-base", "HEAD")
        self.git("config", "branch.feature.gh-merge-base", "release-base")

        # No branch carries the name, so the configured signal answers nothing
        # and the producer's own base selection stands.
        self.assertIsNone(self.resolve_base(), "TAG_ACCEPTED_AS_BASE_BRANCH")

    def test_a_local_base_branch_whose_name_holds_a_slash_is_resolved(self) -> None:
        """The estate's branches are `fix/...`; a base that exists only locally
        must be found under refs/heads, not read as a remote and its branch."""
        self.git("checkout", "-q", "-b", "fix/lower")
        (self.repo / "lower.py").write_text("def lower():\n    return 1\n", encoding="utf-8")
        self.git("add", "lower.py")
        self.git("commit", "-q", "-m", "the lower branch")
        lower = self.git_out("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "fix/leaf")
        (self.repo / "leaf.py").write_text("def leaf():\n    return 2\n", encoding="utf-8")
        self.git("add", "leaf.py")
        self.git("commit", "-q", "-m", "the leaf branch")
        self.git("config", "branch.fix/leaf.gh-merge-base", "fix/lower")

        self.assertEqual(self.resolve_base(), "refs/heads/fix/lower", "LOCAL_SLASHED_BASE_NOT_RESOLVED")
        self.assertEqual(self.git_out("rev-parse", "refs/heads/fix/lower"), lower, "LOCAL_SLASHED_BASE_NOT_RESOLVED")

    def analysis_repo(self, output: str) -> str:
        found = re.search(r"repo=([^;\s]+)", output)
        self.assertIsNotNone(found, f"no GitNexus repo in the intake:\n{output}")
        return found.group(1)

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_reruns_of_an_indexed_pass_leave_its_pass_start_index_alone(self) -> None:
        """One retained pass, four intakes. The branch delta is committed before
        the first intake, so the pass starts clean in pr mode; the reruns then
        vary only the worktree, never HEAD. Each must analyse the candidate slot
        while the index the pass started against stays the one it recorded."""
        marker = "PASS_START_INDEX_WAS_REPOPULATED"
        base = self.git_out("rev-parse", "HEAD")
        (self.repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(2)\n", encoding="utf-8"
        )
        self.git("add", "caller.py")
        self.git("commit", "-q", "-m", "the branch delta, committed before the pass starts")

        first = self.pr_intake(base)
        baseline = self.status().get("passStartSnapshot")
        self.assertIsInstance(baseline, dict, f"{marker}: no pass-start snapshot recorded")
        self.assertIn("<mode>pr</mode>", first, "CLEAN_RERUN_LEFT_PR_MODE")
        delta = self.packet_targets(first)
        self.assertIn("caller.py", delta, "CLEAN_RERUN_LOST_ITS_BRANCH_DELTA_TARGETS")

        # A dirty overlay, never committed: the worktree changes, HEAD does not.
        (self.repo / "caller.py").write_text(
            "from app import compute\n\n\ndef run():\n    return compute(3)\n", encoding="utf-8"
        )
        dirty = self.run_intake(self.bootstrap_command(
            mode="local", gitnexus_mode="auto", map_build="never"))
        candidate = self.analysis_repo(dirty)
        self.assertNotEqual(candidate, baseline["indexRepo"], marker)

        revalidated = self.run_intake(self.bootstrap_command(
            mode="local", gitnexus_mode="auto", map_build="never") + ["--revalidate"])
        self.assertEqual(self.analysis_repo(revalidated), candidate,
                         "REVALIDATE_TOOK_A_THIRD_CHECKOUT")

        # The overlay restored, so the pass is clean again on the same HEAD.
        self.git("checkout", "--", "caller.py")
        clean = self.pr_intake(base)
        self.assertIn("<mode>pr</mode>", clean, "CLEAN_RERUN_LEFT_PR_MODE")
        self.assertEqual(self.packet_targets(clean), delta,
                         "CLEAN_RERUN_LOST_ITS_BRANCH_DELTA_TARGETS")
        # One slot per pass, whatever the mode: the clean rerun rejoins the
        # candidate the dirty one opened rather than taking a third checkout.
        self.assertEqual(self.analysis_repo(clean), candidate, marker)

        self.assertEqual(self.status().get("passStartSnapshot"), baseline, marker)
        # The recorded pathname surviving is not the promise; the index it names
        # must still be the one the pass started against, read from the index
        # itself rather than from the workflow record that describes it.
        live = json.loads((Path(baseline["indexPath"]) / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(str(live.get("lastCommit")), baseline["sourceCommit"], marker)
        self.assertEqual(str(live.get("indexedTree")), baseline["indexedTree"], marker)

    def pr_intake(self, base: str) -> str:
        return self.run_intake(self.bootstrap_command(
            mode="pr", gitnexus_mode="auto", map_build="never", base=base, intent=""))

    def run_intake(self, command: list[str]) -> str:
        result = subprocess.run(
            command, cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False, timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def packet_targets(self, output: str) -> list[str]:
        """The paths the packet actually selected, not a substring of its prose."""
        block = re.search(r"<targets>(.*?)</targets>", output, re.S)
        self.assertIsNotNone(block, f"no <targets> in the intake:\n{output}")
        return re.findall(r'<file path="([^"]+)"', block.group(1))

    def adapter_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("repoforge_bootstrap_under_test", BOOTSTRAP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_a_branch_with_no_pr_base_signal_keeps_the_producers_base(self) -> None:
        main = self.git_out("rev-parse", "HEAD")
        self.git("checkout", "-q", "-b", "feature")
        (self.repo / "feature.py").write_text("def grown():\n    return 1\n", encoding="utf-8")
        self.git("add", "feature.py")
        self.git("commit", "-q", "-m", "feature off main")
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.assertEqual(self.status().get("baseOid"), main, "MAIN_BASED_PASS_CHANGED_BASE")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_the_recorder_counts_cycle_openings_and_nothing_else(self) -> None:
        """`tddCycleCount` is the recorder's own count of cycle-opening REDs.

        Every other outcome leaves it alone: a rerun of the active candidate, the
        GREEN that closes a cycle, the reopen a GREEN regression records under the
        same ambiguous `tdd-reopen` action, and a RED that no longer fails.
        """
        forged = self.graph_bootstrap()
        self.assertEqual(forged.returncode, 0, forged.stdout + forged.stderr)
        self.advance_to_tdd()
        self.assertNotIn("tddCycleCount", self.status(), "a pass with no cycle already counted one")

        first = self.tdd("red", "compute adds two", 3, expected="AssertionError")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(self.status().get("tddCycleCount"), 1, "the first valid RED opened no cycle")

        rerun = self.tdd("red", "compute adds two", 3, expected="AssertionError")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertEqual(self.status().get("tddCycleCount"), 1, "a rerun of the active candidate counted again")

        self.compute_returns(2)
        green = self.tdd("green", "compute adds two", 3)
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        self.assertEqual(self.status().get("tddCycleCount"), 1, "GREEN counted as a cycle opening")

        second = self.tdd("red", "compute adds three", 4, expected="AssertionError")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.status().get("tddCycleCount"), 2, "the next tracer RED opened no cycle")

        self.compute_returns(3)
        second_green = self.tdd("green", "compute adds three", 4)
        self.assertEqual(second_green.returncode, 0, second_green.stdout + second_green.stderr)

        # A GREEN that regresses reopens the cycle through the same recorder
        # action a cycle-opening RED uses, which is exactly why the count cannot
        # be reconstructed from the ledger.
        self.compute_returns(2)
        regressed = self.tdd("green", "compute adds three", 4)
        self.assertEqual(regressed.returncode, 2, regressed.stdout + regressed.stderr)
        self.assertEqual(self.status()["tdd"], "in-progress", "the regression did not reopen the cycle")
        self.assertEqual(self.status().get("tddCycleCount"), 2, "a regression reopen counted as a cycle opening")

        # A RED that no longer fails is not a cycle: it proves nothing.
        self.compute_returns(3)
        passing_red = self.tdd("red", "compute adds three", 4, expected="AssertionError")
        self.assertEqual(passing_red.returncode, 2, passing_red.stdout + passing_red.stderr)
        self.assertEqual(self.status().get("tddCycleCount"), 2, "an invalid RED counted as a cycle opening")


class GraphEvidenceContractTests(unittest.TestCase):
    """The producer-result contract, at the validation Interface the Adapter uses.

    The bootstrap drives identity resolution and the producer from one `--repo`, so a
    packet naming a different checkout cannot be produced through it. The check still
    has to hold, so it is exercised where it lives.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="graph-evidence-"))
        self.root = self.tmp / "repo"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def document_for(self, packet: dict[str, object], **binding: object) -> dict[str, object]:
        path = self.tmp / "packet.json"
        path.write_text(json.dumps(packet), encoding="utf-8")
        return graph_evidence_document(
            str(path), slug="contract", workflow_id="wid", source_root=str(self.root),
            canonical_source_repo="example.invalid/workflow-fixture", **binding,
        )

    def packet(self) -> dict[str, object]:
        packet = graph_packet(str(self.root), "c" * 40, "a" * 40)
        packet["git"]["merge_base"] = "b" * 40
        packet["advisorProjection"]["sourceBaseOid"] = "b" * 40
        return packet

    def test_recorded_projection_targets_keep_only_the_advisor_fields(self) -> None:
        # Issue #191: a real projection carried 154,449 bytes of per-file ranking
        # metadata in the wrapper's format against an 8,000-byte cap.
        marker = "PROJECTION_TARGETS_UNTRIMMED"
        packet = self.packet()
        packet["advisorProjection"]["targets"] = [{
            "path": "hooks/lib/tdd_workflow.py", "surface_role": "production", "rank": 4,
            "changed_symbols": [{"name": "_map_update", "kind": "function", "line": 672, "end_line": 783}],
            "symbols": [{"name": "_map_update"}], "why_selected": ["changed in base...HEAD diff"],
            "analysis_repo": "/cache/analysis-worktrees/x", "changed_ranges": [[678, 679]],
            "cochanges": [], "dependent_count": 0, "dirty_kinds": ["unstaged"], "graph_neighbors": [],
            "in_soulforge_map": True, "intent_required_file": False, "intent_required_symbols": [],
            "line_count": 791, "pagerank": 0.159, "priority_score": 2500.0,
            "rank_signals": ["changed_file"], "scope_contaminated": True,
            "soulforge_impact": {"dependents": []}, "source_dirty_overlap": True, "symbol_count": 20,
        }]
        document = self.document_for(packet)
        target = document["advisorProjection"]["targets"][0]
        self.assertEqual(target, {
            "path": "hooks/lib/tdd_workflow.py", "surface_role": "production", "rank": 4,
            "changed_symbols": ["_map_update"], "why_selected": ["changed in base...HEAD diff"],
        }, marker + ": " + json.dumps(target)[:300])

    def test_recorded_projection_targets_keep_why_selected(self) -> None:
        marker = "PROJECTION_WHY_SELECTED_DROPPED"
        packet = self.packet()
        packet["advisorProjection"]["targets"] = [{
            "path": "hooks/lib/tdd_workflow.py", "surface_role": "production", "rank": 4,
            "changed_symbols": [], "symbols": [], "pagerank": 0.1,
            "why_selected": ["changed in base...HEAD diff", "contains symbols overlapping changed hunks"],
        }]
        target = self.document_for(packet)["advisorProjection"]["targets"][0]
        self.assertIn("why_selected", target, marker)
        self.assertEqual(target, {
            "path": "hooks/lib/tdd_workflow.py", "surface_role": "production", "rank": 4, "changed_symbols": [],
            "why_selected": ["changed in base...HEAD diff", "contains symbols overlapping changed hunks"],
        }, marker + ": " + json.dumps(target)[:300])

    def test_a_packet_for_another_checkout_is_refused(self) -> None:
        foreign = self.tmp / "elsewhere"
        packet = self.packet()
        packet["target_state"] = {"source_repo": str(foreign)}
        with self.assertRaises(ValueError) as refusal:
            self.document_for(packet)
        # Both halves, so the refusal has to name the checkout it rejected as well
        # as the one it wanted; matching the expected root alone would survive a
        # message that never says what it actually read.
        self.assertEqual(
            str(refusal.exception),
            f"the packet was produced for {str(foreign)!r}, not {self.root}",
        )

        packet = self.packet()
        packet["gitnexus"]["analysis"]["authority"]["source_repository"] = str(foreign)
        with self.assertRaisesRegex(ValueError, str(foreign), msg="FOREIGN_GRAPH_IDENTITY_ACCEPTED"):
            self.document_for(packet)

    def test_a_projection_for_another_canonical_source_is_refused(self) -> None:
        packet = self.packet()
        packet["advisorProjection"]["sourceRepo"] = "github.com/foreign-owner/foreign-repo"
        marker = "FOREIGN_ADVISOR_SOURCE_REPO_ACCEPTED"
        with self.assertRaises(ValueError, msg=marker) as refusal:
            self.document_for(packet)
        self.assertEqual(
            str(refusal.exception),
            "the advisor projection was produced for 'github.com/foreign-owner/foreign-repo', "
            "not 'example.invalid/workflow-fixture'",
            marker,
        )

    def test_a_projection_for_another_merge_base_is_refused(self) -> None:
        packet = self.packet()
        packet["advisorProjection"]["sourceBaseOid"] = "d" * 40
        marker = "FOREIGN_SOURCE_BASE_OID_ACCEPTED"
        with self.assertRaises(ValueError, msg=marker) as refusal:
            self.document_for(packet)
        self.assertEqual(
            str(refusal.exception),
            f"the advisor projection was produced for source base {'d' * 40!r}, "
            f"not {'b' * 40!r}",
            marker,
        )

    def test_a_projection_for_another_committed_head_is_refused(self) -> None:
        packet = self.packet()
        packet["advisorProjection"]["committedHeadOid"] = "d" * 40
        marker = "FOREIGN_COMMITTED_HEAD_OID_ACCEPTED"
        with self.assertRaises(ValueError, msg=marker) as refusal:
            self.document_for(packet)
        self.assertEqual(
            str(refusal.exception),
            f"the advisor projection was produced for committed head {'d' * 40!r}, "
            f"not {'a' * 40!r}",
            marker,
        )

    def test_a_projection_for_the_packet_head_is_accepted_with_distinct_merge_base(self) -> None:
        marker = "VALID_COMMITTED_HEAD_REJECTED"
        document = self.document_for(self.packet())
        projection = document["advisorProjection"]
        self.assertEqual(projection["committedHeadOid"], "a" * 40, marker)
        self.assertEqual(projection["sourceBaseOid"], "b" * 40, marker)
        self.assertEqual(document["graph"]["status"], "resolved", marker)
        self.assertEqual(document["workflowId"], "wid", marker)
        self.assertNotIn("gateContext", document, marker)
        self.assertNotIn("gateContextGap", document, marker)

    def test_a_diverged_base_tip_does_not_replace_merge_base_provenance(self) -> None:
        marker = "DIVERGED_BASE_TIP_REJECTED"
        document = self.document_for(
            self.packet(),
            snapshot={"base": "a" * 40, "candidate": "c" * 40},
        )
        self.assertEqual(document["advisorProjection"]["sourceBaseOid"], "b" * 40, marker)
        self.assertEqual(document["gateContext"]["base"], "a" * 40, marker)

    def test_invalid_advisor_projections_are_refused_before_recording(self) -> None:
        mutations = {
            "unsupported schema": lambda projection: projection.__setitem__("schemaVersion", 2),
            "missing producer": lambda projection: projection.__setitem__("producerRevision", {}),
            "missing source": lambda projection: projection.__setitem__("sourceRepo", ""),
            "missing base": lambda projection: projection.__setitem__("sourceBaseOid", ""),
            "candidate mismatch": lambda projection: projection.__setitem__("indexedCandidateTree", "d" * 40),
            "unresolved graph": lambda projection: projection["graph"].__setitem__("status", "blocked"),
            "required omission": lambda projection: projection["graph"].__setitem__("requiredOmissions", ["missing"]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                packet = self.packet()
                projection = packet["advisorProjection"]
                self.assertIsInstance(projection, dict)
                mutate(projection)
                with self.assertRaises(ValueError):
                    self.document_for(packet)

    def test_advisory_coverage_gaps_remain_retained(self) -> None:
        packet = self.packet()
        projection = packet["advisorProjection"]
        self.assertIsInstance(projection, dict)
        projection["coverageGaps"] = [{"kind": "absent_symbol", "reference": "optional"}]
        document = self.document_for(packet)
        self.assertEqual(document["advisorProjection"]["coverageGaps"], projection["coverageGaps"])

    def test_a_snapshot_binding_records_the_gate_shaped_context(self) -> None:
        document = self.document_for(
            self.packet(),
            snapshot={"base": "b" * 40, "candidate": "c" * 40},
        )
        self.assertEqual(document["gateContext"], {
            "base": "b" * 40,
            "candidate": "c" * 40,
            "symbols": [{
                "name": "compute", "file": "app.py",
                "callers": ["Function:caller.py:run"],
            }],
        })
        self.assertNotIn("gateContextGap", document)

    def test_an_unbound_run_records_its_measured_gap_instead(self) -> None:
        document = self.document_for(
            self.packet(),
            snapshot_gap="the worktree changed during the producer run (aaaaaaaaaaaa then bbbbbbbbbbbb)",
        )
        self.assertNotIn("gateContext", document)
        self.assertIn("the worktree changed during the producer run", document["gateContextGap"])

    def test_a_binding_and_a_gap_together_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.document_for(
                self.packet(),
                snapshot={"base": "b" * 40, "candidate": "c" * 40},
                snapshot_gap="also a gap",
            )

    def test_a_binding_without_base_or_candidate_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.document_for(self.packet(), snapshot={"base": "b" * 40})


class BootstrapHelpTests(unittest.TestCase):
    def test_help_lists_the_wrapper_options(self) -> None:
        # X6R11 queried --help twice and then read the wrapper source to find these.
        marker = "WRAPPER_HELP_HIDES_ITS_OPTIONS"
        run = subprocess.run([sys.executable, str(BOOTSTRAP), "--help"], text=True, capture_output=True, check=False)
        self.assertIn("--workflow-slug", run.stdout, marker + ": " + run.stdout[-300:] + run.stderr[-300:])
        self.assertIn("--revalidate", run.stdout, marker)


@unittest.skipUnless(CANONICAL_BOOTSTRAP.is_file(), "real Repo Context Forge source is unavailable")
class IntakeSerialisationTests(unittest.TestCase):
    """One intake at a time: GitNexus rewrites its global registry without an
    atomic replace (future3OOO/GitNexus#25), so two producers running together
    can tear it and break every later intake."""

    def intake_rig(self) -> tuple[Path, list[str], dict[str, str]]:
        """A private HOME is where the real lock path lands, so this attack
        drives that computation rather than a way around it."""
        tmp = Path(tempfile.mkdtemp(prefix="intake-lock-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        lock = tmp / ".cache" / "repo-context-forge" / "intake.lock"
        lock.parent.mkdir(parents=True)
        env = {**os.environ, "HOME": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}
        command = [sys.executable, str(BOOTSTRAP), "--repo", str(repo), "--intent", "intake lock probe"]
        return lock, command, env

    def test_a_held_lock_stops_a_second_intake_before_its_producer(self) -> None:
        import fcntl

        marker = "INTAKE_RAN_WHILE_LOCK_HELD"
        lock, command, env = self.intake_rig()

        with open(lock, "a+", encoding="utf-8") as holder:  # closing releases the flock
            fcntl.flock(holder, fcntl.LOCK_EX)
            with self.assertRaises(subprocess.TimeoutExpired, msg=marker):
                subprocess.run(command, env=env, text=True, capture_output=True, timeout=8, check=False)

        # The same budget the held lock exhausted, and the producer's own exit
        # code for this empty repository: both prove the intake got past the lock.
        released = subprocess.run(command, env=env, text=True, capture_output=True, timeout=8, check=False)
        self.assertEqual(released.returncode, 1,
                         marker + ": released lock still blocked the intake: " + released.stderr[-300:])

    def producers(self, *processes: subprocess.Popen) -> list[list[int]]:
        """Every adapter's producers out of one snapshot. A ps call per adapter can
        catch one producer before a handover and its successor after, and sum the
        two readings into an overlap that never existed."""
        owners = [str(run.pid) for run in processes]
        listing = subprocess.run(["ps", "--ppid", ",".join(owners), "-o", "ppid=,pid=,args="],
                                 text=True, capture_output=True, timeout=5, check=False).stdout
        rows = [line.split(maxsplit=2) for line in listing.splitlines()]
        return [[int(pid) for parent, pid, args in rows
                 if parent == owner and str(CANONICAL_BOOTSTRAP) in args] for owner in owners]

    def stop_intake(self, process: subprocess.Popen) -> None:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            process.communicate(timeout=5)
            return
        process.communicate(timeout=5)

    def start_intake(self, repo: Path, home: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, str(BOOTSTRAP), "--repo", str(repo), "--mode", "repo",
             "--map-build", "never", "--gitnexus-mode", "auto"],
            env={**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, pipesize=4096, start_new_session=True,
        )
        self.addCleanup(self.stop_intake, process)
        return process

    def await_producer(self, process: subprocess.Popen) -> int:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            children = self.producers(process)[0]
            if children:
                return children[0]
            if process.poll() is not None:
                break
            time.sleep(0.05)
        self.fail("real intake did not start its producer within 15s")

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_adapter_death_keeps_its_live_producer_locked(self) -> None:
        import fcntl
        import signal

        marker = "CANCELLED_PARENT_RELEASED_LIVE_WRITER"
        for death in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=death):
                home = Path(tempfile.mkdtemp(prefix="intake-cancel-"))
                self.addCleanup(shutil.rmtree, home, True)
                adapter = self.start_intake(ROOT, home)
                producer = self.await_producer(adapter)
                # Freeze the real producer, not a replacement, so parent death
                # cannot race its natural completion and conceal the lock loss.
                os.kill(producer, signal.SIGSTOP)
                adapter.send_signal(death)
                adapter.wait(timeout=5)
                with open(home / ".cache/repo-context-forge/intake.lock", "a+") as lock:
                    with self.assertRaises(BlockingIOError, msg=marker):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.stop_intake(adapter)

    def intake_pair(self) -> bool:
        marker = "TWO_PRODUCERS_RAN_AT_ONCE"
        started = time.monotonic()
        home = Path(tempfile.mkdtemp(prefix="intake-concurrency-"))
        self.addCleanup(shutil.rmtree, home, True)
        repos = [home / "repo-a", home / "repo-b"]
        for repo in repos:
            subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), str(repo)],
                           check=True, timeout=30)
        first = self.start_intake(repos[0], home)
        self.await_producer(first)
        second = self.start_intake(repos[1], home)
        peak, output_outside_lock, second_started = 0, False, False
        while time.monotonic() - started < 180:
            children = self.producers(first, second)
            peak = max(peak, sum(map(len, children)))
            second_started = second_started or bool(children[1])
            if first.poll() is None and not children[0] and children[1]:
                output_outside_lock = True
            if second_started and not any(children):
                # Both adapters may be blocked on their real output pipes now.
                break
            time.sleep(0.05)
        else:
            self.fail(marker + ": real intakes exceeded 180s")
        paths = []
        for run in (first, second):
            stdout, stderr = run.communicate(timeout=30)
            self.assertEqual(run.returncode, 0, marker + ": " + stderr.decode()[-300:])
            match = re.search(rb"<analysis_repo>([^<]+)</analysis_repo>", stdout)
            self.assertIsNotNone(match, marker + ": no analysis identity")
            paths.append(match.group(1).decode())
        self.assertEqual(peak, 1, marker + f": {peak} producers were alive at once")
        registry = json.loads((home / ".gitnexus/registry.json").read_text())
        self.assertEqual(sorted(row["path"] for row in registry), sorted(paths), marker)
        listing = subprocess.run([GITNEXUS, "list"], env={**os.environ, "HOME": str(home)},
                                 text=True, capture_output=True, timeout=30, check=True)
        for row in registry:
            self.assertIn(row["name"], listing.stdout, marker)
        elapsed = time.monotonic() - started
        print(f"INTAKE_RESOURCE target={ROOT} scale=2-full-repos limit=180s observed={elapsed:.3f}s peak={peak}")
        self.assertLess(elapsed, 180, marker)
        return output_outside_lock

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_two_real_intakes_never_run_two_producers(self) -> None:
        self.intake_pair()

    @unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
    def test_output_does_not_hold_the_producer_lock(self) -> None:
        self.assertTrue(self.intake_pair(), "OUTPUT_HELD_PRODUCER_LOCK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
