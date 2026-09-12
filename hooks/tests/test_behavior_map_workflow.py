#!/usr/bin/env python3
"""Public workflow proofs for preflight-owned Behavior Maps."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib.repo_identity import resolve_repo_identity  # noqa: E402
from hooks.lib.tdd_workflow import edit_blockers  # noqa: E402
from hooks.lib.workflow_state import (  # noqa: E402
    advisor_disposition,
    read_workflow,
    ready_for_edit,
    record_advisor_result,
)
from hooks.tests.support import build_document, pending_behavior, record_context_forge  # noqa: E402

WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"


class BehaviorMapWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-behavior-map-"))
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

    def tearDown(self) -> None:
        if self.previous_state_root is None:
            os.environ.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
        else:
            os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = self.previous_state_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args: str) -> None:
        result = subprocess.run(
            ["git", *args], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKFLOW), *args, "--repo", str(self.repo)],
            cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def begin_to_preflight(self, behavior_map: list[dict[str, object]]) -> tuple[str, str]:
        begun = self.cli("begin", "--slug", "behavior-map", "--intent", "change app value")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        state = json.loads(begun.stdout)
        slug, workflow_id = state["slug"], state["workflowId"]
        identity = record_context_forge(self.repo, self.tmp)
        record_advisor_result(identity, slug, workflow_id, "preflight", "codex-advisor", "completed")
        advisor_disposition(identity, slug, workflow_id, "preflight", "none")
        payload = self.tmp / "preflight.json"
        payload.write_text(
            json.dumps(build_document("behavior map test", behavior_map=behavior_map)),
            encoding="utf-8",
        )
        recorded = self.cli(
            "record-preflight", "--slug", slug, "--workflow-id", workflow_id,
            "--input", str(payload),
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        return slug, workflow_id

    def tdd(
        self,
        slug: str,
        phase: str,
        behavior_id: str,
        script: str,
    ) -> subprocess.CompletedProcess[str]:
        probe = self.repo / "test_behavior_probe.py"
        probe.write_text(
            "import unittest\n\n"
            "class BehaviorProbe(unittest.TestCase):\n"
            "    def test_behavior(self):\n"
            + textwrap.indent(script, "        ")
            + "\n",
            encoding="utf-8",
        )
        return subprocess.run(
            [
                sys.executable,
                str(WORKFLOW),
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
                sys.executable,
                "-m",
                "unittest",
                "test_behavior_probe.BehaviorProbe.test_behavior",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def update_map(
        self,
        slug: str,
        workflow_id: str,
        value: dict[str, object],
    ) -> subprocess.CompletedProcess[str]:
        payload = self.tmp / "map-update.json"
        payload.write_text(json.dumps(value), encoding="utf-8")
        return self.cli(
            "tdd-map", "--slug", slug, "--workflow-id", workflow_id,
            "--input", str(payload),
        )

    def test_consecutive_hook_obligations_are_bounded_without_extra_edit_work(self) -> None:
        # Declared before measurement: 82 rows, complete displayed guarantees,
        # <=2,048 UTF-8 bytes and <=2 seconds/hook; no extra reads/processes
        # versus the supplied old checkout. Audit/SQLite trace observe the real
        # hook without replacing any collaborator or changing its execution.
        contract = pending_behavior("BM_ATTACK")
        keep = {**pending_behavior("BM_KEEP"), "kind": "preservation", "status": "already-satisfied",
                "behavior": "Ordinary writes remain visible through the public reader — 保持",
                "expected": "A second reader sees the committed value", "evidence": "initial preservation"}
        omitted = {**keep, "id": "BM_OMITTED", "status": "omitted", "evidence": "governing exclusion"}
        rows = [contract, keep, omitted, *[
            {**keep, "id": f"BM_KEEP_{index}", "behavior": f"Preserve\t supported\x1b outcome {index}"}
            for index in range(78)], {**keep, "id": "BM_LONG", "behavior": "漢字" * 3000}]
        slug, wid = self.begin_to_preflight(rows[:3])
        result = self.tdd(slug, "red", "BM_ATTACK", "import app; assert app.value == 2, 'VALUE_NOT_TWO'")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        request = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(self.repo / "app.py")}})
        instrument = r'''import json, runpy, sys, time
cost = {"dbReads": 0, "subprocesses": 0}
def sql(statement):
    if statement.lstrip().upper().startswith("SELECT"):
        cost["dbReads"] += 1
def audit(event, args):
    if event == "subprocess.Popen":
        cost["subprocesses"] += 1
def trace(frame, event, arg):
    if frame.f_code.co_name == "_open_connection":
        connection = frame.f_locals.get("connection")
        if event == "line" and connection is not None:
            connection.set_trace_callback(sql)
            return None
        return trace
    return None
sys.addaudithook(audit)
sys.settrace(trace)
start = time.perf_counter()
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
finally:
    cost["latencySeconds"] = time.perf_counter() - start
    print("HOOK_COST " + json.dumps(cost), file=sys.stderr)
'''
        measurements = {}
        contexts = []
        targets = {"candidate": ROOT}
        old = os.environ.get("WORKFLOW_BASE_CHECKOUT")
        if old:
            targets["old"] = Path(old)
        for scale in (3, len(rows)):
            if scale > 3:
                update = self.update_map(slug, wid, {
                    "reassessment": "retain a larger map for the same hook operation",
                    "items": rows[3:],
                })
                self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
            before = {}
            for action, key in (("status", "workflowId"), ("history", "events")):
                captured = self.cli(action)
                self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
                before[action] = json.loads(captured.stdout)
                self.assertIsInstance(before[action], dict)
                self.assertIn(key, before[action])
            for label, root in targets.items():
                runs = []
                contexts = []
                for _ in range(2):
                    result = subprocess.run(
                        [sys.executable, "-c", instrument, str(root / "hooks/rcf-intake-gate.py")],
                        input=request, cwd=self.repo, env=self.env, text=True,
                        capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    cost = json.loads(result.stderr.split("HOOK_COST ")[-1])
                    output = json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else {}
                    self.assertNotIn("permissionDecision", output)
                    context = output.get("additionalContext", "")
                    cost["emittedBytes"] = len(context.encode("utf-8"))
                    runs.append(cost)
                    contexts.append(context)
                measurements[label] = runs
                if label == "candidate":
                    candidate_contexts = contexts
            from hooks.lib.state_store import _active_candidate_tree
            observations = {}
            if old:
                observations = {
                    field: max(run[field] for run in measurements["candidate"])
                    - max(run[field] for run in measurements["old"])
                    for field in ("dbReads", "subprocesses")
                }
            print("HOOK_RESOURCE " + json.dumps({
                "scale": scale, "limitBytes": 2048, "limitSeconds": 2,
                "limitsAdditional": {"dbReads": 0, "subprocesses": 0},
                "observedAdditional": observations if old else None,
                "targets": {label: _active_candidate_tree(resolve_repo_identity(root))
                            for label, root in targets.items()},
                "measurements": measurements,
            }), flush=True)
            for action, key in (("status", "workflowId"), ("history", "events")):
                captured = self.cli(action)
                self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
                after = json.loads(captured.stdout)
                self.assertIsInstance(after, dict)
                self.assertIn(key, after)
                self.assertEqual(after, before[action], "REMINDER_CHANGED_" + action)
            for context, cost in zip(candidate_contexts, measurements["candidate"], strict=True):
                self.assertTrue(context, "ORDERED_MAP_HAS_NO_OBLIGATION_REMINDER")
                self.assertLessEqual(cost["emittedBytes"], 2048, "OBLIGATION_DIGEST_OVER_BUDGET")
                self.assertLessEqual(cost["latencySeconds"], 2, "EDIT_REMINDER_TOO_SLOW")
                self.assertIn(keep["behavior"], context)
                self.assertIn(keep["expected"], context)
                if scale == 3:
                    self.assertIn("non-applicable", context)
                    self.assertIn("BM_OMITTED", context)
                self.assertIn("not displayed", context.lower())
                self.assertNotIn("BM_LONG", context)
                self.assertNotIn("\x1b", context)
                self.assertNotIn("\t", context)
                self.assertNotIn("missing before", context)
            if old:
                for field in ("dbReads", "subprocesses"):
                    self.assertLessEqual(max(run[field] for run in measurements["candidate"]),
                                         max(run[field] for run in measurements["old"]), field)

    def test_repeated_red_keeps_only_its_active_owner_history(self) -> None:
        rows = [pending_behavior(name) for name in ("BM_A", "BM_B", "BM_KEEP")]
        rows[-1]["kind"] = "preservation"
        slug, _ = self.begin_to_preflight(rows)
        fail = "import app; assert app.value == 2, 'VALUE_NOT_TWO'"
        for phase, owner, script, expected in (
            ("red", "BM_A", fail, 0),
            ("green", "BM_A", fail, 2),  # Keep this unsuccessful run in the prefix.
            ("red", "BM_KEEP", "import app; assert app.value == 1, 'VALUE_NOT_TWO'", 0),
        ):
            result = self.tdd(slug, phase, owner, script)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        state = read_workflow(resolve_repo_identity(self.repo))
        evidence = self.cli("evidence", "--evidence-id", str(state["tddEvidence"]))
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        before = json.loads(evidence.stdout)["document"]
        self.assertEqual([run["behaviorId"] for run in before["runs"]], ["BM_A", "BM_A", "BM_KEEP"])
        result = self.tdd(slug, "red", "BM_A", fail)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        repeated = read_workflow(resolve_repo_identity(self.repo))
        evidence = self.cli("evidence", "--evidence-id", str(repeated["tddEvidence"]))
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        after = json.loads(evidence.stdout)["document"]
        self.assertEqual(after["runs"][:-1], before["runs"], "SAME_OWNER_RED_DROPPED_RUNS")
        self.assertEqual(after["runs"][-1]["behaviorId"], "BM_A")
        self.assertEqual(repeated["tddCycleCount"], state["tddCycleCount"])
        for key in ("activeBehaviorId", "command", "surface"):
            self.assertEqual(after[key], before[key])
        result = self.tdd(slug, "red", "BM_B", fail)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        other = read_workflow(resolve_repo_identity(self.repo))
        evidence = self.cli("evidence", "--evidence-id", str(other["tddEvidence"]))
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        document = json.loads(evidence.stdout)["document"]
        self.assertEqual(document["activeBehaviorId"], "BM_B")
        self.assertEqual([run["behaviorId"] for run in document["runs"]], ["BM_B"])
        self.assertEqual(other["tddCycleCount"], state["tddCycleCount"] + 1)
        original = self.cli("evidence", "--evidence-id", str(state["tddEvidence"]))
        self.assertEqual(original.returncode, 0, original.stderr)
        self.assertEqual(json.loads(original.stdout)["document"], before)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        for owner, expected_status in (("BM_B", "pending"), ("BM_A", "passed")):
            result = self.tdd(slug, "green", owner, fail)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = read_workflow(resolve_repo_identity(self.repo))
            evidence = self.cli("evidence", "--evidence-id", str(state["tddEvidence"]))
            self.assertEqual(evidence.returncode, 0, evidence.stderr)
            self.assertEqual(json.loads(evidence.stdout)["document"]["status"], expected_status)

    def test_preflight_requires_a_non_generic_behavior_map(self) -> None:
        begun = self.cli("begin", "--slug", "map-contract")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        state = json.loads(begun.stdout)
        identity = record_context_forge(self.repo, self.tmp)
        record_advisor_result(
            identity, state["slug"], state["workflowId"],
            "preflight", "codex-advisor", "completed",
        )
        advisor_disposition(identity, state["slug"], state["workflowId"], "preflight", "none")

        missing = build_document("missing map", behavior_map=[pending_behavior()])
        missing.pop("behaviorMap")
        payload = self.tmp / "preflight.json"
        payload.write_text(json.dumps(missing), encoding="utf-8")
        refused = self.cli(
            "record-preflight", "--slug", state["slug"],
            "--workflow-id", state["workflowId"], "--input", str(payload),
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("behaviorMap", refused.stderr)

        generic = build_document(
            "generic failure",
            behavior_map=[pending_behavior(red_failure="AttributeError")],
        )
        payload.write_text(json.dumps(generic), encoding="utf-8")
        refused = self.cli(
            "record-preflight", "--slug", state["slug"],
            "--workflow-id", state["workflowId"], "--input", str(payload),
        )
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("product behavior", refused.stderr)

    def test_missing_api_failure_is_not_red_and_valid_red_unlocks_one_slice(self) -> None:
        behavior = pending_behavior(
            "BM_ROLLBACK",
            behavior="checkpoint rollback restores the database state",
            seam="Database checkpoint public Interface",
            expected="the database equals its pre-checkpoint state",
            red_failure="DATABASE_STATE_NOT_RESTORED",
        )
        slug, _ = self.begin_to_preflight([behavior])

        missing_api = self.tdd(
            slug,
            "red",
            "BM_ROLLBACK",
            "import app; app.enable_safe_import()",
        )
        self.assertEqual(missing_api.returncode, 2, missing_api.stdout + missing_api.stderr)
        self.assertIn("AttributeError", missing_api.stdout)
        self.assertIn("RED must fail for the expected reason", missing_api.stderr)
        state = read_workflow(resolve_repo_identity(self.repo))
        self.assertEqual(state["tdd"], "pending", "REFUSED_ATTEMPT_ADVANCED_TDD_PHASE")
        self.assertNotIn("tddCycleCount", state)
        self.assertTrue(edit_blockers(resolve_repo_identity(self.repo), state))
        ready, missing = ready_for_edit(resolve_repo_identity(self.repo), "app.py")
        self.assertFalse(ready, "REFUSED_ATTEMPT_ADVANCED_TDD_PHASE")
        self.assertTrue(any("TDD RED" in item for item in missing), "REFUSED_ATTEMPT_ADVANCED_TDD_PHASE")

        red = self.tdd(
            slug,
            "red",
            "BM_ROLLBACK",
            "import app; assert app.value == 2, 'DATABASE_STATE_NOT_RESTORED'",
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        state = read_workflow(resolve_repo_identity(self.repo))
        self.assertEqual(state["tdd"], "in-progress")
        self.assertEqual(state["tddCycleCount"], 1)
        self.assertEqual(edit_blockers(resolve_repo_identity(self.repo), state), [])

    def test_reassessment_can_add_the_next_architecture_falsifier(self) -> None:
        behavior = pending_behavior("BM_VALUE")
        slug, workflow_id = self.begin_to_preflight([behavior])
        self.assertEqual(
            self.tdd(
                slug, "red", "BM_VALUE",
                "import app; assert app.value == 2, 'VALUE_NOT_TWO'",
            ).returncode,
            0,
        )
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.assertEqual(
            self.tdd(
                slug, "green", "BM_VALUE",
                "import app; assert app.value == 2, 'VALUE_NOT_TWO'",
            ).returncode,
            0,
        )
        next_item = pending_behavior(
            "BM_ATOMIC",
            behavior="rerouted inner operation remains atomic when its failure is caught",
            seam="public operation through the new transaction path",
            expected="no partial inner write survives",
            red_failure="PARTIAL_INNER_WRITE_SURVIVED",
            basis="touched-Seam preservation",
        )
        assessed = self.update_map(
            slug,
            workflow_id,
            {
                "sourceBehaviorId": "BM_VALUE",
                "reassessment": "GREEN rerouted transaction behavior; preserve inner atomicity.",
                "items": [next_item],
            },
        )
        self.assertEqual(assessed.returncode, 0, assessed.stdout + assessed.stderr)
        identity = resolve_repo_identity(self.repo)
        state = read_workflow(identity)
        self.assertEqual(state["tdd"], "in-progress")
        evidence = self.cli("evidence", "--evidence-id", str(state["tddEvidence"]))
        self.assertEqual(evidence.returncode, 0, evidence.stdout + evidence.stderr)
        self.assertEqual(json.loads(evidence.stdout)["document"]["status"],
                         json.loads(assessed.stdout)["status"], "CURRENT_MAP_STATUS_STALE")
        self.assertIn("BM_ATOMIC", edit_blockers(identity, state)[0])
        refused = self.cli("complete", "--slug", slug, "--workflow-id", workflow_id)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("BM_ATOMIC", refused.stderr)


if __name__ == "__main__":
    unittest.main()
