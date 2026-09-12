#!/usr/bin/env python3
"""Public CLI contracts for bounded TDD workflow summaries."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.tests.support import build_no_change_document, record_context_forge  # noqa: E402
from hooks.lib.repo_identity import resolve_repo_identity  # noqa: E402
from hooks.lib.tdd_surface import differences, identify  # noqa: E402
from hooks.lib.workflow_state import advisor_disposition, pause, read_workflow, record_advisor_result, set_phase  # noqa: E402

WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"
QUALITY_GATE = ROOT / "skills" / "production-code" / "scripts" / "code_quality_gate.py"
SEAM = "workflow.py tdd subprocess boundary"


class LegacyImportFreeFormTests(unittest.TestCase):
    """The settling proof for the legacy path: production reaches map-less
    state only through the file importer, so this test writes the real
    pre-migration slot files (workflow.json plus a map-less preflight
    evidence envelope), lets the CLI import them, and drives free-form
    RED/GREEN through it."""

    def test_mapless_import_admits_free_form_red_green(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="workflow-legacy-import-"))
        repo = tmp / "repo"; repo.mkdir()
        env = os.environ.copy()
        env.update({"DEVIN_WORKFLOW_STATE_ROOT": str(tmp / "state"),
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                    "PYTHONDONTWRITEBYTECODE": "1"})
        for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "Harness")):
            subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True, env=env)
        sys.path.insert(0, str(ROOT))
        from hooks.lib.repo_identity import resolve_repo_identity as _rri
        identity = _rri(repo)
        slot = tmp / "state" / identity.key
        slot.mkdir(parents=True)
        workflow_id = "ab" * 16
        evidence_path = slot / "preflight-legacy.json"
        document = build_no_change_document("legacy import")
        document.pop("behaviorMap", None)
        evidence_path.write_text(json.dumps({
            "schemaVersion": 1, "slug": "legacy", "workflowId": workflow_id,
            "document": document, "recordedAt": "2026-08-01T00:00:00+00:00",
        }), encoding="utf-8")
        (slot / "workflow.json").write_text(json.dumps({
            "schemaVersion": 1, "repo": identity.as_dict(), "slug": "legacy",
            "workflowId": workflow_id, "intent": "legacy import",
            "createdAt": "2026-08-01T00:00:00+00:00",
            "updatedAt": "2026-08-01T00:00:00+00:00",
            "phase": "preflight", "nextAction": "tdd",
            "repoContextForge": "passed", "preflight": "passed",
            "preflightEvidence": str(evidence_path),
            "advisorPreflight": {"source": "codex-advisor", "status": "completed", "findings": "none", "reason": None},
            "tdd": "pending", "productionCode": "pending", "implementation": "pending",
            "verification": "pending",
            "codeReview": {"status": "pending", "findings": "pending"},
            "finalReview": {"source": None, "status": "pending", "findings": "pending"},
        }), encoding="utf-8")
        red = subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(repo), "--slug", "legacy",
             "--phase", "red", "--behavior", "value must be 2", "--seam", "app import",
             "--expected-failure", "VALUE_NOT_TWO", "--", sys.executable, "-c",
             "import app; assert app.value == 2, 'VALUE_NOT_TWO'"],
            cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        self.assertTrue(json.loads(red.stdout.strip().splitlines()[-1])["valid"], red.stdout)
        (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(repo), "--slug", "legacy",
             "--phase", "green", "--behavior", "value must be 2", "--seam", "app import",
             "--", sys.executable, "-c",
             "import app; assert app.value == 2, 'VALUE_NOT_TWO'"],
            cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        # Characterization (advisor P2-1): on imported-legacy state after a
        # completed cycle, a re-RED whose command now PASSES still EXECUTES the
        # command (run-then-decide order) and preserves state and evidence.
        state_before = subprocess.run(
            [sys.executable, str(WORKFLOW), "status", "--repo", str(repo)],
            cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        evidence_before = json.loads(state_before.stdout)["tddEvidence"]
        proof = repo / "reran.txt"
        rerun = subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(repo), "--slug", "legacy",
             "--phase", "red", "--behavior", "value must be 2", "--seam", "app import",
             "--expected-failure", "VALUE_NOT_TWO", "--", sys.executable, "-c",
             # The proof path travels as argv, never interpolated into the
             # source: TMPDIR may contain quotes or backslashes.
             "import sys; open(sys.argv[1],'a').write('x'); "
             "import app; assert app.value == 2, 'VALUE_NOT_TWO'",
             str(proof)],
            cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        self.assertTrue(proof.exists(), "the rerun command must actually execute (run-then-decide)")
        self.assertEqual(rerun.returncode, 2, rerun.stdout + rerun.stderr)
        payload = json.loads(rerun.stdout.strip().splitlines()[-1])
        self.assertFalse(payload["valid"], payload)
        state_after = subprocess.run(
            [sys.executable, str(WORKFLOW), "status", "--repo", str(repo)],
            cwd=str(ROOT), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        after = json.loads(state_after.stdout)
        self.assertEqual(after["tddEvidence"], evidence_before, "a preserved invalid rerun must not move evidence")
        self.assertEqual(after["tdd"], "passed", "a preserved invalid rerun must not regress tdd state")
        shutil.rmtree(tmp, ignore_errors=True)


class TddSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-tdd-summary-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.previous_state_root = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.tmp / "state")
        self.env = os.environ.copy()
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
        begun = self.run_script(WORKFLOW, "begin", "--repo", str(self.repo), "--slug", "tdd-summary")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        identity = record_context_forge(self.repo, self.tmp)
        record_advisor_result(identity, "tdd-summary", read_workflow(identity)["workflowId"], "preflight", "codex-advisor", "completed")
        advisor_disposition(identity, "tdd-summary", read_workflow(identity)["workflowId"], "preflight", "none")
        self.record_preflight_evidence()

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

    def run_script(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), *args], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def tdd(
        self,
        phase: str,
        command: tuple[str, ...],
        *,
        behavior: str = "value is two",
        seam: str = SEAM,
        expected: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = [WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
                "--phase", phase, "--behavior", behavior, "--seam", seam]
        if expected:
            args += ["--expected-failure", expected]
        return self.run_script(*args, "--", *command)

    def unittest_target(self) -> str:
        """A real unittest target that logs every argv it is actually run with."""
        (self.repo / "test_app.py").write_text(
            "import pathlib, sys, unittest\n"
            "import app\n"
            "with pathlib.Path('runs.log').open('a', encoding='utf-8') as log:\n"
            "    log.write(' '.join(sys.argv) + '\\n')\n"
            "class TargetTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(app.value, 2, 'AssertionError: value must be 2')\n"
            "    def test_other(self):\n"
            "        self.assertEqual(app.value, app.value)\n",
            encoding="utf-8",
        )
        return "test_app.TargetTests.test_value"

    def runs_log(self) -> str:
        log = self.repo / "runs.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def refuses(self, *command: str, **candidate: str) -> str:
        """Assert a GREEN candidate refuses before the runner executes; return its report."""
        before = self.runs_log()
        result = self.tdd("green", command, **candidate)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.runs_log(), before, "a refused candidate executed its command")
        return result.stderr

    def evidence_record(self, evidence_id: str) -> dict[str, object]:
        result = self.run_script(
            WORKFLOW, "evidence", "--repo", str(self.repo), "--evidence-id", evidence_id,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["evidenceId"], evidence_id)
        return value

    def evidence_document(self, evidence_id: str) -> dict[str, object]:
        return self.evidence_record(evidence_id)["document"]

    def record_preflight_evidence(self) -> None:
        # This suite proves the LEGACY free-form candidate contract, which
        # production reaches only through imported pre-Behavior-Map state. The
        # recorder now always demands a map, so the fixture commits a map-less
        # document through the same library seam the legacy migration writes -
        # the one configuration whose workflows may still drive free-form
        # candidates. Setup only; the candidate semantics under test all run
        # through the real CLI.
        from hooks.lib import workflow_state as w
        identity = resolve_repo_identity(self.repo)
        state = read_workflow(identity)
        document = build_no_change_document("suite setup")
        document.pop("behaviorMap", None)
        w.commit_evidence_phase(
            identity, str(state["slug"]), str(state["workflowId"]), "preflight", document,
        )

    def record_gate_evidence(self) -> None:
        identity = resolve_repo_identity(self.repo)
        gate = self.run_script(QUALITY_GATE, "check", "--repo", str(self.repo), "--json")
        assert gate.returncode == 0, gate.stdout + gate.stderr
        gate_path = self.tmp / "setup-gate.json"
        gate_path.write_text(gate.stdout, encoding="utf-8")
        recorded = self.run_script(WORKFLOW, "record-production-code", "--repo", str(self.repo), "--slug", "tdd-summary",
                                   "--workflow-id", read_workflow(identity)["workflowId"], "--input", str(gate_path))
        assert recorded.returncode == 0, recorded.stdout + recorded.stderr

    def test_red_and_green_are_bound_to_one_real_seam_and_candidate(self) -> None:
        behavior_command = (
            sys.executable, "-c",
            "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        red = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *behavior_command,
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        self.git("add", "app.py")
        result = json.loads(green.stdout.splitlines()[-1])
        summary = self.evidence_document(result["summaryId"])
        valid_runs = [entry for entry in summary["runs"] if entry["valid"]]
        self.assertEqual([entry["phase"] for entry in valid_runs], ["red", "green"])
        self.assertEqual(summary["status"], "passed")
        self.assertEqual(summary["seam"], "workflow.py tdd subprocess boundary")
        for removed in ("head", "startingHead", "candidateChangeFingerprint", "commandSha256"):
            self.assertNotIn(removed, summary)

        downgraded = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--not-required", "changed our mind after recording evidence",
        )
        self.assertEqual(downgraded.returncode, 2, downgraded.stdout + downgraded.stderr)
        self.assertIn("cannot replace valid TDD evidence", downgraded.stderr)

    def test_green_may_drop_fail_fast_and_verbosity_for_the_same_test_surface(self) -> None:
        target = self.unittest_target()
        red_command = (sys.executable, "-m", "unittest", "--failfast", "-v", target)
        green_command = (sys.executable, "-m", "unittest", target)
        red = self.tdd("red", red_command, expected="AssertionError")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.tdd("green", green_command)
        self.assertEqual(
            green.returncode, 0,
            "GREEN dropping fail-fast and verbosity was refused: " + green.stderr,
        )
        summary = self.evidence_document(json.loads(green.stdout.splitlines()[-1])["summaryId"])
        self.assertEqual((summary["status"], summary["schemaVersion"]), ("passed", 1))
        self.assertEqual(
            [entry["command"] for entry in summary["runs"]],
            [shlex.join(red_command), shlex.join(green_command)],
            "the evidence lost one of the two raw commands",
        )
        self.assertEqual(summary["surface"], {
            "surfaceSchemaVersion": 1,
            "runner": "unittest",
            "invocation": shlex.join((sys.executable, "-m", "unittest")),
            "arguments": [target],
            "ignored": [],
            "fallbackReason": None,
        })

    def test_repeated_unittest_verbosity_is_still_the_same_test_surface(self) -> None:
        target = self.unittest_target()
        red = self.tdd("red", (sys.executable, "-m", "unittest", "-vv", target),
                       expected="AssertionError")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.tdd("green", (sys.executable, "-m", "unittest", target))
        self.assertEqual(green.returncode, 0,
                         "GREEN dropping repeated unittest verbosity was refused: " + green.stderr)
        self.assertEqual(
            self.evidence_document(json.loads(green.stdout.splitlines()[-1])["summaryId"])["status"],
            "passed",
        )
        quieted = self.tdd("green", (sys.executable, "-m", "unittest", "-qq", target))
        self.assertEqual(quieted.returncode, 0,
                         "a GREEN adding repeated unittest quiet was refused: " + quieted.stderr)
        self.assertIn("surface.arguments", self.refuses(
            sys.executable, "-m", "unittest", "-vf", target),
            "a mixed short cluster was dropped as pure verbosity")

    def test_a_different_test_surface_refuses_and_names_both_normalized_values(self) -> None:
        target = self.unittest_target()
        red = self.tdd("red", (sys.executable, "-m", "unittest", "--failfast", target),
                       expected="AssertionError")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")

        other = self.refuses(sys.executable, "-m", "unittest", "test_app.TargetTests.test_other")
        self.assertIn("surface.arguments", other)
        self.assertIn("test_app.TargetTests.test_other", other)
        self.assertIn(target, other)
        self.assertIn("surface.arguments", self.refuses(
            sys.executable, "-m", "unittest", "-k", "value", target))
        self.assertIn("surface.runner", self.refuses("pytest", "test_app.py::TargetTests::test_value"))
        self.assertIn("behavior", self.refuses(
            sys.executable, "-m", "unittest", target, behavior="a different behavior"))
        self.assertIn("seam", self.refuses(
            sys.executable, "-m", "unittest", target, seam="a different interface"))

        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual(state["tdd"], "in-progress", "a refused candidate moved the TDD gate")

    def test_a_mapless_cycle_never_consults_the_map(self) -> None:
        marker = "MAPLESS_TDD_CRASHES"
        target = self.unittest_target()
        red = self.tdd("red", (sys.executable, "-m", "unittest", target), expected="AssertionError")
        self.assertEqual(red.returncode, 0, marker + ": " + red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.tdd("green", (sys.executable, "-m", "unittest", target))
        self.assertEqual(green.returncode, 0, marker + ": " + green.stdout + green.stderr)

    def test_an_opaque_shell_command_keeps_exact_command_identity(self) -> None:
        target = self.unittest_target()
        script = f"{shlex.quote(sys.executable)} -m unittest --failfast {target}"
        red = self.tdd("red", ("bash", "-lc", script), expected="AssertionError")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        surface = self.evidence_document(
            json.loads(red.stdout.splitlines()[-1])["summaryId"])["surface"]
        self.assertEqual(surface["runner"], "exact")
        self.assertEqual(surface["arguments"], ["bash", "-lc", script])
        self.assertTrue(surface["fallbackReason"], "an exact-bound surface gave no reason")

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        report = self.refuses("bash", "-lc", script.replace(" --failfast", ""))
        self.assertIn("surface.arguments", report)

    def test_pytest_options_are_classified_by_that_runners_own_grammar(self) -> None:
        """Classification coverage for pytest, whose grammar differs from unittest's.

        The repository ships no pytest, so this crosses the surface Module's own
        public Interface rather than manufacturing a stand-in runner; the CLI-Seam
        RED/GREEN above uses the real stdlib runner.
        """
        target = "tests/test_thing.py::TestThing::test_value"
        selected = identify(("pytest", target))
        for equivalent in (("pytest", "-x", "-vv", target),
                           ("pytest", "--exitfirst", "--quiet", target),
                           ("pytest", "--maxfail=1", target)):
            self.assertEqual(differences(identify(equivalent), selected), [],
                             f"{equivalent} was not recognised as the same test surface")
        for retained in (("pytest", "--maxfail=2", target),
                         ("pytest", "--maxfail", "1", target),
                         ("pytest", "-xq", target),
                         ("pytest", "-k", "value", target),
                         ("pytest", "--rootdir", "other", target),
                         (sys.executable, "-m", "pytest", target)):
            self.assertTrue(differences(identify(retained), selected),
                            f"{retained} silently compared equal to a different surface")
        self.assertEqual(identify(("pytest", "--", "-x"))["arguments"], ["--", "-x"],
                         "an option spelling after a bare -- was dropped as an option")

    def test_in_flight_evidence_without_a_surface_stays_bound_to_its_exact_command(self) -> None:
        # Contract pin (not Seam proof): this change makes the CLI always record a
        # surface, so only the Module boundary can still produce the pre-surface
        # shape. The cross-version proof drives the pre-change CLI at the real Seam.
        from hooks.lib.workflow_state import commit_tdd
        target = self.unittest_target()
        red_command = (sys.executable, "-m", "unittest", "--failfast", target)
        red = self.tdd("red", red_command, expected="AssertionError")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        identity = resolve_repo_identity(self.repo)
        recorded = self.evidence_document(json.loads(red.stdout.splitlines()[-1])["summaryId"])
        commit_tdd(
            identity, "tdd-summary", read_workflow(identity)["workflowId"],
            {key: value for key, value in recorded.items() if key != "surface"},
            "in-progress", expected_evidence_id=read_workflow(identity)["tddEvidence"],
        )

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        report = self.refuses(sys.executable, "-m", "unittest", target)
        self.assertIn("rerun RED under the new contract", report)
        exact = self.tdd("green", red_command)
        self.assertEqual(exact.returncode, 0, exact.stdout + exact.stderr)

    def test_invalid_or_mismatched_runs_do_not_regress_recorded_tdd_state(self) -> None:
        behavior_command = (
            sys.executable, "-c",
            "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        red = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *behavior_command,
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        summary_id = json.loads(green.stdout.splitlines()[-1])["summaryId"]

        green_marker = self.tmp / "mismatched-green-ran"
        mismatched = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "a different behavior",
            "--seam", "workflow.py tdd subprocess boundary", "--", sys.executable, "-c",
            f"open({str(green_marker)!r}, 'w').close()",
        )
        self.assertEqual(mismatched.returncode, 2, mismatched.stdout + mismatched.stderr)
        self.assertFalse(green_marker.exists(), "a mismatched GREEN executed its command")

        state = self.run_script(WORKFLOW, "status", "--repo", str(self.repo))
        self.assertEqual(state.returncode, 0, state.stdout + state.stderr)
        self.assertEqual(
            json.loads(state.stdout)["tdd"], "passed",
            "a mismatched candidate regressed the recorded TDD gate",
        )
        self.assertEqual(
            self.evidence_document(summary_id)["status"], "passed",
        )

    def test_rerun_semantics_preserve_passed_state_and_record_regressions(self) -> None:
        behavior_command = (
            sys.executable, "-c",
            "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        red = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *behavior_command,
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        summary_id = json.loads(green.stdout.splitlines()[-1])["summaryId"]

        red_after_green = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *behavior_command,
        )
        self.assertEqual(red_after_green.returncode, 2, red_after_green.stdout + red_after_green.stderr)
        self.assertEqual(
            self.evidence_document(summary_id)["status"], "passed",
            "a matching RED that now passes overwrote completed GREEN evidence",
        )
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual(state["tdd"], "passed")

        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        regressed = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(regressed.returncode, 2, regressed.stdout + regressed.stderr)
        regression_id = json.loads(regressed.stdout.splitlines()[-1])["summaryId"]
        self.assertNotEqual(regression_id, summary_id)
        summary = self.evidence_document(regression_id)
        self.assertEqual(summary["status"], "pending")
        self.assertFalse(summary["runs"][-1]["valid"], "the regression run was not recorded")
        self.assertEqual(self.evidence_document(summary_id)["status"], "passed",
                         "a regression mutated immutable completed evidence")
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual(state["tdd"], "in-progress")
        self.assertEqual(state["implementation"], "in-progress", "a recorded GREEN regression did not reopen implementation")
        self.assertEqual(state["verification"], "pending")
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"})

    def test_summaries_are_owned_by_one_workflow_instance(self) -> None:
        behavior_command = (
            sys.executable, "-c",
            "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        red = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *behavior_command,
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        summary_id = json.loads(red.stdout.splitlines()[-1])["summaryId"]
        first_summary = self.evidence_document(summary_id)
        identity = resolve_repo_identity(self.repo)
        self.assertEqual(
            first_summary.get("workflowId"), read_workflow(identity)["workflowId"],
            "the TDD summary does not record its owning workflow instance",
        )

        rebegun = self.run_script(WORKFLOW, "begin", "--repo", str(self.repo), "--slug", "tdd-summary")
        self.assertEqual(rebegun.returncode, 0, rebegun.stdout + rebegun.stderr)
        record_context_forge(self.repo, self.tmp)
        record_advisor_result(identity, "tdd-summary", read_workflow(identity)["workflowId"], "preflight", "codex-advisor", "completed")
        advisor_disposition(identity, "tdd-summary", read_workflow(identity)["workflowId"], "preflight", "none")
        self.record_preflight_evidence()

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        stale_green = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(
            stale_green.returncode, 2,
            "a GREEN under a new workflow instance consumed the previous instance's RED evidence",
        )
        self.assertEqual(
            self.evidence_document(summary_id), first_summary,
            "a new workflow instance mutated the previous instance's summary",
        )

    def test_producer_finishing_after_same_slug_replacement_is_rejected(self) -> None:
        replace_and_fail = (
            sys.executable, "-c",
            "import subprocess, sys; "
            f"subprocess.run([sys.executable, {str(WORKFLOW)!r}, 'begin', '--repo', {str(self.repo)!r}, '--slug', 'tdd-summary'], check=True, capture_output=True); "
            "raise AssertionError('AssertionError: value must be 2')",
        )
        raced = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *replace_and_fail,
        )
        self.assertEqual(raced.returncode, 2, raced.stdout + raced.stderr)
        self.assertIn(
            "workflow instance", raced.stderr,
            "a producer finishing after a same-slug replacement advanced the new workflow",
        )
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual(state["tdd"], "pending", "the replacement workflow inherited the raced producer's state")

    def test_next_tracer_red_reopens_the_cycle_and_midcycle_switches_reject(self) -> None:
        check_two = (sys.executable, "-c", "import app; assert app.value == 2, 'AssertionError: value must be 2'")
        first = self.tdd("red", check_two, behavior="first behavior", expected="AssertionError")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.tdd("green", check_two, behavior="first behavior")
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)

        identity = resolve_repo_identity(self.repo)
        wid = read_workflow(identity)["workflowId"]
        self.record_gate_evidence()
        set_phase(identity, "implementation", "passed")
        set_phase(identity, "verification", "passed")
        pause(identity, "tdd-summary", wid, "waiting for the next tracer")
        self.assertIn("paused", read_workflow(identity))

        check_three = (sys.executable, "-c", "import app; assert app.value == 3, 'AssertionError: value must be 3'")
        second = self.tdd("red", check_three, behavior="second behavior", expected="AssertionError")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual(state["tdd"], "in-progress")
        self.assertEqual(state["implementation"], "in-progress", "a next-tracer RED did not reopen implementation")
        self.assertEqual(state["verification"], "pending")
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"})
        self.assertNotIn("paused", state, "a next-tracer RED did not clear the pause")

        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        second_green = self.tdd("green", check_three, behavior="second behavior")
        self.assertEqual(second_green.returncode, 0, second_green.stdout + second_green.stderr)
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual(state["tdd"], "passed")
        self.assertEqual(state["verification"], "pending", "stale verification survived the next GREEN")
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"})

        third = self.tdd("red", (sys.executable, "-c", "raise AssertionError('AssertionError: four')"),
                         behavior="third behavior", expected="AssertionError")
        self.assertEqual(third.returncode, 0, third.stdout + third.stderr)
        summary_id = json.loads(third.stdout.splitlines()[-1])["summaryId"]
        before = self.evidence_document(summary_id)
        marker = self.tmp / "midcycle-command-ran"
        switch = self.tdd(
            "red",
            (sys.executable, "-c",
             f"open({str(marker)!r}, 'w').close(); raise AssertionError('AssertionError: five')"),
            behavior="fourth behavior mid-cycle", expected="AssertionError",
        )
        self.assertEqual(switch.returncode, 2, "a mid-cycle candidate switch was accepted")
        self.assertIn("candidate does not match the active cycle", switch.stderr)
        self.assertFalse(marker.exists(), "the rejected switch executed its command")
        self.assertEqual(self.evidence_document(summary_id), before, "a rejected switch mutated the active candidate")

    def test_producer_commits_enforce_ordering_and_evidence_cas(self) -> None:
        rebegun = self.run_script(WORKFLOW, "begin", "--repo", str(self.repo), "--slug", "tdd-summary")
        self.assertEqual(rebegun.returncode, 0, rebegun.stdout + rebegun.stderr)
        premature = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "too early",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", sys.executable, "-c", "raise AssertionError('AssertionError: early')",
        )
        self.assertEqual(premature.returncode, 2, "a RED at intake bypassed the ordering gate")
        self.assertIn("requires", premature.stderr)
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual((state["tdd"], state["implementation"]), ("pending", "pending"))

        identity = record_context_forge(self.repo, self.tmp)
        record_advisor_result(identity, "tdd-summary", read_workflow(identity)["workflowId"], "preflight", "codex-advisor", "completed")
        advisor_disposition(identity, "tdd-summary", read_workflow(identity)["workflowId"], "preflight", "none")
        self.record_preflight_evidence()

        red = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "CAS candidate",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", sys.executable, "-c", "raise AssertionError('AssertionError: red')",
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        current_id = json.loads(red.stdout.splitlines()[-1])["summaryId"]
        current_document = self.evidence_document(current_id)

        # Contract pin (not Seam proof): the read-to-transaction window is not
        # deterministic through the CLI, so the logical evidence CAS is pinned
        # at the Module boundary while every behavior test uses the real CLI.
        from hooks.lib.workflow_state import WorkflowError, commit_tdd
        with self.assertRaises(WorkflowError) as raced:
            commit_tdd(
                identity, "tdd-summary", read_workflow(identity)["workflowId"],
                {"schemaVersion": 1, "stale": True}, "in-progress",
                expected_evidence_id=None,
            )
        self.assertIn("evidence changed during the run", str(raced.exception))
        self.assertEqual(read_workflow(identity)["tddEvidence"], current_id)
        self.assertEqual(
            self.evidence_document(current_id), current_document,
            "the stale producer mutated the interleaved logical evidence",
        )

    def test_terminal_workflow_rejects_reruns_before_touching_evidence(self) -> None:
        behavior_command = (
            sys.executable, "-c",
            "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        red = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *behavior_command,
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        record_context_forge(self.repo, self.tmp)
        summary_id = json.loads(green.stdout.splitlines()[-1])["summaryId"]
        before = self.evidence_document(summary_id)

        identity = resolve_repo_identity(self.repo)
        wid = read_workflow(identity)["workflowId"]
        doc = build_no_change_document("terminal workflow rerun")
        doc_path = self.tmp / "preflight-doc.json"
        doc_path.write_text(json.dumps(doc), encoding="utf-8")
        recorded = self.run_script(WORKFLOW, "record-preflight", "--repo", str(self.repo), "--slug", "tdd-summary",
                                   "--workflow-id", wid, "--input", str(doc_path))
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        gate = self.run_script(QUALITY_GATE, "check", "--repo", str(self.repo), "--json")
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        gate_path = self.tmp / "gate-verdict.json"
        gate_path.write_text(gate.stdout, encoding="utf-8")
        recorded = self.run_script(WORKFLOW, "record-production-code", "--repo", str(self.repo), "--slug", "tdd-summary",
                                   "--workflow-id", wid, "--input", str(gate_path))
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        set_phase(identity, "implementation", "passed")
        verified = self.run_script(WORKFLOW, "verify", "--repo", str(self.repo), "--slug", "tdd-summary",
                                   "--", sys.executable, "-c", "pass")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        quality = self.run_script(WORKFLOW, "verify", "--repo", str(self.repo), "--slug", "tdd-summary",
                                  "--kind", "quality-gate", "--base-ref", "HEAD")
        self.assertEqual(quality.returncode, 0, quality.stdout + quality.stderr)
        set_phase(identity, "code-review", "passed", findings="none")
        record_advisor_result(identity, "tdd-summary", wid, "final", "codex-advisor", "commit-ready")
        advisor_disposition(identity, "tdd-summary", wid, "final", "none")
        completed = self.run_script(WORKFLOW, "complete", "--repo", str(self.repo))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        rerun = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(rerun.returncode, 2, rerun.stdout + rerun.stderr)
        self.assertIn("terminal", rerun.stderr)
        self.assertEqual(
            self.evidence_document(summary_id), before,
            "a rerun against a terminal workflow mutated its evidence summary",
        )
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual((state["phase"], state["tdd"]), ("complete", "passed"))

    def test_only_the_declared_failure_and_seam_count(self) -> None:
        silent = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", sys.executable, "-c", "raise SystemExit(1)",
        )
        self.assertEqual(silent.returncode, 2, silent.stdout + silent.stderr)

        timed_out = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--timeout", "1", "--", sys.executable, "-c", "import time; print('AssertionError'); time.sleep(30)",
        )
        self.assertEqual(timed_out.returncode, 2, timed_out.stdout + timed_out.stderr)

        genuine = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", sys.executable, "-c", "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        self.assertEqual(genuine.returncode, 0, genuine.stdout + genuine.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        wrong_command = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "workflow.py tdd subprocess boundary", "--", sys.executable, "-c", "import app; assert app.value == 2",
        )
        self.assertEqual(wrong_command.returncode, 2, wrong_command.stdout + wrong_command.stderr)
        wrong_seam = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "captures command outcome",
            "--seam", "different interface", "--", sys.executable, "-c", "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        self.assertEqual(wrong_seam.returncode, 2, wrong_seam.stdout + wrong_seam.stderr)

    def test_a_valid_red_replaces_a_not_required_decision_and_reopens_the_cycle(self) -> None:
        decision = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--not-required", "no production behavior changed",
        )
        self.assertEqual(decision.returncode, 0, decision.stdout + decision.stderr)
        decision_id = json.loads(decision.stdout)["summaryId"]
        decision_document = self.evidence_document(decision_id)

        identity = resolve_repo_identity(self.repo)
        self.record_gate_evidence()
        set_phase(identity, "implementation", "passed")
        set_phase(identity, "verification", "passed")
        pause(identity, "tdd-summary", read_workflow(identity)["workflowId"], "waiting on the scope decision")

        behavior_command = (
            sys.executable, "-c",
            "import app; assert app.value == 2, 'AssertionError: value must be 2'",
        )
        red = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "red", "--behavior", "scope changed after the not-required decision",
            "--seam", "workflow.py tdd subprocess boundary", "--expected-failure", "AssertionError",
            "--", *behavior_command,
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        red_id = json.loads(red.stdout.splitlines()[-1])["summaryId"]
        summary = self.evidence_document(red_id)
        self.assertEqual(summary["status"], "pending")
        self.assertNotIn("reason", summary, "the not-required decision survived the replacing RED")
        self.assertEqual(self.evidence_document(decision_id), decision_document,
                         "replacing a decision mutated immutable historical evidence")
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual((state["tdd"], state["implementation"]), ("in-progress", "in-progress"))
        self.assertEqual(state["verification"], "pending")
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"})
        self.assertNotIn("paused", state, "the replacing RED did not clear the pause")

        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--phase", "green", "--behavior", "scope changed after the not-required decision",
            "--seam", "workflow.py tdd subprocess boundary", "--", *behavior_command,
        )
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        state = json.loads(self.run_script(WORKFLOW, "status", "--repo", str(self.repo)).stdout)
        self.assertEqual(state["tdd"], "passed")
        self.assertEqual(state["verification"], "pending", "stale verification survived the replaced cycle")
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"})
        self.assertEqual(state["finalReview"], {"source": None, "status": "pending", "findings": "pending"})

    def test_not_required_decision_is_recorded_in_pass_state(self) -> None:
        (self.repo / "notes.md").write_text("documentation only\n", encoding="utf-8")
        decision = self.run_script(
            WORKFLOW, "tdd", "--cwd", str(self.repo), "--slug", "tdd-summary",
            "--not-required", "no production behavior changed",
        )
        self.assertEqual(decision.returncode, 0, decision.stdout + decision.stderr)
        result = json.loads(decision.stdout)
        summary = self.evidence_document(result["summaryId"])
        self.assertEqual(summary["reason"], "no production behavior changed")
        status = self.run_script(WORKFLOW, "status", "--repo", str(self.repo))
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout)["tdd"], "not-required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
