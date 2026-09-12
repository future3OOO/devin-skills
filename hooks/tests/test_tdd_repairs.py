#!/usr/bin/env python3
"""Real-Seam regression contracts for mapped TDD."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib import behavior_map  # noqa: E402
from hooks.lib.command_runner import run as runner_run  # noqa: E402
from hooks.lib.repo_identity import resolve_repo_identity  # noqa: E402
from hooks.lib.tdd_workflow import completion_blockers, current_map  # noqa: E402
from hooks.lib.workflow_state import (  # noqa: E402
    advisor_disposition,
    evidence_document,
    read_workflow,
    record_advisor_result,
)
from hooks.tests.support import (  # noqa: E402
    build_document,
    pending_behavior,
    record_context_forge,
)

WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"
PYTEST_AVAILABLE = shutil.which("pytest") is not None


class MappedTddRepairTests(unittest.TestCase):
    """Harness plus public workflow behavior checks reused by sibling suites."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mapped-tdd-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.previous_state_root = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")
        self.env = os.environ.copy()
        self.env.update(
            {
                "DEVIN_WORKFLOW_STATE_ROOT": str(self.tmp / "state"),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            }
        )
        for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
            self.env.pop(name, None)
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = self.env[
            "DEVIN_WORKFLOW_STATE_ROOT"
        ]
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Workflow Harness")
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        self.git("add", "app.py")
        self.git("commit", "-q", "-m", "base")

    def tearDown(self) -> None:
        if self.previous_state_root is None:
            os.environ.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
        else:
            os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = self.previous_state_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args: str) -> None:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKFLOW), *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def begin_with_map(
        self, items: list[dict[str, object]], slug: str = "mapped-repair"
    ) -> tuple[str, str]:
        begun = self.cli(
            "begin",
            "--repo",
            str(self.repo),
            "--slug",
            slug,
            "--intent",
            "exercise mapped TDD behavior",
        )
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        workflow_id = str(json.loads(begun.stdout)["workflowId"])
        identity = record_context_forge(self.repo, self.tmp)
        record_advisor_result(
            identity, slug, workflow_id, "preflight", "codex-advisor", "completed"
        )
        advisor_disposition(identity, slug, workflow_id, "preflight", "none")
        preflight = self.tmp / f"{slug}-preflight.json"
        preflight.write_text(
            json.dumps(build_document("mapped TDD", behavior_map=items)),
            encoding="utf-8",
        )
        recorded = self.cli(
            "record-preflight",
            "--repo",
            str(self.repo),
            "--slug",
            slug,
            "--workflow-id",
            workflow_id,
            "--input",
            str(preflight),
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        return slug, workflow_id

    def tdd(
        self,
        slug: str,
        phase: str,
        behavior_id: str,
        command: tuple[str, ...],
    ) -> subprocess.CompletedProcess[str]:
        return self.cli(
            "tdd",
            "--repo",
            str(self.repo),
            "--slug",
            slug,
            "--phase",
            phase,
            "--behavior-id",
            behavior_id,
            "--",
            *command,
        )

    def update_map(
        self, slug: str, workflow_id: str, document: object
    ) -> subprocess.CompletedProcess[str]:
        path = self.tmp / "map-update.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return self.cli(
            "tdd-map",
            "--repo",
            str(self.repo),
            "--slug",
            slug,
            "--workflow-id",
            workflow_id,
            "--input",
            str(path),
        )

    def evidence(self) -> dict[str, object]:
        identity = resolve_repo_identity(self.repo)
        state = read_workflow(identity)
        evidence_id = state.get("tddEvidence")
        self.assertIsInstance(evidence_id, str)
        document = evidence_document(identity, str(evidence_id))
        self.assertIsInstance(document, dict)
        return document

    def write_unittest(self, expected: int, marker: str) -> tuple[str, ...]:
        (self.repo / "test_app.py").write_text(
            "import unittest\n"
            "import app\n"
            "class ValueTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            f"        self.assertEqual(app.value, {expected}, {marker!r})\n",
            encoding="utf-8",
        )
        return (
            sys.executable,
            "-m",
            "unittest",
            "test_app.ValueTests.test_value",
        )

    def test_reassessment_added_item_runs_a_fresh_cycle(self) -> None:
        first = pending_behavior("BM_A", red_failure="VALUE_NOT_TWO")
        slug, workflow_id = self.begin_with_map([first], "continuation")
        command = self.write_unittest(2, "VALUE_NOT_TWO")
        self.assertEqual(self.tdd(slug, "red", "BM_A", command).returncode, 0)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", command).returncode, 0)

        second = pending_behavior(
            "BM_B",
            behavior="value becomes three",
            expected="value is three",
            red_failure="VALUE_NOT_THREE",
            basis="post-GREEN reassessment",
        )
        assessed = self.update_map(
            slug,
            workflow_id,
            {
                "sourceBehaviorId": "BM_A",
                "reassessment": "The first GREEN exposes the next behavior.",
                "items": [second],
            },
        )
        self.assertEqual(assessed.returncode, 0, assessed.stdout + assessed.stderr)
        command = self.write_unittest(3, "VALUE_NOT_THREE")
        self.assertEqual(self.tdd(slug, "red", "BM_B", command).returncode, 0)
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_B", command).returncode, 0)
        finished = self.update_map(
            slug,
            workflow_id,
            {
                "sourceBehaviorId": "BM_B",
                "reassessment": "No further behavior surfaced.",
                "items": [],
            },
        )
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        identity = resolve_repo_identity(self.repo)
        self.assertEqual(completion_blockers(identity, read_workflow(identity)), [])

    def test_unittest_loader_failure_is_not_red(self) -> None:
        marker = "UNREACHED_ASSERTION"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_BAD", red_failure=marker)], "unittest-loader"
        )
        (self.repo / "test_bad.py").write_text(
            f"raise AssertionError({marker!r})\n", encoding="utf-8"
        )
        result = self.tdd(
            slug,
            "red",
            "BM_BAD",
            (sys.executable, "-m", "unittest", "test_bad"),
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertNotIn("tddCycleCount", read_workflow(resolve_repo_identity(self.repo)))

    def test_unittest_assertion_records_reached_proof(self) -> None:
        marker = "UNITTEST_PRODUCT_ASSERTION"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_UNIT", red_failure=marker)], "unittest-red"
        )
        result = self.tdd(slug, "red", "BM_UNIT", self.write_unittest(2, marker))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proof = self.evidence()["runs"][-1]["redProof"]
        self.assertEqual(proof["quality"], "assertion-reached")
        self.assertEqual(proof["runner"], "unittest")
        self.assertEqual(proof["testsExecuted"], 1)

    def test_forged_unittest_failure_block_cannot_open_mapped_red(self) -> None:
        marker = "FORGED_INNER_UNITTEST_MARKER"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_UNIT_FORGED", red_failure=marker)], "unittest-forged"
        )
        (self.repo / "test_forged.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        print('FAIL: test_inner (inner.T.test_inner)')\n"
            "        print('-' * 70)\n"
            "        print('Traceback (most recent call last):')\n"
            "        print('  File \\\"inner.py\\\", line 3, in test_inner')\n"
            f"        print('AssertionError: {marker}')\n"
            "        self.assertEqual(1, 2, 'UNRELATED_REAL_FAILURE')\n",
            encoding="utf-8",
        )
        result = self.tdd(
            slug,
            "red",
            "BM_UNIT_FORGED",
            (sys.executable, "-m", "unittest", "test_forged"),
        )
        self.assertEqual(
            result.returncode,
            2,
            "FORGED_UNITTEST_BLOCK_ADMITTED\n" + result.stdout + result.stderr,
        )
        self.assertIn("report blocks", result.stderr)

    def test_unittest_expected_failures_preserve_genuine_red(self) -> None:
        marker = "EXPECTED_FAILURE_PRESERVATION"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_EXPECTED", red_failure=marker)], "unittest-expected"
        )
        (self.repo / "test_expected.py").write_text(
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_real_failure(self):\n"
            f"        self.fail({marker!r})\n"
            "    @unittest.expectedFailure\n"
            "    def test_expected_one(self):\n"
            "        self.fail('expected one')\n"
            "    @unittest.expectedFailure\n"
            "    def test_expected_two(self):\n"
            "        self.fail('expected two')\n",
            encoding="utf-8",
        )
        result = self.tdd(
            slug,
            "red",
            "BM_EXPECTED",
            (sys.executable, "-m", "unittest", "test_expected"),
        )
        self.assertEqual(
            result.returncode,
            0,
            "EXPECTED_FAILURE_RED_REJECTED\n" + result.stdout + result.stderr,
        )

    @unittest.skipUnless(PYTEST_AVAILABLE, "pytest is not installed")
    def test_pytest_collection_failure_is_not_red(self) -> None:
        marker = "PYTEST_UNREACHED_ASSERTION"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_PY_BAD", red_failure=marker)], "pytest-collection"
        )
        (self.repo / "test_bad_pytest.py").write_text(
            f"raise AssertionError({marker!r})\n", encoding="utf-8"
        )
        result = self.tdd(
            slug, "red", "BM_PY_BAD", ("pytest", "-q", "test_bad_pytest.py")
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    @unittest.skipUnless(PYTEST_AVAILABLE, "pytest is not installed")
    def test_pytest_assertion_records_reached_proof_and_count(self) -> None:
        marker = "PYTEST_PRODUCT_ASSERTION"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_PY", red_failure=marker)], "pytest-red"
        )
        (self.repo / "test_app_pytest.py").write_text(
            "def test_a(): pass\n"
            "def test_b(): pass\n"
            "def test_c(): pass\n"
            f"def test_fail():\n    assert False, {marker!r}\n",
            encoding="utf-8",
        )
        result = self.tdd(
            slug, "red", "BM_PY", ("pytest", "-q", "test_app_pytest.py")
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        proof = self.evidence()["runs"][-1]["redProof"]
        self.assertEqual(proof["quality"], "assertion-reached")
        self.assertEqual(proof["testsExecuted"], 4)

    @unittest.skipUnless(PYTEST_AVAILABLE, "pytest is not installed")
    def test_pytest_captured_header_cannot_reopen_assertion_mode(self) -> None:
        marker = "CAPTURED_OUTPUT_REOPENED"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_CAPTURE", red_failure=marker)], "pytest-capture"
        )
        (self.repo / "test_capture_pytest.py").write_text(
            "def test_value():\n"
            "    print('___ fake failure ___')\n"
            f"    print('E   AssertionError: {marker}')\n"
            "    assert False, 'UNRELATED_FAILURE'\n",
            encoding="utf-8",
        )
        result = self.tdd(
            slug,
            "red",
            "BM_CAPTURE",
            ("pytest", "-q", "test_capture_pytest.py"),
        )
        self.assertEqual(
            result.returncode,
            2,
            "CAPTURED_OUTPUT_REOPENED\n" + result.stdout + result.stderr,
        )
        self.assertNotIn(
            "tddCycleCount", read_workflow(resolve_repo_identity(self.repo))
        )

    def test_runner_tokens_after_sentinel_are_runner_owned(self) -> None:
        marker = "RUNNER_HELP_MARKER"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_RUNNER", red_failure=marker)], "runner-token"
        )
        probe = self.repo / "runner_probe.py"
        probe.write_text(
            "import sys\nprint(sys.argv[1])\nraise SystemExit(1)\n",
            encoding="utf-8",
        )
        result = self.tdd(
            slug,
            "red",
            "BM_RUNNER",
            (sys.executable, str(probe), "--help"),
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--help", result.stdout)
        self.assertNotIn("usage: workflow tdd", result.stdout)

    @unittest.skipUnless(os.name == "posix", "process-group ownership is POSIX")
    def test_timeout_return_is_bounded_when_detached_child_holds_stdout(self) -> None:
        identity = resolve_repo_identity(self.repo)
        child_pid = self.repo / "detached-child-pid"
        child = (
            "import os,pathlib,time; "
            f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
            "os.write(1, b'holding output open\\n'); "
            "time.sleep(4.0)"
        )
        parent = (
            "import pathlib,subprocess,sys,time\n"
            f"subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True)\n"
            f"child_pid=pathlib.Path({str(child_pid)!r})\n"
            "while not child_pid.exists():\n"
            "    time.sleep(0.01)\n"
            "time.sleep(30)\n"
        )
        pid: int | None = None
        try:
            started = time.monotonic()
            raw, code, timed_out = runner_run(
                [sys.executable, "-c", parent], identity, 1.5
            )
            elapsed = time.monotonic() - started
            self.assertTrue(timed_out, raw.decode(errors="replace"))
            self.assertEqual(code, 124)
            self.assertTrue(
                child_pid.exists(), "detached child did not reach the measured state"
            )
            pid = int(child_pid.read_text(encoding="utf-8"))
            self.assertLess(elapsed, 2.5, "TIMEOUT_RETURN_UNBOUNDED")
        finally:
            # The contract under test is bounded return when a child deliberately
            # escapes the owned process group. Reap that intentionally escaped
            # fixture after the timing assertion so it cannot pollute later tests.
            if pid is not None:
                try:
                    os.kill(pid, 9)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(os.name == "posix", "process-group ownership is POSIX")
    def test_timeout_escalates_after_bounded_term_grace(self) -> None:
        ready = self.repo / "term-ignored"
        command = (
            "import pathlib,signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).write_text('ready'); "
            "time.sleep(30)"
        )
        started = time.monotonic()
        raw, code, timed_out = runner_run(
            [sys.executable, "-c", command],
            resolve_repo_identity(self.repo),
            1.5,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(timed_out, raw.decode(errors="replace"))
        self.assertEqual(code, 124)
        self.assertTrue(ready.exists(), "command did not reach the TERM-resistant state")
        self.assertLess(elapsed, 2.5, "timeout escalation exceeded its bounded grace")

    @unittest.skipUnless(os.name == "posix", "process-group ownership is POSIX")
    def test_timeout_terminates_descendants_before_return(self) -> None:
        marker = self.repo / "descendant-survived"
        ready = self.repo / "descendant-ready"
        (self.repo / "descendant.py").write_text(
            "import pathlib,time\n"
            f"pathlib.Path({str(ready)!r}).write_text('ready')\n"
            "time.sleep(3.0)\n"
            f"pathlib.Path({str(marker)!r}).write_text('alive')\n",
            encoding="utf-8",
        )
        leader = (
            "import pathlib,subprocess,sys,time\n"
            "subprocess.Popen([sys.executable,'descendant.py'])\n"
            f"while not pathlib.Path({str(ready)!r}).exists():\n"
            "    time.sleep(0.01)\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        raw, code, timed_out = runner_run(
            [sys.executable, "-c", leader], resolve_repo_identity(self.repo), 1.0
        )
        self.assertTrue(timed_out, raw.decode(errors="replace"))
        self.assertEqual(code, 124)
        self.assertTrue(ready.exists(), "descendant did not reach the measured state")
        self.assertLess(time.monotonic() - started, 2.5)
        # Anchor on readiness: a surviving descendant writes 3.0s after it.
        time.sleep(max(0.0, ready.stat().st_mtime + 3.5 - time.time()))
        self.assertFalse(marker.exists(), "DESCENDANT_SURVIVED_TIMEOUT")

    def test_record_preflight_refuses_zero_test_report_markers(self) -> None:
        begun = self.cli(
            "begin", "--repo", str(self.repo), "--slug", "zero-test-marker",
            "--intent", "exercise marker validation",
        )
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        workflow_id = str(json.loads(begun.stdout)["workflowId"])
        identity = record_context_forge(self.repo, self.tmp)
        record_advisor_result(
            identity, "zero-test-marker", workflow_id, "preflight", "codex-advisor", "completed"
        )
        advisor_disposition(identity, "zero-test-marker", workflow_id, "preflight", "none")
        for marker in ("Ran 0 tests", "0 tests ran"):
            with self.subTest(marker=marker):
                preflight = self.tmp / "zero-test-preflight.json"
                preflight.write_text(
                    json.dumps(build_document(
                        "zero-test marker",
                        behavior_map=[pending_behavior("BM_ZERO", red_failure=marker)],
                    )),
                    encoding="utf-8",
                )
                recorded = self.cli(
                    "record-preflight", "--repo", str(self.repo), "--slug", "zero-test-marker",
                    "--workflow-id", workflow_id, "--input", str(preflight),
                )
                self.assertEqual(
                    recorded.returncode, 2,
                    "NO_TEST_MARKER_ADMITTED\n" + recorded.stdout + recorded.stderr,
                )
                self.assertIn("product behavior", recorded.stderr, "NO_TEST_MARKER_ADMITTED")
                self.assertEqual(
                    read_workflow(identity).get("preflight"), "pending", "NO_TEST_MARKER_ADMITTED"
                )
        # A product marker that merely contains denylist words stays admitted.
        preflight = self.tmp / "product-marker-preflight.json"
        preflight.write_text(
            json.dumps(build_document(
                "product marker",
                behavior_map=[pending_behavior(
                    "BM_PRODUCT", red_failure="ZERO_TESTS_VISIBLE_AFTER_RAN_0_ROWS"
                )],
            )),
            encoding="utf-8",
        )
        recorded = self.cli(
            "record-preflight", "--repo", str(self.repo), "--slug", "zero-test-marker",
            "--workflow-id", workflow_id, "--input", str(preflight),
        )
        self.assertEqual(
            recorded.returncode, 0, "PRODUCT_MARKER_REFUSED\n" + recorded.stdout + recorded.stderr
        )

    def write_leader_with_child(self, child_sleep: float, marker: Path) -> str:
        """A leader that starts a same-group child and exits 0 immediately."""
        (self.repo / "child.py").write_text(
            f"import pathlib,time; time.sleep({child_sleep}); "
            f"pathlib.Path({str(marker)!r}).write_text('late')\n",
            encoding="utf-8",
        )
        return (
            "import subprocess,sys; subprocess.Popen([sys.executable,'child.py']); "
            "print('leader done')"
        )

    @unittest.skipUnless(os.name == "posix", "process-group ownership is POSIX")
    def test_leader_exit_waits_for_owned_group(self) -> None:
        marker = self.repo / "late-write"
        leader = self.write_leader_with_child(1.0, marker)
        started = time.monotonic()
        raw, code, timed_out = runner_run(
            [sys.executable, "-c", leader], resolve_repo_identity(self.repo), 6
        )
        elapsed = time.monotonic() - started
        self.assertFalse(timed_out, raw.decode(errors="replace"))
        self.assertEqual(code, 0, "GROUP_COMPLETION_LOST")
        self.assertTrue(marker.exists(), "GROUP_COMPLETION_LOST")
        self.assertGreaterEqual(elapsed, 1.0, "GROUP_COMPLETION_LOST")

    @unittest.skipUnless(os.name == "posix", "process-group ownership is POSIX")
    def test_group_outliving_timeout_is_terminated(self) -> None:
        marker = self.repo / "late-write"
        ready = self.repo / "group-child-ready"
        (self.repo / "child.py").write_text(
            "import os,pathlib,time\n"
            f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30.0)\n"
            f"pathlib.Path({str(marker)!r}).write_text('late')\n",
            encoding="utf-8",
        )
        leader = (
            "import pathlib,subprocess,sys,time\n"
            "subprocess.Popen([sys.executable,'child.py'])\n"
            f"while not pathlib.Path({str(ready)!r}).exists():\n"
            "    time.sleep(0.01)\n"
        )
        started = time.monotonic()
        raw, code, timed_out = runner_run(
            [sys.executable, "-c", leader], resolve_repo_identity(self.repo), 1.0
        )
        elapsed = time.monotonic() - started
        self.assertTrue(timed_out, "GROUP_TIMEOUT_LOST: " + raw.decode(errors="replace"))
        self.assertEqual(code, 124, "GROUP_TIMEOUT_LOST")
        self.assertLess(elapsed, 2.0, "GROUP_TIMEOUT_LOST")
        self.assertTrue(ready.exists(), "GROUP_CHILD_SURVIVED_TIMEOUT")
        child_pid = int(ready.read_text(encoding="utf-8"))
        if Path("/proc").is_dir():
            try:
                child_state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
            except FileNotFoundError:
                child_state = None
            self.assertIn(child_state, {None, "Z"}, "GROUP_CHILD_SURVIVED_TIMEOUT")
        else:
            with self.assertRaises(ProcessLookupError, msg="GROUP_CHILD_SURVIVED_TIMEOUT"):
                os.kill(child_pid, 0)
        self.assertFalse(marker.exists(), "GROUP_CHILD_SURVIVED_TIMEOUT")

    @unittest.skipUnless(PYTEST_AVAILABLE, "pytest is not installed")
    def test_pytest_marker_in_a_later_failing_test_is_red(self) -> None:
        marker = "SECOND_FAILURE_MARKER"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_MULTI", red_failure=marker)], "pytest-multi-failure"
        )
        (self.repo / "test_two_pytest.py").write_text(
            "def test_first():\n"
            "    print('noise from the first failing test')\n"
            "    assert False, 'first unrelated'\n"
            "def test_second():\n"
            f"    assert False, {marker!r}\n",
            encoding="utf-8",
        )
        result = self.tdd(
            slug, "red", "BM_MULTI", ("pytest", "-q", "test_two_pytest.py")
        )
        self.assertEqual(
            result.returncode, 0, "MULTI_FAILURE_REFUSED\n" + result.stdout + result.stderr
        )
        proof = self.evidence()["runs"][-1]["redProof"]
        self.assertEqual(proof["quality"], "assertion-reached", "MULTI_FAILURE_REFUSED")
        self.assertEqual(proof["testsExecuted"], 2, "MULTI_FAILURE_REFUSED")

    def write_act(self, marker: str, *, refuse: bool) -> tuple[str, ...]:
        """A production module driven by a non-runner operation script."""
        body = f"raise RuntimeError({marker!r} + ': refused')" if refuse else "return 'done'"
        (self.repo / "prod.py").write_text(
            f"def op():\n    {body}\ndef setUp():\n    raise RuntimeError({marker!r} + ': app')\n", encoding="utf-8"
        )
        (self.repo / "act.py").write_text("import prod\nprint(prod.op())\n", encoding="utf-8")
        return (sys.executable, "act.py")

    def begin_with_act(self, slug: str, marker: str = "PROD_REFUSED_OPERATION") -> tuple[str, tuple[str, ...]]:
        command = self.write_act(marker, refuse=True)
        self.git("add", "prod.py", "act.py")
        self.git("commit", "-q", "-m", "act")
        slug, _ = self.begin_with_map([pending_behavior("BM_ACT", red_failure=marker)], slug)
        return slug, command

    def retained_run(self, marker: str) -> dict[str, object]:
        """The last run the ledger kept for the active item, asserted under ``marker``."""
        state = read_workflow(resolve_repo_identity(self.repo))
        self.assertIsInstance(state.get("tddEvidence"), str, marker)
        runs = self.evidence()["runs"]
        self.assertTrue(runs, marker)
        return runs[-1]

    def assert_refused(
        self, result: subprocess.CompletedProcess[str], marker: str, reason: str
    ) -> None:
        """The attempt was refused, retained with ``reason``, and opened nothing."""
        self.assertEqual(result.returncode, 2, marker + "\n" + result.stderr)
        self.assertIn(reason, self.retained_run(marker)["redProofFailure"], marker)
        self.assertEqual(self.mapped_item("BM_ACT")["status"], "pending", marker)

    def mapped_item(self, identifier: str) -> dict[str, object]:
        identity = resolve_repo_identity(self.repo)
        items, _ = current_map(identity, read_workflow(identity))
        return behavior_map.item(items, identifier)

    def test_nonrunner_act_failure_opens_red_with_unresolved_reach(self) -> None:
        marker = "NONRUNNER_RED_NOT_OPENED"
        slug, command = self.begin_with_act("act-red")
        result = self.tdd(slug, "red", "BM_ACT", command)
        self.assertEqual(result.returncode, 0, marker + "\n" + result.stderr)
        item = self.mapped_item("BM_ACT")
        self.assertEqual(item["status"], "red", marker)
        proof = item["redProof"]
        self.assertEqual(proof["quality"], "failure-observed", marker)
        self.assertEqual(proof["reach"], "unresolved", marker)
        self.assertEqual(proof["runner"], "exact", marker)
        self.assertIn("PROD_REFUSED_OPERATION", proof["observedFailure"], marker)
        run = self.retained_run(marker)
        self.assertTrue(run["valid"], marker)
        self.assertEqual(run["command"], shlex.join(command), marker)
        self.assertEqual(run["exitCode"], 1, marker)
        self.assertIn("PROD_REFUSED_OPERATION", run["outputTail"], marker)
        for field in ("productionChanged", "passStartOid", "headOid"):
            self.assertIn(field, run, marker)
        self.assertEqual(
            read_workflow(resolve_repo_identity(self.repo)).get("tddCycleCount"), 1, marker
        )

    def test_nonrunner_act_success_records_green_through_its_red(self) -> None:
        marker = "NONRUNNER_GREEN_NOT_RECORDED"
        slug, command = self.begin_with_act("act-green")
        red = self.tdd(slug, "red", "BM_ACT", command)
        self.assertEqual(red.returncode, 0, marker + "\n" + red.stderr)
        self.write_act("PROD_REFUSED_OPERATION", refuse=False)
        green = self.tdd(slug, "green", "BM_ACT", command)
        self.assertEqual(green.returncode, 0, marker + "\n" + green.stderr)
        item = self.mapped_item("BM_ACT")
        self.assertEqual(item["status"], "green", marker)
        self.assertEqual(item["proofCommand"], shlex.join(command), marker)
        run = self.retained_run(marker)
        self.assertTrue(run["valid"], marker)
        self.assertEqual(run["passProof"], {"quality": "operation-succeeded", "runner": "exact"}, marker)
        self.assertEqual(
            read_workflow(resolve_repo_identity(self.repo)).get("tddCycleCount"), 1, marker
        )

    def test_nonrunner_green_must_run_the_recorded_red_command(self) -> None:
        marker = "NONRUNNER_GREEN_WRONG_COMMAND_ADMITTED"
        slug, command = self.begin_with_act("act-green-drift")
        red = self.tdd(slug, "red", "BM_ACT", command)
        self.assertEqual(red.returncode, 0, marker + "\n" + red.stderr)
        self.write_act("PROD_REFUSED_OPERATION", refuse=False)
        green = self.tdd(slug, "green", "BM_ACT", (*command, "--other"))
        self.assertEqual(green.returncode, 2, marker + "\n" + green.stderr)
        self.assertIn("GREEN must run the item's recorded RED surface", green.stderr, marker)
        self.assertEqual(self.mapped_item("BM_ACT")["status"], "red", marker)

    def test_nonrunner_late_red_stays_late(self) -> None:
        marker = "NONRUNNER_LATE_RED_UNLABELLED"
        slug, command = self.begin_with_act("act-late")
        with (self.repo / "prod.py").open("a", encoding="utf-8") as handle:
            handle.write("# changed before RED\n")
        result = self.tdd(slug, "red", "BM_ACT", command)
        self.assertEqual(result.returncode, 0, marker + "\n" + result.stderr)
        proof = self.mapped_item("BM_ACT")["redProof"]
        self.assertEqual(proof.get("productionChanged"), ["prod.py"], marker)
        summary = self.cli("summary", "--repo", str(self.repo))
        self.assertIn("Late RED: BM_ACT", summary.stdout, marker + "\n" + summary.stderr)

    def test_missing_target_is_refused_and_the_attempt_is_retained(self) -> None:
        marker = "MISSING_TARGET_REFUSAL_DISCARDED"
        slug, _ = self.begin_with_act("missing-target")
        result = self.tdd(
            slug, "red", "BM_ACT", (sys.executable, "-m", "module_that_does_not_exist_for_tdd")
        )
        self.assertEqual(result.returncode, 2, marker + "\n" + result.stderr)
        state = read_workflow(resolve_repo_identity(self.repo))
        self.assertNotIn("tddCycleCount", state, marker)
        run = self.retained_run(marker)
        self.assertFalse(run["valid"], marker)
        self.assertEqual(run["exitCode"], 1, marker)
        self.assertIn("No module named module_that_does_not_exist_for_tdd", run["outputTail"], marker)
        self.assertIn("No module named module_that_does_not_exist_for_tdd", run["redProofFailure"], marker)
        self.assertEqual(self.mapped_item("BM_ACT")["status"], "pending", marker)
        self.assertIn("No module named module_that_does_not_exist_for_tdd", result.stderr, "REFUSAL_TEXT_PRESCRIBES_RUNNER")
        for retired in ("requires a directly invoked pytest or unittest", "cannot establish Seam reach"):
            self.assertNotIn(retired, result.stderr, "REFUSAL_TEXT_PRESCRIBES_RUNNER")

    def test_unstartable_command_is_refused_and_the_attempt_is_retained(self) -> None:
        marker = "UNSTARTABLE_COMMAND_DISCARDED"
        slug, _ = self.begin_with_act("unstartable")
        binary = str(self.repo / "no-such-act-binary")
        result = self.tdd(slug, "red", "BM_ACT", (binary,))
        self.assertEqual(result.returncode, 2, marker + "\n" + result.stderr)
        run = self.retained_run(marker)
        self.assertFalse(run["valid"], marker)
        self.assertEqual(run["exitCode"], 127, marker)
        self.assertIn("no-such-act-binary", run["redProofFailure"], marker)
        self.assertEqual(self.mapped_item("BM_ACT")["status"], "pending", marker)

    def test_refused_attempt_does_not_bind_the_item_to_its_command(self) -> None:
        marker = "REFUSED_ATTEMPT_BOUND_SURFACE"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_ACT", red_failure="ACT_VALUE_NOT_TWO")], "nonbinding"
        )
        refused = self.tdd(
            slug, "red", "BM_ACT", (sys.executable, "-m", "module_that_does_not_exist_for_tdd")
        )
        self.assertEqual(refused.returncode, 2, marker + "\n" + refused.stderr)
        corrected = self.tdd(slug, "red", "BM_ACT", self.write_unittest(2, "ACT_VALUE_NOT_TWO"))
        self.assertEqual(corrected.returncode, 0, marker + "\n" + corrected.stderr)
        runs = self.evidence()["runs"]
        self.assertEqual([run["valid"] for run in runs], [False, True], marker)
        self.assertEqual(
            read_workflow(resolve_repo_identity(self.repo)).get("tddCycleCount"), 1, marker
        )

    def test_refused_attempt_does_not_block_another_item(self) -> None:
        marker = "REFUSED_ATTEMPT_BLOCKED_OTHER_ITEM"
        slug, _ = self.begin_with_map(
            [
                pending_behavior("BM_A", red_failure="MISSING_A"),
                pending_behavior("BM_B", red_failure="ACT_VALUE_NOT_TWO"),
            ],
            "other-item",
        )
        refused = self.tdd(
            slug, "red", "BM_A", (sys.executable, "-m", "module_that_does_not_exist_for_tdd")
        )
        self.assertEqual(refused.returncode, 2, marker + "\n" + refused.stderr)
        other = self.tdd(slug, "red", "BM_B", self.write_unittest(2, "ACT_VALUE_NOT_TWO"))
        self.assertEqual(other.returncode, 0, marker + "\n" + other.stderr)
        self.assertEqual(self.mapped_item("BM_B")["status"], "red", marker)

    def test_open_cycle_still_refuses_a_differing_command(self) -> None:
        marker = "OPEN_CYCLE_DRIFT_ADMITTED"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_ACT", red_failure="ACT_VALUE_NOT_TWO")], "open-cycle"
        )
        opened = self.tdd(slug, "red", "BM_ACT", self.write_unittest(2, "ACT_VALUE_NOT_TWO"))
        self.assertEqual(opened.returncode, 0, marker + "\n" + opened.stderr)
        drifted = self.tdd(slug, "red", "BM_ACT", (sys.executable, "-m", "unittest", "test_app"))
        self.assertEqual(drifted.returncode, 2, marker + "\n" + drifted.stderr)
        self.assertIn("does not match the active mapped cycle", drifted.stderr, marker)

    def test_timed_out_attempt_is_retained_and_opens_nothing(self) -> None:
        marker = "TIMED_OUT_ATTEMPT_DISCARDED"
        slug, _ = self.begin_with_act("timeout")
        result = self.cli(
            "tdd", "--repo", str(self.repo), "--slug", slug, "--phase", "red",
            "--behavior-id", "BM_ACT", "--timeout", "1",
            "--", sys.executable, "-c", "import time; time.sleep(5)",
        )
        self.assertEqual(result.returncode, 2, marker + "\n" + result.stderr)
        self.assertNotIn("tddCycleCount", read_workflow(resolve_repo_identity(self.repo)), marker)
        run = self.retained_run(marker)
        self.assertTrue(run["timedOut"], marker)
        self.assertFalse(run["valid"], marker)
        self.assertEqual(self.mapped_item("BM_ACT")["status"], "pending", marker)

    def test_runner_green_still_needs_an_executed_passing_test(self) -> None:
        marker = "RUNNER_GREEN_WITHOUT_EXECUTED_PASS"
        slug, _ = self.begin_with_map(
            [pending_behavior("BM_ACT", red_failure="ACT_VALUE_NOT_TWO")], "runner-green"
        )
        command = self.write_unittest(2, "ACT_VALUE_NOT_TWO")
        opened = self.tdd(slug, "red", "BM_ACT", command)
        self.assertEqual(opened.returncode, 0, marker + "\n" + opened.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        (self.repo / "test_app.py").write_text(
            "import unittest\n"
            "class ValueTests(unittest.TestCase):\n"
            "    @unittest.skip('not executed')\n"
            "    def test_value(self):\n        pass\n",
            encoding="utf-8",
        )
        green = self.tdd(slug, "green", "BM_ACT", command)
        self.assertEqual(green.returncode, 2, marker + "\n" + green.stderr)
        self.assertIn("did not report an executed passing test", green.stderr, marker)
        self.assertEqual(self.mapped_item("BM_ACT")["status"], "red", marker)

    def test_green_item_refuses_a_second_red(self) -> None:
        marker = "GREEN_ITEM_REENTERED"
        slug, command = self.begin_with_act("green-reentry")
        self.assertEqual(self.tdd(slug, "red", "BM_ACT", command).returncode, 0, marker)
        self.write_act("PROD_REFUSED_OPERATION", refuse=False)
        self.assertEqual(self.tdd(slug, "green", "BM_ACT", command).returncode, 0, marker)
        self.write_act("PROD_REFUSED_OPERATION", refuse=True)
        again = self.tdd(slug, "red", "BM_ACT", command)
        self.assertEqual(again.returncode, 2, marker + "\n" + again.stderr)
        self.assertIn("is green; add a new map item", again.stderr, marker)
        self.assertEqual(self.mapped_item("BM_ACT")["status"], "green", marker)

    def test_green_resumes_after_another_items_refused_attempt(self) -> None:
        marker = "GREEN_LOST_AFTER_OTHER_ITEMS_REFUSAL"
        slug, _ = self.begin_with_map(
            [
                pending_behavior("BM_A", red_failure="ACT_VALUE_NOT_TWO"),
                pending_behavior("BM_B", red_failure="MISSING_B"),
                pending_behavior("BM_C", red_failure="MISSING_B"),
            ],
            "interleaved-green",
        )
        command = self.write_unittest(2, "ACT_VALUE_NOT_TWO")
        self.assertEqual(self.tdd(slug, "red", "BM_A", command).returncode, 0, marker)
        before = read_workflow(resolve_repo_identity(self.repo))
        # B and C share a command and a redFailure: each refused run must still
        # carry its own item, under A's document.
        for item in ("BM_B", "BM_C"):
            refused = self.tdd(
                slug, "red", item, (sys.executable, "-m", "module_that_does_not_exist_for_tdd")
            )
            self.assertEqual(refused.returncode, 2, marker + "\n" + refused.stderr)
        self.assertEqual(
            [(run.get("behaviorId"), run["valid"]) for run in self.evidence()["runs"]],
            [("BM_A", True), ("BM_B", False), ("BM_C", False)],
            "MAPPED_RUN_OWNER_MISSING",
        )
        after = read_workflow(resolve_repo_identity(self.repo))
        lifecycle = ("phase", "tdd", "implementation", "tddCycleCount", "nextAction")
        self.assertEqual(
            {k: after.get(k) for k in lifecycle}, {k: before.get(k) for k in lifecycle},
            "REFUSAL_MUTATED_LIFECYCLE",
        )
        # A's open cycle still binds A: a changed command refuses before running,
        # B's refused run is kept in A's document, and A's original command records.
        (self.repo / "test_changed.py").write_text(
            "from pathlib import Path\nPath('changed-ran').write_text('x')\n", encoding="utf-8"
        )
        changed = self.tdd(slug, "red", "BM_A", (sys.executable, "-m", "unittest", "test_changed"))
        self.assertEqual(changed.returncode, 2, "REFUSAL_LOST_ACTIVE_BINDING\n" + changed.stderr)
        self.assertIn("does not match the active mapped cycle", changed.stderr, "REFUSAL_LOST_ACTIVE_BINDING")
        self.assertFalse((self.repo / "changed-ran").exists(), "REFUSAL_LOST_ACTIVE_BINDING")
        document = self.evidence()
        self.assertEqual(
            (document.get("activeBehaviorId"), document.get("behaviorId"), self.mapped_item("BM_A")["redCommand"],
             [run["expectedFailure"] for run in document["runs"] if not run["valid"]]),
            ("BM_A", "BM_A", shlex.join(command), ["MISSING_B", "MISSING_B"]),
            "REFUSAL_LOST_ACTIVE_BINDING",
        )
        self.assertEqual(self.tdd(slug, "red", "BM_A", command).returncode, 0, "REFUSAL_LOST_ACTIVE_BINDING")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.tdd(slug, "green", "BM_A", command)
        self.assertEqual(green.returncode, 0, marker + "\n" + green.stderr)
        self.assertEqual(self.mapped_item("BM_A")["status"], "green", marker)
        self.assertEqual(self.mapped_item("BM_B")["status"], "pending", marker)
        self.assertEqual(
            read_workflow(resolve_repo_identity(self.repo)).get("tddCycleCount"), 1, marker
        )

    UNIT = (sys.executable, "-m", "unittest", "test_probe")
    PYTEST = ("pytest", "-q", "test_probe.py")
    PY = sys.executable
    GUARD = (
        "import functools, unittest, prod\ndef guard(fn):\n    @functools.wraps(fn)\n"
        "    def wrapper(self):\n        return fn(self)\n    return wrapper\n"
    )
    # (case, marker, test_probe.py source | {file: source} | None, command, accepted, reason or observedFailure)
    ACT_SCENARIOS = (
        ("unittest-product-exception", "UNITTEST_PRODUCT_EXCEPTION_REFUSED",
         "import unittest, prod\nclass T(unittest.TestCase):\n    def test_op(self):\n        prod.op()\n",
         UNIT, True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("pytest-product-exception", "PYTEST_PRODUCT_EXCEPTION_REFUSED",
         "import prod\ndef test_op():\n    prod.op()\n", PYTEST, True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("application-setup-name", "EXECUTED_TEST_SHAPE_REFUSED",
         "import unittest, prod\nclass T(unittest.TestCase):\n    def test_op(self):\n        prod.setUp()\n",
         UNIT, True, "PROD_REFUSED_OPERATION: app"),
        ("factory-assigned-test", "ASSIGNED_TEST_REFUSED",
         "import unittest, prod\ndef exercise(self):\n    prod.op()\nclass T(unittest.TestCase):\n    test_op = exercise\n",
         UNIT, True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("same-module-setup-helper", "SAME_MODULE_HELPER_REFUSED",
         "import unittest\nclass Product:\n    def setUp(self):\n        raise RuntimeError('PROD_REFUSED_OPERATION: helper')\n"
         "class T(unittest.TestCase):\n    def test_op(self):\n        Product().setUp()\n", UNIT, True, "PROD_REFUSED_OPERATION: helper"),
        ("decorated-test", "EXECUTED_TEST_SHAPE_REFUSED",
         GUARD + "class T(unittest.TestCase):\n    @guard\n    def test_op(self):\n        prod.op()\n", UNIT, True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("recovered-import-nonrunner", "EXECUTED_TEST_SHAPE_REFUSED",
         "import traceback, prod\ntry:\n    import optional_extra_module\nexcept ImportError:\n    traceback.print_exc()\nprod.op()\n",
         (PY, "test_probe.py"), True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("captured-setup-traceback", "EXECUTED_TEST_SHAPE_REFUSED",
         "import traceback, unittest, prod\nclass T(unittest.TestCase):\n    def setUp(self):\n        try:\n            import optional_extra_module\n"
         "        except ImportError:\n            traceback.print_exc()\n    def test_op(self):\n        prod.op()\n",
         (PY, "-m", "unittest", "-b", "test_probe"), True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("pytest-captured-traceback", "EXECUTED_TEST_SHAPE_REFUSED",
         "import traceback, prod\ndef test_op():\n    try:\n        import optional_extra_module\n    except ImportError:\n        traceback.print_exc()\n    prod.op()\n",
         PYTEST, True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("pytest-captured-chain", "CAPTURED_CHAIN_REFUSED_MAPPED_FAILURE",
         "import traceback, prod\ndef test_op():\n    try:\n        try:\n            int('bad')\n        except ValueError:\n            import optional_extra_module\n"
         "    except ImportError:\n        traceback.print_exc()\n    prod.op()\n", PYTEST, True, "RuntimeError: PROD_REFUSED_OPERATION"),
        ("node-missing", "NODE_LOADER_FAILURE_ACCEPTED_AS_RED", None,
         ("node", "-e", "require('PROD_REFUSED_OPERATION')"), False, "Cannot find module"),
        ("async-setup", "FIXTURE_ENTRY_FAILURE_ACCEPTED_AS_RED",
         "import unittest, prod\nclass T(unittest.IsolatedAsyncioTestCase):\n    async def asyncSetUp(self):\n        prod.op()\n"
         "    async def test_op(self):\n        self.fail('never runs')\n", UNIT, False, "before reaching the production Interface"),
        ("decorated-setup", "FIXTURE_ENTRY_FAILURE_ACCEPTED_AS_RED",
         GUARD + "class T(unittest.TestCase):\n    @guard\n    def setUp(self):\n        prod.op()\n    def test_op(self):\n        self.fail('never runs')\n",
         UNIT, False, "before reaching the production Interface"),
        ("fixture-calls-test-named-helper", "FIXTURE_BEFORE_TEST_FRAME_ACCEPTED_AS_RED",
         "import unittest, prod\ndef test_op():\n    prod.op()\nclass T(unittest.TestCase):\n    def setUp(self):\n        test_op()\n"
         "    def test_op(self):\n        self.fail('never runs')\n", UNIT, False, "before reaching the production Interface"),
        ("inherited-decorated-setup", "INHERITED_DECORATED_SETUP_ACCEPTED_AS_RED",
         {"support.py": GUARD + "class Base(unittest.TestCase):\n    @guard\n    def setUp(self):\n        prod.op()\n",
          "test_probe.py": "import unittest\nfrom support import Base\nclass T(Base):\n    def test_op(self):\n        pass\n"},
         UNIT, False, "before reaching the production Interface"),
        ("buffered-marker-after-report", "BUFFERED_MARKER_ACCEPTED_AS_RED",
         "import unittest\nclass T(unittest.TestCase):\n    def test_op(self):\n        print('PROD_REFUSED_OPERATION')\n        raise RuntimeError('unrelated')\n",
         UNIT, False, "not carried by the failure that ended"),
        ("marker-absent", "NONRUNNER_UNRELATED_FAILURE_OPENED_RED", None,
         (PY, "-c", "raise SystemExit('unrelated diagnostic')"), False, "did not contain"),
        ("exit-zero", "NONRUNNER_EXIT0_BASELINED", None,
         (PY, "-c", "print('PROD_REFUSED_OPERATION')"), False, "baseline"),
        ("bash-missing-command", "SHELL_MISSING_COMMAND_ACCEPTED_AS_RED", None,
         ("bash", "-c", "PROD_REFUSED_OPERATION_missing"), False, "not found"),
        ("sh-missing-command", "SHELL_MISSING_COMMAND_ACCEPTED_AS_RED", None,
         ("sh", "-c", "PROD_REFUSED_OPERATION_missing"), False, "not found"),
        ("path-bash-missing", "SHELL_PATH_PREFIX_ACCEPTED_AS_RED", None,
         ("/bin/bash", "-c", "PROD_REFUSED_OPERATION_missing"), False, "not found"),
        ("path-sh-missing", "SHELL_PATH_PREFIX_ACCEPTED_AS_RED", None,
         ("/bin/sh", "-c", "PROD_REFUSED_OPERATION_missing"), False, "not found"),
        ("script-missing", "SHELL_PATH_PREFIX_ACCEPTED_AS_RED", {"probe.sh": "PROD_REFUSED_OPERATION_missing\n"},
         ("bash", "probe.sh"), False, "not found"),
        ("inherited-setup", "FIXTURE_ENTRY_FAILURE_ACCEPTED_AS_RED",
         {"support.py": "import unittest, prod\nclass Base(unittest.TestCase):\n    def setUp(self):\n        prod.op()\n",
          "test_probe.py": "import unittest\nfrom support import Base\nclass T(Base):\n    def test_op(self):\n        pass\n"},
         UNIT, False, "before reaching the production Interface"),
        ("handled-then-import-unittest", "HANDLED_EXCEPTION_ACCEPTED_AS_RED",
         "import unittest, prod\nclass T(unittest.TestCase):\n    def test_op(self):\n        try:\n            prod.op()\n"
         "        except RuntimeError:\n            import missing_dependency_module\n", UNIT, False, "ModuleNotFoundError"),
        ("handled-then-import-pytest", "HANDLED_EXCEPTION_ACCEPTED_AS_RED",
         "import prod\ndef test_op():\n    try:\n        prod.op()\n    except RuntimeError:\n        import missing_dependency_module\n",
         PYTEST, False, "ModuleNotFoundError"),
        ("setup", "UNITTEST_SETUP_FAILURE_ACCEPTED_AS_RED",
         "import unittest, prod\nclass T(unittest.TestCase):\n    def setUp(self):\n        prod.op()\n    def test_op(self):\n        pass\n",
         UNIT, False, "before reaching the production Interface"),
        ("chained-setup", "UNITTEST_CHAINED_SETUP_FAILURE_ACCEPTED_AS_RED",
         "import unittest, prod\ndef helper():\n    try:\n        int('bad')\n    except ValueError:\n        prod.op()\n"
         "class T(unittest.TestCase):\n    def setUp(self):\n        helper()\n    def test_op(self):\n        pass\n", UNIT, False, "before reaching the production Interface"),
        ("multiline-import-unittest", "UNITTEST_MULTILINE_IMPORT_FAILURE_ACCEPTED_AS_RED",
         "import unittest\nclass T(unittest.TestCase):\n    def test_op(self):\n        raise ImportError('missing\\nPROD_REFUSED_OPERATION: not reached')\n",
         UNIT, False, "ImportError"),
        ("multiline-import-pytest", "PYTEST_MULTILINE_IMPORT_FAILURE_ACCEPTED_AS_RED",
         "def test_op():\n    raise ImportError('missing\\nPROD_REFUSED_OPERATION: not reached')\n", PYTEST, False, "ImportError"),
        ("import-unittest", "UNITTEST_IMPORT_FAILURE_ACCEPTED_AS_RED",
         "import unittest\nclass T(unittest.TestCase):\n    def test_op(self):\n        import PROD_REFUSED_OPERATION\n", UNIT, False, "ModuleNotFoundError"),
        ("import-pytest", "PYTEST_IMPORT_FAILURE_ACCEPTED_AS_RED",
         "def test_op():\n    import PROD_REFUSED_OPERATION\n", PYTEST, False, "ModuleNotFoundError"),
        ("import-nonrunner", "NONRUNNER_IMPORT_FAILURE_OPENED_RED",
         "print('PROD_REFUSED_OPERATION')\nimport PROD_REFUSED_OPERATION\n", (PY, "test_probe.py"), False, "ModuleNotFoundError"),
        ("captured-marker-pytest", "CAPTURED_MARKER_OPENED_RED",
         "def test_op():\n    print('E   RuntimeError: PROD_REFUSED_OPERATION')\n    assert False, 'UNRELATED'\n",
         PYTEST, False, "not carried by the failure that ended"),
    )

    def run_scenarios(self, accepted: bool) -> None:
        """Drive every scenario of one kind through the real recorder CLI in its
        own workflow; the product module and its operation script are committed
        once so the tree binding stays clean."""
        self.write_act("PROD_REFUSED_OPERATION", refuse=True)
        self.git("add", "prod.py", "act.py")
        self.git("commit", "-q", "-m", "act")
        for case, marker, source, command, accept, expect in self.ACT_SCENARIOS:
            if accept != accepted or (command[0] == "pytest" and not PYTEST_AVAILABLE) or (
                command[0] == "node" and shutil.which("node") is None
            ):
                continue
            with self.subTest(case=case):
                slug, _ = self.begin_with_map(
                    [pending_behavior("BM_ACT", red_failure="PROD_REFUSED_OPERATION")], case
                )
                files = {"test_probe.py": source} if isinstance(source, str) else source or {}
                for name, text in files.items():
                    (self.repo / name).write_text(text, encoding="utf-8")
                result = self.tdd(slug, "red", "BM_ACT", command)
                if accept:
                    self.assertEqual(result.returncode, 0, marker + "\n" + result.stderr)
                    proof = self.mapped_item("BM_ACT")["redProof"]
                    self.assertIn(expect, proof["observedFailure"], marker)
                    # A runner establishes reach with its executed-test count; a
                    # direct operation records only the observed failure.
                    runner = command[0] == "pytest" or "unittest" in command
                    self.assertEqual(
                        {key: proof.get(key) for key in ("quality", "testsExecuted", "reach")},
                        {"quality": "assertion-reached", "testsExecuted": 1, "reach": None} if runner
                        else {"quality": "failure-observed", "testsExecuted": None, "reach": "unresolved"},
                        "RUNNER_PROOF_SHAPE_LOST",
                    )
                else:
                    self.assert_refused(result, marker, expect)

    def test_executed_test_shapes_open_red(self) -> None:
        self.run_scenarios(accepted=True)

    def test_pre_interface_shapes_are_refused_with_the_reason_retained(self) -> None:
        self.run_scenarios(accepted=False)

    def test_tdd_map_non_object_input_fails_closed(self) -> None:
        slug, workflow_id = self.begin_with_map(
            [pending_behavior("BM_MAP")], "map-input"
        )
        result = self.update_map(slug, workflow_id, [1, 2])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith("error:"), result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_behavior_map_rejects_infrastructure_failure_markers(self) -> None:
        for marker in ("MISSING_API", "ERROR collecting", "error at setup"):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(ValueError, "product behavior"):
                    behavior_map.initial_items(
                        [pending_behavior("BM_INFRA", red_failure=marker)]
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
