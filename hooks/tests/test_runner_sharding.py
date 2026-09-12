#!/usr/bin/env python3
"""The integrated runner schedules real work concurrently and still fails loudly.

Every attack drives the repository's own runner over its own test modules: no
sentinel scripts, no synthetic exit codes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "hooks" / "tests" / "run.sh"
DEAL = ROOT / "hooks" / "tests" / "deal.py"
RAN = re.compile(r"^Ran (\d+) tests?", re.MULTILINE)
# A targeted selection that outlives this has already lost the runner's point,
# so the budget is the assertion rather than an unattributable timeout error.
BUDGET = 300
# Named here rather than imported from the dealer: a test that asserts against the
# constant under test cannot catch that constant being wrong.
OPTIONAL = ".github/scripts/test_pr_scope.py"
REQUIRED = ("skills/production-code/scripts/test_code_quality_gate.py",
            "skills/codex-advisor/tests/test-ask-codex-advisor.sh")


class RunnerAttack(unittest.TestCase):
    def run_runner(self, *jobs: str, workers: int, marker: str, runner: Path = RUNNER,
                   budget: int = BUDGET, home: str | None = None) -> tuple[subprocess.CompletedProcess[str], float]:
        env = {**os.environ, "HOOKS_TEST_WORKERS": str(workers), "PYTHONDONTWRITEBYTECODE": "1"}
        if home is not None:
            env["DEVIN_ESTATE_HOME"] = home
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["bash", str(runner), *jobs], env=env, text=True, capture_output=True,
                timeout=budget, check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"{marker}: the run did not finish within {budget}s")
        return result, time.monotonic() - started

    def total_ran(self, run: subprocess.CompletedProcess[str]) -> int:
        """Every shard reports its own count, so the selection's real size is their sum."""
        return sum(int(count) for count in RAN.findall(run.stdout + run.stderr))

    def shards(self, run: subprocess.CompletedProcess[str]) -> int:
        """One unittest report per shard the selection was dealt into."""
        return len(RAN.findall(run.stdout + run.stderr))


class RunnerShardingTests(RunnerAttack):
    def test_a_newline_job_is_rejected_before_dispatch(self) -> None:
        marker = "RUNNER_SPLIT_A_JOB_NAME"
        with tempfile.TemporaryDirectory(prefix="runner-path-") as tmp:
            job = Path(tmp) / "line\nbreak.py"
            job.symlink_to(ROOT / OPTIONAL)
            result, _ = self.run_runner(str(job), "hooks.tests.test_state_foundation",
                                        workers=2, marker=marker, budget=10)
            self.assertNotEqual(result.returncode, 0, marker)
            self.assertIn("newline in job name", result.stderr, marker)
            self.assertEqual(self.total_ran(result), 0, marker)

    def test_nonfiles_are_rejected_before_dealing(self) -> None:
        marker = "DEAL_ACCEPTED_A_NONFILE"
        with tempfile.TemporaryDirectory(prefix="runner-path-") as tmp:
            fifo = Path(tmp) / "stream.sh"
            os.mkfifo(fifo)
            for job in (Path(tmp), fifo):
                with self.subTest(job=job.name):
                    result = subprocess.run(
                        [sys.executable, str(DEAL), "2", str(job), "hooks.tests.test_state_foundation"],
                        cwd=ROOT, text=True, capture_output=True, timeout=10, check=False,
                    )
                    self.assertNotEqual(result.returncode, 0, marker)
                    self.assertIn("not a regular file", result.stderr, marker)
                    self.assertEqual(result.stdout, "", marker)

    @unittest.skipUnless((ROOT / OPTIONAL).is_file(), "the real CI-only job is unavailable")
    def test_whole_file_names_preserve_spaces_and_carriage_returns(self) -> None:
        marker = "RUNNER_REJECTED_A_VALID_FILE"
        with tempfile.TemporaryDirectory(prefix="runner-path-") as tmp:
            for name in ("plain.py", "space name.py", "carriage\rreturn.py"):
                with self.subTest(name=name):
                    job = Path(tmp) / name
                    job.symlink_to(ROOT / OPTIONAL)
                    result, _ = self.run_runner(str(job), workers=2, marker=marker, budget=10)
                    self.assertEqual(result.returncode, 0, marker)
                    self.assertEqual(self.total_ran(result), 3, marker)

    def test_a_selection_is_dealt_across_workers(self) -> None:
        """A targeted selection must shard too, in either form a caller may write
        it, and must still run every case exactly once: the slow modules are
        exactly the ones a reviewer runs on their own."""
        marker = "RUNNER_DID_NOT_SHARD_A_SELECTION"
        module = "hooks.tests.test_tdd_summary"
        path = str(ROOT / "hooks" / "tests" / "test_tdd_summary.py")
        # Class and method selectors are forms the Interface admits and a caller
        # writes, so each is attacked here rather than only through the unloadable
        # id elsewhere. Each selection is paired with the id sizing it.
        method = module + ".LegacyImportFreeFormTests.test_mapless_import_admits_free_form_red_green"
        selections = ((path, module), (module, module),
                      (module + ".TddSummaryTests", module + ".TddSummaryTests"),
                      (method, method))
        expected = unittest.defaultTestLoader.loadTestsFromName(module).countTestCases()
        self.assertGreater(expected, 4, marker + ": the selection did not load here either")
        # Exit codes only in these messages: a nested runner report quoted into a
        # failure message is unattributable to the test that raised it.
        serial, serial_seconds = self.run_runner(path, workers=1, marker=marker)
        self.assertEqual(serial.returncode, 0, marker + ": serial selection run failed")
        self.assertEqual(self.total_ran(serial), expected, marker + ": one worker ran the wrong count")
        for selection, sized_by in selections:
            with self.subTest(selection=selection):
                want = unittest.defaultTestLoader.loadTestsFromName(sized_by).countTestCases()
                sharded, sharded_seconds = self.run_runner(selection, workers=4, marker=marker)
                self.assertEqual(sharded.returncode, 0, marker + ": sharded selection run failed")
                self.assertEqual(self.total_ran(sharded), want,
                                 marker + ": sharding changed the selection's case count")
                # One report per shard: the count assertion alone would also hold
                # for a selection that was never dealt out at all. For the
                # single-case method form this is inert, and that row instead
                # proves the Interface admits the form and runs its one case.
                self.assertEqual(self.shards(sharded), min(4, want),
                                 marker + ": the selection was not dealt across workers")
                if want == expected:  # only the whole-module forms race the serial baseline
                    self.assertLess(
                        sharded_seconds, serial_seconds * 0.75,
                        f"{marker}: {sharded_seconds:.1f}s sharded against {serial_seconds:.1f}s serial",
                    )

    def test_a_failing_selection_fails_the_run(self) -> None:
        """An unrunnable selection is a real failure, and the rest of the selection
        still has to run: a selection that resolved to nothing would fail the run
        for the wrong reason and look identical from the exit code alone."""
        marker = "RUNNER_SWALLOWED_A_FAILURE"
        good = "hooks.tests.test_state_foundation"
        missing = "hooks.tests.test_state_foundation.NoSuchTestCase.test_absent"
        expected = unittest.defaultTestLoader.loadTestsFromName(good).countTestCases() + 1
        self.assertGreater(expected, 2, marker + ": the good selection did not load here either")
        for workers in (1, 2):
            with self.subTest(workers=workers):
                result, _ = self.run_runner(good, missing, workers=workers, marker=marker, budget=60)
                self.assertNotEqual(result.returncode, 0, marker + f" at {workers} worker(s)")
                # The counts alone: a nested runner report quoted into a failure
                # message is unattributable to this test.
                self.assertEqual(self.total_ran(result), expected,
                                 marker + f": the selection ran the wrong count at {workers} worker(s)")

    def test_a_named_missing_selection_fails_the_run(self) -> None:
        """A .py under hooks/tests the caller named is a job the caller asked for.
        Dropping it is the silent-green case: beside a selector that does resolve,
        the run reported success having done only the half that existed."""
        marker = "RUNNER_DROPPED_A_NAMED_JOB"
        good = "hooks.tests.test_state_foundation"
        # Absent, and present but holding no cases: the same class, and the second
        # is the one a rename or a half-finished refactor actually produces.
        # The path form and the dotted form are separate branches of the dealer, and
        # a caller reaches the silent-drop shape through either.
        for named in ("hooks/tests/test_typo_does_not_exist.py", "hooks/tests/deal.py",
                      "hooks.tests.deal"):
            for selection in ((named,), (good, named)):
                with self.subTest(selection=selection):
                    result, _ = self.run_runner(*selection, workers=2, marker=marker, budget=120)
                    output = result.stdout + result.stderr
                    self.assertNotEqual(result.returncode, 0,
                                        marker + ": the run passed without it: " + output[-300:])
                    self.assertIn(f"no tests selected by {named}", output,
                                  marker + ": the run never named it as the job it dropped: "
                                  + output[-300:])

class ProbeTreeTests(RunnerAttack):
    """What the runner does when the rest of the estate is absent, and which home
    it hands each job, are only observable in a tree this test owns."""

    PASSING_MODULE = ("import unittest\n"
                      "class T(unittest.TestCase):\n"
                      "    def test_probe(self): pass\n")

    def probe_tree(self, marker: str, module_source: str = PASSING_MODULE) -> Path:
        self.assertTrue(DEAL.is_file(), marker + ": the runner deals no jobs here")
        tree = Path(tempfile.mkdtemp(prefix="runner-probe-"))
        self.addCleanup(shutil.rmtree, tree, True)
        tests = tree / "hooks" / "tests"
        tests.mkdir(parents=True)
        (tree / "hooks" / "__init__.py").write_text("", encoding="utf-8")
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_probe.py").write_text(module_source, encoding="utf-8")
        shutil.copy(RUNNER, tests / "run.sh")
        shutil.copy(DEAL, tests / "deal.py")
        return tree

    def test_a_missing_required_job_fails_the_run(self) -> None:
        """Only .github/scripts is absent from the installed estate. A required job
        that vanished must reach the runner and fail there, not be dealt away."""
        marker = "DEAL_SKIPPED_A_REQUIRED_JOB"
        tree = self.probe_tree(marker)
        result, _ = self.run_runner(workers=1, marker=marker, runner=tree / "hooks" / "tests" / "run.sh", budget=60)
        self.assertNotEqual(result.returncode, 0, marker + ": a tree without them still passed")
        output = result.stdout + result.stderr
        for required in REQUIRED:
            self.assertIn(required, output, marker + ": " + output[-300:])
        self.assertNotIn(OPTIONAL, output,
                         marker + ": the CI-only job was dealt into a tree without it")

    def dealt(self, tree: Path, marker: str, *args: str) -> list[str]:
        """The dealer's own stdout, which is where it says what the run will do."""
        run = subprocess.run([sys.executable, str(tree / "hooks" / "tests" / "deal.py"), "1", *args],
                             cwd=tree, text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(run.returncode, 0, marker + ": " + run.stdout + run.stderr)
        return run.stdout.splitlines()

    def test_the_ci_only_job_may_be_absent_while_the_required_ones_run(self) -> None:
        """Whether the CI-only job is dealt is decided by its presence alone: the
        installed estate is the required-present, optional-absent tree, and
        'required jobs fail loudly' must not mean 'any tree is dealt as failing'.
        This attack owns which jobs are dealt; that they then run is evidenced by
        the recorded whole-suite run, not by any attack here."""
        marker = "DEAL_FAILED_A_TREE_MISSING_ONLY_THE_CI_JOB"
        tree = self.probe_tree(marker)
        absent = self.dealt(tree, marker)
        for job in REQUIRED:
            self.assertIn(job, absent, marker + ": " + "; ".join(absent))
        self.assertNotIn(OPTIONAL, absent, marker + ": dealt into a tree without it")
        # Its presence is the dealer's only input for that decision.
        (tree / OPTIONAL).parent.mkdir(parents=True, exist_ok=True)
        (tree / OPTIONAL).write_text("", encoding="utf-8")
        present = self.dealt(tree, marker)
        self.assertIn(OPTIONAL, present, marker + ": " + "; ".join(present))
        self.assertEqual([job for job in present if job != OPTIONAL], absent,
                         marker + ": its presence changed some other job")

    def test_the_dealer_deals_every_id_exactly_once(self) -> None:
        """Counts alone would still hold for a deal that ran one case twice and
        skipped another, so the identities themselves are compared here."""
        marker = "DEAL_LOST_OR_REPEATED_AN_ID"
        self.assertTrue(DEAL.is_file(), marker + ": the runner deals no jobs here")
        module = "hooks.tests.test_tdd_summary"
        def ids(suite: unittest.TestSuite):
            for item in suite:
                yield from ids(item) if isinstance(item, unittest.TestSuite) else (item.id(),)

        want = sorted(ids(unittest.defaultTestLoader.loadTestsFromName(module)))
        # Both sides resolve ids through the same loader, so an unloadable module
        # would collapse both to unittest's one-case stub and this would compare
        # that stub with itself.
        self.assertGreater(len(want), 4, marker + ": the module did not load here either")
        run = subprocess.run([sys.executable, str(DEAL), "4", module],
                             cwd=ROOT, text=True, capture_output=True, timeout=60, check=False)
        self.assertEqual(run.returncode, 0, marker + ": " + run.stdout + run.stderr)
        lines = run.stdout.splitlines()
        dealt = [identity for line in lines for identity in line.split()]
        self.assertEqual(len(lines), 4, marker + ": the module was not dealt into four shards")
        self.assertEqual(sorted(dealt), want, marker + ": the dealt ids are not the module's own")

    def test_an_empty_job_name_fails_the_run(self) -> None:
        """`run.sh "$SELECTION"` with the variable unset is an ordinary wrapper
        shape. The empty name reaches the whole-jobs branch, which is the one
        branch exempt from the per-member case check, so it is rejected earlier."""
        marker = "RUNNER_INVENTED_A_JOB"
        tree = self.probe_tree(marker)
        for selection in (("",), ("", "hooks/tests/test_probe.py")):
            with self.subTest(selection=selection):
                result, _ = self.run_runner(*selection, workers=2, marker=marker,
                                            runner=tree / "hooks" / "tests" / "run.sh", budget=60)
                output = result.stdout + result.stderr
                self.assertNotIn("Ran ", output, marker + ": a test process ran anyway: " + output[-300:])
                self.assertIn("empty job name", output, marker + ": " + output[-300:])
                self.assertNotEqual(result.returncode, 0, marker + ": " + output[-300:])

    def test_a_dealer_failure_fails_the_run(self) -> None:
        """The real dealer, a valid worker count, and a tree whose test package is
        not importable: discovery raises and the dealer exits non-zero. The runner
        must stop there rather than dispatch whatever the dealer managed to print."""
        marker = "RUNNER_IGNORED_A_DEALER_FAILURE"
        tree = self.probe_tree(marker)
        # Neither package marker: discovery cannot import the start directory, so
        # the real dealer raises. A namespace package would still import.
        (tree / "hooks" / "__init__.py").unlink()
        (tree / "hooks" / "tests" / "__init__.py").unlink()
        result, _ = self.run_runner(workers=2, marker=marker,
                                    runner=tree / "hooks" / "tests" / "run.sh", budget=60)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, marker + ": " + output[-300:])
        self.assertIn("deal.py", output,
                      marker + ": the failure never named the dealer: " + output[-300:])
        self.assertNotIn("Ran ", output,
                         marker + ": the runner dispatched anyway: " + output[-300:])

    def test_two_jobs_of_one_run_overlap(self) -> None:
        """Shard and case counts establish that work was dealt out, not that it ran
        at the same time. CLOCK_MONOTONIC is system-wide, so the two jobs' own
        intervals compare directly however many cores the machine has."""
        marker = "RUNNER_RAN_ITS_JOBS_ONE_AT_A_TIME"
        tree = self.probe_tree(
            marker,
            "import time\n"
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_one(self): self.span()\n"
            "    def test_two(self): self.span()\n"
            "    def span(self):\n"
            "        start = time.monotonic()\n"
            "        time.sleep(0.5)\n"
            "        print(f'[SPAN {start} {time.monotonic()}]')\n",
        )
        result, _ = self.run_runner("hooks/tests/test_probe.py", workers=2, marker=marker,
                                    runner=tree / "hooks" / "tests" / "run.sh", budget=60)
        output = result.stdout + result.stderr
        spans = [(float(a), float(b)) for a, b in re.findall(r"\[SPAN (\S+) (\S+)\]", output)]
        self.assertEqual(len(spans), 2, marker + ": " + output[-300:])
        self.assertLess(max(s for s, _ in spans), min(e for _, e in spans),
                        marker + f": the two jobs did not overlap: {spans}")

    def test_two_jobs_of_one_run_get_two_homes(self) -> None:
        """Jobs run at the same time, so the runner owns each job's estate state
        rather than requiring its caller to leave DEVIN_ESTATE_HOME alone."""
        marker = "RUNNER_SHARED_ONE_HOME_ACROSS_JOBS"
        tree = self.probe_tree(
            marker,
            "import os\n"
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            # Delimited: concurrent jobs share one pipe, so a newline is not a
            # boundary these values can be read back across.
            "    def test_one(self): print('[JOB_HOME=' + os.environ['DEVIN_ESTATE_HOME'] + ']')\n"
            "    def test_two(self): print('[JOB_HOME=' + os.environ['DEVIN_ESTATE_HOME'] + ']')\n",
        )
        caller = str(tree / "caller-home")
        result, _ = self.run_runner(
            "hooks/tests/test_probe.py", workers=2, marker=marker,
            runner=tree / "hooks" / "tests" / "run.sh", budget=60, home=caller,
        )
        self.assertEqual(result.returncode, 0, marker + ": " + (result.stdout + result.stderr)[-300:])
        homes = re.findall(r"\[JOB_HOME=([^\]]+)\]", result.stdout + result.stderr)
        self.assertEqual(len(homes), 2, marker + ": " + (result.stdout + result.stderr)[-300:])
        self.assertNotIn(caller, homes, marker + ": a job ran in the home its caller exported")
        self.assertEqual(len(set(homes)), 2, marker + ": both jobs ran in one home")


if __name__ == "__main__":
    unittest.main()
