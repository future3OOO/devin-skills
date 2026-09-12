#!/usr/bin/env python3
"""Public CLI contracts for production workflow state."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"
REARM = ROOT / "hooks" / "skill-discipline-rearm.py"
QUALITY_GATE = ROOT / "skills" / "production-code" / "scripts" / "code_quality_gate.py"
ADVISOR = ROOT / "skills" / "codex-advisor" / "scripts" / "ask-codex-advisor.sh"

from hooks.lib.preflight_document import SECTIONS as PREFLIGHT_SECTIONS  # noqa: E402
from hooks.tests.support import (  # noqa: E402
    build_no_change_document, graph_packet, record_context_forge,
    wait_for_trace_writes,
)

from hooks.lib._workflow_db import database_path  # noqa: E402
from hooks.lib.repo_identity import resolve_repo_identity  # noqa: E402
from hooks.lib.state_store import _active_candidate_tree  # noqa: E402
from hooks.lib.workflow_documents import design_file_declaration, graph_evidence_document  # noqa: E402
from hooks.lib.workflow_state import (  # noqa: E402
    WorkflowError, commit_evidence_phase, read_workflow, set_phase,
)


# Driven as a child process by the mid-gate mutation test. It runs outside this
# interpreter deliberately: a thread here competes for one GIL with the runner and
# can be starved for a whole gate on a two-core machine, which is how the same
# assertion failed two different ways on CI while passing locally every time.
MID_GATE_MUTATOR = '''
import os, sys, time
from pathlib import Path

target, marker = Path(sys.argv[1]), Path(sys.argv[2])
# Split so this script's own text never carries the token: `python -c` puts the
# whole program in its /proc cmdline, and a mutator that matches itself would
# start writing before the gate exists and never stop.
GATE = "code_quality" + "_gate.py"
# The gate is launched as `--repo <canonical root>`, which the workflow derives through
# `realpath -e`, so the fixture path is canonicalised here too: an alternate spelling of
# the same directory would otherwise never match and silently stop confirming anything.
REPO = str(target.parent.resolve())


def identity(pid):
    """The process start time, which pins a pid to one incarnation of it."""
    try:
        return (Path("/proc") / pid / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def gate_child():
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().decode("utf-8", "replace").split(chr(0))
        except OSError:
            continue
        # Both conditions, and against parsed argv rather than the raw text: the script
        # name alone also matches a shell whose command line merely mentions it and any
        # gate running for another repository, and confirming against one of those
        # certifies an overlap window this run never controlled.
        if not any(GATE in arg for arg in args):
            continue
        if any(args[index] == "--repo" and args[index + 1] == REPO for index in range(len(args) - 1)):
            return entry.name, identity(entry.name)
    return None, None


def record(count):
    """Atomically, so the reader cannot catch a half-written marker."""
    temporary = marker.with_name(marker.name + ".partial")
    temporary.write_text(str(count), encoding="utf-8")
    os.replace(temporary, marker)


confirmed, counter, deadline = 0, 0, time.monotonic() + 300
pid, started = None, None
while pid is None and time.monotonic() < deadline:
    pid, started = gate_child()
    if pid is None:
        time.sleep(0.001)
while pid is not None and started is not None:
    counter += 1
    target.write_text("value = %d\\n" % counter, encoding="utf-8")
    # Confirmed only once the same incarnation is still running after the write:
    # a check taken beforehand races the child's exit and would count an overlap
    # that never happened.
    if identity(pid) != started:
        break
    confirmed += 1
    record(confirmed)
    # The gate needs the machine more than this loop does; the handshake above,
    # not write volume, is what makes the overlap real.
    time.sleep(0.001)
'''


class PassLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-pass-lifecycle-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.previous_state_root = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.tmp / "state")
        self.env = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.env.pop(name, None)
        self.env.update({
            "DEVIN_WORKFLOW_STATE_ROOT": str(self.tmp / "state"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Workflow Harness")
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "base")
        self.documents = 0
        self.design_declaration = self.tmp / "design-absent.json"
        self.design_declaration.write_text(json.dumps({
            "schemaVersion": 1, "status": "absent", "reason": "test pass has no governing design",
        }), encoding="utf-8")

    def tearDown(self) -> None:
        if self.previous_state_root is None:
            os.environ.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
        else:
            os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = self.previous_state_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout.rstrip("\n")

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        values = list(args)
        if values and values[0] == "advisor-result" and "--design-declaration" not in values:
            values += ["--design-declaration", str(self.design_declaration)]
        return subprocess.run(
            [sys.executable, str(WORKFLOW), *values, "--repo", str(self.repo)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def owner_phase(self, phase: str, status: str, *, findings: str | None = None) -> None:
        set_phase(resolve_repo_identity(self.repo), phase, status, findings=findings)

    def begin_slug(self, slug: str) -> str:
        begun = self.cli("begin", "--slug", slug)
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        return json.loads(begun.stdout)["workflowId"]

    def evidence(self, evidence_id: str) -> dict[str, object]:
        result = self.cli("evidence", "--evidence-id", evidence_id)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["document"]

    def json_file(self, name: str, value: object) -> Path:
        path = self.tmp / name; path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def history_events(self) -> list[dict[str, object]]:
        result = self.cli("history")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["events"]

    def disposition_context(self) -> dict[str, str]:
        candidate = _active_candidate_tree(resolve_repo_identity(self.repo))
        return {"workflowId": json.loads(self.cli("status").stdout)["workflowId"],
                "candidateTree": candidate, "prHead": self.git("rev-parse", "HEAD")}

    def rewrite_latest_state(self, update) -> None:
        """Prepare a legacy/corrupt snapshot case inside the real ledger.

        Ordinary behavior tests use commands only. These few compatibility probes
        deliberately damage the latest authoritative event, then assert fail-closed
        public behavior.
        """
        import sqlite3
        identity = resolve_repo_identity(self.repo)
        database = Path(self.env["DEVIN_WORKFLOW_STATE_ROOT"]) / identity.key / "workflow.sqlite3"
        connection = sqlite3.connect(database)
        try:
            event_id = connection.execute(
                "SELECT event_id FROM active_projection WHERE slot = 1"
            ).fetchone()[0]
            state = json.loads(connection.execute(
                "SELECT state_json FROM workflow_events WHERE event_id = ?", (event_id,)
            ).fetchone()[0])
            update(state)
            connection.execute(
                "UPDATE workflow_events SET state_json = ? WHERE event_id = ?",
                (json.dumps(state, sort_keys=True, separators=(",", ":")), event_id),
            )
            connection.commit()
        finally:
            connection.close()

    def disposition_document(self, status: str = "fixed", consequence: str = "material", **overrides: object) -> str:
        """A structurally valid one-finding disposition document, written to a file.

        Each verdict defaults to exactly what it owes: measurement text for the
        resolved two, a reference for the follow-up.
        """
        self.documents += 1
        path = self.tmp / f"disposition-{self.documents}.json"
        owed = ({"reference": "https://example.invalid/issues/1"} if status == "accepted-follow-up"
                else {"evidence": "walked complete() with the fold applied"})
        path.write_text(json.dumps({
            "context": self.disposition_context(),
            "findings": [{"id": "ADV-1", "claim": "the fold could bypass completion"}],
            "dispositions": [{"finding_id": "ADV-1", "status": status, "kind": "nonbehavioral",
                "premise": {"claim": "the fold can bypass completion", "command": "inspect complete()", "result": "true"},
                "occurrence": {"domain": "complete()", "count": 1, "complete": True, "command": "inspect complete()", "result": "one path"},
                "materialConsequence": {"claim": "completion can be wrong", "command": "inspect result", "result": consequence},
                **owed, **overrides}],
        }), encoding="utf-8")
        return str(path)

    def finding_disposition_document(
        self, intake_id: str, status: str = "fixed", kind: str = "behavioral", consequence: str = "material",
    ) -> Path:
        self.documents += 1
        path = self.tmp / f"finding-disposition-{self.documents}.json"
        owed = ({"reference": "issue-1"} if status == "accepted-follow-up" else {"evidence": "linked proof"})
        occurrence = {"domain": "advisor finding", "count": 0, "complete": True,
                      "command": "inspect current result", "result": "count=0"}
        path.write_text(json.dumps({"context": self.disposition_context(), "intakeEvidenceId": intake_id, "dispositions": [{
            "finding_id": "SPEC-1", "status": status, "kind": kind,
            "premise": {"claim": "proof is missing", "command": "inspect proof", "result": "true"},
            "occurrence": occurrence, "materialConsequence": {"claim": "proof is blocked",
            "command": "run proof", "result": consequence}, **owed}]}), encoding="utf-8")
        return path

    def mixed_finding_disposition_document(self, intake_id: str, status: str) -> Path:
        path = self.finding_disposition_document(intake_id, status)
        document = json.loads(path.read_text(encoding="utf-8"))
        document["dispositions"].append({
            "finding_id": "SPEC-2", "status": "report-only", "kind": "nonbehavioral",
            "premise": {"claim": "documentation is incomplete", "command": "inspect docs", "result": "true"},
            "occurrence": {"domain": "advisor documentation", "count": 1, "complete": True,
                           "command": "inspect docs", "result": "one incomplete statement"},
            "materialConsequence": {"claim": "runtime behavior changes", "command": "inspect runtime",
                                    "result": "false"},
            "evidence": "documentation-only finding retained unchanged",
        })
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def review_finding_disposition_document(self, intake_id: str, status: str) -> Path:
        self.documents += 1
        path = self.tmp / f"review-finding-disposition-{self.documents}.json"
        fixed = status == "fixed"
        extra = {"evidence": "GREEN and reassessment recorded" if fixed else "measured current-tree evidence"}
        occurrence = {"domain": "the complete fixture repository", "count": 0, "complete": True,
                      "command": "python -m unittest test_review_fix", "result": "passes"}
        path.write_text(json.dumps({
            "context": self.disposition_context(),
            "intakeEvidenceId": intake_id,
            "dispositions": [{
                "finding_id": "SPEC-1", "status": status, "kind": "behavioral",
                "premise": {"claim": "app.value is wrong", "command": "inspect app.py",
                            "result": "value = 2; the original premise is now false" if fixed else "value = 1"},
                "occurrence": occurrence,
                "materialConsequence": {"claim": "the result is wrong", "command": "inspect app.value",
                                        "result": "callers observe the corrected value" if fixed else "callers observe the wrong value"},
                **extra,
            }],
        }), encoding="utf-8")
        return path

    def dispose(self, slug: str, wid: str, stage: str, findings: str, *input_path: str) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "advisor-disposition", "--slug", slug, "--workflow-id", wid,
            "--stage", stage, "--findings", findings, *(("--input", *input_path) if input_path else ()),
        )

    def checkpoint(self, phase: str) -> dict[str, object]:
        result = self.cli("checkpoint", "--phase", phase)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def run_cli(self, *transitions: tuple[str, ...]) -> None:
        for transition in transitions:
            result = self.cli(*transition)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def advance_to_context_forge(self) -> None:
        record_context_forge(self.repo, self.tmp)

    def advance_to_preflight(self, slug: str, wid: str) -> None:
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "preflight", "--findings", "none"),
        )
        recorded = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)

    def advance_to_verification(self, slug: str, wid: str) -> None:
        self.advance_to_preflight(slug, wid)
        self.owner_phase("tdd", "not-required")
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        verified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)

    def complete_slug(self, slug: str) -> str:
        wid = self.begin_slug(slug)
        self.advance_to_verification(slug, wid)
        self.owner_phase("code-review", "passed", findings="none")
        self.run_cli(
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--findings", "none"),
            ("complete",),
        )
        return wid

    def shell(self, script: str, *args: str) -> subprocess.CompletedProcess[str]:
        """Run code in the repo the way the defect does: through the shell, with no editor tool."""
        return subprocess.run(
            [sys.executable, "-c", script, *args], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def finalize(self, slug: str, wid: str) -> None:
        """The final consult and its lead disposition. Recording the review is left to
        each test: it refreshes the manifest, so where it happens is the behavior."""
        self.run_cli(
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
             "--source", "codex-advisor", "--verdict", "commit-ready"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--findings", "none"),
        )

    # The task text a caller actually has: pipes, newlines, and padding a summary
    # would quietly lose. Multi-KB text is why callers reach for stdin or a file
    # instead of a shell argument.
    VERBATIM_INTENT = "  line one | pipe\nline two\n\ttabbed\t\n"

    def recorded_intent(self) -> str:
        status = self.cli("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        return json.loads(status.stdout)["intent"]

    def test_begin_records_the_task_text_verbatim_from_a_file_or_stdin(self) -> None:
        source = self.tmp / "intent.txt"
        source.write_text(self.VERBATIM_INTENT, encoding="utf-8")
        from_file = self.cli("begin", "--slug", "verbatim-file", "--intent-file", str(source))
        self.assertEqual(from_file.returncode, 0, from_file.stdout + from_file.stderr)
        self.assertEqual(self.recorded_intent(), self.VERBATIM_INTENT,
                         "the recorded intent was normalized or truncated")

        from_stdin = subprocess.run(
            [sys.executable, str(WORKFLOW), "begin", "--slug", "verbatim-stdin",
             "--intent", "-", "--repo", str(self.repo)],
            cwd=ROOT, env=self.env, text=True, input=self.VERBATIM_INTENT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(from_stdin.returncode, 0, from_stdin.stdout + from_stdin.stderr)
        self.assertEqual(self.recorded_intent(), self.VERBATIM_INTENT,
                         "stdin intake did not record the task text verbatim")

        # A literal argument stays legal and is still recorded exactly as given.
        literal = self.cli("begin", "--slug", "verbatim-literal", "--intent", " padded ")
        self.assertEqual(literal.returncode, 0, literal.stdout + literal.stderr)
        self.assertEqual(self.recorded_intent(), " padded ")

        # Line endings are content, not formatting: a request pasted from a Windows
        # editor must record the bytes it has, and both intakes must agree.
        crlf = "line one | pipe\r\nline two\rold mac"
        source.write_bytes(crlf.encode("utf-8"))
        from_crlf_file = self.cli("begin", "--slug", "verbatim-crlf", "--intent-file", str(source))
        self.assertEqual(from_crlf_file.returncode, 0, from_crlf_file.stdout + from_crlf_file.stderr)
        self.assertEqual(self.recorded_intent(), crlf,
                         "file intake translated line endings instead of recording them")

        crlf_stdin = subprocess.run(
            [sys.executable, str(WORKFLOW), "begin", "--slug", "verbatim-crlf-stdin",
             "--intent", "-", "--repo", str(self.repo)],
            cwd=ROOT, env=self.env, text=False, input=crlf.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(crlf_stdin.returncode, 0, crlf_stdin.stderr.decode())
        self.assertEqual(self.recorded_intent(), crlf, "the two intakes disagree on line endings")

    def test_begin_binds_pass_start_and_candidate_identity_to_content(self) -> None:
        marker = "PASS_CANDIDATE_IDENTITY_DIVERGED"
        begun_at, wid = self.git("rev-parse", "HEAD"), self.begin_slug("pass-candidate-identity")
        begun = json.loads(self.cli("status").stdout)
        self.assertEqual(begun.get("passStartOid"), begun_at, marker)
        candidate = begun.get("activeCandidateTree")
        self.assertRegex(str(candidate), r"^[0-9a-f]{40}$", marker)
        self.git("commit", "-q", "--allow-empty", "-m", "same tree")
        continued = json.loads(self.cli("status").stdout)
        self.assertEqual(
            (continued.get("workflowId"), continued.get("passStartOid"), continued.get("activeCandidateTree")),
            (wid, begun_at, candidate), marker,
        )

    def test_checkpoint_is_the_complete_advisor_stage_descriptor(self) -> None:
        marker = "CHECKPOINT_DESCRIPTOR_INCOMPLETE"
        wid = self.begin_slug("checkpoint-descriptor")
        self.advance_to_context_forge()
        status = json.loads(self.cli("status").stdout)
        checkpoint = self.checkpoint("preflight-advice")
        projection = checkpoint.get("advisorProjection")
        self.assertEqual(
            (
                checkpoint.get("schemaVersion"), checkpoint.get("ready"),
                checkpoint.get("slug"), checkpoint.get("workflowId"),
                checkpoint.get("nextAction"), checkpoint.get("sessionMode"),
                checkpoint.get("passStartOid"), checkpoint.get("activeCandidateTree"),
                checkpoint.get("advisorProjectionEvidence"),
                projection.get("schemaVersion") if isinstance(projection, dict) else None,
                projection.get("expectedCandidateTree") if isinstance(projection, dict) else None,
                projection.get("indexedCandidateTree") if isinstance(projection, dict) else None,
            ),
            (
                1, True, "checkpoint-descriptor", wid, "preflight", "create",
                status["passStartOid"], status["activeCandidateTree"],
                status["repoContextForgeEvidence"], 1,
                status["activeCandidateTree"], status["activeCandidateTree"],
            ),
            marker + json.dumps(checkpoint, sort_keys=True),
        )

    def test_checkpoint_refuses_a_projection_for_an_old_candidate(self) -> None:
        marker = "INVALID_PROJECTION_REACHED_ADVISOR"
        self.begin_slug("checkpoint-candidate-drift")
        self.advance_to_context_forge()
        app = self.repo / "app.py"
        app.write_text(app.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

        checkpoint = self.checkpoint("preflight-advice")
        self.assertEqual(
            (
                checkpoint.get("ready"), checkpoint.get("advisorProjection"),
                "advisor projection does not describe the active candidate tree"
                in checkpoint.get("missing", []),
            ),
            (False, None, True),
            marker + json.dumps(checkpoint, sort_keys=True),
        )

    def _packet_with_unindexed_entry(self, slug: str, kind: str) -> tuple[str, str]:
        identity = resolve_repo_identity(self.repo)
        state = read_workflow(identity)
        packet = graph_packet(
            str(identity.root), str(_active_candidate_tree(identity)),
            str(state["passStartOid"]),
        )
        # The producer's own shape: an entry starts as {kind, file, target, direction,
        # status} and unindexed_file() adds only status and diagnostic, so there is no
        # resolved_identity key at all (repo-context-forge gitnexus_analysis.py).
        packet["gitnexus"]["analysis"]["entries"].append({
            "kind": kind, "file": "package-lock.json", "target": "package-lock.json",
            "direction": "", "status": "unindexed",
            "diagnostic": "GitNexus does not index this file",
        })
        path = self.tmp / f"{slug}.json"
        path.write_text(json.dumps(packet), encoding="utf-8")
        return str(path), str(identity.root)

    def test_graph_evidence_accepts_an_unindexed_file_context_entry(self) -> None:
        marker = "UNINDEXED_ENTRY_REFUSED"
        slug, wid = "unindexed-file-entry", self.begin_slug("unindexed-file-entry")
        path, root = self._packet_with_unindexed_entry(slug, "file_context")
        try:
            document = graph_evidence_document(
                path, slug=slug, workflow_id=wid, source_root=root,
                canonical_source_repo="example.invalid/workflow-fixture",
            )
        except ValueError as error:
            self.fail(f"{marker}: {error}")
        self.assertEqual(
            [entry["status"] for entry in document["graph"]["entries"]],
            ["resolved", "unindexed"],
            marker,
        )

    def test_gate_context_omits_the_unindexed_entry(self) -> None:
        marker = "UNINDEXED_ENTRY_LEAKED_INTO_GATE_SYMBOLS"
        slug, wid = "unindexed-gate-context", self.begin_slug("unindexed-gate-context")
        path, root = self._packet_with_unindexed_entry(slug, "file_context")
        document = graph_evidence_document(
            path, slug=slug, workflow_id=wid, source_root=root,
            canonical_source_repo="example.invalid/workflow-fixture",
            snapshot={"base": "a" * 40, "candidate": _active_candidate_tree(resolve_repo_identity(self.repo))},
        )
        self.assertEqual(
            [(symbol["name"], symbol["file"]) for symbol in document["gateContext"]["symbols"]],
            [("compute", "app.py")],
            marker,
        )

    def test_graph_evidence_still_refuses_a_resolved_entry_without_identity(self) -> None:
        marker = "IDENTITYLESS_RESOLVED_ENTRY_ACCEPTED"
        slug, wid = "identityless-resolved-entry", self.begin_slug("identityless-resolved-entry")
        path, root = self._packet_with_unindexed_entry(slug, "file_context")
        packet = json.loads(Path(path).read_text(encoding="utf-8"))
        packet["gitnexus"]["analysis"]["entries"][1]["status"] = "resolved"
        Path(path).write_text(json.dumps(packet), encoding="utf-8")
        with self.assertRaises(ValueError, msg=marker) as raised:
            graph_evidence_document(
                path, slug=slug, workflow_id=wid, source_root=root,
                canonical_source_repo="example.invalid/workflow-fixture",
            )
        self.assertEqual(str(raised.exception), "a graph entry is unresolved or missing its identity", marker)

    def test_graph_evidence_still_refuses_an_unindexed_symbol_entry(self) -> None:
        marker = "UNINDEXED_SYMBOL_ENTRY_ACCEPTED"
        slug, wid = "unindexed-symbol-entry", self.begin_slug("unindexed-symbol-entry")
        path, root = self._packet_with_unindexed_entry(slug, "symbol_context")
        with self.assertRaises(ValueError, msg=marker) as raised:
            graph_evidence_document(
                path, slug=slug, workflow_id=wid, source_root=root,
                canonical_source_repo="example.invalid/workflow-fixture",
            )
        self.assertEqual(str(raised.exception), "a graph entry is unresolved or missing its identity", marker)

    def test_checkpoint_refuses_non_integer_graph_evidence_schemas(self) -> None:
        slug, wid = "checkpoint-schema-refusal", self.begin_slug("checkpoint-schema-refusal")
        identity = resolve_repo_identity(self.repo)
        state = read_workflow(identity)
        packet = graph_packet(
            str(identity.root), str(_active_candidate_tree(identity)),
            str(state["passStartOid"]),
        )
        path = self.tmp / "non-integer-projection.json"
        path.write_text(json.dumps(packet), encoding="utf-8")
        document = graph_evidence_document(
            str(path), slug=slug, workflow_id=wid,
            source_root=str(identity.root),
            canonical_source_repo="example.invalid/workflow-fixture",
        )
        document["schemaVersion"] = True
        commit_evidence_phase(
            identity, slug, wid, "repo-context-forge", document,
        )

        checkpoint = self.checkpoint("preflight-advice")
        self.assertEqual(
            (
                checkpoint.get("ready"), checkpoint.get("advisorProjection"),
                "advisor projection evidence belongs to another workflow"
                in checkpoint.get("missing", []),
            ),
            (False, None, True),
            "BOOLEAN_GRAPH_SCHEMA_REACHED_ADVISOR"
            + json.dumps(checkpoint, sort_keys=True),
        )

        document["schemaVersion"] = 1
        projection = document["advisorProjection"]
        self.assertIsInstance(projection, dict)
        projection["schemaVersion"] = True
        commit_evidence_phase(
            identity, slug, wid, "repo-context-forge", document,
        )

        checkpoint = self.checkpoint("preflight-advice")
        self.assertEqual(
            (
                checkpoint.get("ready"), checkpoint.get("advisorProjection"),
                "advisor projection requires the installed schemaVersion 1 shape"
                in checkpoint.get("missing", []),
            ),
            (False, None, True),
            "INVALID_PROJECTION_REACHED_ADVISOR"
            + json.dumps(checkpoint, sort_keys=True),
        )

    def test_completion_refuses_graph_evidence_that_checkpoint_would_reject(self) -> None:
        marker = "FOREIGN_GRAPH_OWNERSHIP_COMPLETED"
        for defect in ("evidence-schema", "projection-schema", "workflow-owner"):
            with self.subTest(defect=defect):
                slug = f"completion-{defect}"
                wid = self.begin_slug(slug)
                self.advance_to_verification(slug, wid)
                self.owner_phase("code-review", "passed", findings="none")
                self.finalize(slug, wid)
                identity = resolve_repo_identity(self.repo)
                state = read_workflow(identity)
                packet = graph_packet(
                    str(identity.root), str(_active_candidate_tree(identity)),
                    str(state["passStartOid"]),
                )
                path = self.tmp / f"{defect}-completion-graph.json"
                path.write_text(json.dumps(packet), encoding="utf-8")
                document = graph_evidence_document(
                    str(path), slug=slug, workflow_id=wid,
                    source_root=str(identity.root),
                    canonical_source_repo="example.invalid/workflow-fixture",
                )
                if defect == "evidence-schema":
                    document["schemaVersion"] = True
                elif defect == "projection-schema":
                    document["advisorProjection"]["schemaVersion"] = True
                else:
                    document["slug"] = "another-workflow"
                    document["workflowId"] = "f" * 32
                commit_evidence_phase(identity, slug, wid, "repo-context-forge", document)

                completed = self.cli("complete")
                self.assertEqual(
                    completed.returncode, 2,
                    marker + completed.stdout + completed.stderr,
                )
                self.assertIn("repoContextForge", completed.stderr, marker)

    def test_phased_advisor_refuses_caller_selected_fresh_mode(self) -> None:
        marker = "ADVISOR_SESSION_MODE_WRONG"
        result = subprocess.run(
            [
                str(ADVISOR), "--slug", "fresh-refusal",
                "--phase", "preflight-advice", "--cwd", str(self.repo),
                "--design-absent", "test pass has no design", "--fresh", "--", "q",
            ],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(
            (result.returncode, "phased consults do not accept --fresh" in result.stderr),
            (2, True), marker + result.stdout + result.stderr,
        )

    def test_phased_advisor_refuses_caller_selected_payload_anchors(self) -> None:
        marker = "ADVISOR_PAYLOAD_DUPLICATED"
        packet = self.tmp / "caller-packet.json"
        packet.write_text("{}", encoding="utf-8")
        for extra in (("--packet", str(packet)), ("--base-ref", "HEAD")):
            with self.subTest(extra=extra[0]):
                result = subprocess.run(
                    [
                        str(ADVISOR), "--slug", "payload-anchor-refusal",
                        "--phase", "preflight-advice", "--cwd", str(self.repo),
                        "--design-absent", "test pass has no design", *extra, "--", "q",
                    ],
                    cwd=ROOT, env=self.env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                self.assertEqual(
                    (
                        result.returncode,
                        "phased consults do not accept --packet or --base-ref" in result.stderr,
                    ),
                    (2, True), marker + result.stdout + result.stderr,
                )

    def test_advisor_result_refuses_a_candidate_newer_than_its_checkpoint(self) -> None:
        marker = "STALE_ADVISOR_RESULT_RECORDED"
        slug, wid = "stale-advisor-result", self.begin_slug("stale-advisor-result")
        self.advance_to_context_forge()
        checkpoint_candidate = self.checkpoint("preflight-advice")["activeCandidateTree"]
        envelope = self.tmp / "advisor-envelope.json"
        envelope.write_text(json.dumps({
            "schemaVersion": 1, "findings": [], "verdict": "completed",
        }), encoding="utf-8")
        app = self.repo / "app.py"
        app.write_text(app.read_text(encoding="utf-8") + "# provider race\n", encoding="utf-8")
        before = self.ledger_rows()

        result = self.cli(
            "advisor-result", "--slug", slug, "--workflow-id", wid,
            "--stage", "preflight", "--source", "codex-advisor",
            "--input", str(envelope),
            "--expected-candidate-tree", str(checkpoint_candidate),
        )
        self.assertEqual(
            (
                result.returncode,
                "active candidate changed" in result.stderr.lower(),
                self.ledger_rows(),
            ),
            (2, True, before),
            marker + result.stdout + result.stderr,
        )

    def ledger_rows(self) -> tuple[tuple[object, ...], ...]:
        tables = ("metadata", "workflows", "evidence", "review_manifests", "workflow_events",
                  "event_evidence", "event_manifests", "active_projection", "migration_records")
        with closing(sqlite3.connect(database_path(resolve_repo_identity(self.repo)))) as connection:
            return tuple((table, *row) for table in tables
                         for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))

    def assert_candidate_race_refused(self, args: list[str], marker: str) -> None:
        trace, before = self.tmp / f"{args[0]}-{self.documents}-trace.json", self.ledger_rows()
        self.documents += 1
        lock = sqlite3.connect(database_path(resolve_repo_identity(self.repo))); lock.execute("BEGIN IMMEDIATE")
        process = subprocess.Popen([sys.executable, str(WORKFLOW), *args, "--repo", str(self.repo)], cwd=ROOT,
            env={**self.env, "GIT_TRACE2_EVENT": str(trace)}, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertGreaterEqual(wait_for_trace_writes(trace), 2, marker)
            app = self.repo / "app.py"; app.write_text(app.read_text(encoding="utf-8") + "# race\n", encoding="utf-8")
            lock.commit(); stdout, stderr = process.communicate(timeout=10)
            self.assertEqual((process.returncode, "active candidate changed" in stderr.lower(), self.ledger_rows()),
                             (2, True, before), marker + stdout + stderr)
        finally:
            lock.close()
            if process.poll() is None:
                process.kill(); process.wait()

    def test_candidate_reporting_commands_cannot_commit_stale_candidate(self) -> None:
        slug, wid = "atomic-emission", self.begin_slug("atomic-emission")
        self.assert_candidate_race_refused(["pause", "--slug", slug, "--workflow-id", wid, "--reason", "race"],
                                           "MUTATION_RESPONSE_COMMITTED_STALE_CANDIDATE")
        slug, wid = "atomic-replay", self.begin_slug("atomic-replay"); self.advance_to_context_forge()
        replay = ["advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                  "--source", "codex-advisor", "--verdict", "completed", "--design-declaration", str(self.design_declaration)]
        self.run_cli(tuple(replay)); self.assert_candidate_race_refused(replay, "IDEMPOTENT_REPLAY_RETURNED_STALE_CANDIDATE")
        self.begin_slug("before-racing-begin")
        self.assert_candidate_race_refused(["begin", "--slug", "racing-begin"], "BEGIN_RESPONSE_COMMITTED_STALE_CANDIDATE")
        slug, wid = "atomic-phase", self.begin_slug("atomic-phase"); self.advance_to_preflight(slug, wid)
        self.owner_phase("tdd", "not-required"); self.record_real_gate(wid)
        self.assert_candidate_race_refused(["set-phase", "--phase", "implementation", "--status", "passed",
                                            "--slug", slug, "--workflow-id", wid], "FINAL_SAMPLE_CONTRACT_OVERCLAIMED")
        slug, wid = "atomic-disposition", self.begin_slug("atomic-disposition"); self.advance_to_context_forge()
        self.run_cli(("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                      "--source", "codex-advisor", "--verdict", "completed"))
        self.assert_candidate_race_refused(["advisor-disposition", "--slug", slug, "--workflow-id", wid,
                                            "--stage", "preflight", "--findings", "none"],
                                           "FINAL_SAMPLE_CONTRACT_OVERCLAIMED")

    def test_guarded_candidate_capture_keeps_competing_writer_bounded(self) -> None:
        marker = "GUARDED_WRITER_CONTENTION_BECAME_UNBOUNDED"
        script, ready, release = (self.tmp / name for name in ("filter.py", "ready", "release"))
        script.write_text("import pathlib,sys,time\nr,x=map(pathlib.Path,sys.argv[1:3]);r.touch()\nwhile not x.exists():time.sleep(.01)\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n", encoding="utf-8")
        self.git("config", "filter.guard.clean", "cat"); self.git("config", "filter.guard.required", "true")
        (self.repo / ".gitattributes").write_text("app.py filter=guard\n", encoding="utf-8")
        self.git("add", ".gitattributes"); self.git("commit", "-q", "-m", "configure filter")
        slug, wid = "guarded-writer", self.begin_slug("guarded-writer"); self.advance_to_context_forge()
        self.run_cli(("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
                     ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "preflight", "--findings", "none"))
        payload = self.json_file("busy-preflight.json", self.preflight_document())
        holder = sqlite3.connect(database_path(resolve_repo_identity(self.repo))); holder.execute("BEGIN IMMEDIATE")
        trace = self.tmp / "guarded-writer-trace.json"
        primary = subprocess.Popen([sys.executable, str(WORKFLOW), "pause", "--repo", str(self.repo), "--slug", slug,
            "--workflow-id", wid, "--reason", "capture"], cwd=ROOT, env={**self.env, "GIT_TRACE2_EVENT": str(trace)},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertGreaterEqual(wait_for_trace_writes(trace), 2, marker)
            self.git("config", "filter.guard.clean", f"{sys.executable} {script} {ready} {release}"); holder.commit()
            deadline = time.monotonic() + 10
            while not ready.exists() and primary.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            started = time.monotonic(); contender = self.cli("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(payload))
            self.assertEqual((ready.exists(), contender.returncode, "database is busy" in contender.stderr,
                              2 <= time.monotonic() - started < 8), (True, 2, True, True), marker + contender.stderr)
            release.touch(); stdout, stderr = primary.communicate(timeout=10)
            retry = self.cli("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(payload))
            self.assertEqual((primary.returncode, retry.returncode), (0, 0), marker + stdout + stderr + retry.stderr)
        finally:
            release.touch(); holder.close()
            if primary.poll() is None:
                primary.kill(); primary.wait()

    def test_candidate_capture_timeout_is_a_bounded_workflow_error(self) -> None:
        marker = "GIT_TIMEOUT_ESCAPED_WORKFLOW_INTERFACE"; self.begin_slug("before-timeout")
        command = f"{sys.executable} -c 'import sys,time;time.sleep(31);sys.stdout.buffer.write(sys.stdin.buffer.read())'"
        self.git("config", "filter.slow.clean", command); self.git("config", "filter.slow.required", "true")
        (self.repo / ".gitattributes").write_text("app.py filter=slow\n", encoding="utf-8")
        before = self.ledger_rows(); result = self.cli("begin", "--slug", "timed-out-candidate")
        self.assertEqual((result.returncode, "git add timed out after 30" in result.stderr,
                          "Traceback" in result.stderr, self.ledger_rows()), (2, True, False, before), marker + result.stderr)

    def test_begin_refuses_without_a_pass_start_commit(self) -> None:
        self.git("update-ref", "-d", "HEAD")
        begun, status = self.cli("begin", "--slug", "unborn-pass"), self.cli("status")
        self.assertEqual((begun.returncode, status.returncode, "no active workflow" in status.stderr), (2, 2, True), "BEGIN_WITHOUT_PASS_START_CREATED_WORKFLOW")

    def test_begin_refuses_intent_text_the_consult_payload_cannot_carry(self) -> None:
        # The payload reaches the advisor through a shell variable, which cannot hold
        # U+0000. Accepting it would record text the chain then silently truncates.
        source = self.tmp / "nul-intent.txt"
        source.write_bytes(b"before\x00after")

        refused = self.cli("begin", "--slug", "nul-intent", "--intent-file", str(source))
        self.assertEqual(refused.returncode, 2,
                         "begin accepted intent text the consult payload cannot carry: "
                         + refused.stdout + refused.stderr)
        self.assertIn("U+0000", refused.stderr, "the refusal did not name the rejected character")

    def test_begin_refuses_both_intent_sources_and_keeps_the_active_pass(self) -> None:
        self.begin_slug("single-source")
        before = json.loads(self.cli("status").stdout)
        source = self.tmp / "both.txt"
        source.write_text(self.VERBATIM_INTENT, encoding="utf-8")

        refused = self.cli("begin", "--slug", "both-sources", "--intent", "summary",
                           "--intent-file", str(source))
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("--intent-file", refused.stderr, "the refusal did not name the conflicting source")
        self.assertEqual(json.loads(self.cli("status").stdout), before,
                         "a refused begin replaced the active workflow")

    def test_record_preflight_echoes_the_recorded_intent(self) -> None:
        begun = self.cli("begin", "--slug", "preflight-echo", "--intent", self.VERBATIM_INTENT)
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", "preflight-echo", "--workflow-id", wid,
             "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "preflight-echo", "--workflow-id", wid,
             "--stage", "preflight", "--findings", "none"),
        )

        recorded = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout).get("intent"), self.VERBATIM_INTENT,
                         "record-preflight did not echo the recorded intent")

    def test_a_shell_mutation_after_review_refuses_the_final_recording(self) -> None:
        wid = self.begin_slug("review-to-final-window")
        self.advance_to_verification("review-to-final-window", wid)
        self.owner_phase("code-review", "passed", findings="none")

        self.shell("import pathlib; pathlib.Path('app.py').write_text('value = 999  # never reviewed\\n')")

        refused = self.cli(
            "advisor-result", "--slug", "review-to-final-window", "--workflow-id", wid,
            "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready",
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("app.py", refused.stderr, "the refusal did not name the changed path")
        self.assertEqual(
            json.loads(self.cli("status").stdout)["finalReview"]["status"], "pending",
            "a verdict from before the mutation was recorded against the changed tree",
        )

    def test_completion_refuses_a_shell_mutation_that_lands_after_the_final_review(self) -> None:
        wid = self.begin_slug("landing-window")
        self.advance_to_verification("landing-window", wid)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("landing-window", wid)

        reviewed = (self.repo / "app.py").read_bytes()
        self.shell("import pathlib; pathlib.Path('app.py').write_text('value = 3\\n')")
        self.advance_to_context_forge()
        refused = self.cli("complete")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("after the final review", refused.stderr, "the refusal did not attribute the window")
        self.assertIn("app.py", refused.stderr, "the refusal did not name the changed path")

        # Restore the reviewed bytes and prove the workflow is fresh again, so the
        # next refusal can only come from the edit inside the combined call: a probe
        # against a still-stale workflow would refuse whether or not completion
        # recomputes after the same-call edit.
        (self.repo / "app.py").write_bytes(reviewed)
        restored = self.checkpoint("final-review")
        self.assertEqual(
            [item for item in restored["missing"] if "stale" in item or "changed" in item],
            [], "restoring the reviewed bytes did not restore freshness",
        )
        self.advance_to_context_forge()

        # The same edit and the completion inside one shell call: completion
        # recomputes after the edit has already landed, so it is still caught.
        combined = self.shell(
            "import pathlib, subprocess, sys\n"
            "sys.path.insert(0, sys.argv[3])\n"
            "from hooks.tests.support import record_context_forge\n"
            "repo = pathlib.Path(sys.argv[2])\n"
            "pathlib.Path('app.py').write_text('value = 4\\n')\n"
            "record_context_forge(repo, pathlib.Path(sys.argv[4]))\n"
            "raise SystemExit(subprocess.run([sys.executable, sys.argv[1], 'complete', '--repo', sys.argv[2]]).returncode)",
            str(WORKFLOW), str(self.repo), str(ROOT), str(self.tmp),
        )
        self.assertEqual(combined.returncode, 2, combined.stdout + combined.stderr)
        self.assertIn("after the final review", combined.stderr)
        self.assertIn("review-manifest-stale", combined.stderr)
        self.assertIn("app.py", combined.stderr)
        self.assertEqual(
            json.loads(self.cli("status").stdout)["phase"], "repo-context-forge",
            "an edit-and-complete shell call landed the pass",
        )

    def test_a_chmod_after_review_reopens_the_approval(self) -> None:
        wid = self.begin_slug("mode-drift")
        self.advance_to_verification("mode-drift", wid)
        self.owner_phase("code-review", "passed", findings="none")

        # A mode-only shell mutation: the bytes are untouched, so a content-only
        # hash sees nothing, but the reviewed file is now executable.
        before = (self.repo / "app.py").read_bytes()
        self.shell("import os, stat; os.chmod('app.py', os.stat('app.py').st_mode | stat.S_IXUSR)")
        self.assertEqual((self.repo / "app.py").read_bytes(), before, "the probe changed content, not just mode")

        stale = self.checkpoint("final-review")
        self.assertTrue(
            any("review-manifest-stale" in item and "app.py" in item for item in stale["missing"]),
            f"a chmod on a reviewed file left the approval standing: {stale['missing']}",
        )
        refused = self.cli(
            "advisor-result", "--slug", "mode-drift", "--workflow-id", wid,
            "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready",
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)

    def test_a_line_ending_mutation_after_review_reopens_the_approval(self) -> None:
        # Git attributes make hash-object normalise content before hashing, so a
        # line-ending-only rewrite can leave a content digest identical.
        (self.repo / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
        self.git("add", ".gitattributes")
        self.git("commit", "-q", "-m", "normalise line endings")

        wid = self.begin_slug("normalised-drift")
        self.advance_to_verification("normalised-drift", wid)
        self.owner_phase("code-review", "passed", findings="none")

        self.shell("import pathlib; pathlib.Path('app.py').write_bytes(b'value = 1\\r\\n')")
        self.assertEqual((self.repo / "app.py").read_bytes(), b"value = 1\r\n", "the probe did not land CRLF on disk")

        stale = self.checkpoint("final-review")
        self.assertTrue(
            any("review-manifest-stale" in item and "app.py" in item for item in stale["missing"]),
            f"a normalised content change left the approval standing: {stale['missing']}",
        )

    def test_a_submodule_move_after_review_reopens_the_approval(self) -> None:
        sub = self.tmp / "sub"
        sub.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Sub")):
            subprocess.run(["git", *args], cwd=sub, env=self.env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (sub / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=sub, env=self.env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=sub, env=self.env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
        self.git("-c", "protocol.file.allow=always", "commit", "-q", "-m", "add submodule")
        for args in (("user.email", "test@example.invalid"), ("user.name", "Sub")):
            subprocess.run(["git", "-C", str(self.repo / "vendor"), "config", *args], env=self.env,
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        wid = self.begin_slug("submodule-drift")
        self.advance_to_verification("submodule-drift", wid)
        self.owner_phase("code-review", "passed", findings="none")

        # Move the submodule's checked-out HEAD without staging it in the parent:
        # the parent's index gitlink still points at the reviewed commit.
        vendor = self.repo / "vendor"
        indexed_before = self.git("ls-files", "-s", "vendor")
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "moved"], cwd=vendor, env=self.env,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(self.git("ls-files", "-s", "vendor"), indexed_before,
                         "the probe staged the submodule move, so the index would have shown it")

        stale = self.checkpoint("final-review")
        self.assertTrue(
            any("review-manifest-stale" in item and "vendor" in item for item in stale["missing"]),
            f"an unstaged submodule move left the approval standing: {stale['missing']}",
        )

    def add_submodule(self, at: str) -> Path:
        source = self.tmp / f"sub-{at.replace('/', '-')}"
        source.mkdir()
        for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"), ("config", "user.name", "Sub")):
            subprocess.run(["git", *args], cwd=source, env=self.env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (source / "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=source, env=self.env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=source, env=self.env, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), at)
        self.git("-c", "protocol.file.allow=always", "commit", "-q", "-m", f"add submodule {at}")
        checkout = self.repo / at
        for args in (("user.email", "test@example.invalid"), ("user.name", "Sub")):
            subprocess.run(["git", "-C", str(checkout), "config", *args], env=self.env, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return checkout

    def test_a_submodule_on_an_excluded_path_stays_out_of_the_manifest(self) -> None:
        checkout = self.add_submodule("docs/vendor")

        wid = self.begin_slug("excluded-submodule")
        self.advance_to_verification("excluded-submodule", wid)
        self.owner_phase("code-review", "passed", findings="none")

        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "moved"], cwd=checkout, env=self.env,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertIn("docs/vendor", self.git("status", "--porcelain"),
                      "the probe did not actually move the excluded submodule")

        ready = self.checkpoint("final-review")
        self.assertEqual(
            [item for item in ready["missing"] if "review-manifest" in item], [],
            f"a submodule on a documentation path invalidated the review: {ready['missing']}",
        )

    def test_a_symlink_is_recorded_as_the_link_not_its_referent(self) -> None:
        outside = self.tmp / "outside.txt"
        outside.write_text("external\n", encoding="utf-8")
        for name in ("a.py", "b.py"):
            (self.repo / name).write_text("same\n", encoding="utf-8")
        (self.repo / "link.py").symlink_to("a.py")
        (self.repo / "escape.py").symlink_to(outside)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "symlinks")

        wid = self.begin_slug("symlink-drift")
        self.advance_to_verification("symlink-drift", wid)
        self.owner_phase("code-review", "passed", findings="none")

        # A file outside the repository is not part of the reviewed tree, so
        # changing it must not drift the manifest through a symlink.
        outside.write_text("CHANGED OUTSIDE THE REPOSITORY\n", encoding="utf-8")
        unaffected = self.checkpoint("final-review")
        self.assertEqual(
            [item for item in unaffected["missing"] if "review-manifest" in item], [],
            f"a change outside the repository drifted the manifest: {unaffected['missing']}",
        )

        # Re-pointing a reviewed link is a change to the reviewed tree even when
        # the new referent happens to hold identical bytes.
        (self.repo / "link.py").unlink()
        (self.repo / "link.py").symlink_to("b.py")
        stale = self.checkpoint("final-review")
        self.assertTrue(
            any("review-manifest-stale" in item and "link.py" in item for item in stale["missing"]),
            f"a re-pointed symlink left the approval standing: {stale['missing']}",
        )

    def test_a_group_execute_chmod_after_review_keeps_the_approvals(self) -> None:
        wid = self.begin_slug("group-execute-noise")
        self.advance_to_verification("group-execute-noise", wid)
        self.owner_phase("code-review", "passed", findings="none")

        # Git's regular-file mode is decided by the owner execute bit alone, so a
        # group-execute flip is a change git will never record and cannot land.
        before = (self.repo / "app.py").read_bytes()
        self.shell("import os, stat; os.chmod('app.py', os.stat('app.py').st_mode | stat.S_IXGRP)")
        self.assertEqual((self.repo / "app.py").read_bytes(), before, "the probe changed content, not just mode")
        self.assertIn("app.py", self.git("ls-files", "-s", "app.py"), "probe sanity")
        self.assertIn("100644", self.git("ls-files", "-s", "app.py"),
                      "git itself considers the file executable now, so the premise fails")

        ready = self.checkpoint("final-review")
        self.assertEqual(
            [item for item in ready["missing"] if "review-manifest" in item], [],
            f"a mode change git will never record invalidated the review: {ready['missing']}",
        )

    def test_non_mutating_shell_work_after_review_keeps_the_approvals(self) -> None:
        wid = self.begin_slug("ordinary-landing")
        self.advance_to_verification("ordinary-landing", wid)
        self.owner_phase("code-review", "passed", findings="none")

        # Ordinary landing work: read the tree, query Git. Nothing is written.
        self.shell("import pathlib; pathlib.Path('app.py').read_text()")
        self.git("status", "--porcelain")
        self.git("log", "--oneline")

        self.finalize("ordinary-landing", wid)
        completed = self.cli("complete")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_additions_deletions_and_multi_file_mutations_are_each_named(self) -> None:
        (self.repo / "lib.py").write_text("helper = True\n", encoding="utf-8")
        self.git("add", "lib.py")
        self.git("commit", "-q", "-m", "second production file")

        wid = self.begin_slug("named-drift")
        self.advance_to_verification("named-drift", wid)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("named-drift", wid)

        # One formatter-shaped call touching three paths in three different ways.
        self.shell(
            "import pathlib\n"
            "pathlib.Path('new_module.py').write_text('created = True\\n')\n"
            "pathlib.Path('lib.py').unlink()\n"
            "pathlib.Path('app.py').write_text('value = 5\\n')\n"
        )
        self.advance_to_context_forge()
        refused = self.cli("complete")
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("added=new_module.py", refused.stderr)
        self.assertIn("changed=app.py", refused.stderr)
        self.assertIn("removed=lib.py", refused.stderr)

    def test_state_without_a_manifest_refuses_until_the_review_is_re_recorded(self) -> None:
        wid = self.begin_slug("legacy-manifest")
        self.advance_to_verification("legacy-manifest", wid)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("legacy-manifest", wid)

        # A pass already in flight when this contract shipped carries no logical manifest id.
        self.assertIn("reviewManifestId", json.loads(self.cli("status").stdout),
                      "the recorded review persisted no manifest")
        self.rewrite_latest_state(lambda state: state.pop("reviewManifestId", None))

        blocked = self.cli("complete")
        self.assertEqual(blocked.returncode, 2, blocked.stdout + blocked.stderr)
        self.assertIn("review-manifest-missing", blocked.stderr, "an unknown tree read as green")

        refused_final = self.cli(
            "advisor-result", "--slug", "legacy-manifest", "--workflow-id", wid, "--stage", "final",
            "--source", "codex-advisor", "--verdict", "commit-ready",
        )
        self.assertEqual(refused_final.returncode, 2, refused_final.stdout + refused_final.stderr)
        self.assertIn("review-manifest-missing", refused_final.stderr)

        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("legacy-manifest", wid)
        unblocked = self.cli("complete")
        self.assertEqual(unblocked.returncode, 0, unblocked.stdout + unblocked.stderr)

    def test_re_recording_the_review_requires_a_fresh_final_consult(self) -> None:
        wid = self.begin_slug("stale-verdict")
        self.advance_to_verification("stale-verdict", wid)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("stale-verdict", wid)

        self.shell("import pathlib; pathlib.Path('app.py').write_text('value = 6\\n')")
        self.assertEqual(self.cli("complete").returncode, 2)
        self.advance_to_context_forge()

        # Re-verifying and re-reviewing refreshes the manifest; the verdict from
        # the old tree must not survive that refresh.
        reverified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(reverified.returncode, 0, reverified.stdout + reverified.stderr)
        self.owner_phase("code-review", "passed", findings="none")
        self.assertEqual(
            json.loads(self.cli("status").stdout)["finalReview"],
            {"source": None, "status": "pending", "findings": "pending"},
            "a commit-ready verdict from the pre-mutation tree survived the re-review",
        )
        still_blocked = self.cli("complete")
        self.assertEqual(still_blocked.returncode, 2, still_blocked.stdout + still_blocked.stderr)
        self.assertIn("finalReview", still_blocked.stderr)

        self.finalize("stale-verdict", wid)
        self.assertEqual(self.cli("complete").returncode, 0)

    def test_governance_revalidation_completes_against_the_refreshed_manifest(self) -> None:
        from hooks.lib.workflow_state import invalidate_after_edit

        wid = self.complete_slug("revalidated-manifest")
        identity = resolve_repo_identity(self.repo)
        invalidate_after_edit(identity, "skills/diagnose/SKILL.md")
        self.shell("import pathlib; pathlib.Path('app.py').write_text('value = 7\\n')")

        reverified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(reverified.returncode, 0, reverified.stdout + reverified.stderr)
        stale = self.checkpoint("final-review")
        self.assertTrue(
            any("review-manifest-stale" in item and "app.py" in item for item in stale["missing"]),
            f"revalidation reported readiness without naming the drifted tree: {stale['missing']}",
        )

        try:
            record_context_forge(self.repo, self.tmp)
        except WorkflowError as exc:
            self.fail("REVALIDATION_PROJECTION_REFRESH_BLOCKED" + str(exc))
        self.owner_phase("code-review", "passed", findings="none")
        self.assertTrue(self.checkpoint("final-review")["ready"], "the refreshed manifest did not reopen the consult")
        self.finalize("revalidated-manifest", wid)
        completed = self.cli("complete")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def preflight_document(self) -> dict[str, str]:
        return build_no_change_document("concrete content for this pass")

    def record_preflight(self, wid: str, document: dict[str, str]) -> subprocess.CompletedProcess[str]:
        payload = self.tmp / "preflight-input.json"
        payload.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(WORKFLOW), "record-preflight", "--repo", str(self.repo),
             "--slug", json.loads(self.cli("status").stdout)["slug"],
             "--workflow-id", wid, "--input", str(payload)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_the_preflight_contract_names_the_skills_thirteen_sections(self) -> None:
        # Independent literal pin: the shared fixture derives from SECTIONS, so
        # this assertion is the one place the contract cannot drift silently.
        self.assertEqual(PREFLIGHT_SECTIONS, (
            "affectedSurface", "authoritativeContract", "invariants", "proofPlan",
            "reusePath", "chosenApproach", "rejectedAlternatives", "touchpoints",
            "verify", "update", "modularityPlan", "riskChecks", "openQuestions",
        ))

    def test_preflight_records_only_with_its_document(self) -> None:
        wid = self.begin_slug("evidence-preflight")
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", "evidence-preflight", "--workflow-id", wid,
             "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "evidence-preflight", "--workflow-id", wid,
             "--stage", "preflight", "--findings", "none"),
        )

        bare = self.cli("set-phase", "--phase", "preflight", "--status", "passed")
        self.assertEqual(bare.returncode, 2, "a bare preflight claim was accepted: " + bare.stdout + bare.stderr)
        self.assertIn("record-preflight", bare.stderr, "the refusal did not name the producer")
        self.assertEqual(json.loads(self.cli("status").stdout)["preflight"], "pending")

        payload = self.tmp / "preflight-input.json"
        fields = ",\n".join(f'"{name}": "text"' for name in PREFLIGHT_SECTIONS if name != "openQuestions")
        payload.write_text(
            "{" + fields + ', "openQuestions": "none", "proofPlan": "repeated"}', encoding="utf-8")
        duplicated = subprocess.run(
            [sys.executable, str(WORKFLOW), "record-preflight", "--repo", str(self.repo),
             "--slug", "evidence-preflight", "--workflow-id", wid, "--input", str(payload)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(duplicated.returncode, 2, duplicated.stdout + duplicated.stderr)
        self.assertIn("repeats a section", duplicated.stderr)
        self.assertIn("proofPlan", duplicated.stderr)

        recorded = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        state = json.loads(self.cli("status").stdout)
        self.assertEqual(state["preflight"], "passed")
        # The persisted value is what the Stop payload and resume banner instruct.
        self.assertEqual(state["nextAction"], "tdd", "a recorded phase was named as the next action")
        evidence = self.evidence(json.loads(recorded.stdout)["evidenceId"])
        self.assertEqual(evidence["workflowId"], wid, "evidence is not bound to the workflow instance")

    def record_real_gate(self, wid: str) -> None:
        gate = subprocess.run(
            [sys.executable, str(QUALITY_GATE), "check", "--repo", str(self.repo), "--json"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        recorded = self.record_production_code(wid, gate.stdout)
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)

    def record_production_code(self, wid: str, gate_json: str) -> subprocess.CompletedProcess[str]:
        payload = self.tmp / "gate-input.json"
        payload.write_text(gate_json, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(WORKFLOW), "record-production-code", "--repo", str(self.repo),
             "--slug", json.loads(self.cli("status").stdout)["slug"],
             "--workflow-id", wid, "--input", str(payload)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_production_code_records_only_with_the_gate_verdict(self) -> None:
        wid = self.begin_slug("evidence-gate")
        self.advance_to_preflight("evidence-gate", wid)
        self.owner_phase("tdd", "not-required")

        bare = self.cli("set-phase", "--phase", "production-code", "--status", "passed")
        self.assertEqual(bare.returncode, 2, "a bare production-code claim was accepted: " + bare.stdout + bare.stderr)
        self.assertIn("record-production-code", bare.stderr, "the refusal did not name the producer")
        self.assertEqual(json.loads(self.cli("status").stdout)["productionCode"], "pending")

        before_state = json.loads(self.cli("status").stdout)
        before_events = len(self.history_events())
        unparseable = self.record_production_code(wid, "verdict: pass")
        self.assertEqual(unparseable.returncode, 2, unparseable.stdout + unparseable.stderr)
        self.assertIn("gate JSON", unparseable.stderr)
        self.assertEqual(json.loads(self.cli("status").stdout), before_state,
                         "a refused recording mutated workflow state")
        self.assertEqual(len(self.history_events()), before_events,
                         "a refused recording appended an event")

        failing = self.record_production_code(wid, json.dumps({"ok": False, "gateVersion": "test", "checks": []}))
        self.assertEqual(failing.returncode, 2, failing.stdout + failing.stderr)
        self.assertIn("ok", failing.stderr)

        gate = subprocess.run(
            [sys.executable, str(QUALITY_GATE), "check", "--repo", str(self.repo)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        recorded = self.record_production_code(wid, gate.stdout.strip().splitlines()[-1])
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        state = json.loads(self.cli("status").stdout)
        self.assertEqual(state["productionCode"], "passed")
        self.assertEqual(state["nextAction"], "verification", "a recorded phase was named as the next action")
        evidence = self.evidence(json.loads(recorded.stdout)["evidenceId"])
        self.assertEqual(evidence["workflowId"], wid)
        self.assertTrue(evidence["gate"]["ok"], "the recorded evidence is not the gate verdict")

    def verify_run(self, *command: str, gate: bool = True) -> subprocess.CompletedProcess[str]:
        slug = json.loads(self.cli("status").stdout)["slug"]
        result = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
             "--slug", slug, "--", *command],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if result.returncode == 0 and gate:
            gate_result = subprocess.run(
                [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
                 "--slug", slug, "--kind", "quality-gate", "--base-ref", "HEAD"],
                cwd=ROOT, env=self.env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(gate_result.returncode, 0, gate_result.stdout + gate_result.stderr)
        return result

    def test_quality_gate_refuses_a_tree_that_changed_during_the_run(self) -> None:
        # The persisted manifest must be the tree the gate actually checked. A
        # tracked file mutating while the gate runs cannot be blessed as green.
        slug = "mid-gate-mutation"
        wid = self.begin_slug(slug)
        self.advance_to_preflight(slug, wid)
        self.owner_phase("tdd", "not-required")
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        generic = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(generic.returncode, 0, generic.stdout + generic.stderr)

        # The mutation has to land between the two manifests the runner samples
        # around the gate child. A thread spraying writes only makes that likely:
        # on a loaded two-core runner it can be starved for the whole gate, and
        # the run then passes for the wrong reason. So the mutator is a separate
        # process, and it counts a write only once it has re-confirmed that the
        # same gate child — identified by pid and start time, so a recycled pid
        # cannot stand in for it — was still alive after the write landed.
        marker = self.tmp / "confirmed-writes"
        mutator = subprocess.Popen(
            [sys.executable, "-c", MID_GATE_MUTATOR, str(self.repo / "app.py"), str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            result = subprocess.run(
                [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
                 "--slug", slug, "--kind", "quality-gate", "--base-ref", "HEAD"],
                cwd=ROOT, env=self.env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        finally:
            mutator.terminate()
            _, mutator_stderr = mutator.communicate(timeout=30)
        confirmed = int(marker.read_text(encoding="utf-8")) if marker.exists() else 0
        # The refusal is meaningless unless the tree really changed mid-run. A child
        # that died instead of overlapping reports the same zero, so its own output
        # travels with the failure rather than being thrown away; it is not asserted
        # empty, because the child is terminated on every run and says so.
        self.assertGreater(
            confirmed, 0,
            "the mutation never overlapped the gate child, so the drift window was never "
            f"exercised; mutator stderr: {mutator_stderr!r}",
        )

        state = json.loads(self.cli("status").stdout)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        emitted = json.loads(result.stdout.splitlines()[-1])
        run = self.evidence(emitted["evidenceId"])["runs"][-1]
        self.assertFalse(run["valid"])
        # Either attribution proves the same thing: the gate never saw a tree that
        # held still. Which one surfaces depends on whether the write landed in the
        # recorder's own sampling window or inside the gate's `git add` capture, and
        # the second is what made this test intermittent before it was named.
        reason = run["bindingError"] or ""
        self.assertTrue(
            reason.startswith("reviewable tree changed during the quality-gate run")
            or reason.startswith("the quality gate could not capture the reviewable tree:"),
            f"the mid-run mutation went unattributed: {reason!r}",
        )
        self.assertEqual(state["verification"], "pending")
        self.assertNotIn("qualityGateManifestId", state)

    def test_the_mutator_ignores_a_gate_running_for_another_repository(self) -> None:
        """Overlap is only overlap with this fixture's own gate.

        The detector reads every process on the host, so a gate belonging to a
        concurrent developer or CI job can satisfy it. Confirming against one of
        those certifies a window this test never controlled, and if it exits before
        the real gate starts the fixture holds still and the verification passes for
        the wrong reason. Both false-positive shapes are present here at once: real
        `code_quality_gate.py` invocations for a different repository, and the shell
        looping them, whose own command line carries the script name too.
        """
        other = self.tmp / "other-repo"
        other.mkdir()
        marker = self.tmp / "foreign-writes"
        decoy = subprocess.Popen(
            ["bash", "-c", f'while :; do "{sys.executable}" "{QUALITY_GATE}" check --repo "{other}" >/dev/null 2>&1; done'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        mutator = subprocess.Popen(
            [sys.executable, "-c", MID_GATE_MUTATOR, str(self.repo / "app.py"), str(marker)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            # A window, because absence is what is being proved: the current detector
            # matches within a millisecond and writes every millisecond after that, so
            # two seconds is thousands of chances to record a confirmation.
            time.sleep(2)
            alive = mutator.poll() is None
        finally:
            mutator.terminate()
            _, mutator_stderr = mutator.communicate(timeout=30)
            decoy.kill()
            decoy.wait(timeout=30)

        # Asserted before the count, so a mutator that died cannot pass this by silence.
        self.assertTrue(alive, f"the mutator exited before it could confirm anything: {mutator_stderr!r}")
        confirmed = int(marker.read_text(encoding="utf-8")) if marker.exists() else 0
        self.assertEqual(
            confirmed, 0,
            "the mutator confirmed writes against a quality gate belonging to another "
            f"repository, so its overlap marker certifies a window it never controlled; "
            f"mutator stderr: {mutator_stderr!r}",
        )

    def test_a_gate_that_cannot_capture_the_tree_says_why(self) -> None:
        """A capture that fails is as unusable as one that drifts, and must be named.

        A required clean filter that exits non-zero makes the gate's own `git add`
        capture fail deterministically, which is the same condition a mid-run
        mutation produces intermittently. `tree_manifest` hashes with
        `--no-filters`, so the runner's own sampling is untouched: the only thing
        broken is the gate's view of the tree.
        """
        slug = "capture-failure"
        wid = self.begin_slug(slug)
        self.advance_to_preflight(slug, wid)
        self.owner_phase("tdd", "not-required")
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        self.assertEqual(self.verify_run(sys.executable, "-c", "pass").returncode, 0)

        self.git("config", "filter.boom.clean", "exit 1")
        self.git("config", "filter.boom.required", "true")
        (self.repo / ".gitattributes").write_text("app.py filter=boom\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
             "--slug", slug, "--kind", "quality-gate", "--base-ref", "HEAD"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        run = self.evidence(json.loads(result.stdout.splitlines()[-1])["evidenceId"])["runs"][-1]
        self.assertFalse(run["valid"])
        self.assertIsNotNone(
            run["bindingError"], "a gate that could not capture the tree reported no reason",
        )
        self.assertIn("could not capture the reviewable tree", run["bindingError"])
        self.assertIn("candidate capture failed at git add", run["bindingError"])
        state = read_workflow(resolve_repo_identity(self.repo))
        self.assertEqual(state["verification"], "pending")
        self.assertNotIn("qualityGateManifestId", state)

    def test_committed_verification_is_not_reported_as_refused_when_stdout_is_gone(self) -> None:
        # An unbuffered write to a pipe whose reader is already closed takes
        # EPIPE at the print itself, after commit_verification has persisted the
        # run. A reporting failure must not be re-labelled as a refusal.
        slug = "closed-stdout-verify"
        wid = self.begin_slug(slug)
        self.advance_to_preflight(slug, wid)
        self.owner_phase("tdd", "not-required")
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))

        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            result = subprocess.run(
                [sys.executable, "-u", str(WORKFLOW), "verify", "--repo", str(self.repo),
                 "--slug", slug, "--", sys.executable, "-c", "print('x' * 200)"],
                cwd=ROOT, env={**self.env, "PYTHONUNBUFFERED": "1"}, text=True,
                stdout=write_fd, stderr=subprocess.PIPE, check=False,
            )
        finally:
            os.close(write_fd)

        self.assertEqual(self.history_events()[-1]["kind"], "record-verification")
        self.assertEqual(json.loads(self.cli("status").stdout)["verification"], "passed")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verification_records_only_through_the_runner_per_command_latest(self) -> None:
        wid = self.begin_slug("evidence-verification")
        self.advance_to_preflight("evidence-verification", wid)
        self.owner_phase("tdd", "not-required")
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))

        bare = self.cli("set-phase", "--phase", "verification", "--status", "passed")
        self.assertEqual(bare.returncode, 2, "a bare verification claim was accepted: " + bare.stdout + bare.stderr)
        self.assertIn("workflow verify", bare.stderr, "the refusal did not name the runner")
        self.assertEqual(json.loads(self.cli("status").stdout)["verification"], "pending")

        # Command A fails until the flag file exists — the same command text later passes.
        flag = self.repo / "flag"
        command_a = "import sys, pathlib; sys.exit(0 if pathlib.Path('flag').exists() else 1)"
        a_red = self.verify_run(sys.executable, "-c", command_a)
        self.assertNotEqual(a_red.returncode, 0, "the runner reported success for a failing command")
        red_state = json.loads(self.cli("status").stdout)
        self.assertEqual(red_state["verification"], "pending")
        self.assertEqual(red_state["nextAction"], "verification", "a red run advertised progress it had not made")

        # An unrelated green command must not mask A's latest red result.
        b_ok = self.verify_run(sys.executable, "-c", "print('ok')")
        self.assertEqual(b_ok.returncode, 0, b_ok.stdout + b_ok.stderr)
        self.assertEqual(
            json.loads(self.cli("status").stdout)["verification"], "pending",
            "an unrelated green command masked a failing one",
        )

        # Rerunning the SAME command green clears it: every distinct command's latest run is green.
        flag.write_text("", encoding="utf-8")
        a_green = self.verify_run(sys.executable, "-c", command_a)
        self.assertEqual(a_green.returncode, 0, a_green.stdout + a_green.stderr)
        state = json.loads(self.cli("status").stdout)
        self.assertEqual(state["verification"], "passed")
        self.assertEqual(state["nextAction"], "code-review", "a recorded phase was named as the next action")

        state = json.loads(self.cli("status").stdout)
        evidence = self.evidence(state["verificationLatestEvidence"])
        self.assertEqual(evidence["workflowId"], wid)
        generic = [run for run in evidence["runs"] if run.get("kind") == "generic"]
        self.assertEqual(len(generic), 3, "the runner did not persist every executed command")
        self.assertEqual([run["exitCode"] for run in generic], [1, 0, 0])

    def test_generic_verification_keeps_next_action_at_verification_until_quality_gate(self) -> None:
        wid = self.begin_slug("typed-verification-next-action")
        self.advance_to_preflight("typed-verification-next-action", wid)
        self.owner_phase("tdd", "not-required")
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))

        generic = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
             "--slug", "typed-verification-next-action", "--", sys.executable, "-c", "pass"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(generic.returncode, 0, generic.stdout + generic.stderr)
        generic_state = json.loads(self.cli("status").stdout)
        self.assertEqual(generic_state["verification"], "passed")
        self.assertNotIn("qualityGateEvidence", generic_state)
        self.assertEqual(
            generic_state["nextAction"], "verification",
            "generic verification advertised code review before the typed quality gate existed",
        )

        quality = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
             "--slug", "typed-verification-next-action", "--kind", "quality-gate",
             "--base-ref", "HEAD"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(quality.returncode, 0, quality.stdout + quality.stderr)
        self.assertEqual(json.loads(self.cli("status").stdout)["nextAction"], "code-review")

    def test_edit_requires_fresh_generic_and_quality_gate_verification(self) -> None:
        """A new tree cannot reuse either half of the previous verification cycle."""
        from hooks.lib.workflow_state import invalidate_after_edit

        wid = self.begin_slug("fresh-verification-cycle")
        self.advance_to_verification("fresh-verification-cycle", wid)
        identity = resolve_repo_identity(self.repo)
        before = json.loads(self.cli("status").stdout)
        for field in (
            "verificationEvidence", "verificationLatestEvidence",
            "qualityGateEvidence", "qualityGateManifestId",
        ):
            self.assertIn(field, before)

        invalidate_after_edit(identity, "app.py")
        invalidated = json.loads(self.cli("status").stdout)
        self.assertEqual(invalidated["verification"], "pending")
        for field in (
            "verificationEvidence", "verificationLatestEvidence",
            "qualityGateEvidence", "qualityGateManifestId",
        ):
            self.assertNotIn(field, invalidated, f"an edit retained stale {field}")

        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        gate_only = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
             "--slug", "fresh-verification-cycle", "--kind", "quality-gate",
             "--base-ref", "HEAD"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(gate_only.returncode, 0, gate_only.stdout + gate_only.stderr)
        self.assertEqual(
            json.loads(self.cli("status").stdout)["verification"], "pending",
            "a quality-gate-only rerun reused the prior generic verification",
        )

        generic = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo),
             "--slug", "fresh-verification-cycle", "--", sys.executable, "-c", "pass"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(generic.returncode, 0, generic.stdout + generic.stderr)
        self.assertEqual(json.loads(self.cli("status").stdout)["verification"], "passed")

    def test_legacy_passed_phases_without_evidence_cannot_complete(self) -> None:
        # A pass recorded under the pre-evidence regime: phases read passed but
        # no evidence references exist. Simulated by stripping the refs from a
        # real producer-recorded pass - the ordered writers themselves no
        # longer construct such state. Unknown is not green - it must not land.
        wid = self.begin_slug("legacy-evidence")
        self.advance_to_verification("legacy-evidence", wid)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("legacy-evidence", wid)
        def strip_evidence(state: dict[str, object]) -> None:
            for field in ("preflightEvidence", "productionCodeEvidence", "verificationEvidence"):
                state.pop(field, None)
        self.rewrite_latest_state(strip_evidence)

        blocked = self.cli("complete")
        self.assertEqual(blocked.returncode, 2, "a pass with bare phase claims and no evidence completed: " + blocked.stdout)
        for name in ("preflightEvidence", "productionCodeEvidence", "verificationEvidence"):
            self.assertIn(name, blocked.stderr, f"the refusal did not name {name}")

        # Re-recording through the real producers writes the evidence and unblocks.
        recorded = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.record_real_gate(wid)
        verified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("legacy-evidence", wid)
        completed = self.cli("complete")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_a_fix_round_demands_fresh_evidence_and_tdd_waits_for_preflight_evidence(self) -> None:
        wid = self.begin_slug("fresh-evidence")
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", "fresh-evidence", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "fresh-evidence", "--workflow-id", wid, "--stage", "preflight", "--findings", "none"),
        )

        # No preflight evidence, no TDD run: the chain needs no new machinery.
        marker = self.tmp / "early-red-ran"
        early_red = subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(self.repo), "--slug", "fresh-evidence",
             "--phase", "red", "--behavior", "chain proof", "--seam", "workflow CLI",
             "--expected-failure", "AssertionError", "--", sys.executable, "-c",
             f"open({str(marker)!r}, 'w').close(); raise AssertionError('AssertionError: early')"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(early_red.returncode, 2, early_red.stdout + early_red.stderr)
        self.assertFalse(marker.exists(), "workflow.py tdd executed a command while preflight evidence was absent")

        recorded = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        first_evidence_id = json.loads(recorded.stdout)["evidenceId"]
        self.assertEqual(self.evidence(first_evidence_id)["workflowId"], wid)

        # A fix round replaces the instance: the old instance's evidence cannot record for it.
        new_wid = self.begin_slug("fresh-evidence")
        self.assertNotEqual(new_wid, wid)
        self.assertEqual(json.loads(self.cli("status").stdout)["preflight"], "pending",
                         "the replacement instance inherited a recorded preflight")
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", "fresh-evidence", "--workflow-id", new_wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "fresh-evidence", "--workflow-id", new_wid, "--stage", "preflight", "--findings", "none"),
        )
        stale = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(stale.returncode, 2, "the old instance recorded evidence onto the fix round")
        self.assertIn("does not match the active workflow instance", stale.stderr)

        fresh = self.record_preflight(new_wid, self.preflight_document())
        self.assertEqual(fresh.returncode, 0, fresh.stdout + fresh.stderr)
        fresh_evidence_id = json.loads(fresh.stdout)["evidenceId"]
        self.assertEqual(
            self.evidence(fresh_evidence_id)["workflowId"], new_wid,
            "the fix round's evidence does not carry the new instance",
        )
        self.assertEqual(self.evidence(first_evidence_id)["workflowId"], wid,
                         "the retained historical evidence changed owners")

    def test_a_bare_transition_cannot_resurrect_prior_verification_evidence(self) -> None:
        wid = self.begin_slug("ref-replay")
        self.advance_to_verification("ref-replay", wid)

        # A bare library round-trip over the same phase: the ref from the real
        # runner must not survive, so the very next ordered transition refuses.
        self.owner_phase("verification", "pending")
        self.owner_phase("verification", "passed")
        self.assertNotIn("verificationEvidence", json.loads(self.cli("status").stdout),
                         "a bare pending-to-passed replay resurrected prior evidence")
        with self.assertRaises(Exception) as blocked:
            self.owner_phase("code-review", "passed", findings="none")
        self.assertIn("verification", str(blocked.exception))

        # The real runner re-records and completion proceeds.
        verified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize("ref-replay", wid)
        self.assertEqual(self.cli("complete").returncode, 0)

    def test_tdd_demands_preflight_evidence_not_just_status(self) -> None:
        wid = self.begin_slug("bare-preflight-tdd")
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", "bare-preflight-tdd", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "bare-preflight-tdd", "--workflow-id", wid, "--stage", "preflight", "--findings", "none"),
        )
        self.owner_phase("preflight", "passed")  # bare claim: status without evidence

        marker = self.tmp / "bare-preflight-red-ran"
        red = subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(self.repo), "--slug", "bare-preflight-tdd",
             "--phase", "red", "--behavior", "evidence gate", "--seam", "workflow CLI",
             "--expected-failure", "AssertionError", "--", sys.executable, "-c",
             f"open({str(marker)!r}, 'w').close(); raise AssertionError('AssertionError: bare')"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(red.returncode, 2, "workflow.py tdd accepted a bare preflight claim: " + red.stdout + red.stderr)
        self.assertIn("preflight evidence", red.stderr)
        self.assertFalse(marker.exists(), "workflow.py tdd executed its command on a bare preflight claim")

    def test_exit_codes_reflect_the_recording_not_the_reporting(self) -> None:
        wid = self.begin_slug("exit-honesty")
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", "exit-honesty", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "exit-honesty", "--workflow-id", wid, "--stage", "preflight", "--findings", "none"),
        )

        # A successful recording whose success line cannot be written must not
        # report refusal: exit 2 means nothing was recorded.
        payload = self.tmp / "preflight-input.json"
        payload.write_text(json.dumps(self.preflight_document()), encoding="utf-8")
        with open("/dev/full", "w") as full:
            recorded = subprocess.run(
                [sys.executable, str(WORKFLOW), "record-preflight", "--repo", str(self.repo),
                 "--slug", "exit-honesty", "--workflow-id", wid, "--input", str(payload)],
                cwd=ROOT, env=self.env, text=True,
                stdout=full, stderr=subprocess.PIPE, check=False,
            )
        state = json.loads(self.cli("status").stdout)
        self.assertEqual(state["preflight"], "passed", "the recording itself failed under a full stdout")
        self.assertEqual(recorded.returncode, 0,
                         "a successful recording reported refusal because its success line could not be written: "
                         + recorded.stderr)

    def test_midpass_gates_demand_evidence_not_just_status(self) -> None:
        wid = self.begin_slug("midpass-evidence")
        self.advance_to_preflight("midpass-evidence", wid)
        self.owner_phase("tdd", "not-required")


        # Bare verification status must not open the paid final-review consult.
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        self.owner_phase("verification", "passed")
        with self.assertRaises(Exception) as blocked:
            self.owner_phase("code-review", "passed", findings="none")
        self.assertIn("verification", str(blocked.exception),
                      "a bare verification claim admitted the code-review recording")
        ready = self.checkpoint("final-review")
        self.assertFalse(ready["ready"],
                         "a bare verification claim opened the final-review consult: " + json.dumps(ready))
        self.assertTrue(any("verification" in item for item in ready["missing"]), ready["missing"])

        # The real runner restores readiness.
        verified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.owner_phase("code-review", "passed", findings="none")
        self.assertTrue(self.checkpoint("final-review")["ready"])

    def test_graph_readiness_tracks_the_dirty_candidate_through_completion(self) -> None:
        marker, slug = "FOREIGN_GRAPH_IDENTITY_ACCEPTED", "candidate-sensitive-graph"
        wid = self.begin_slug(slug)
        self.advance_to_context_forge()
        analyzed = json.loads(self.cli("status").stdout)

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        stale = json.loads(self.cli("status").stdout)
        self.assertEqual((stale["repoContextForge"], stale["gitnexus"]), ("pending", "pending"), marker)
        self.assertFalse(self.checkpoint("preflight-advice")["ready"], marker)

        self.advance_to_context_forge()
        refreshed = json.loads(self.cli("status").stdout)
        graph = self.evidence(str(refreshed["repoContextForgeEvidence"]))
        self.assertNotEqual(refreshed["activeCandidateTree"], analyzed["activeCandidateTree"], marker)
        self.assertEqual(graph["advisorProjection"]["expectedCandidateTree"], refreshed["activeCandidateTree"], marker)

        self.advance_to_verification(slug, wid)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize(slug, wid)
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        blocked = self.cli("complete")
        self.assertEqual(blocked.returncode, 2, marker + blocked.stdout + blocked.stderr)
        self.assertIn("repoContextForge", blocked.stderr, marker)

    def test_workflow_completion_survives_a_same_tree_review_commit(self) -> None:
        missing = self.cli("status")
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertIn("no active workflow", missing.stderr)

        begun = self.cli("begin", "--slug", "PR2 Replacement", "--intent", "enforce workflow completion")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        state = json.loads(begun.stdout)
        self.assertEqual(state["slug"], "pr2-replacement")
        self.assertEqual(state["phase"], "intake")
        self.assertEqual(state["nextAction"], "repo-context-forge")

        wrong_source = self.cli(
            "advisor-result", "--slug", "pr2-replacement", "--workflow-id", wid,
            "--stage", "preflight", "--source", "codex-agent", "--verdict", "completed",
        )
        self.assertEqual(wrong_source.returncode, 2, wrong_source.stdout + wrong_source.stderr)

        self.advance_to_verification("pr2-replacement", wid)
        review = self.tmp / "legacy-empty-review.json"
        review.write_text(json.dumps({"findings": [], "dispositions": []}), encoding="utf-8")
        recorded_review = self.cli(
            "record-review", "--slug", "pr2-replacement", "--workflow-id", wid,
            "--resolved-model", "test-model", "--review-context-id", "legacy-empty",
            "--input", str(review),
        )
        self.assertEqual(recorded_review.returncode, 0, "LEGACY_FINDINGLESS_FLOW_REGRESSED" + recorded_review.stdout + recorded_review.stderr)
        candidate = json.loads(self.cli("status").stdout)["activeCandidateTree"]
        self.git("commit", "-q", "--allow-empty", "-m", "same-tree commit after lead review")
        self.assertEqual(
            (json.loads(self.cli("status").stdout)["activeCandidateTree"], self.checkpoint("final-review")["ready"]),
            (candidate, True), "SAME_TREE_REVIEW_INVALIDATED",
        )
        final = self.cli(
            "advisor-result", "--slug", "pr2-replacement", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor",
            "--verdict", "commit-ready",
        )
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
        disposed = self.cli("advisor-disposition", "--slug", "pr2-replacement", "--workflow-id", wid, "--stage", "final", "--findings", "none")
        self.assertEqual(disposed.returncode, 0, disposed.stdout + disposed.stderr)

        completed = self.cli("complete")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        state = json.loads(completed.stdout)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["finalReview"], {
            "findings": "none",
            "source": "codex-advisor",
            "status": "commit-ready",
        })

    def test_public_phase_updates_follow_order_and_cannot_bypass_owned_producers(self) -> None:
        begun = self.cli("begin", "--slug", "ordered-workflow")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)

        out_of_order = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(out_of_order.returncode, 2, out_of_order.stdout + out_of_order.stderr)
        self.assertIn("tdd", out_of_order.stderr)

        for phase, refusal in (
            ("repo-context-forge", "run the Repo Context Forge bootstrap"),
            ("tdd", "lead-owned"),
            ("code-review", "lead-owned"),
        ):
            shortcut = self.cli("set-phase", "--phase", phase, "--status", "passed")
            self.assertEqual(shortcut.returncode, 2, shortcut.stdout + shortcut.stderr)
            self.assertIn(refusal, shortcut.stderr)

    def test_a_bare_context_forge_claim_publishes_as_pending_everywhere(self) -> None:
        """Producer evidence is what a passed graph step means; a claim alone is not it."""
        self.begin_slug("bare-context-claim")
        self.owner_phase("repo-context-forge", "passed")

        state = json.loads(self.cli("status").stdout)
        self.assertEqual(state["repoContextForge"], "pending", "a bare claim published as passed")
        self.assertEqual(state["gitnexus"], "pending")
        self.assertEqual(state["nextAction"], "repo-context-forge")
        self.assertIn("repo-context-forge=pending", self.cli("summary").stdout)
        self.assertFalse(self.checkpoint("preflight-advice")["ready"])
        self.assertIn("repoContextForgeEvidence", self.cli("complete").stderr)

        # The same claim carrying real producer evidence reads passed on every surface.
        record_context_forge(self.repo, self.tmp)
        state = json.loads(self.cli("status").stdout)
        self.assertEqual((state["repoContextForge"], state["gitnexus"]), ("passed", "passed"))
        self.assertTrue(self.checkpoint("preflight-advice")["ready"])

    def test_the_retired_gitnexus_transition_refuses_as_an_obsolete_step(self) -> None:
        """No manual bookkeeping survives: the graph step is producer-recorded or absent."""
        self.begin_slug("obsolete-gitnexus")
        before = json.loads(self.cli("status").stdout)
        self.assertEqual(before["gitnexus"], "pending")

        for identity in ((), ("--slug", "obsolete-gitnexus", "--workflow-id", before["workflowId"])):
            refused = self.cli("set-phase", "--phase", "gitnexus", "--status", "passed", *identity)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertIn("no longer a workflow step", refused.stderr)
        self.assertEqual(json.loads(self.cli("status").stdout), before)

        # Derived, not written: the same field reads passed once the producer's own
        # evidence exists, and nothing else can move it.
        record_context_forge(self.repo, self.tmp)
        self.assertEqual(json.loads(self.cli("status").stdout)["gitnexus"], "passed")

    def test_next_action_derives_from_the_complete_state(self) -> None:
        wid = self.begin_slug("derived-next")
        self.advance_to_preflight("derived-next", wid)

        record_context_forge(self.repo, self.tmp)
        self.assertEqual(
            json.loads(self.cli("status").stdout)["nextAction"], "tdd",
            "re-recording an earlier phase rewound nextAction instead of deriving it",
        )

    def test_implementation_and_reviews_wait_for_green(self) -> None:
        wid = self.begin_slug("tdd-gates")
        self.advance_to_preflight("tdd-gates", wid)

        self.owner_phase("tdd", "in-progress")
        self.record_real_gate(wid)
        started = self.cli("set-phase", "--phase", "implementation", "--status", "in-progress")
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)

        premature = self.cli("set-phase", "--phase", "implementation", "--status", "passed")
        self.assertEqual(premature.returncode, 2, premature.stdout + premature.stderr)
        self.assertIn("tdd", premature.stderr)

        early_verify = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(early_verify.returncode, 2, early_verify.stdout + early_verify.stderr)
        self.assertIn("tdd", early_verify.stderr)

        early_review = self.cli("set-phase", "--phase", "code-review", "--status", "not-required", "--findings", "none")
        self.assertEqual(early_review.returncode, 2, early_review.stdout + early_review.stderr)
        early_final = self.cli(
            "advisor-result", "--slug", "tdd-gates", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready",
        )
        self.assertEqual(early_final.returncode, 2, early_final.stdout + early_final.stderr)

        self.owner_phase("tdd", "passed")
        landed = self.cli("set-phase", "--phase", "implementation", "--status", "passed")
        self.assertEqual(landed.returncode, 0, landed.stdout + landed.stderr)

        state = json.loads(self.cli("status").stdout)
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"})
        self.assertEqual(state["finalReview"], {"source": None, "status": "pending", "findings": "pending"})
        self.assertEqual(state["verification"], "pending")

    def test_preflight_advice_requires_a_measured_outage_or_disposed_findings(self) -> None:
        wid = self.begin_slug("advisor-preflight-contract")
        self.advance_to_context_forge()

        unavailable = self.cli(
            "advisor-result", "--slug", "advisor-preflight-contract", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor",
            "--verdict", "unavailable", "--reason", "",
        )
        self.assertEqual(unavailable.returncode, 2, unavailable.stdout + unavailable.stderr)
        self.assertIn("unavailable requires --reason", unavailable.stderr)

        pending = self.cli(
            "advisor-result", "--slug", "advisor-preflight-contract", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor",
            "--verdict", "completed",
        )
        self.assertEqual(pending.returncode, 0, pending.stdout + pending.stderr)
        self.assertEqual(json.loads(pending.stdout)["nextAction"], "preflight")
        marker = "UNMEASURED_ADVISOR_FIXED_ACCEPTED"
        unmeasured = self.tmp / "unmeasured-advisor-fixed.json"
        unmeasured.write_text(json.dumps({"findings": [{"id": "ADV-1", "claim": "claim"}],
            "dispositions": [{"finding_id": "ADV-1", "status": "fixed", "evidence": "claimed"}]}))
        before_events = len(self.history_events())
        refused = self.dispose("advisor-preflight-contract", wid, "preflight", "addressed", str(unmeasured))
        self.assertEqual(refused.returncode, 2, marker + refused.stdout + refused.stderr)
        self.assertEqual(len(self.history_events()), before_events, marker)
        stale_marker = "STALE_ADVISOR_MEASUREMENTS_ACCEPTED"
        stale = self.disposition_document()
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        copied = self.dispose("advisor-preflight-contract", wid, "preflight", "addressed", stale)
        self.assertEqual(copied.returncode, 2, stale_marker + copied.stdout + copied.stderr)
        self.git("checkout", "--", "app.py")
        initial_fixed = self.dispose(
            "advisor-preflight-contract", wid, "preflight", "addressed",
            self.disposition_document(occurrence={
                "domain": "the complete current workflow", "count": 0, "complete": True,
                "command": "inspect current workflow", "result": "count=0",
            }),
        )
        self.assertEqual(initial_fixed.returncode, 2, "PREFLIGHT_FIXED_ACCEPTED")
        self.assertIn("immutable finding intake", initial_fixed.stderr, "PREFLIGHT_FIXED_ACCEPTED")
        addressed = self.dispose("advisor-preflight-contract", wid, "preflight", "addressed", self.disposition_document("report-only", "false"))
        self.assertEqual(addressed.returncode, 0, "ADVISOR_REPORT_ONLY_REFUSED" + addressed.stdout + addressed.stderr)
        preflight = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(preflight.returncode, 0, preflight.stdout + preflight.stderr)

    def test_advisor_refusals_name_each_disposition_shape_atomically(self) -> None:
        marker = "DISPOSITION_SHAPE_GUIDANCE_MISSING"
        for status in ("fixed", "rejected-with-evidence", "report-only", "accepted-follow-up"):
            slug = f"shape-{status}"
            wid = self.begin_slug(slug)
            self.advance_to_context_forge()
            envelope = self.tmp / f"{slug}-envelope.json"
            kind = "nonbehavioral"
            envelope.write_text(json.dumps({"schemaVersion": 1, "findings": [{
                "id": "SPEC-1", "claim": "shape is wrong", "material": True, "kind": kind,
            }], "verdict": "completed"}), encoding="utf-8")
            recorded = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid,
                                "--stage", "preflight", "--source", "codex-advisor", "--input", str(envelope))
            intake_id = json.loads(recorded.stdout)["advisorPreflight"]["intakeEvidence"]
            path = self.finding_disposition_document(
                intake_id, status, kind, "false" if status == "report-only" else "material",
            )
            document = json.loads(path.read_text(encoding="utf-8"))
            if status == "fixed":
                document["dispositions"].append(dict(document["dispositions"][0]))
            elif status == "rejected-with-evidence": document["dispositions"][0]["finding_id"] = "   "
            else:
                document["dispositions"][0]["premise"]["claim"] = "   "
            path.write_text(json.dumps(document), encoding="utf-8")
            before = self.cli("status").stdout, len(self.history_events())
            refused = self.dispose(slug, wid, "preflight", "addressed", str(path))
            self.assertEqual((refused.returncode, (self.cli("status").stdout, len(self.history_events()))),
                             (2, before), marker + refused.stdout + refused.stderr)
            self.assertIn(f"{status} expected shape", refused.stderr, "DUPLICATE_DISPOSITION_SHAPE_MISSING" if status == "fixed" else "BLANK_FINDING_ID_SHAPE_MISSING" if status == "rejected-with-evidence" else marker)
            self.assertIn("non-empty text", refused.stderr, "DISPOSITION_TEXT_SHAPE_INCOMPLETE")
            if status == "fixed":
                self.assertIn("strips and lowercases to false", refused.stderr, "DISPOSITION_NORMALIZATION_SHAPE_MISMATCH")
            path = self.finding_disposition_document(
                intake_id, status, kind, "false" if status == "report-only" else "material",
            )
            accepted = self.dispose(slug, wid, "preflight", "addressed", str(path))
            self.assertEqual(accepted.returncode, 0, marker + accepted.stdout + accepted.stderr)

    def test_legacy_behavioral_disposition_requires_immutable_intake(self) -> None:
        marker, slug = "LEGACY_BEHAVIORAL_DISPOSITION_ACCEPTED_OR_MUTATED_STATE", "legacy-behavioral"
        wid = self.begin_slug(slug)
        self.advance_to_context_forge()
        self.rewrite_latest_state(lambda state: state.__setitem__(
            "advisorPreflight", {"source": "codex-advisor", "status": "completed"}))
        path = Path(self.disposition_document("accepted-follow-up"))
        document = json.loads(path.read_text(encoding="utf-8"))
        document["dispositions"][0]["kind"] = "behavioral"
        path.write_text(json.dumps(document), encoding="utf-8")
        before = self.cli("status").stdout, len(self.history_events())
        refused = self.dispose(slug, wid, "preflight", "addressed", str(path))
        after = self.cli("status").stdout, len(self.history_events())
        self.assertEqual((refused.returncode, after), (2, before), marker + refused.stdout + refused.stderr)
        self.assertIn("immutable finding intake", refused.stderr, marker)

    def test_advisor_disposition_cannot_create_or_alter_raw_results(self) -> None:
        wid = self.begin_slug("producer-owned-advice")
        self.advance_to_context_forge()

        orphan = self.dispose("producer-owned-advice", wid, "preflight", "addressed", self.disposition_document("accepted-follow-up"))
        self.assertEqual(orphan.returncode, 2, orphan.stdout + orphan.stderr)
        self.assertIn("cannot create", orphan.stderr)

        direct = self.cli(
            "advisor-result", "--slug", "producer-owned-advice", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor",
            "--verdict", "completed", "--findings", "addressed",
        )
        self.assertEqual(direct.returncode, 2, direct.stdout + direct.stderr)
        self.assertIn("findings=pending", direct.stderr)

        recorded = self.cli(
            "advisor-result", "--slug", "producer-owned-advice", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor",
            "--verdict", "completed",
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        raw = json.loads(recorded.stdout)["advisorPreflight"]
        self.assertEqual(raw, {"source": "codex-advisor", "status": "completed", "findings": "none", "reason": None})

        stale = self.dispose("some-other-pass", wid, "preflight", "addressed", self.disposition_document("accepted-follow-up"))
        self.assertEqual(stale.returncode, 2, stale.stdout + stale.stderr)
        self.assertIn("does not match the active workflow", stale.stderr)
        self.assertEqual(
            json.loads(self.cli("status").stdout)["advisorPreflight"]["findings"], "none",
            "a stale-slug disposition mutated the active workflow",
        )

        stale_pause = self.cli("pause", "--reason", "waiting", "--slug", "some-other-pass", "--workflow-id", wid)
        self.assertEqual(stale_pause.returncode, 2, stale_pause.stdout + stale_pause.stderr)
        self.assertNotIn("paused", json.loads(self.cli("status").stdout))

        disposed = self.dispose("producer-owned-advice", wid, "preflight", "addressed", self.disposition_document("accepted-follow-up"))
        self.assertEqual(disposed.returncode, 0, disposed.stdout + disposed.stderr)
        after = json.loads(disposed.stdout)["advisorPreflight"]
        disposition_id = after.pop("dispositionEvidence")
        self.assertEqual(after, {"source": "codex-advisor", "status": "completed", "findings": "addressed", "reason": None})
        self.assertEqual(self.evidence(disposition_id)["stage"], "preflight")

    def test_preflight_behavioral_finding_rides_the_map_and_closes_through_green(self) -> None:
        marker, mixed_marker, slug = (
            "PREFLIGHT_ATTACK_LIFECYCLE_BROKEN",
            "MIXED_INTAKE_SUBSET_DISPOSITION_BLOCKED",
            "owned-attack",
        )
        wid = self.begin_slug(slug)
        self.advance_to_context_forge()
        envelope = self.tmp / "advisor-envelope.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "findings": [
            {"id": "SPEC-1", "claim": "proof is missing", "material": True, "kind": "behavioral"},
            {"id": "SPEC-2", "claim": "documentation is incomplete", "material": True, "kind": "nonbehavioral"},
        ], "verdict": "completed"}), encoding="utf-8")
        recorded = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                            "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(recorded.returncode, 0, marker + recorded.stdout + recorded.stderr)
        intake_id = json.loads(recorded.stdout)["advisorPreflight"]["intakeEvidence"]
        # A subset disposition resolves the nonbehavioral finding; the behavioral
        # one rides the pass as a direct map-owned attack obligation.
        subset = self.mixed_finding_disposition_document(intake_id, "fixed")
        value = json.loads(subset.read_text(encoding="utf-8"))
        value["dispositions"] = value["dispositions"][1:]
        subset.write_text(json.dumps(value), encoding="utf-8")
        addressed = self.dispose(slug, wid, "preflight", "addressed", str(subset))
        self.assertEqual(addressed.returncode, 0, mixed_marker + addressed.stdout + addressed.stderr)
        source_ref = [{"type": "finding", "evidenceId": intake_id, "id": "SPEC-1"}]
        mapped = {"id": "BM_ADV_1", "kind": "contract", "basis": "advisor finding",
            "behavior": "the owned attack closes the finding", "seam": "workflow CLI", "redFailure": marker,
            "expected": "the explicit fixed disposition closes the finding", "status": "pending",
            "sourceRefs": source_ref}
        document = self.preflight_document()
        document["behaviorMap"] = [{**mapped, "sourceRefs": []}]
        unowned = self.record_preflight(wid, document)
        self.assertEqual(unowned.returncode, 2, marker + unowned.stdout + unowned.stderr)
        self.assertIn("SPEC-1", unowned.stderr, marker)
        document["behaviorMap"] = [mapped]
        preflight = self.record_preflight(wid, document)
        self.assertEqual(preflight.returncode, 0, marker + preflight.stdout + preflight.stderr)
        early = self.dispose(slug, wid, "preflight", "addressed", str(self.finding_disposition_document(intake_id, "fixed")))
        self.assertEqual(early.returncode, 2, marker + early.stdout + early.stderr)
        (self.repo / "test_preflight_proof.py").write_text("import app, unittest\nclass Proof(unittest.TestCase):\n"
            f"    def test_value(self): self.assertEqual(app.value, 2, {marker!r})\n", encoding="utf-8")
        command = [sys.executable, str(WORKFLOW), "tdd", "--repo", str(self.repo), "--slug", slug, "--phase", "red", "--behavior-id", "BM_ADV_1", "--", sys.executable, "-m", "unittest", "test_preflight_proof"]
        phase_index = command.index("red")
        for phase, value in (("red", 1), ("green", 2)):
            (self.repo / "app.py").write_text(f"value = {value}\n", encoding="utf-8")
            command[phase_index] = phase
            result = subprocess.run(command, cwd=ROOT, env=self.env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, marker + result.stdout + result.stderr)
        update = self.tmp / "preflight-proof-reassessment.json"
        update.write_text(json.dumps({"sourceBehaviorId": "BM_ADV_1", "reassessment": "no new proof obligations", "items": [], "dispositions": []}), encoding="utf-8")
        reassessed = self.cli("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update))
        self.assertEqual(reassessed.returncode, 0, marker + reassessed.stdout + reassessed.stderr)
        fixed = self.dispose(slug, wid, "preflight", "addressed", str(self.finding_disposition_document(intake_id, "fixed")))
        self.assertEqual(fixed.returncode, 0, marker + fixed.stdout + fixed.stderr)
        closed = json.loads(fixed.stdout)
        self.assertEqual({entry["findingId"]: entry["status"] for entry in closed["findingStates"]},
                         {"SPEC-1": "fixed", "SPEC-2": "report-only"}, marker)
        self.advance_to_context_forge()
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        self.assertEqual(self.verify_run(sys.executable, "-c", "pass").returncode, 0, marker)
        self.owner_phase("code-review", "passed", findings="none")
        self.finalize(slug, wid)
        self.assertEqual(self.cli("complete").returncode, 0, marker)
    def post_edit_hook(self, slug: str) -> None:
        hook = subprocess.run(
            [sys.executable, str(ROOT / "hooks" / "code-quality-gate.py")], cwd=self.repo,
            env=self.env, text=True, input=json.dumps({"session_id": slug, "tool_input": {"file_path": str(self.repo / "app.py")}}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(hook.returncode, 0, hook.stdout + hook.stderr)

    def context_mismatch_then_edit(self, slug: str) -> tuple[dict[str, object], dict[str, object]]:
        wid = self.begin_slug(slug)
        self.advance_to_verification(slug, wid)
        self.owner_phase("code-review", "passed", findings="none")
        envelope = self.tmp / f"{slug}-mismatch.json"
        envelope.write_text('{"schemaVersion":1,"findings":[],"verdict":"context-mismatch"}', encoding="utf-8")
        mismatch = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(mismatch.returncode, 0, mismatch.stdout + mismatch.stderr)
        self.post_edit_hook(slug)
        return json.loads(mismatch.stdout), json.loads(self.cli("status").stdout)

    def test_context_mismatch_invalidation_preserves_reopened_gates(self) -> None:
        before, after = self.context_mismatch_then_edit("mismatch-gates")
        evidence_id = str(before["finalReviewContextMismatchEvidence"])
        self.assertEqual(
            (after["phase"], after["implementation"], after["verification"], after["codeReview"]["status"], after["finalReview"]["status"]),
            ("implementation", "in-progress", "pending", "pending", "pending"), "CONTEXT_MISMATCH_INVALIDATION_GATES_REGRESSED")
        self.assertEqual(self.evidence(evidence_id)["verdict"], "context-mismatch", "CONTEXT_MISMATCH_INVALIDATION_GATES_REGRESSED")
        self.assertFalse(self.checkpoint("final-review")["ready"], "CONTEXT_MISMATCH_INVALIDATION_GATES_REGRESSED")

    def test_context_mismatch_invalidation_retires_live_marker(self) -> None:
        _, after = self.context_mismatch_then_edit("mismatch-routing")
        self.assertEqual(("finalReviewContextMismatchEvidence" in after, after["nextAction"]),
                         (False, "verification"), "CONTEXT_MISMATCH_INVALIDATION_MISROUTED")

    def test_final_rejections_use_one_context_matched_appeal_and_effective_readiness(self) -> None:
        marker = "FINAL_APPEAL_STATE_ADVANCED_INCORRECTLY"
        def reject(slug: str, identifiers: tuple[str, ...]) -> tuple[str, Path]:
            wid = self.begin_slug(slug); self.advance_to_verification(slug, wid)
            self.owner_phase("code-review", "passed", findings="none")
            envelope = self.json_file(f"{slug}-final.json", {"schemaVersion": 1, "findings": [
                {"id": item, "claim": f"{item} remains material", "material": True, "kind": "nonbehavioral"}
                for item in identifiers], "verdict": "fix-before-commit"})
            recorded = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                                "--source", "codex-advisor", "--input", str(envelope))
            intake = json.loads(recorded.stdout)["finalReview"]["intakeEvidence"]
            disposition = self.json_file(f"{slug}-rejections.json", {"context": self.disposition_context(),
                "intakeEvidenceId": intake, "dispositions": [{"finding_id": item, "status": "rejected-with-evidence",
                "kind": "nonbehavioral", "premise": {"claim": "premise", "command": "inspect", "result": "false"},
                "occurrence": {"domain": "fixture", "count": 1, "complete": True, "command": "inspect", "result": "one"},
                "materialConsequence": {"claim": "material", "command": "inspect", "result": "material"},
                "evidence": "current tree disproves the premise"} for item in identifiers]})
            self.run_cli(("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                          "--findings", "addressed", "--input", str(disposition)))
            return wid, envelope

        slug = "appeal-stale-gates"; wid, envelope = reject(slug, ("SPEC-1",))
        events, ran = len(self.history_events()), self.tmp / "ordinary-appeal-generic-ran"
        blocked = self.verify_run(sys.executable, "-c", f"from pathlib import Path; Path({str(ran)!r}).touch()")
        self.assertEqual((self.checkpoint("final-review")["ready"], blocked.returncode, ran.exists(), len(self.history_events()) - events),
                         (True, 0, True, 2), "APPEAL_REVALIDATION_SCOPE_BYPASSED")
        envelope.write_text('{"schemaVersion":1,"findings":[],"verdict":"commit-ready"}', encoding="utf-8")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8"); events = len(self.history_events())
        stale = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                         "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual((stale.returncode, len(self.history_events()) - events), (2, 0), "APPEAL_STALE_CANDIDATE_ACCEPTED")
        generic = self.verify_run(sys.executable, "-c", "pass", gate=False)
        gate = self.cli("verify", "--slug", slug, "--kind", "quality-gate", "--base-ref", "HEAD")
        review = self.json_file("shell-drift-review.json", {"findings": []})
        recorded = self.cli("record-review", "--slug", slug, "--workflow-id", wid, "--resolved-model", "test-model",
                            "--review-context-id", "shell-drift", "--input", str(review))
        self.assertEqual((generic.returncode, gate.returncode, recorded.returncode), (0, 0, 0), "APPEAL_SHELL_DRIFT_UNRECOVERABLE")
        self.post_edit_hook(slug)
        reassessment = self.json_file("appeal-reassessment.json", {"reassessment": "refresh changed-candidate appeal bindings"})
        self.run_cli(("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(reassessment)))
        self.advance_to_context_forge()
        self.owner_phase("implementation", "passed"); self.assertEqual(self.verify_run(sys.executable, "-c", "pass").returncode, 0)
        self.owner_phase("code-review", "passed", findings="none")
        appeal_args = ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                       "--source", "codex-advisor", "--input", str(envelope))
        events = len(self.history_events()); appealed = self.cli(*appeal_args); state = json.loads(appealed.stdout)
        events = len(self.history_events()); second = self.cli(*appeal_args); delta = len(self.history_events()) - events; completed = self.cli("complete")
        self.assertEqual((appealed.returncode, state["finalAppealConsumed"], second.returncode, delta, completed.returncode),
                         (0, True, 2, 0, 0), marker)

        slug = "appeal-concession"; wid, _ = reject(slug, ("SPEC-1", "SPEC-2"))
        appeal = self.json_file("appeal-concession.json", {"schemaVersion": 1, "findings": [
            {"id": "SPEC-1", "claim": "accepted", "material": False, "kind": "nonbehavioral"},
            {"id": "SPEC-NEW", "claim": "new issue", "material": True, "kind": "nonbehavioral"}],
            "verdict": "fix-before-commit"})
        appealed = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                            "--source", "codex-advisor", "--input", str(appeal)); state = json.loads(appealed.stdout)
        by_id = {entry["findingId"]: entry for entry in state["findingStates"]}
        self.assertEqual((by_id["SPEC-1"]["appealStatus"], by_id["SPEC-2"]["appealStatus"], by_id["SPEC-NEW"]["status"]),
                         ("conceded", "conceded", "pending"), marker)
        closure = self.json_file("appeal-new-finding.json", {"context": self.disposition_context(),
            "intakeEvidenceId": state["finalReview"]["intakeEvidence"], "dispositions": [{"finding_id": "SPEC-NEW",
            "status": "rejected-with-evidence", "kind": "nonbehavioral",
            "premise": {"claim": "issue", "command": "inspect", "result": "false"},
            "occurrence": {"domain": "fixture", "count": 1, "complete": True, "command": "inspect", "result": "one"},
            "materialConsequence": {"claim": "runtime", "command": "inspect", "result": "false"}, "evidence": "no consequence"}]})
        closed = json.loads(self.dispose(slug, wid, "final", "addressed", str(closure)).stdout)
        events = len(self.history_events()); second = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid,
            "--stage", "final", "--source", "codex-advisor", "--input", str(appeal))
        self.assertEqual((closed["nextAction"], next(x for x in closed["findingStates"] if x["findingId"] == "SPEC-NEW")["appealStatus"],
                          self.checkpoint("final-review")["ready"], second.returncode, len(self.history_events()) - events),
                         ("complete-workflow", "disagreement", False, 2, 0), "REJECTION_AFTER_APPEAL_DID_NOT_STAND")

    def test_terminal_context_mismatch_allows_reconsult(self) -> None:
        marker, slug = "TERMINAL_MISMATCH_RECONSULT_REJECTED", "terminal-mismatch-reconsult"
        wid = self.begin_slug(slug); self.advance_to_verification(slug, wid)
        self.owner_phase("code-review", "passed", findings="none"); self.run_cli(("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready"), ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--findings", "none"))
        mismatch = self.tmp / "terminal-context-mismatch.json"; mismatch.write_text('{"schemaVersion":1,"findings":[],"verdict":"context-mismatch"}', encoding="utf-8")
        self.run_cli(("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--input", str(mismatch)))
        self.assertEqual(self.cli("complete").returncode, 2, marker)
        before = len(self.history_events()); response = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready")
        self.assertEqual((response.returncode, len(self.history_events()) - before), (0, 1), marker + response.stdout + response.stderr)

    def test_open_correction_batch_blocks_broad_gates_and_routes_tdd_reassessment(self) -> None:
        marker, appeal_marker = "OPEN_CORRECTION_BYPASSED_GATE", "MIXED_CORRECTION_APPEAL_ADMITTED"
        def mixed_disposition(intake: str) -> Path:
            path = self.finding_disposition_document(intake); document = json.loads(path.read_text(encoding="utf-8"))
            document["dispositions"] = [{"finding_id": "SPEC-2", "status": "rejected-with-evidence", "kind": "behavioral",
                "premise": {"claim": "claim", "command": "inspect", "result": "false"},
                "occurrence": {"domain": "probe", "count": 0, "complete": True, "command": "inspect", "result": "zero"},
                "materialConsequence": {"claim": "material", "command": "inspect", "result": "none"}, "evidence": "false premise"}]
            path.write_text(json.dumps(document), encoding="utf-8"); return path
        slug, wid = "correction-gating", self.begin_slug("correction-gating")
        self.advance_to_verification(slug, wid); self.owner_phase("code-review", "passed", findings="none")
        envelope = self.json_file("correction-gating-final.json", {"schemaVersion": 1, "findings": [
            {"id": "SPEC-1", "claim": "proof missing", "material": True, "kind": "behavioral"},
            {"id": "SPEC-2", "claim": "rejected", "material": True, "kind": "behavioral"}], "verdict": "fix-before-commit"})
        recorded = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                            "--source", "codex-advisor", "--input", str(envelope)); state = json.loads(recorded.stdout)
        events = len(self.history_events()); duplicate = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid,
            "--stage", "final", "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual((state["nextAction"], duplicate.returncode, len(self.history_events()) - events),
                         ("classify-current-findings", 2, 0), "INVALID_FINAL_INTAKE_ADMITTED")
        ran = self.tmp / "blocked-generic-ran"; review_input = self.json_file("blocked-review.json", {"findings": []})
        generic = self.verify_run(sys.executable, "-c", f"from pathlib import Path; Path({str(ran)!r}).touch()")
        gate = self.cli("verify", "--slug", slug, "--kind", "quality-gate", "--base-ref", "HEAD")
        review = self.cli("record-review", "--slug", slug, "--workflow-id", wid, "--resolved-model", "test-model",
                          "--review-context-id", "blocked-correction", "--input", str(review_input))
        self.assertEqual((self.cli("complete").returncode, generic.returncode, ran.exists(), gate.returncode, review.returncode),
                         (2, 0, True, 0, 0), marker)
        intake = state["finalReview"]["intakeEvidence"]
        self.run_cli(("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                      "--findings", "addressed", "--input", str(mixed_disposition(intake))))
        appeal = self.json_file("mixed-appeal.json", {"schemaVersion": 1, "findings": [
            {"id": "SPEC-2", "claim": "rejected", "material": False, "kind": "behavioral"}], "verdict": "commit-ready"})
        events = len(self.history_events()); blocked = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid,
            "--stage", "final", "--source", "codex-advisor", "--input", str(appeal))
        self.assertEqual((blocked.returncode, len(self.history_events()) - events), (2, 0), appeal_marker)
        ref = [{"type": "finding", "evidenceId": intake, "id": "SPEC-1"}]
        update = self.json_file("correction-map.json", {"reassessment": "map correction", "dispositions": [], "items": [
            {"id": "BM_ADV_1", "kind": "contract", "basis": "finding", "behavior": "correction closes", "seam": "workflow CLI",
             "expected": "observable", "redFailure": marker, "status": "pending", "sourceRefs": ref},
            {"id": "BM_ADV_PRESERVE", "kind": "preservation", "basis": "finding", "behavior": "preserve advisor intake",
             "seam": "advisor intake", "expected": "immutable", "redFailure": marker, "status": "already-satisfied",
             "evidence": "intake remains recorded", "sourceRefs": ref}]})
        self.run_cli(("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update)))
        (self.repo / "test_correction_gate.py").write_text("import app,unittest\nclass T(unittest.TestCase):\n"
            f" def test_value(self):self.assertEqual(app.value,2,{marker!r})\n", encoding="utf-8")
        command = [sys.executable, str(WORKFLOW), "tdd", "--repo", str(self.repo), "--slug", slug, "--phase", "red",
                   "--behavior-id", "BM_ADV_1", "--", sys.executable, "-m", "unittest", "test_correction_gate"]
        phase = command.index("red")
        for name, value in (("red", 1), ("green", 2)):
            command[phase] = name; (self.repo / "app.py").write_text(f"value = {value}\n", encoding="utf-8")
            result = subprocess.run(command, cwd=ROOT, env=self.env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, marker + result.stdout + result.stderr)
        update.write_text(json.dumps({"sourceBehaviorId": "BM_ADV_1", "reassessment": "none", "items": [], "dispositions": []}), encoding="utf-8")
        self.run_cli(("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update)))
        fixed = self.dispose(slug, wid, "final", "addressed", str(self.finding_disposition_document(intake, "fixed")))
        self.assertEqual(json.loads(fixed.stdout)["nextAction"], "appeal-final-review", marker)
        self.owner_phase("implementation", "passed"); self.assertEqual(self.verify_run(sys.executable, "-c", "pass").returncode, 0)
        self.owner_phase("code-review", "passed", findings="none")
        appealed = self.cli("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                            "--source", "codex-advisor", "--input", str(appeal))
        self.assertEqual((appealed.returncode, json.loads(appealed.stdout)["nextAction"]), (0, "complete-workflow"), appeal_marker)

    def test_behavioral_fixed_requires_linked_green_and_reassessment(self) -> None:
        marker = "BEHAVIORAL_FIXED_WITHOUT_GREEN_CLOSURE"
        completion_marker = "FIXED_RESERVATION_ORPHANED"
        unrelated_marker = "UNRELATED_GREEN_BLOCKED_BY_FIXED_FINDING"
        slug = "behavioral-fixed"
        wid = self.begin_slug(slug)
        self.advance_to_verification(slug, wid)
        self.owner_phase("code-review", "passed", findings="none")
        envelope = self.tmp / "fixed-envelope.json"
        envelope.write_text(json.dumps({
            "schemaVersion": 1,
            "findings": [{
                "id": "SPEC-1", "claim": "proof is missing",
                "material": True, "kind": "behavioral",
            }],
            "verdict": "fix-before-commit",
        }), encoding="utf-8")
        recorded = self.cli(
            "advisor-result", "--slug", slug, "--workflow-id", wid,
            "--stage", "final", "--source", "codex-advisor", "--input", str(envelope),
        )
        self.assertEqual(recorded.returncode, 0, marker + recorded.stdout + recorded.stderr)
        intake_id = json.loads(recorded.stdout)["finalReview"]["intakeEvidence"]
        source_ref = [{"type": "finding", "evidenceId": intake_id, "id": "SPEC-1"}]
        mapped = {
            "id": "BM_ADV_1", "kind": "contract", "basis": "advisor finding",
            "behavior": "the workflow opens the mapped proof cycle", "seam": "workflow CLI",
            "expected": "the RED transition is observable", "redFailure": "PROOF_CYCLE_NOT_OPEN",
            "status": "pending", "sourceRefs": source_ref,
        }
        preserved = {
            "id": "BM_ADV_PRESERVE", "kind": "preservation", "basis": "advisor finding",
            "behavior": "preserve advisor intake", "seam": "advisor intake",
            "expected": "advisor intake remains valid", "redFailure": "PROOF_CYCLE_NOT_OPEN",
            "status": "already-satisfied", "evidence": "the current advisor intake is preserved",
            "sourceRefs": source_ref,
        }
        update = self.tmp / "fixed-reassessment.json"
        update.write_text(json.dumps({"reassessment": "map final finding", "items": [mapped, preserved], "dispositions": []}), encoding="utf-8")
        mapped_result = self.cli("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update))
        self.assertEqual(mapped_result.returncode, 0, marker + mapped_result.stdout + mapped_result.stderr)
        disposition = self.finding_disposition_document(intake_id, "fixed")
        early = self.dispose(slug, wid, "final", "addressed", str(disposition))
        self.assertEqual(early.returncode, 2, marker + early.stdout + early.stderr)
        probe = self.repo / "test_cycle_probe.py"
        probe.write_text("import app, unittest\nclass CycleProbe(unittest.TestCase):\n"
                         "    def test_value(self): self.assertEqual(app.value, 2, 'PROOF_CYCLE_NOT_OPEN')\n",
                         encoding="utf-8")
        command = [
            sys.executable, str(WORKFLOW), "tdd", "--repo", str(self.repo), "--slug", slug,
            "--phase", "red", "--behavior-id", "BM_ADV_1", "--",
            sys.executable, "-m", "unittest", "test_cycle_probe",
        ]
        phase_index = command.index("red")
        for phase, value in (("red", 1), ("green", 2)):
            command[phase_index] = phase
            (self.repo / "app.py").write_text(f"value = {value}\n", encoding="utf-8")
            result = subprocess.run(command, cwd=ROOT, env=self.env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, marker + result.stdout + result.stderr)
        update.write_text(json.dumps({
            "sourceBehaviorId": "BM_ADV_1", "reassessment": "no new proof obligations",
            "items": [], "dispositions": [],
        }), encoding="utf-8")
        reassessed = self.cli(
            "tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update)
        )
        self.assertEqual(reassessed.returncode, 0, marker + reassessed.stdout + reassessed.stderr)
        disposition = self.finding_disposition_document(intake_id, "fixed")
        fixed = self.dispose(slug, wid, "final", "addressed", str(disposition))
        self.assertEqual(fixed.returncode, 0, marker + fixed.stdout + fixed.stderr)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        self.assertEqual(self.verify_run(sys.executable, "-c", "pass").returncode, 0, marker)
        self.owner_phase("code-review", "passed", findings="none")
        self.run_cli(
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
             "--source", "codex-advisor", "--verdict", "commit-ready"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid,
             "--stage", "final", "--findings", "none"),
        )
        update.write_text(json.dumps({
            "reassessment": "a sharper item replaces the fixed proof", "items": [{
                "id": "BM_ADV_2", "kind": "contract", "basis": "sharper proof",
                "behavior": "the replacement reaches GREEN", "seam": "app module",
                "expected": "app.value is 2", "redFailure": unrelated_marker, "status": "pending",
                "sourceRefs": source_ref,
            }], "dispositions": [{
                "id": "BM_ADV_1", "status": "superseded", "supersededBy": "BM_ADV_2",
                "evidence": "the sharper item owns the outcome",
            }],
        }), encoding="utf-8")
        before_state, before_events = json.loads(self.cli("status").stdout), len(self.history_events())
        superseded = self.cli("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update))
        self.assertEqual(superseded.returncode, 2, completion_marker + superseded.stdout + superseded.stderr)
        self.assertEqual(json.loads(self.cli("status").stdout), before_state, completion_marker)
        self.assertEqual(len(self.history_events()), before_events, completion_marker)
        for status in ("rejected-with-evidence", "accepted-follow-up"):
            disposition = self.finding_disposition_document(intake_id, status)
            refused = self.dispose(slug, wid, "final", "addressed", str(disposition))
            self.assertEqual(refused.returncode, 2, completion_marker + refused.stdout + refused.stderr)
            self.assertIn("already has terminal disposition fixed", refused.stderr, completion_marker)
            self.assertEqual(json.loads(self.cli("status").stdout), before_state, completion_marker)
            self.assertEqual(len(self.history_events()), before_events, completion_marker)
        unrelated = json.loads(update.read_text(encoding="utf-8"))
        unrelated["dispositions"] = []
        update.write_text(json.dumps(unrelated), encoding="utf-8")
        self.assertEqual(self.cli("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update)).returncode, 0, unrelated_marker)
        probe.write_text(f"import app, unittest\nclass CycleProbe(unittest.TestCase):\n    def test_value(self): self.assertEqual(app.value, 2, {unrelated_marker!r})\n", encoding="utf-8")
        command[command.index("--behavior-id") + 1] = "BM_ADV_2"
        phase_index = command.index("--phase") + 1
        for phase, value in (("red", 1), ("green", 2)):
            (self.repo / "app.py").write_text(f"value = {value}\n", encoding="utf-8")
            command[phase_index] = phase
            result = subprocess.run(command, cwd=ROOT, env=self.env, text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, unrelated_marker + result.stdout + result.stderr)
        blocked = self.cli("complete")
        self.assertEqual(blocked.returncode, 2, unrelated_marker + blocked.stdout + blocked.stderr)
        self.assertIn("workflow incomplete", blocked.stderr, unrelated_marker)
        update.write_text(json.dumps({"sourceBehaviorId": "BM_ADV_2", "reassessment": "GREEN replacement preserves fixed proof", "items": [], "dispositions": [{"id": "BM_ADV_1", "status": "superseded", "supersededBy": "BM_ADV_2", "evidence": "the GREEN replacement owns the outcome"}]}), encoding="utf-8")
        self.assertEqual(self.cli("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update)).returncode, 0, "FIXED_GREEN_SUPERSESSION_REFUSED")
        self.advance_to_context_forge()
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        verified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(verified.returncode, 0, marker + verified.stdout + verified.stderr)
        self.owner_phase("code-review", "passed", findings="none")
        self.run_cli(
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
             "--source", "codex-advisor", "--verdict", "commit-ready"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid,
             "--stage", "final", "--findings", "none"),
        )
        completed = self.cli("complete")
        self.assertEqual(completed.returncode, 0, completion_marker + completed.stdout + completed.stderr)

    def test_review_finding_owns_its_attack_through_tdd_map_and_green_closes_fixed(self) -> None:
        marker, slug = "REVIEW_FINDING_NOT_FIXED", "review-finding-proof"
        wid = self.begin_slug(slug)
        self.advance_to_verification(slug, wid)
        review_args = (
            "record-review", "--slug", slug, "--workflow-id", wid,
            "--resolved-model", "gpt-5", "--review-context-id", "review-proof", "--input",
        )
        review = self.tmp / "review-intake.json"
        review.write_text(json.dumps({"findings": [{
            "id": "SPEC-1", "axis": "Spec", "severity": "high", "material": True,
            "kind": "behavioral", "location": "app.py:1", "claim": "value is wrong",
            "evidence": "app.value is 1", "consequence": "the result is wrong",
            "smallest_action": "set the value to 2",
        }]}), encoding="utf-8")
        intake = self.cli(*review_args, str(review))
        self.assertEqual(intake.returncode, 0, marker + intake.stdout + intake.stderr)
        intake_id = json.loads(intake.stdout)["summaryId"]

        update = self.tmp / "review-finding-map.json"
        update.write_text(json.dumps({
            "reassessment": "own the review finding with a real attack", "dispositions": [], "items": [{
                "id": "BM_ADV_1", "kind": "contract", "basis": "review finding",
                "behavior": "the reviewed value is corrected", "seam": "app module",
                "expected": "app.value is 2", "redFailure": marker, "status": "pending",
                "sourceRefs": [{"type": "finding", "evidenceId": intake_id, "id": "SPEC-1"}]}],
        }), encoding="utf-8")
        mapped = self.cli("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update))
        self.assertEqual(mapped.returncode, 0, marker + mapped.stdout + mapped.stderr)
        early = self.cli(*review_args, str(self.review_finding_disposition_document(intake_id, "fixed")))
        self.assertEqual(early.returncode, 2, marker + early.stdout + early.stderr)
        self.assertIn("GREEN", early.stderr, marker)

        probe = self.repo / "test_review_fix.py"
        probe.write_text("import app, unittest\nclass ReviewFix(unittest.TestCase):\n"
                         f"    def test_value(self): self.assertEqual(app.value, 2, {marker!r})\n",
                         encoding="utf-8")
        command = [
            sys.executable, str(WORKFLOW), "tdd", "--repo", str(self.repo), "--slug", slug,
            "--phase", "red", "--behavior-id", "BM_ADV_1", "--",
            sys.executable, "-m", "unittest", "test_review_fix",
        ]
        red = subprocess.run(command, cwd=ROOT, env=self.env, text=True, capture_output=True, check=False)
        self.assertEqual(red.returncode, 0, marker + red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        command[command.index("red")] = "green"
        green = subprocess.run(command, cwd=ROOT, env=self.env, text=True, capture_output=True, check=False)
        self.assertEqual(green.returncode, 0, marker + green.stdout + green.stderr)
        update.write_text(json.dumps({
            "sourceBehaviorId": "BM_ADV_1", "reassessment": "no new proof obligations",
            "items": [], "dispositions": [],
        }), encoding="utf-8")
        reassessed = self.cli("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(update))
        self.assertEqual(reassessed.returncode, 0, marker + reassessed.stdout + reassessed.stderr)
        fixed = self.cli(*review_args, str(self.review_finding_disposition_document(intake_id, "fixed")))
        self.assertEqual(fixed.returncode, 0, marker + fixed.stdout + fixed.stderr)
        self.assertEqual(json.loads(fixed.stdout)["status"], "pending", marker)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        self.assertEqual(self.verify_run(sys.executable, "-c", "pass").returncode, 0, marker)
        review.write_text(json.dumps({"findings": [], "dispositions": []}), encoding="utf-8")
        refreshed = self.cli(*review_args, str(review))
        self.assertEqual(refreshed.returncode, 0, marker + refreshed.stdout + refreshed.stderr)
        self.assertEqual(json.loads(refreshed.stdout)["status"], "passed", marker)
    def test_addressed_disposition_demands_a_structured_document(self) -> None:
        wid = self.begin_slug("disposition-document")
        self.advance_to_context_forge()
        self.run_cli((
            "advisor-result", "--slug", "disposition-document", "--workflow-id", wid,
            "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed",
        ))
        initial_events = len(self.history_events())

        undocumented = self.dispose("disposition-document", wid, "preflight", "addressed")
        unbacked = "an addressed disposition was recorded with no document"
        self.assertEqual(undocumented.returncode, 2, unbacked)
        self.assertEqual(json.loads(self.cli("status").stdout)["advisorPreflight"]["findings"], "none", unbacked)
        self.assertNotIn("dispositionEvidence",
                         json.loads(self.cli("status").stdout)["advisorPreflight"], unbacked)
        self.assertEqual(len(self.history_events()), initial_events, unbacked)

        malformed = self.tmp / "malformed.json"
        for reason, body in (
            ("requires context, findings, and dispositions", {"findings": []}),
            ("no findings is --findings none", {"findings": [], "dispositions": []}),
            ("ids must be non-empty and unique", {
                "findings": [{"id": "", "claim": "c"}], "dispositions": []}),
            ("requires a claim", {
                "findings": [{"id": "ADV-1", "claim": "  "}], "dispositions": []}),
            ("every finding requires one lead disposition", {
                "findings": [{"id": "ADV-1", "claim": "c"}, {"id": "ADV-2", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "fixed", "evidence": "e"}]}),
            ("must reference a finding", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "fixed", "evidence": "e"},
                                 {"finding_id": "GHOST", "status": "fixed", "evidence": "e"}]}),
            ("invalid or duplicate disposition", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "waived", "evidence": "e"}]}),
            ("requires evidence", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "fixed", "evidence": " "}]}),
            ("accepted-follow-up requires reference", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "accepted-follow-up", "evidence": "e"}]}),
            ("requires evidence", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "fixed",
                                  "reference": "https://example.invalid/issues/1"}]}),
            # The document's fields are text. A coerced number or object would
            # satisfy a truthiness check and record a finding nobody can read.
            ("requires a claim", {
                "findings": [{"id": "ADV-1", "claim": 7}],
                "dispositions": [{"finding_id": "ADV-1", "status": "fixed", "evidence": "e"}]}),
            ("requires evidence", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "fixed",
                                  "evidence": {"measured": True}}]}),
            ("accepted-follow-up requires reference", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": "accepted-follow-up",
                                  "reference": 42}]}),
            # An unhashable value must refuse, not reach a set membership test:
            # `x in <set>` raises TypeError, which escapes main() as exit 1.
            ("each disposition must reference a finding", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": [], "status": "fixed", "evidence": "e"}]}),
            ("invalid or duplicate disposition", {
                "findings": [{"id": "ADV-1", "claim": "c"}],
                "dispositions": [{"finding_id": "ADV-1", "status": {}, "evidence": "e"}]}),
        ):
            body["context"] = self.disposition_context()
            for item in body.get("dispositions", []):
                if isinstance(item, dict):
                    item.update({"kind": "nonbehavioral", "premise": {"claim": "c", "command": "inspect", "result": "true"},
                                 "occurrence": {"domain": "fixture", "count": 1, "complete": True, "command": "inspect", "result": "one"},
                                 "materialConsequence": {"claim": "material", "command": "inspect", "result": "yes"}})
                    if item.get("status") == "fixed": item.update({"status": "report-only", "materialConsequence": {"claim": "material", "command": "inspect", "result": "false"}})
            malformed.write_text(json.dumps(body), encoding="utf-8")
            rejected = self.dispose("disposition-document", wid, "preflight", "addressed", str(malformed))
            self.assertEqual(rejected.returncode, 2, f"a malformed document was accepted ({reason})")
            self.assertIn(reason, rejected.stderr, "INVALID_STATUS_DIAGNOSTIC_CHANGED" if reason == "invalid or duplicate disposition" else reason)
            self.assertNotIn(
                "dispositionEvidence", json.loads(self.cli("status").stdout)["advisorPreflight"],
                f"a document rejected for {reason} was still written",
            )
        self.assertEqual(json.loads(self.cli("status").stdout)["advisorPreflight"]["findings"], "none")

        with_document = self.dispose(
            "disposition-document", wid, "preflight", "none", self.disposition_document())
        self.assertEqual(with_document.returncode, 2, with_document.stdout + with_document.stderr)
        self.assertIn("findings none carries no document", with_document.stderr)

        recorded = self.dispose(
            "disposition-document", wid, "preflight", "addressed",
            self.disposition_document("accepted-follow-up"))
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertEqual(json.loads(recorded.stdout)["advisorPreflight"]["findings"], "addressed")
        disposition_id = json.loads(self.cli("status").stdout)["advisorPreflight"]["dispositionEvidence"]
        document = self.evidence(disposition_id)
        self.assertEqual(document["slug"], "disposition-document")
        self.assertEqual(document["workflowId"], wid)
        self.assertEqual(document["stage"], "preflight")
        self.assertEqual([finding["id"] for finding in document["findings"]], ["ADV-1"])

    def test_a_disposition_document_answers_only_for_its_own_stage_and_instance(self) -> None:
        wid = self.begin_slug("disposition-lifetime")
        self.advance_to_context_forge()
        self.run_cli((
            "advisor-result", "--slug", "disposition-lifetime", "--workflow-id", wid,
            "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed",
        ))
        self.assertEqual(
            self.dispose("disposition-lifetime", wid, "preflight", "addressed", self.disposition_document("accepted-follow-up")).returncode,
            0,
        )
        preflight_id = json.loads(self.cli("status").stdout)["advisorPreflight"]["dispositionEvidence"]
        kept = self.evidence(preflight_id)

        stale_slug = self.dispose("some-other-pass", wid, "preflight", "addressed", self.disposition_document("accepted-follow-up"))
        self.assertEqual(stale_slug.returncode, 2, stale_slug.stdout + stale_slug.stderr)
        self.assertEqual(self.evidence(preflight_id), kept,
                         "a rejected re-record overwrote the document it had no right to touch")

        self.record_preflight(wid, self.preflight_document())
        self.owner_phase("tdd", "not-required")
        self.record_real_gate(wid)
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        self.assertEqual(self.verify_run(sys.executable, "-c", "pass").returncode, 0)
        self.owner_phase("code-review", "passed", findings="none")
        envelope = self.tmp / "report-only.json"
        envelope.write_text('{"schemaVersion":1,"findings":[{"id":"SPEC-1","claim":"harmless","material":true,"kind":"nonbehavioral"}],"verdict":"fix-before-commit"}', encoding="utf-8")
        result = self.cli("advisor-result", "--slug", "disposition-lifetime", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        intake_id = json.loads(result.stdout)["finalReview"]["intakeEvidence"]
        reported = self.dispose("disposition-lifetime", wid, "final", "addressed", str(self.finding_disposition_document(intake_id, "report-only", "nonbehavioral", "false")))
        state = json.loads(reported.stdout)
        self.assertEqual((reported.returncode, state["finalReview"]["findings"]), (0, "addressed"))
        self.assertEqual(self.evidence(preflight_id), kept, "the final disposition clobbered the preflight document")
        self.assertEqual((state["finalReview"]["dispositionEvidence"] == preflight_id, self.evidence(state["finalReview"]["dispositionEvidence"])["stage"]), (False, "final"))
        relabeled = self.dispose("disposition-lifetime", wid, "final", "addressed", str(self.finding_disposition_document(intake_id, "fixed", "nonbehavioral")))
        self.assertEqual(relabeled.returncode, 2, "ADVISOR_REPORT_ONLY_RELABELED" + relabeled.stdout + relabeled.stderr)
        self.run_cli(("complete",))

        # A same-slug begin starts a new instance without clearing artifacts, so a
        # findings-none pass must stop publishing the dead instance's dispositions.
        reused = self.begin_slug("disposition-lifetime")
        self.advance_to_context_forge()
        self.run_cli(
            ("advisor-result", "--slug", "disposition-lifetime", "--workflow-id", reused,
             "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "disposition-lifetime", "--workflow-id", reused,
             "--stage", "preflight", "--findings", "none"),
        )
        self.assertNotIn(
            "dispositionEvidence", json.loads(self.cli("status").stdout)["advisorPreflight"],
            "findings none published an earlier instance's disposition",
        )
        self.assertEqual(self.evidence(preflight_id), kept,
                         "begin deleted retained history instead of merely deactivating it")

    def test_design_wrapper_accepts_any_readable_design_narrative(self) -> None:
        marker = "DESIGN_SHAPE_GUIDANCE_MISSING"
        repo = self.tmp / "design-shape-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, env=self.env, check=True)
        wrapper = ROOT / "skills" / "codex-advisor" / "scripts" / "ask-codex-advisor.sh"

        def run(path: Path) -> subprocess.CompletedProcess[str]:
            return subprocess.run([
                str(wrapper), "--slug", "shape", "--phase", "preflight-advice",
                "--design-file", str(path), "--cwd", str(repo), "--", "q",
            ], cwd=ROOT, env=self.env, text=True, capture_output=True, check=False)

        empty = self.tmp / "empty-design.md"
        empty.write_text("   \n", encoding="utf-8")
        refused = run(empty)
        self.assertEqual(refused.returncode, 1, marker + refused.stdout + refused.stderr)
        self.assertIn("governed design is empty", refused.stderr, marker)

        narrative = self.tmp / "narrative-design.md"
        narrative.write_text("Decision only: deepen the existing module.\n", encoding="utf-8")
        accepted = run(narrative)
        self.assertEqual(accepted.returncode, 2, marker + accepted.stdout + accepted.stderr)
        self.assertIn("requires an active workflow", accepted.stderr, marker)
        declared = design_file_declaration(str(narrative))
        self.assertEqual(sorted(declared), ["schemaVersion", "sha256", "status"], marker)
    def test_identical_pending_preflight_design_replay_is_a_no_op(self) -> None:
        wid = self.begin_slug("design-replay")
        self.advance_to_context_forge()
        first = self.cli(
            "advisor-result", "--slug", "design-replay", "--workflow-id", wid,
            "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        before = json.loads(self.cli("status").stdout)
        before_events = json.loads(self.cli("history", "--workflow-id", wid).stdout)["events"]
        replay = self.cli(
            "advisor-result", "--slug", "design-replay", "--workflow-id", wid,
            "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        after = json.loads(self.cli("status").stdout)
        after_events = json.loads(self.cli("history", "--workflow-id", wid).stdout)["events"]
        self.assertEqual(
            (after["phase"], after["advisorPreflight"], after["updatedAt"], len(after_events)),
            (before["phase"], before["advisorPreflight"], before["updatedAt"], len(before_events)),
            "PENDING_DESIGN_REPLAY_MUTATED_STATE",
        )

    def test_advisor_results_bind_to_the_workflow_instance(self) -> None:
        begun = self.cli("begin", "--slug", "reused-slug")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        first = json.loads(begun.stdout)
        self.assertTrue(first.get("workflowId"), "begin did not assign a workflowId")
        self.advance_to_context_forge()

        bound = self.cli(
            "advisor-result", "--stage", "preflight", "--source", "codex-advisor",
            "--verdict", "completed", "--slug", "reused-slug", "--workflow-id", first["workflowId"],
        )
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)

        rebegun = self.cli("begin", "--slug", "reused-slug")
        self.assertEqual(rebegun.returncode, 0, rebegun.stdout + rebegun.stderr)
        second = json.loads(rebegun.stdout)
        self.assertNotEqual(second["workflowId"], first["workflowId"])
        self.advance_to_context_forge()

        delayed = self.cli(
            "advisor-result", "--stage", "preflight", "--source", "codex-advisor",
            "--verdict", "completed", "--slug", "reused-slug", "--workflow-id", first["workflowId"],
        )
        self.assertEqual(delayed.returncode, 2, "a delayed consult updated a later workflow with a reused slug")
        self.assertIn("workflow instance", delayed.stderr)

        unbound = self.cli(
            "advisor-result", "--stage", "preflight", "--source", "codex-advisor",
            "--verdict", "completed", "--slug", "reused-slug",
        )
        self.assertEqual(unbound.returncode, 2, unbound.stdout + unbound.stderr)
        self.assertEqual(
            json.loads(self.cli("status").stdout)["advisorPreflight"]["status"], "pending",
            "an unbound consult mutated the new workflow instance",
        )

    def test_completed_state_is_terminal_until_governance_revalidation(self) -> None:
        wid = self.complete_slug("terminal-state")
        terminal = self.checkpoint("final-review")
        self.assertFalse(terminal["ready"], "a completed workflow was reported consult-ready")
        self.assertIn("open-workflow", terminal["missing"])

        terminal_verify = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(terminal_verify.returncode, 2, terminal_verify.stdout + terminal_verify.stderr)
        self.assertIn("terminal", terminal_verify.stderr)

        for mutation in (
            ("advisor-result", "--slug", "terminal-state", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready"),
            ("advisor-disposition", "--slug", "terminal-state", "--workflow-id", wid, "--stage", "final", "--findings", "none"),
            ("pause", "--slug", "terminal-state", "--workflow-id", wid, "--reason", "waiting"),
        ):
            rejected = self.cli(*mutation)
            self.assertEqual(rejected.returncode, 2, mutation[0] + ": " + rejected.stdout + rejected.stderr)
            self.assertIn("terminal", rejected.stderr, mutation[0])

        from hooks.lib.workflow_state import invalidate_after_edit, ready_for_edit
        identity = resolve_repo_identity(self.repo)
        terminal = json.loads(self.cli("status").stdout)
        event_count = len(self.history_events())
        invalidate_after_edit(identity, "app.py")
        self.assertEqual(json.loads(self.cli("status").stdout), terminal,
                         "a reviewable edit resurrected a completed workflow")
        self.assertEqual(len(self.history_events()), event_count,
                         "a terminal no-op appended an event")
        blocked, _ = ready_for_edit(identity, "app.py")
        self.assertFalse(blocked, "a reviewable edit reopened production editing on a completed pass")

        from hooks.lib.workflow_state import TDD_CLOSED, WorkflowError, annotate_tdd_evidence
        prepared = {"workflowId": wid, "behaviorMap": self.preflight_document()["behaviorMap"], "runs": []}
        # Observe the real writer before transaction acquisition; substitute no collaborator.
        program = """import json, sys
from hooks.lib.repo_identity import resolve_repo_identity
from hooks.lib.workflow_state import annotate_tdd_evidence
def wait_at_mutation(frame, event, arg):
    if event == 'call' and frame.f_code.co_name == 'mutation':
        sys.settrace(None)
        print('before mutation', flush=True)
        sys.stdin.readline()
    return wait_at_mutation
sys.settrace(wait_at_mutation)
annotate_tdd_evidence(resolve_repo_identity(sys.argv[1]), 'terminal-state', sys.argv[2],
                      json.loads(sys.argv[3]), expected_evidence_id=json.loads(sys.argv[4]))
"""
        with subprocess.Popen(
            [sys.executable, "-c", program, str(self.repo), wid, json.dumps(prepared),
             json.dumps(terminal.get("tddEvidence"))], cwd=ROOT, env=self.env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as writer:
            self.assertEqual(writer.stdout.readline(), "before mutation\n")
            invalidate_after_edit(identity, "skills/diagnose/SKILL.md")
            state, history = json.loads(self.cli("status").stdout), self.history_events()
            _, error = writer.communicate("\n", timeout=30)
        failure = "GOVERNANCE_CHANGED_BEFORE_COMMIT_WAS_MISSED"
        self.assertNotEqual(writer.returncode, 0, failure)
        self.assertIn(TDD_CLOSED, error, failure)
        self.assertEqual(state["verification"], "pending")
        failure = "GOVERNANCE_ANNOTATION_MUTATED_DOCUMENT: STALE_PREPARATION_BYPASSED_GOVERNANCE"
        with self.assertRaisesRegex(WorkflowError, TDD_CLOSED, msg=failure):
            annotate_tdd_evidence(identity, "terminal-state", wid, prepared,
                                  expected_evidence_id=state.get("tddEvidence"))
        self.assertEqual(json.loads(self.cli("status").stdout), state, failure)
        self.assertEqual(self.history_events(), history, failure)
        update = self.tmp / "governance-map.json"
        update.write_text(json.dumps({"reassessment": "recheck frozen map", "dispositions": [
            {"id": "BM_NO_CHANGE", "revalidate": True, "evidence": "governance freeze"}]}))
        rejected = self.cli("tdd-map", "--slug", "terminal-state", "--workflow-id", wid,
                            "--input", str(update))
        failure = "GOVERNANCE_REVALIDATION_ACCEPTED_TDD_MAP_MUTATION"
        self.assertEqual(rejected.returncode, 2, failure)
        self.assertIn(TDD_CLOSED, rejected.stderr, failure)
        self.assertEqual(json.loads(self.cli("status").stdout), state, failure)
        self.assertEqual(self.history_events(), history, failure)

        for phase in ("repo-context-forge", "preflight", "implementation"):
            rejected = self.cli("set-phase", "--phase", phase, "--status", "passed")
            self.assertEqual(rejected.returncode, 2, f"{phase} mutation was accepted during revalidation")
        closed_preflight = self.record_preflight(wid, self.preflight_document())
        self.assertEqual(closed_preflight.returncode, 2, closed_preflight.stdout + closed_preflight.stderr)
        self.assertIn("revalidation", closed_preflight.stderr)

        marker = self.tmp / "revalidation-command-ran"
        raced_tdd = subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd",
             "--cwd", str(self.repo), "--slug", "terminal-state",
             "--phase", "red", "--behavior", "revalidation escape",
             "--seam", "workflow CLI", "--expected-failure", "AssertionError",
             "--", sys.executable, "-c",
             f"open({str(marker)!r}, 'w').close(); raise AssertionError('AssertionError: escape')"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(raced_tdd.returncode, 2, "TDD recording escaped the revalidation window")
        self.assertIn("revalidation", raced_tdd.stderr)
        self.assertFalse(marker.exists(), "workflow.py tdd launched the command for a closed revalidation window")
        preflight_consult = self.cli(
            "advisor-result", "--slug", "terminal-state", "--workflow-id", wid,
            "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed",
        )
        self.assertEqual(preflight_consult.returncode, 2, "a preflight consult was recorded during revalidation")
        preflight_disposition = self.dispose(
            "terminal-state", wid, "preflight", "addressed", self.disposition_document("accepted-follow-up"))
        self.assertEqual(preflight_disposition.returncode, 2, "a preflight disposition landed during revalidation")
        self.assertIn("revalidation", preflight_disposition.stderr)
        closed = self.checkpoint("preflight-advice")
        self.assertFalse(closed["ready"], "preflight advice was reported consult-ready during revalidation")
        self.assertIn("open-workflow", closed["missing"])

        reverified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(reverified.returncode, 0, reverified.stdout + reverified.stderr)

        from hooks.lib.workflow_state import ready_for_edit
        ready, missing = ready_for_edit(resolve_repo_identity(self.repo), "app.py")
        self.assertFalse(ready, "a production edit was admitted during governance revalidation")
        self.assertTrue(any("revalidation" in item or "new active workflow" in item for item in missing), missing)

        self.owner_phase("code-review", "passed", findings="none")
        self.assertTrue(
            self.checkpoint("final-review")["ready"],
            "revalidation closed the final review it exists to re-run",
        )
        final = self.cli("advisor-result", "--slug", "terminal-state", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready")
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
        disposed = self.cli("advisor-disposition", "--slug", "terminal-state", "--workflow-id", wid, "--stage", "final", "--findings", "none")
        self.assertEqual(disposed.returncode, 0, disposed.stdout + disposed.stderr)
        completed = self.cli("complete")
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertNotIn("revalidation", json.loads(completed.stdout))

        again = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(again.returncode, 2, "completion did not restore the terminal state")

    def test_optional_lead_identity_is_validated_against_the_active_instance(self) -> None:
        stale_wid = self.begin_slug("lead-identity")
        self.owner_phase("repo-context-forge", "passed")
        replacement = json.loads(self.cli("begin", "--slug", "lead-identity-replacement").stdout)
        self.owner_phase("repo-context-forge", "passed")

        for label, transition in (
            ("set-phase", ("set-phase", "--phase", "implementation", "--status", "passed",
                           "--slug", "lead-identity", "--workflow-id", stale_wid)),
            ("complete", ("complete", "--slug", "lead-identity", "--workflow-id", stale_wid)),
        ):
            stale = self.cli(*transition)
            self.assertEqual(stale.returncode, 2, f"{label}: {stale.stdout}{stale.stderr}")
            self.assertIn("does not match", stale.stderr, label)

        # The cases above stop at the slug check, so each command also gets the
        # replacement's slug with the stale id: that is the only input reaching
        # the instance comparison.
        for label, transition in (
            ("set-phase", ("set-phase", "--phase", "implementation", "--status", "passed",
                           "--slug", "lead-identity-replacement", "--workflow-id", stale_wid)),
            ("complete", ("complete", "--slug", "lead-identity-replacement", "--workflow-id", stale_wid)),
        ):
            stale_instance = self.cli(*transition)
            self.assertEqual(stale_instance.returncode, 2, f"{label}: {stale_instance.stdout}{stale_instance.stderr}")
            self.assertIn("--workflow-id does not match", stale_instance.stderr, label)

        state = json.loads(self.cli("status").stdout)
        self.assertEqual(state["workflowId"], replacement["workflowId"])
        self.assertEqual(state["implementation"], "pending",
                         "a stale lead command advanced the replacement workflow")

        # A matching identity, and an omitted one, both reach the transition itself:
        # they fail on this pass's readiness rather than on identity.
        for label, identity in (
            ("matching", ("--slug", "lead-identity-replacement",
                          "--workflow-id", str(replacement["workflowId"]))),
            ("omitted", ()),
        ):
            accepted = self.cli("complete", *identity)
            self.assertEqual(accepted.returncode, 2, f"{label}: {accepted.stdout}{accepted.stderr}")
            self.assertNotIn("does not match", accepted.stderr, label)
            self.assertIn("workflow incomplete", accepted.stderr, label)

    def test_production_code_records_once_and_survives_the_rest_of_the_pass(self) -> None:
        from hooks.lib.workflow_state import invalidate_after_edit, ready_for_edit

        wid = self.begin_slug("production-code-lifetime")
        self.advance_to_preflight("production-code-lifetime", wid)
        self.owner_phase("tdd", "not-required")
        identity = resolve_repo_identity(self.repo)

        bare = self.cli("set-phase", "--phase", "production-code", "--status", "passed")
        self.assertEqual(bare.returncode, 2, "production-code accepted a bare claim")
        self.assertIn("record-production-code", bare.stderr)


        self.record_real_gate(wid)
        admitted, missing = ready_for_edit(identity, "app.py")
        self.assertTrue(admitted, missing)

        invalidate_after_edit(identity, "app.py")
        self.assertEqual(json.loads(self.cli("status").stdout)["productionCode"], "passed",
                         "an ordinary production edit erased the production-code step")
        self.run_cli(("set-phase", "--phase", "implementation", "--status", "passed"))
        legacy_verified = self.verify_run(sys.executable, "-c", "pass")
        self.assertEqual(legacy_verified.returncode, 0, legacy_verified.stdout + legacy_verified.stderr)
        self.owner_phase("code-review", "passed", findings="none")
        self.run_cli(
            ("advisor-result", "--slug", "production-code-lifetime", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready"),
            ("advisor-disposition", "--slug", "production-code-lifetime", "--workflow-id", wid, "--stage", "final", "--findings", "none"),
            ("complete",),
        )
        invalidate_after_edit(identity, "skills/diagnose/SKILL.md")
        self.assertEqual(json.loads(self.cli("status").stdout)["productionCode"], "passed",
                         "governance revalidation erased the production-code step")

        rebegun = self.cli("begin", "--slug", "production-code-lifetime")
        self.assertEqual(rebegun.returncode, 0, rebegun.stdout + rebegun.stderr)
        self.assertEqual(json.loads(rebegun.stdout)["productionCode"], "pending",
                         "a replacement pass inherited the previous production-code step")

        self.assertIn("production-code=pending", self.cli("summary").stdout,
                      "a new pass did not read the phase as pending")

    def test_legacy_state_without_an_instance_id_rejects_every_producer(self) -> None:
        identity = resolve_repo_identity(self.repo)
        state_dir = Path(self.env["DEVIN_WORKFLOW_STATE_ROOT"]) / identity.key
        state_dir.mkdir(parents=True, mode=0o700)
        state_path = state_dir / "workflow.json"
        legacy = {
            "schemaVersion": 1,
            "repo": identity.as_dict(),
            "slug": "legacy-instance",
            "phase": "preflight",
            "nextAction": "tdd",
            "repoContextForge": "passed",
            "gitnexus": "passed",
            "advisorPreflight": {"source": "codex-advisor", "status": "completed", "findings": "none", "reason": None},
            "preflight": "passed",
            "tdd": "pending",
            "productionCode": "pending",
            "implementation": "pending",
            "verification": "pending",
            "codeReview": {"status": "pending", "findings": "pending"},
            "finalReview": {"source": None, "status": "pending", "findings": "pending"},
            "createdAt": "2026-01-01T00:00:00+00:00",
            "updatedAt": "2026-01-01T00:00:00+00:00",
        }
        state_path.write_text(json.dumps(legacy, sort_keys=True), encoding="utf-8")
        before = state_path.read_text(encoding="utf-8")

        status = self.cli("status")
        self.assertEqual(status.returncode, 2, status.stdout + status.stderr)
        self.assertIn("no workflowId", status.stderr)

        marker = self.tmp / "tdd-command-ran"
        red = subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(self.repo), "--slug", "legacy-instance",
             "--phase", "red", "--behavior", "legacy fence", "--seam", "workflow CLI",
             "--expected-failure", "AssertionError", "--", sys.executable, "-c",
             f"open({str(marker)!r}, 'w').close(); raise AssertionError('AssertionError: legacy')"],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(red.returncode, 2, red.stdout + red.stderr)
        self.assertFalse(marker.exists(), "the TDD command ran before legacy identity was accepted")

        review_input = self.tmp / "review.json"
        review_input.write_text(json.dumps({"findings": [], "dispositions": []}), encoding="utf-8")
        review = subprocess.run(
            [sys.executable, str(WORKFLOW), "record-review", "--repo", str(self.repo), "--slug", "legacy-instance",
             "--workflow-id", "", "--resolved-model", "test-model", "--review-context-id", "ctx-1",
             "--input", str(review_input)],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(review.returncode, 2, review.stdout + review.stderr)
        self.assertIn("no workflowId", review.stderr)

        preflight_input = self.tmp / "legacy-preflight.json"
        preflight_input.write_text(json.dumps(self.preflight_document()), encoding="utf-8")
        stale_preflight = subprocess.run(
            [sys.executable, str(WORKFLOW), "record-preflight", "--repo", str(self.repo),
             "--slug", "legacy-instance", "--workflow-id", "", "--input", str(preflight_input)],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(stale_preflight.returncode, 2, stale_preflight.stdout + stale_preflight.stderr)
        self.assertIn("no workflowId", stale_preflight.stderr)
        self.assertEqual(state_path.read_text(encoding="utf-8"), before, "a rejected import mutated legacy state")

        import sqlite3
        database = state_dir / "workflow.sqlite3"
        connection = sqlite3.connect(database)
        try:
            marker_row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'authority'"
            ).fetchone()
            events = connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0]
        finally:
            connection.close()
        self.assertIsNone(marker_row, "a failed import marked SQLite authoritative")
        self.assertEqual(events, 0, "a failed import left a partial event")

    def test_rearm_adapter_restores_only_recorded_pass_state(self) -> None:
        begun = self.cli("begin", "--slug", "compact recovery")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)

        # A bare claim with no producer evidence must not re-arm a compacted session
        # with graph readiness it never earned.
        self.owner_phase("repo-context-forge", "passed")
        self.assertIn("repo-context-forge=pending", self.cli("summary").stdout)

        record_context_forge(self.repo, self.tmp)
        rearmed = subprocess.run(
            [str(REARM)], cwd=ROOT, env=self.env, text=True,
            input=json.dumps({"cwd": str(self.repo), "source": "compact"}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(rearmed.returncode, 0, rearmed.stdout + rearmed.stderr)
        self.assertIn("Discipline re-arm", rearmed.stdout)
        self.assertIn("slug=compact-recovery", rearmed.stdout)
        self.assertIn("repo-context-forge=passed", rearmed.stdout)
        self.assertIn("advisor preflight", rearmed.stdout)
        self.assertIn("final review", rearmed.stdout)

    def test_completion_requires_a_ready_final_review_and_resolved_findings(self) -> None:
        wid = self.begin_slug("completion-contract")
        self.advance_to_verification("completion-contract", wid)
        self.owner_phase("code-review", "passed", findings="none")

        missing = self.cli("complete")
        self.assertEqual((missing.returncode, "finalReview" in missing.stderr), (2, True), missing.stdout + missing.stderr)

        unimplemented = self.cli(
            "advisor-result", "--slug", "completion-contract", "--workflow-id", wid,
            "--stage", "final", "--source", "codex-agent",
            "--verdict", "commit-ready", "--findings", "none",
        )
        self.assertEqual(unimplemented.returncode, 2, unimplemented.stdout + unimplemented.stderr)
        self.assertIn("unsupported reviewer source", unimplemented.stderr)

        before_events = len(self.history_events())
        mismatch = self.cli(
            "advisor-result", "--slug", "completion-contract", "--workflow-id", wid,
            "--stage", "final", "--source", "codex-advisor", "--verdict", "context-mismatch",
        )
        self.assertEqual(
            (mismatch.returncode, len(self.history_events())), (2, before_events),
            "CONTEXT_MISMATCH_LEGACY_ACCEPTED" + mismatch.stdout + mismatch.stderr,
        )

        rejected = self.cli(
            "advisor-result", "--slug", "completion-contract", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor",
            "--verdict", "fix-before-commit", "--findings", "pending",
        )
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        blocked = self.cli("complete")
        self.assertEqual((blocked.returncode, "finalReview" in blocked.stderr), (2, True), blocked.stdout + blocked.stderr)

        legacy_disposed = self.dispose("completion-contract", wid, "final", "addressed", self.disposition_document("report-only", "false"))
        self.assertEqual(legacy_disposed.returncode, 0, legacy_disposed.stdout + legacy_disposed.stderr)
        legacy_review = json.loads(self.cli("status").stdout)["finalReview"]
        self.assertEqual(("dispositionEvidence" in legacy_review, "intakeEvidence" in legacy_review), (True, False))
        legacy_blocked = self.cli("complete")
        self.assertEqual(
            (legacy_blocked.returncode, "finalReview" in legacy_blocked.stderr),
            (2, True),
            "legacy raw fix verdict completed without immutable intake: "
            + legacy_blocked.stdout + legacy_blocked.stderr,
        )

        envelope = self.tmp / "material-commit-ready.json"
        for raw, marker in (('{"schemaVersion":1,"findings":[{"id":"SPEC-1","claim":"must fix","material":true,"kind":"nonbehavioral"}],"verdict":"commit-ready"}', "MATERIAL_COMMIT_READY_ACCEPTED"), ('{"schemaVersion":1,"findings":[],"verdict":"fix-before-commit"}', "EMPTY_FIX_VERDICT_ACCEPTED")):
            envelope.write_text(raw, encoding="utf-8")
            before = json.loads(self.cli("status").stdout), len(self.history_events())
            refused = self.cli("advisor-result", "--slug", "completion-contract", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--input", str(envelope))
            self.assertEqual((refused.returncode, json.loads(self.cli("status").stdout), len(self.history_events())), (2, *before), marker + refused.stdout + refused.stderr)
        recovered = self.cli("advisor-result", "--slug", "completion-contract", "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready")
        self.assertEqual(recovered.returncode, 0, "REAL_LEGACY_FINAL_RECOVERY_REJECTED" + recovered.stdout + recovered.stderr)
        self.assertEqual(self.cli("complete").returncode, 0, "a final review with no finding did not complete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
