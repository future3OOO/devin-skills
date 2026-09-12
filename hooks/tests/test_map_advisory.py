"""The estate map-ownership advisory, delivered on a successful production edit.

Every test drives the real workflow CLI against a real pass-start index: a
governed intake through the bootstrap Adapter indexes a fixture repository, the
recorder takes a RED, the production edit lands, and then the real PostToolUse
edit-success adapter (code-quality-gate.py) runs; the assertion reads the
advisory notice it emitted through hookSpecificOutput.additionalContext and the
detect-changes launches counted at the real gitnexus CLI boundary (a mapped
GREEN, where one is taken, must emit nothing). Nothing substitutes the producer,
the hook, the index, or the map.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib.repo_identity import resolve_repo_identity  # noqa: E402
from hooks.lib.state_store import repo_state_dir  # noqa: E402
from hooks.lib.workflow_state import (  # noqa: E402
    advisor_disposition,
    instance_id,
    read_workflow,
    record_advisor_result,
)
from hooks.tests.support import (  # noqa: E402
    POST_EDIT,
    WORKFLOW,
    build_document,
    fixture_env,
    pending_behavior,
    run_git,
    run_intake,
    run_post_edit,
    run_workflow,
)

CANONICAL_BOOTSTRAP = Path.home() / ".local/share/repo-context-forge/current/scripts/codex_context_bootstrap.py"
GITNEXUS = shutil.which("gitnexus")
ADVISORY = "map advisory:"
UNITTEST = (sys.executable, "-m", "unittest")
COMPUTE = "tests.test_app.AppTests.test_compute"
ITEM = "BM_FIXTURE"
SESSION = "map-advisory-session"

APP = "def compute(value):\n    return value + {}\n"
# Three tests call compute, so a change to it impacts all three. The first two
# fail on the base tree and pass once compute adds two; the third always passes.
TESTS = """import unittest

from app import compute


class AppTests(unittest.TestCase):
    def test_compute(self):
        self.assertEqual(compute(1), 3, "FIXTURE_VALUE_NOT_THREE")

    def test_second(self):
        self.assertEqual(compute(1), 3, "FIXTURE_VALUE_NOT_THREE")

    def test_other(self):
        self.assertGreaterEqual(compute(2), 3)
"""
EXTRA = "from app import compute\nimport unittest\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        self.assertGreaterEqual(compute(0), 1)\n"


@unittest.skipUnless(CANONICAL_BOOTSTRAP.is_file(), "real Repo Context Forge source is unavailable")
@unittest.skipUnless(GITNEXUS, "the real GitNexus CLI is unavailable")
class MapAdvisoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-map-advisory-"))
        self.repo = self.tmp / "repo"
        (self.repo / "tests").mkdir(parents=True)
        self.slug = "map-advisory"
        self.intent = "advise on impacted tests the map does not own"
        previous = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")

        def restore_state_root() -> None:
            if previous is None:
                os.environ.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
            else:
                os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = previous

        self.addCleanup(restore_state_root)
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.tmp / "state")
        self.env = fixture_env(self.tmp / "state")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Workflow Harness")
        self.git("remote", "add", "origin", "https://example.invalid/workflow-fixture.git")
        (self.repo / "app.py").write_text(APP.format(1), encoding="utf-8")
        (self.repo / "caller.py").write_text("from app import compute\n\n\ndef run():\n    return compute(1)\n", encoding="utf-8")
        (self.repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "tests" / "test_app.py").write_text(TESTS, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- one governed pass, every producer step through the real CLI ---------

    def git(self, *args: str) -> None:
        result = run_git(self.repo, self.env, *args)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def workflow(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_workflow(self.repo, self.env, *args)

    def intake(self, *extra: str) -> None:
        result = run_intake(self.repo, self.env, self.slug, self.intent, *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def status(self) -> dict[str, object]:
        result = self.workflow("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def begin_pass(self, *runner: str) -> None:
        """One governed pass to its RED: begin, the real intake that builds the
        pass-start index, preflight with the single fixture item, then the RED
        through the runner argv given verbatim — its selection is what owns tests."""
        begun = self.workflow("begin", "--slug", self.slug, "--intent", self.intent)
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        self.intake()
        identity = resolve_repo_identity(self.repo)
        workflow_id = instance_id(read_workflow(identity))
        record_advisor_result(identity, self.slug, workflow_id, "preflight", "codex-advisor", "completed")
        advisor_disposition(identity, self.slug, workflow_id, "preflight", "none")
        document = self.tmp / "preflight.json"
        document.write_text(json.dumps(build_document("map advisory fixture", behavior_map=[pending_behavior(
            ITEM, behavior="compute adds two", seam="tests/test_app.py through unittest",
            expected="compute(1) is 3", red_failure="FIXTURE_VALUE_NOT_THREE",
        )])), encoding="utf-8")
        recorded = self.workflow(
            "record-preflight", "--slug", self.slug, "--workflow-id", workflow_id, "--input", str(document),
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.tdd_runner("red", *runner)

    def tdd_runner(self, phase: str, *runner: str,
                   env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        """One recorder RED/GREEN of the fixture item; the runner argv is verbatim."""
        result = subprocess.run(
            [
                sys.executable, str(WORKFLOW), "tdd", "--repo", str(self.repo),
                "--slug", self.slug, "--phase", phase, "--behavior-id", ITEM, "--", *runner,
            ],
            cwd=self.repo, env={**self.env, **(env_extra or {})}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def edit_compute(self, added: int = 2, *, unindexed: bool = False) -> None:
        (self.repo / "app.py").write_text(APP.format(added), encoding="utf-8")
        if unindexed:
            # A file absent from the pass-start index cannot be attributed to any
            # indexed symbol, so the producer reports a partial analysis.
            (self.repo / "extra.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    @property
    def advisory_cache(self) -> Path:
        return repo_state_dir(resolve_repo_identity(self.repo)) / "map-advisory.json"

    # --- the advisory's delivery, its graph calls counted at the real CLI ----

    def _counted(self) -> tuple[dict[str, str], Path]:
        """A fresh detect-changes counter for one invocation: PATH shadows
        gitnexus with a shim that tallies detect-changes launches and delegates
        to the real binary, so a scan is observed rather than inferred."""
        descriptor, name = tempfile.mkstemp(dir=self.tmp, prefix="scans-")
        os.close(descriptor)
        shim_dir = self.tmp / "shim"
        shim_dir.mkdir(exist_ok=True)
        shim = shim_dir / "gitnexus"
        shim.write_text(f'#!/bin/sh\ncase "$1" in detect-changes) echo x >> "{name}";; esac\nexec "{GITNEXUS}" "$@"\n')
        shim.chmod(0o755)
        return {"PATH": f"{shim_dir}{os.pathsep}{self.env['PATH']}"}, Path(name)

    def hook(self, relative: str = "app.py") -> tuple[list[str], int]:
        """Run the real edit hook on one file; return its advisory lines and the
        detect-changes launches of this invocation. It must never fail the edit."""
        env_extra, counter = self._counted()
        result = run_post_edit(self.repo, self.env, relative, session=SESSION, env_extra=env_extra)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        lines: list[str] = []
        for line in reversed(result.stdout.splitlines()):
            if line.startswith("{"):
                context = json.loads(line)["hookSpecificOutput"]["additionalContext"]
                lines = [entry for entry in context.splitlines() if entry.startswith(ADVISORY)]
                break
        return lines, counter.read_text().count("x")

    def green(self, *runner: str) -> tuple[list[str], int, bool]:
        """The recorder GREEN of the fixture item; return the advisory lines it
        printed on stderr, its detect-changes launches, and the payload's validity."""
        env_extra, counter = self._counted()
        result = self.tdd_runner("green", *runner, env_extra=env_extra)
        payload = next(json.loads(line) for line in reversed(result.stdout.splitlines()) if line.startswith("{"))
        lines = [entry for entry in result.stderr.splitlines() if entry.startswith(ADVISORY)]
        return lines, counter.read_text().count("x"), bool(payload["valid"])

    # --- attacks -------------------------------------------------------------

    def test_a_production_edit_advises_once_and_a_test_edit_or_green_never_does(self) -> None:
        # Owning test_compute leaves test_second and test_other impacted in the
        # one file: one production edit yields exactly one notice naming
        # tests/test_app.py once with two unowned tests and no gap, from one
        # graph call, writing only the advisory's own cache. An identical edit
        # is scanned again but publishes nothing; a test-file edit and the mapped
        # GREEN neither advise nor scan — the production edit is the one trigger.
        marker = "UNOWNED_IMPACTED_TESTS_MISADVISED"
        self.begin_pass(*UNITTEST, COMPUTE)
        self.edit_compute()
        before = self.status()
        self.assertFalse(self.advisory_cache.is_file(), f"{marker}: cache present before any advisory")
        lines, scans = self.hook()
        self.assertEqual(len(lines), 1, marker)
        self.assertEqual(lines[0].count("tests/test_app.py"), 1, f"{marker}: {lines[0]}")
        self.assertIn("2 impacted tests not owned by the map", lines[0], marker)
        self.assertNotIn("gap", lines[0], marker)
        self.assertEqual(scans, 1, f"{marker}: {scans} detect-changes launches")
        self.assertTrue(self.advisory_cache.is_file(), f"{marker}: advisory did not write its cache")
        after = self.status()
        self.assertEqual((after["tdd"], after["passStartSnapshot"]),
                         (before["tdd"], before["passStartSnapshot"]), marker)
        stored = (self.advisory_cache.read_bytes(), self.advisory_cache.stat().st_mtime_ns)
        self.assertEqual(self.hook(), ([], 1), f"{marker}: an identical result must scan once and publish nothing")
        self.assertEqual((self.advisory_cache.read_bytes(), self.advisory_cache.stat().st_mtime_ns), stored, marker)
        # A test-file edit is reviewable but not a production edit (the same
        # exclusion production_changes applies), so it neither advises nor scans.
        (self.repo / "tests" / "test_app.py").write_text(TESTS + "\n# touched\n", encoding="utf-8")
        self.assertEqual(self.hook("tests/test_app.py"), ([], 0), f"{marker}: a test edit advised or scanned")
        self.assertEqual(self.green(*UNITTEST, COMPUTE), ([], 0, True), f"{marker}: the GREEN advised or scanned")

    def test_no_workflow_means_no_advice(self) -> None:
        marker = "ADVISED_WITHOUT_A_WORKFLOW"
        (self.repo / "loose.py").write_text("def loose():\n    return 1\n", encoding="utf-8")
        self.assertEqual(self.hook("loose.py"), ([], 0), marker)

    def test_complete_ownership_is_silent(self) -> None:
        # A pytest directory selection is recursive, so `pytest tests/` owns every
        # impacted test under tests/: the graph is still consulted once, and the
        # advisory is silent — the real runner->recursive->ownership wiring.
        marker = "ALL_OWNED_STILL_ADVISED"
        self.begin_pass(sys.executable, "-m", "pytest", "-q", "tests/")
        self.edit_compute()
        self.assertEqual(self.hook(), ([], 1), marker)

    def test_an_unknown_selection_never_owns(self) -> None:
        marker = "UNKNOWN_SELECTION_MANUFACTURED_OWNERSHIP"
        # -k is an option the parse does not resolve, so the selection is unknown
        # even though the run really executed the two failing tests.
        self.begin_pass(*UNITTEST, "-k", "compute", "tests.test_app")
        self.edit_compute()
        lines, _ = self.hook()
        self.assertEqual(len(lines), 1, marker)
        self.assertIn("tests/test_app.py", lines[0], marker)

    def test_a_unittest_package_selection_does_not_own_the_subtree(self) -> None:
        marker = "PACKAGE_SELECTION_OVER_OWNED_THE_SUBTREE"
        # `unittest tests <method>` runs only the method; the bare `tests`
        # package load is non-recursive, so it must not suppress the sibling
        # impacted tests the run never executed.
        self.begin_pass(*UNITTEST, "tests", COMPUTE)
        self.edit_compute()
        lines, _ = self.hook()
        self.assertEqual(len(lines), 1, marker)
        self.assertIn("tests/test_app.py", lines[0], marker)

    def test_each_gap_cause_is_named_and_never_blocks_the_pass(self) -> None:
        # tests.test_app owns every impacted test, so only a gap can speak. Three
        # causes are raised one at a time — an unindexed file (partial analysis),
        # an unwritable cache with the index intact, a swept pass-start index —
        # each named by its own reason; the GREEN then records normally.
        marker = "GAP_MISATTRIBUTED_OR_BLOCKING"
        self.begin_pass(*UNITTEST, "tests.test_app")
        self.edit_compute(unindexed=True)
        lines, scans = self.hook()
        self.assertEqual((len(lines), scans), (1, 1), f"{marker}: {lines}")
        self.assertIn("gap, the graph analysis is", lines[0], marker)
        (self.repo / "extra.py").unlink()
        before = self.status()["tdd"]
        # The atomic write cannot replace a directory, so the cache write fails.
        self.advisory_cache.unlink(missing_ok=True)
        self.advisory_cache.mkdir()
        lines, _ = self.hook()
        self.assertEqual(len(lines), 1, f"{marker}: {lines}")
        self.assertIn("gap, the advisory cache could not be written", lines[0], marker)
        self.assertEqual(self.status()["tdd"], before, marker)
        self.advisory_cache.rmdir()
        shutil.rmtree(Path(str(self.status()["passStartSnapshot"]["indexPath"])), ignore_errors=True)
        lines, _ = self.hook()
        self.assertEqual(len(lines), 1, f"{marker}: {lines}")
        self.assertIn("gap, the pass-start index could not be diffed", lines[0], marker)
        _, _, valid = self.green(*UNITTEST, "tests.test_app")
        self.assertTrue(valid, marker)
        self.assertEqual(self.status()["tdd"], "passed", marker)

    def test_bounded_output_shows_ten_escaped_paths(self) -> None:
        # Twelve unowned files: test_app, a tab-named test (git-valid; the
        # producer surfaces the control character verbatim) and ten more. The
        # notice shows ten sorted paths — the tab path among them, escaped so
        # the line stays control-free — and counts the two it omits.
        marker = "UNBOUNDED_OR_UNESCAPED_ADVISORY_OUTPUT"
        for index in range(10):
            (self.repo / "tests" / f"test_z{index:02d}.py").write_text(EXTRA, encoding="utf-8")
        (self.repo / "tests" / "test_tab\ttab.py").write_text(EXTRA, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "eleven more callers, one tab-named")
        self.begin_pass(*UNITTEST, COMPUTE)
        self.edit_compute()
        lines, _ = self.hook()
        self.assertEqual(len(lines), 1, marker)
        self.assertEqual(lines[0].count("tests/test_"), 10, f"{marker}: {lines[0]}")
        self.assertIn("tests/test_tab\\x09tab.py", lines[0], marker)
        self.assertNotIn("\t", lines[0], marker)
        self.assertIn("(and 2 more)", lines[0], marker)

    def test_revalidation_keeps_the_pass_start_baseline(self) -> None:
        # --revalidate re-indexes the dirty candidate, compute included; diffing
        # against that graph would find nothing changed.
        marker = "REVALIDATED_INDEX_HID_EARLIER_EDITS"
        self.begin_pass(*UNITTEST, COMPUTE)
        self.edit_compute()
        self.intake("--revalidate")
        lines, _ = self.hook()
        self.assertEqual(len(lines), 1, marker)
        self.assertIn("tests/test_app.py", lines[0], marker)

    def test_a_new_workflow_notifies_again(self) -> None:
        marker = "NEW_WORKFLOW_SUPPRESSED_BY_OLD_RESULT"
        self.begin_pass(*UNITTEST, COMPUTE)
        self.edit_compute()
        self.assertEqual(len(self.hook()[0]), 1, marker)
        # The next pass starts from a clean tree that already adds two, so its
        # RED needs a new expectation and its edit a new value.
        (self.repo / "tests" / "test_app.py").write_text(TESTS.replace("3", "4"), encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "next expectation")
        self.slug = "map-advisory-next"
        self.begin_pass(*UNITTEST, COMPUTE)
        self.edit_compute(3)
        lines, _ = self.hook()
        self.assertEqual(len(lines), 1, marker)
        self.assertIn("tests/test_app.py", lines[0], marker)

    def test_the_hook_delivers_under_a_live_mcp_holder(self) -> None:
        # The hook path delivers the notice while a live gitnexus MCP server
        # holds the same pass-start index open: no DB-lock hang and no silent
        # non-delivery. Latency is not measured here; the direct whole-advisory
        # acceptance receipts own that.
        marker = "ADAPTER_PATH_DID_NOT_DELIVER"
        self.begin_pass(*UNITTEST, COMPUTE)
        self.edit_compute()
        index_repo = str(self.status()["passStartSnapshot"]["indexRepo"])

        srv = subprocess.Popen(
            # The fixture environment, like every other subprocess here: the server
            # has to read the same registry the fixture's own intake wrote.
            ["gitnexus", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, env=self.env,
        )
        try:
            def rpc(obj: dict[str, object]) -> dict[str, object]:
                srv.stdin.write(json.dumps(obj) + "\n"); srv.stdin.flush()
                return json.loads(srv.stdout.readline())
            init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
            self.assertEqual((init.get("result") or {}).get("serverInfo", {}).get("name"), "gitnexus", marker)
            srv.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
            srv.stdin.flush()
            # Prove real graph access: a known symbol from this index comes back,
            # so the database is genuinely open while the hook's CLI call runs.
            ctx = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "context", "arguments": {"repo": index_repo, "name": "compute"}}})
            ctx_text = "".join(c.get("text", "") for c in (ctx.get("result") or {}).get("content", []))
            self.assertIn("app.py", ctx_text, marker)
            self.assertIsNone(srv.poll(), f"{marker}: MCP holder died before the hook")

            self.advisory_cache.unlink(missing_ok=True)
            lines, _ = self.hook()
            self.assertIsNone(srv.poll(), f"{marker}: MCP holder died during the hook")
            self.assertEqual(len(lines), 1, f"{marker}: no notice under the MCP holder")
        finally:
            for stream in (srv.stdin, srv.stdout):
                if stream is not None:
                    stream.close()
            srv.terminate()
            try:
                srv.wait(timeout=5)
            except subprocess.TimeoutExpired:
                srv.kill()

    def test_the_edit_survives_a_closed_stdout_reader(self) -> None:
        # The advisory's notice rides the hook's own stdout, so a lint-clean
        # eligible edit whose stdout reader closes must still exit 0 — the
        # delivery write is guarded, failing at the write with no buffered data
        # to re-raise at shutdown. A broken reader loses the notice, never the edit.
        marker = "EDIT_FAILS_ON_CLOSED_STDOUT"
        self.begin_pass(*UNITTEST, COMPUTE)
        self.edit_compute()
        self.advisory_cache.unlink(missing_ok=True)
        payload = json.dumps({"tool_input": {"file_path": str(self.repo / "app.py")}, "session_id": SESSION})
        proc = subprocess.Popen(
            [str(POST_EDIT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=self.repo, env=self.env)
        proc.stdout.close()  # the reader is gone before the hook writes its feedback
        _, err = proc.communicate(input=payload, timeout=120)
        self.assertEqual(proc.returncode, 0, f"{marker}: exit={proc.returncode} {err[-200:]}")
        self.assertNotIn("BrokenPipeError", err, marker)


if __name__ == "__main__":
    unittest.main()
