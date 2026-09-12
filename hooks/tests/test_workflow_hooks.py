#!/usr/bin/env python3
"""Real hook contracts for workflow sequencing and invalidation."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib.repo_identity import resolve_repo_identity  # noqa: E402
from hooks.tests.support import (  # noqa: E402
    build_document,
    build_no_change_document,
    pending_behavior,
    record_context_forge,
    run_post_edit,
)
from hooks.lib.workflow_state import record_base_oid, set_phase  # noqa: E402

WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"
QUALITY_GATE = ROOT / "skills" / "production-code" / "scripts" / "code_quality_gate.py"
INTAKE = ROOT / "hooks" / "rcf-intake-gate.py"
RCF_BOOTSTRAP = ROOT / "skills" / "repo-context-forge" / "scripts" / "bootstrap.py"
ADVISOR = ROOT / "skills" / "codex-advisor" / "scripts" / "ask-codex-advisor.sh"
SESSION = "real-hook-session"


class HookHarness(unittest.TestCase):
    """Fixture repository, state root, and workflow drivers shared by the hook
    suites; carries no tests of its own so subclasses never duplicate them."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-hooks-"))
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

    def git(self, *args: str, repo: Path | None = None) -> None:
        result = subprocess.run(
            ["git", *args], cwd=repo or self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def second_repo(self, name: str) -> Path:
        """A committed Git repository that is not the one the session edits."""
        repo = self.tmp / name
        repo.mkdir()
        self.git("init", "-q", repo=repo)
        self.git("config", "user.email", "test@example.invalid", repo=repo)
        self.git("config", "user.name", "Workflow Harness", repo=repo)
        (repo / "other.py").write_text("value = 2\n", encoding="utf-8")
        self.git("add", "other.py", repo=repo)
        self.git("commit", "-q", "-m", "base", repo=repo)
        return repo

    def state(self, *args: str, repo: Path | None = None) -> subprocess.CompletedProcess[str]:
        values = list(args)
        if values and values[0] == "advisor-result" and "--design-declaration" not in values:
            values += ["--design-declaration", str(self.design_declaration)]
        return subprocess.run(
            [sys.executable, str(WORKFLOW), *values, "--repo", str(repo or self.repo)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def intake(self, relative: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INTAKE)], cwd=self.repo, env=self.env, text=True,
            input=json.dumps({"tool_input": {"file_path": str(self.repo / relative)}}),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def post_edit(self, relative: str, *, repo: Path | None = None,
                  session: str | None = SESSION,
                  env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return run_post_edit(repo or self.repo, self.env, relative, session=session, env_extra=env_extra)

    def owner_phase(self, phase: str, status: str, *, findings: str | None = None) -> None:
        set_phase(resolve_repo_identity(self.repo), phase, status, findings=findings)

    def rewrite_latest_state(self, update) -> None:
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

    def assert_obligations_only(self, *identifiers: str) -> None:
        before = {}
        for action, key in (("status", "workflowId"), ("history", "events")):
            result = self.state(action)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            before[action] = json.loads(result.stdout)
            self.assertIsInstance(before[action], dict)
            self.assertIn(key, before[action])
        result = self.intake("app.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output)
        context = output["additionalContext"]
        self.assertNotIn("missing before", context)
        red_ids = re.findall(r"(?m)^([A-Z][A-Z0-9_-]*) \[red;", context)
        self.assertCountEqual(red_ids, identifiers, context)
        for action, key in (("status", "workflowId"), ("history", "events")):
            result = self.state(action)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            after = json.loads(result.stdout)
            self.assertIsInstance(after, dict)
            self.assertIn(key, after)
            self.assertEqual(after, before[action])

    def record_preflight_evidence(self, slug: str, wid: str, behavior_map: list | None = None) -> None:
        if behavior_map is None:
            document = build_no_change_document("hook-suite setup")
        else:
            document = build_document("hook-suite setup", behavior_map=behavior_map)
        doc_path = self.tmp / "preflight-doc.json"
        doc_path.write_text(json.dumps(document), encoding="utf-8")
        recorded = subprocess.run(
            [sys.executable, str(WORKFLOW), "record-preflight", "--repo", str(self.repo),
             "--slug", slug, "--workflow-id", wid, "--input", str(doc_path)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)

    def record_gate_evidence(self, slug: str, wid: str) -> None:
        gate = subprocess.run(
            [sys.executable, str(QUALITY_GATE), "check", "--repo", str(self.repo), "--json"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        gate_path = self.tmp / "gate-verdict.json"
        gate_path.write_text(gate.stdout, encoding="utf-8")
        recorded = subprocess.run(
            [sys.executable, str(WORKFLOW), "record-production-code", "--repo", str(self.repo),
             "--slug", slug, "--workflow-id", wid, "--input", str(gate_path)],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)

    def run_verification(self, slug: str) -> None:
        verified = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo), "--slug", slug,
             "--", sys.executable, "-c", "pass"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        quality = subprocess.run(
            [sys.executable, str(WORKFLOW), "verify", "--repo", str(self.repo), "--slug", slug,
             "--kind", "quality-gate", "--base-ref", "HEAD"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(quality.returncode, 0, quality.stdout + quality.stderr)

    def complete_workflow(self, slug: str = "hook-sequence", *, resume: bool = False, finish: bool = True) -> None:
        if resume:
            wid = json.loads(self.state("status").stdout)["workflowId"]
        else:
            begun = self.state("begin", "--slug", slug)
            self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
            wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        transitions = (
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "preflight", "--findings", "none"),
            ("set-phase", "--phase", "implementation", "--status", "passed"),
        )
        for transition in transitions:
            if transition[0] == "set-phase":
                # These tests exercise the hooks; the evidence phases advance
                # through the real producers, whose contracts are proven in
                # test_pass_lifecycle. Keyed on the step rather than its position,
                # so the sequence can change without silently reordering this.
                self.record_preflight_evidence(slug, wid)
                self.owner_phase("tdd", "not-required")
                self.record_gate_evidence(slug, wid)
            result = self.state(*transition)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        tail = [
            ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--source", "codex-advisor", "--verdict", "commit-ready"),
            ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--findings", "none"),
        ]
        if finish:
            tail.append(("complete",))
        for transition in tail:
            result = self.state(*transition)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

class WorkflowHookTests(HookHarness):
    def test_the_edit_gate_advises_missing_steps_instead_of_denying(self) -> None:
        marker = "GATE_STILL_DENIES_MISSING_STEPS"

        def advice(relative: str) -> str:
            result = self.intake(relative)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(result.stdout, marker + ": no advice for " + relative)
            output = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertNotIn("permissionDecision", output, marker + ": " + result.stdout)
            self.assertEqual(output["hookEventName"], "PreToolUse", marker)
            return output["additionalContext"]

        self.assertIn("active workflow", advice("app.py"), marker)
        self.assertIn("active workflow", advice("requirements.txt"), marker)
        docs = self.intake("notes.md")
        self.assertEqual(docs.returncode, 0, docs.stdout + docs.stderr)
        self.assertEqual(docs.stdout, "")

        begun = self.state("begin", "--slug", "tdd-ordering")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        for transition in (
            ("advisor-result", "--slug", "tdd-ordering", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "tdd-ordering", "--workflow-id", wid, "--stage", "preflight", "--findings", "none"),
        ):
            result = self.state(*transition)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("production preflight", advice("app.py"), marker)
        self.record_preflight_evidence("tdd-ordering", wid, behavior_map=[pending_behavior("BM_HOOK", behavior="app value must be 2", seam="app module import", expected="value equals 2", red_failure="VALUE_NOT_TWO")])
        self.assertIn("TDD", advice("app.py"), marker)

        test_edit = self.intake("tests/test_app.py")
        self.assertEqual(test_edit.returncode, 0, test_edit.stdout + test_edit.stderr)
        self.assertEqual(test_edit.stdout, "", "test-file edit before RED was advised")

        red = self.red("tdd-ordering")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        self.assert_obligations_only("BM_HOOK")

        self.complete_workflow()
        self.assertIn("new active workflow", advice("app.py"), marker)

    def app_value_command(self) -> tuple[str, ...]:
        (self.repo / "test_app_behavior.py").write_text(
            "import unittest\n"
            "import app\n"
            "class AppValueTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(app.value, 2, 'VALUE_NOT_TWO')\n",
            encoding="utf-8",
        )
        return (
            sys.executable,
            "-m",
            "unittest",
            "test_app_behavior.AppValueTests.test_value",
        )

    def red(self, slug: str) -> subprocess.CompletedProcess[str]:
        """Drive one real valid mapped RED through workflow.py tdd."""
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(WORKFLOW), "tdd", "--cwd", str(self.repo), "--slug", slug,
             "--phase", "red", "--behavior-id", "BM_HOOK",
             "--", *self.app_value_command()],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_a_clean_edit_emits_no_gate_feedback(self) -> None:
        # Issue #182: per-edit feedback is limited to genuinely local signals.
        # A lint-clean edit produces no output at all — full gate analysis and
        # its warnings live at the verification boundaries.
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        result = self.post_edit("app.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "", "a clean edit re-injected gate feedback")

    def test_python_edit_surfaces_ruff_findings_in_the_feedback(self) -> None:
        # Real-time lint: a Python edit whose content pyflakes rejects must
        # surface the ruff finding line on the hook's feedback channel, beside
        # the gate warnings, while the hook still exits zero.
        (self.repo / "app.py").write_text("value = undefined_name\n", encoding="utf-8")
        result = self.post_edit("app.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), "hook emitted no feedback at all")
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("F821", context)

    def test_uppercase_python_suffix_still_gets_lint_feedback(self) -> None:
        # is_code_path lowercases the suffix, so module.PY is a code path; the
        # lint guard must classify it the same way or the file gets a gate run
        # with silently missing lint.
        (self.repo / "module.PY").write_text("value = undefined_name\n", encoding="utf-8")
        result = self.post_edit("module.PY")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), "hook emitted no feedback for the .PY edit")
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("F821", context)

    def test_gate_excluded_python_paths_still_get_lint_feedback(self) -> None:
        # Gate path policy exempts docs and scratch; lint policy does not — a
        # Python edit there still surfaces its findings without a gate run.
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "snippet.py").write_text("value = undefined_name\n", encoding="utf-8")
        result = self.post_edit("docs/snippet.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), "hook emitted no feedback for the excluded python path")
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("F821", context)
        self.assertNotIn("production quality gate warnings", context)

    def test_conflict_markers_surface_as_lint_findings(self) -> None:
        # The genuinely-local error class stays per-edit through ruff: conflict
        # markers are a syntax error the single-file lint names immediately,
        # with no gate subprocess involved.
        (self.repo / "app.py").write_text(
            "<<<<<<< HEAD\nx = undefined_thing\n=======\ny = 2\n>>>>>>> other\n",
            encoding="utf-8",
        )
        result = self.post_edit("app.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("invalid-syntax", context)
        self.assertNotIn("production-code gate FAILED", result.stdout + result.stderr)

    def broken_ruff_edit(self, name: str, shim: bytes, mode: int) -> subprocess.CompletedProcess[str]:
        """A Python edit in an environment whose only PATH ruff is the given
        broken shim — the shared seam for every launch-failure cause."""
        shim_dir = self.tmp / name
        shim_dir.mkdir()
        (shim_dir / "ruff").write_bytes(shim)
        (shim_dir / "ruff").chmod(mode)
        ruff_dir = os.path.realpath(os.path.dirname(shutil.which("ruff") or self.fail("suite requires ruff")))
        entries = [str(shim_dir)] + [p for p in self.env["PATH"].split(os.pathsep) if os.path.realpath(p) != ruff_dir]
        (self.repo / "app.py").write_text(
            "<<<<<<< HEAD\nx = 1\n=======\ny = 2\n>>>>>>> other\n", encoding="utf-8",
        )
        return self.post_edit("app.py", env_extra={"PATH": os.pathsep.join(entries)})

    def test_malformed_ruff_executable_names_the_gap(self) -> None:
        # A malformed +x ruff (exec format error) is the third measured
        # launch-failure cause: the notice appears on the feedback channel
        # instead of the hook crashing.
        result = self.broken_ruff_edit("malformed-bin", b"\x7fGARBAGE-not-a-binary\n", 0o755)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ruff could not run: python lint skipped", context)

    def test_unrunnable_ruff_names_the_gap(self) -> None:
        # A PATH-resolvable but non-executable ruff must not crash the hook:
        # the lint gap is named on the feedback channel.
        result = self.broken_ruff_edit("noexec-bin", b"#!/bin/sh\nexit 0\n", 0o644)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ruff could not run: python lint skipped", context)

    def test_missing_ruff_is_named_not_silently_skipped(self) -> None:
        # Honest absence: without ruff on PATH the hook names the lint gap on
        # its feedback channel instead of faking coverage, and still exits zero.
        ruff_dir = os.path.realpath(os.path.dirname(shutil.which("ruff") or self.fail("suite requires ruff")))
        entries = [p for p in self.env["PATH"].split(os.pathsep) if os.path.realpath(p) != ruff_dir]
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        result = self.post_edit("app.py", env_extra={"PATH": os.pathsep.join(entries)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ruff could not run: python lint skipped", context)

    def test_the_recorded_base_stays_first_wins(self) -> None:
        # The recorder is the one production writer of the pass base (the
        # bootstrap Interface is proven in test_repoforge_workflow): it demands
        # a commit OID and keeps the first record; the typed quality-gate run
        # consumes it at verification.
        begun = self.state("begin", "--slug", "with-base")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        identity = resolve_repo_identity(self.repo)
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        ).stdout.strip()
        other = subprocess.run(
            ["git", "commit-tree", "HEAD^{tree}", "-p", "HEAD"],
            cwd=self.repo, env=self.env, text=True, input="other base\n",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        ).stdout.strip()
        self.assertNotEqual(other, base)
        with self.assertRaises(ValueError):
            record_base_oid(identity, "with-base", wid, "base-main")
        self.assertEqual(record_base_oid(identity, "with-base", wid, base).get("baseOid"), base)
        self.assertEqual(record_base_oid(identity, "with-base", wid, other).get("baseOid"), base)

    def test_non_code_production_edit_invalidates_but_docs_do_not(self) -> None:
        self.complete_workflow(finish=False)
        (self.repo / "requirements.txt").write_text("package==1\n", encoding="utf-8")
        changed = self.post_edit("requirements.txt")
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        state = json.loads(self.state("status").stdout)
        self.assertEqual(state["phase"], "implementation")
        self.assertEqual(state["finalReview"], {"source": None, "status": "pending", "findings": "pending"})

        self.complete_workflow()
        (self.repo / "notes.md").write_text("documentation\n", encoding="utf-8")
        docs = self.post_edit("notes.md")
        self.assertEqual(docs.returncode, 0, docs.stdout + docs.stderr)
        state = json.loads(self.state("status").stdout)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["nextAction"], "delivery-and-reviewer-completion")

    def test_governance_doc_edit_is_admitted_then_invalidates_review_readiness(self) -> None:
        self.complete_workflow()
        governance = self.repo / "skills" / "diagnose" / "SKILL.md"
        governance.parent.mkdir(parents=True)

        admitted = self.intake("skills/diagnose/SKILL.md")
        self.assertEqual(admitted.returncode, 0, admitted.stdout + admitted.stderr)
        self.assertEqual(admitted.stdout, "")

        governance.write_text("updated agent behavior\n", encoding="utf-8")
        changed = self.post_edit("skills/diagnose/SKILL.md")
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        state = json.loads(self.state("status").stdout)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["nextAction"], "verification")
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"})
        self.assertEqual(state["finalReview"], {"source": None, "status": "pending", "findings": "pending"})

        advised = self.intake("app.py")
        self.assertEqual(advised.returncode, 0, advised.stdout + advised.stderr)
        output = json.loads(advised.stdout)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output, advised.stdout)
        self.assertIn("new active workflow", output["additionalContext"])

    def test_the_gate_names_a_revalidation_window_distinctly(self) -> None:
        marker = "REVALIDATION_ADVICE_INDISTINCT"
        self.complete_workflow()
        completed = json.loads(self.intake("app.py").stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new active workflow", completed, marker)
        self.assertNotIn("revalidation", completed, marker + ": " + completed)
        governance = self.repo / "skills" / "diagnose" / "SKILL.md"
        governance.parent.mkdir(parents=True)
        governance.write_text("updated agent behavior\n", encoding="utf-8")
        self.assertEqual(self.post_edit("skills/diagnose/SKILL.md").returncode, 0)
        revalidating = json.loads(self.intake("app.py").stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("new active workflow", revalidating, marker)
        self.assertIn("revalidation", revalidating, marker + ": " + revalidating)

    def test_first_governance_edit_resumes_at_the_first_pending_phase(self) -> None:
        begun = self.state("begin", "--slug", "governance-sequence")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        for transition in (
            ("advisor-result", "--slug", "governance-sequence", "--workflow-id", wid, "--stage", "preflight", "--source", "codex-advisor", "--verdict", "completed"),
            ("advisor-disposition", "--slug", "governance-sequence", "--workflow-id", wid, "--stage", "preflight", "--findings", "none"),
        ):
            result = self.state(*transition)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.record_preflight_evidence("governance-sequence", wid)

        governance = self.repo / "CLAUDE.md"
        governance.write_text("updated agent behavior\n", encoding="utf-8")
        changed = self.post_edit("CLAUDE.md")
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        state = json.loads(self.state("status").stdout)
        self.assertEqual(state["implementation"], "pending")
        self.assertEqual(state["nextAction"], "tdd")

    def test_shipped_hooks_do_not_intercept_bash_or_git(self) -> None:
        settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        pre_tool = settings["hooks"]["PreToolUse"]
        self.assertFalse(any(entry.get("matcher") == "Bash" for entry in pre_tool))
        self.assertFalse((ROOT / ".githooks").exists())
        self.assertFalse((ROOT / "hooks" / "repoforge-commit-gate.sh").exists())
        self.assertFalse((ROOT / "hooks" / "codex-challenge-commit-gate.sh").exists())

    def test_sqlite_state_is_durable_without_a_precompact_flush_hook(self) -> None:
        self.assertFalse((ROOT / "hooks" / "pre-compact-flush.py").exists())
        begun = self.state("begin", "--slug", "compact-state")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        before = json.loads(begun.stdout)

        # A fresh process reads the committed event directly. Durability is the
        # public contract; rewriting a JSON snapshot before compaction was only
        # machinery for the superseded store.
        after = json.loads(self.state("status").stdout)
        for field in ("slug", "workflowId", "phase", "nextAction", "finalReview"):
            self.assertEqual(after[field], before[field])
        database = (Path(self.env["DEVIN_WORKFLOW_STATE_ROOT"])
                    / resolve_repo_identity(self.repo).key / "workflow.sqlite3")
        self.assertTrue(database.is_file())

        settings = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        self.assertNotIn("PreCompact", settings["hooks"])

class PerEditOverheadTests(HookHarness):
    """Issue 182: per-edit path carries only the cheap transition and local
    signals; full gate authority lives at the existing boundaries."""

    def history_length(self, kind: str | None = None) -> int:
        events = json.loads(self.state("history").stdout)["events"]
        return len([e for e in events if kind is None or e.get("kind") == kind])

    def test_a_gate_failing_ruff_clean_edit_gets_local_feedback_only(self) -> None:
        marker = "PER_EDIT_GATE_SUBPROCESS_STILL_CHARGED"
        self.complete_workflow(finish=False)
        escape = "TO" + "DO"
        (self.repo / "app.py").write_text(f"value = 2  # {escape}: escape\n", encoding="utf-8")
        result = self.post_edit("app.py")
        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, marker + ": " + combined)
        self.assertNotIn("production quality gate", combined, marker)
        self.assertNotIn("production-code gate FAILED", combined, marker)
        state = json.loads(self.state("status").stdout)
        self.assertEqual(state["phase"], "implementation", marker)
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"}, marker)

    def test_a_second_dirty_edit_appends_no_ledger_event(self) -> None:
        marker = "REDUNDANT_INVALIDATION_COMMITTED"
        self.complete_workflow(finish=False)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        first = self.post_edit("app.py")
        self.assertEqual(first.returncode, 0, marker + ": " + first.stdout + first.stderr)
        before = self.history_length()
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        second = self.post_edit("app.py")
        self.assertEqual(second.returncode, 0, marker + ": " + second.stdout + second.stderr)
        self.assertEqual(self.history_length(), before,
                         marker + ": a no-op dirty edit committed a ledger event")

    def test_a_repeated_governance_edit_appends_no_ledger_event(self) -> None:
        marker = "REDUNDANT_GOVERNANCE_INVALIDATION_COMMITTED"
        self.complete_workflow(finish=False)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.post_edit("app.py")
        (self.repo / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
        self.post_edit("CLAUDE.md")
        before = self.history_length()
        (self.repo / "CLAUDE.md").write_text("# rules v2\n", encoding="utf-8")
        repeated = self.post_edit("CLAUDE.md")
        self.assertEqual(repeated.returncode, 0, marker + ": " + repeated.stdout + repeated.stderr)
        self.assertEqual(self.history_length(), before,
                         marker + ": a no-op governance edit committed a ledger event")

    def test_revalidate_refuses_without_an_active_workflow(self) -> None:
        marker = "REVALIDATE_FLAG_ABSENT_OR_RUNS_PRODUCER_BLIND"
        bare = self.second_repo("no-workflow")
        result = subprocess.run(
            [sys.executable, str(RCF_BOOTSTRAP), "--repo", str(bare), "--revalidate"],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        tail = (result.stderr.strip().splitlines() or [""])[-1]
        self.assertEqual(result.returncode, 2, marker + ": " + tail)
        self.assertIn("revalidate", tail.lower(), marker + ": " + tail)
        self.assertIn("workflow", tail.lower(), marker + ": " + tail)

    def test_revalidate_pins_every_mode_occurrence_to_local(self) -> None:
        marker = "REPEATED_MODE_DEFEATS_FORCED_LOCAL"
        bare = self.second_repo("mode-pin")
        # The producer's argparse honors the last --mode, so the wrapper must
        # refuse on any non-local occurrence, not just the first.
        for shape in (["--mode", "pr"], ["--mode", "local", "--mode", "pr"]):
            result = subprocess.run(
                [sys.executable, str(RCF_BOOTSTRAP), "--repo", str(bare),
                 "--workflow-slug", "mode-pin", "--revalidate", *shape],
                cwd=ROOT, env=self.env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            tail = (result.stderr.strip().splitlines() or [""])[-1]
            self.assertEqual(result.returncode, 2, marker + ": " + tail)
            self.assertIn("local", tail.lower(), marker + ": " + tail)
            self.assertIn("mode", tail.lower(), marker + ": " + tail)

    def test_the_typed_gate_still_rejects_a_failing_candidate(self) -> None:
        marker = "BOUNDARY_GATE_AUTHORITY_LOST"
        self.complete_workflow(slug="boundary", finish=False)
        escape = "TO" + "DO"
        (self.repo / "app.py").write_text(f"value = 2  # {escape}: escape\n", encoding="utf-8")
        self.post_edit("app.py")
        wid = json.loads(self.state("status").stdout)["workflowId"]
        update = self.tmp / "reassess.json"
        update.write_text(json.dumps({
            "reassessment": "probe candidate deliberately carries a quality escape; non-behavioral for this fixture",
            "items": [], "dispositions": [],
        }), encoding="utf-8")
        recorded = self.state("tdd-map", "--slug", "boundary", "--workflow-id", wid,
                              "--input", str(update))
        self.assertEqual(recorded.returncode, 0, marker + ": " + recorded.stdout + recorded.stderr)
        self.owner_phase("implementation", "passed")
        quality = self.state("verify", "--slug", "boundary", "--kind", "quality-gate",
                             "--base-ref", "HEAD")
        combined = quality.stdout + quality.stderr
        self.assertNotEqual(quality.returncode, 0, marker + ": the typed gate accepted a failing candidate")
        self.assertIn("quality escapes", combined, marker + ": " + combined[-300:])

    def test_first_edit_after_review_still_resets_downstream(self) -> None:
        marker = "FIRST_EDIT_TRANSITION_LOST"
        self.complete_workflow(finish=False)
        before = self.history_length("production-edit-invalidated")
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        result = self.post_edit("app.py")
        self.assertEqual(result.returncode, 0, marker + ": " + result.stdout + result.stderr)
        state = json.loads(self.state("status").stdout)
        self.assertEqual(state["phase"], "implementation", marker)
        self.assertEqual(state["codeReview"], {"status": "pending", "findings": "pending"}, marker)
        self.assertEqual(state["finalReview"], {"source": None, "status": "pending", "findings": "pending"}, marker)
        self.assertEqual(self.history_length("production-edit-invalidated"), before + 1,
                         marker + ": the first edit after review must commit exactly one transition")

    def test_an_edit_clears_a_recorded_pause(self) -> None:
        marker = "PAUSE_SURVIVED_EDIT"
        self.complete_workflow(finish=False)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.post_edit("app.py")
        wid = json.loads(self.state("status").stdout)["workflowId"]
        paused = self.state("pause", "--slug", "hook-sequence", "--workflow-id", wid,
                            "--reason", "hold for probe")
        self.assertEqual(paused.returncode, 0, marker + ": " + paused.stdout + paused.stderr)
        self.assertIn("paused", json.loads(self.state("status").stdout), marker)
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        cleared = self.post_edit("app.py")
        self.assertEqual(cleared.returncode, 0, marker + ": " + cleared.stdout + cleared.stderr)
        self.assertNotIn("paused", json.loads(self.state("status").stdout), marker)

@unittest.skipUnless(shutil.which("bwrap"), "bwrap unavailable: producer absence is exercised natively on hosts without the producer install")
class RevalidateWithoutProducerTests(HookHarness):
    """Issue #182 CI fix: wrapper-owned --revalidate refusals precede the
    producer-existence blocker. The bwrap tmpfs masks the real producer install
    path, driving genuine absence through the real CLI on hosts that have it."""

    PRODUCER_ROOT = "/home/prop_/.local/share/repo-context-forge"

    def masked_bootstrap(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bwrap", "--dev-bind", "/", "/", "--tmpfs", self.PRODUCER_ROOT, "--",
             sys.executable, str(RCF_BOOTSTRAP), *args],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def test_revalidate_refusals_fire_before_the_producer_blocker(self) -> None:
        marker = "PRODUCER_BLOCKER_PREEMPTS_REVALIDATE_REFUSAL"
        bare = self.second_repo("masked")
        missing_slug = self.masked_bootstrap("--repo", str(bare), "--revalidate")
        tail = (missing_slug.stderr.strip().splitlines() or [""])[-1]
        self.assertEqual(missing_slug.returncode, 2, marker + ": " + tail)
        self.assertIn("revalidate", tail.lower(), marker + ": " + tail)
        self.assertIn("workflow", tail.lower(), marker + ": " + tail)
        bad_mode = self.masked_bootstrap("--repo", str(bare), "--workflow-slug", "masked",
                                         "--revalidate", "--mode", "local", "--mode", "pr")
        tail = (bad_mode.stderr.strip().splitlines() or [""])[-1]
        self.assertEqual(bad_mode.returncode, 2, marker + ": " + tail)
        self.assertIn("local", tail.lower(), marker + ": " + tail)
        self.assertIn("mode", tail.lower(), marker + ": " + tail)

    def test_a_mode_abbreviation_cannot_defeat_forced_local(self) -> None:
        # The producer's argparse honors abbreviations and the last occurrence,
        # so --mod pr after an exact local must refuse just like --mode pr.
        marker = "MODE_ABBREVIATION_DEFEATS_FORCED_LOCAL"
        bare = self.second_repo("masked-abbrev")
        abbreviated = self.masked_bootstrap("--repo", str(bare), "--workflow-slug", "masked",
                                            "--revalidate", "--mode", "local", "--mod", "pr")
        tail = (abbreviated.stderr.strip().splitlines() or [""])[-1]
        self.assertEqual(abbreviated.returncode, 2, marker + ": " + tail)
        self.assertIn("local", tail.lower(), marker + ": " + tail)
        self.assertIn("mode", tail.lower(), marker + ": " + tail)

    def test_a_nonrevalidate_run_still_hits_the_producer_blocker_first(self) -> None:
        marker = "NONREVALIDATE_ERROR_SURFACE_CHANGED"
        bare = self.second_repo("masked-plain")
        dangling = self.masked_bootstrap("--repo", str(bare), "--workflow-slug")
        combined = dangling.stdout + dangling.stderr
        self.assertEqual(dangling.returncode, 2, marker + ": " + combined)
        self.assertIn("repo-context-forge source bootstrap not found", combined, marker + ": " + combined)


ADVISOR_WRAPPER = ROOT / "skills" / "codex-advisor" / "scripts" / "ask-codex-advisor.sh"

PROVIDER_SHIM = """#!/usr/bin/env bash
set -u
count_file="$CAPTURE_DIR/count"
count=0; [[ -f "$count_file" ]] && count=$(cat "$count_file")
count=$((count + 1)); printf '%s\\n' "$count" >"$count_file"
cat >"$CAPTURE_DIR/payload-$count"
printf '%s\\n' "$@" >"$CAPTURE_DIR/args-$count"
if [[ -n "${ADVISOR_SHIM_REPLY:-}" ]]; then
  printf '%s\\n' "$ADVISOR_SHIM_REPLY"
elif [[ " $* " == *" --resume "* ]]; then
  printf '%s\\n' '{"schemaVersion":1,"findings":[],"verdict":"commit-ready"}'
else
  printf '%s\\n' '{"schemaVersion":1,"findings":[],"verdict":"completed"}'
fi
"""

DESIGN_BODY = """UNIQUE-DESIGN-BODY-MARKER
Chosen architecture preserves PRES-1 and records ASSUMP-1.
<!-- governed-design-labels:v1 -->
```json
{"schemaVersion":1,"labels":[{"id":"PRES-1","kind":"preservation"},{"id":"ASSUMP-1","kind":"assumption","behavioral":false}]}
```
"""

BATCH_DEMAND = "do not ration findings across rounds"
VERDICT_SYMMETRY = "names no measured or concretely reachable failure is not material"
BOUNDARY_SEAM = "outgoing process boundary is the real Seam"
RESERVED_MISMATCH = "Reserve context-mismatch for a candidate or projection identity mismatch"
WORDING_VERDICT = ("original request's literal wording that quotes a real-Seam measurement "
                   "is answered with a verdict, never context-mismatch")
DIFF_SECTION = "--- current-pass diff: passStartOid^{tree} -> activeCandidateTree ---"
DEFINITIONS_SECTION = "--- invoked test definitions"
PROBE_RULE = "retained real-Seam probe"

# A test-classified module whose Seam is invoked directly in the test and again
# through a two-level same-file helper chain, with the assertion 60 lines below
# the direct invocation so an ordinary three-line hunk never reaches it.
TEST_MODULE = (
    "import unittest\n"
    "from app import compute\n\n\n"
    "class ComputeTests(unittest.TestCase):\n"
    "    def setUp(self):\n"
    "        self.base = 1  # SETUP-BODY\n\n"
    "    def invoke(self, value):\n"
    "        return compute(value)  # HELPER-SEAM-INVOCATION\n\n"
    "    def run_app(self):\n"
    "        return self.invoke(self.base)\n\n"
    "    def test_compute_adds_one(self):\n"
    "        direct = compute(1)  # DIRECT-SEAM-INVOCATION\n"
    "        via_helper = self.run_app()\n"
    + "".join(f"        pad_{index} = {index}\n" for index in range(60))
    + "        self.assertEqual(direct, 2)\n"
    "        self.assertEqual(via_helper, 2)\n"
)
ADDED_ASSERTION = "        self.assertIsInstance(direct, int)  # ADDED-ASSERTION\n"
PRODUCTION_MODULE = (
    "def compute(value):\n    return value + 1\n\n\n"
    'FAR_LINE = "PRODUCTION-FAR-LINE"\n'
    + "".join(f"pad_{index} = {index}\n" for index in range(20))
    + "value = 1\n"
)


class WrapperPromptTests(HookHarness):
    """Issue #186 part 4 and its root cause: the assembled final-review prompt
    demands batched enumeration and binds both sides of the verdict, and the
    delegate role names the outgoing-boundary Seam. Drives the real wrapper
    with the suite's provider-capture contract."""

    def wrapper_rig(self) -> dict[str, str]:
        rig = self.tmp / "advisor-rig"
        for name in ("bin", "capture", "home", "claude"):
            (rig / name).mkdir(parents=True)
        provider = rig / "bin" / "claude"
        provider.write_text(PROVIDER_SHIM, encoding="utf-8")
        provider.chmod(0o755)
        (rig / "home" / ".bashrc").write_text(
            "alias claudex='ANTHROPIC_BASE_URL=https://transport.invalid "
            "ANTHROPIC_AUTH_TOKEN=offline-token CLAUDE_CODE_SUBAGENT_MODEL=offline-model \\\n"
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS=272000 CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000 \\\n"
            "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80 claude --model offline-model'\n",
            encoding="utf-8",
        )
        (rig / "design.md").write_text(DESIGN_BODY, encoding="utf-8")
        self.git("remote", "add", "origin", "https://example.invalid/prompt-rig.git")
        return dict(self.env, PATH=f"{rig / 'bin'}{os.pathsep}{self.env['PATH']}",
                    HOME=str(rig / "home"), DEVIN_ESTATE_HOME=str(rig / "claude"),
                    CAPTURE_DIR=str(rig / "capture"))

    def run_advisor(self, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ADVISOR_WRAPPER), "--cwd", str(self.repo), *args],
                              cwd=ROOT, env=env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

    def payload(self, env: dict[str, str], index: int) -> str:
        # surrogateescape: a payload may carry test source that is not UTF-8.
        return (Path(env["CAPTURE_DIR"]) / f"payload-{index}").read_text(
            encoding="utf-8", errors="surrogateescape")

    def preflight_consult(self, env: dict[str, str], slug: str, *, intent: str | None = None,
                          edit=None, marker: str = "") -> None:
        """`edit` runs after begin and before the graph recording, so the candidate
        tree the checkpoint binds is the edited one. `marker` labels a wrapper
        failure with the caller's mapped behavior."""
        begun = self.state("begin", "--slug", slug, *(("--intent", intent) if intent else ()))
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        if edit is not None:
            edit()
        record_context_forge(self.repo, self.tmp)
        rig = Path(env["CAPTURE_DIR"]).parent
        result = self.run_advisor(env, "--slug", slug, "--phase", "preflight-advice",
                                  "--design-file", str(rig / "design.md"), "--", "scope question")
        self.assertEqual(result.returncode, 0, marker + result.stdout + result.stderr)

    def test_the_preflight_prompt_lacks_the_batch_demand(self) -> None:
        marker = "PREFLIGHT_PROMPT_CONTAMINATED"
        env = self.wrapper_rig()
        self.preflight_consult(env, "prompt-pre")
        self.assertNotIn(BATCH_DEMAND, self.payload(env, 1), marker)

    def final_consult(self, env: dict[str, str], slug: str, *, intent: str | None = None,
                      edit=None, marker: str = "") -> str:
        """Advance a pass (no change unless `edit` makes one) to final review and
        return the final payload."""
        self.preflight_consult(env, slug, intent=intent, edit=edit, marker=marker)
        wid = json.loads(self.state("status").stdout)["workflowId"]
        self.assertEqual(self.state("advisor-disposition", "--slug", slug, "--workflow-id", wid,
                                    "--stage", "preflight", "--findings", "none").returncode, 0)
        state = json.loads(self.state("status").stdout)
        document = build_no_change_document("prompt rig")
        document["behaviorMap"][0]["sourceRefs"] = [{"type": "design",
            "evidenceId": state["governedDesignEvidence"], "id": "PRES-1"}]
        doc_path = self.tmp / "prompt-preflight.json"
        doc_path.write_text(json.dumps(document), encoding="utf-8")
        recorded = self.state("record-preflight", "--slug", slug, "--workflow-id", wid,
                              "--input", str(doc_path))
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.owner_phase("tdd", "not-required")
        self.record_gate_evidence(slug, wid)
        self.assertEqual(self.state("set-phase", "--phase", "implementation",
                                    "--status", "passed").returncode, 0)
        self.run_verification(slug)
        self.owner_phase("code-review", "not-required", findings="none")
        rig = Path(env["CAPTURE_DIR"]).parent
        result = self.run_advisor(env, "--slug", slug, "--phase", "final-review",
                                  "--design-file", str(rig / "design.md"), "--", "final question")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.final_stderr = result.stderr
        count = int((Path(env["CAPTURE_DIR"]) / "count").read_text().strip())
        return self.payload(env, count)

    def test_the_final_prompt_demands_batched_enumeration(self) -> None:
        marker = "FINAL_RUBRIC_RATIONS_FINDINGS"
        env = self.wrapper_rig()
        payload = self.final_consult(env, "prompt-final")
        self.assertIn(BATCH_DEMAND, payload, marker)
        self.assertIn("every additional material reachable failure class", payload, marker)

    def test_the_final_prompt_binds_both_sides_of_the_verdict(self) -> None:
        # Root cause of the six-round SPEC-2 loop: the rubric named conditions
        # that invalidate commit-ready and none that invalidate fix-before-commit.
        marker = "VERDICT_ONE_DIRECTIONAL"
        env = self.wrapper_rig()
        payload = self.final_consult(env, "prompt-verdict")
        self.assertIn(VERDICT_SYMMETRY, payload, marker)
        self.assertIn("new measurement contradicting", payload, marker)
        self.assertNotIn(VERDICT_SYMMETRY, self.payload(env, 1), marker)

    def test_the_delegate_role_names_the_outgoing_boundary_seam(self) -> None:
        # The role reaches the provider as --append-system-prompt, not stdin.
        marker = "OUTGOING_BOUNDARY_SEAM_UNDEFINED"
        env = self.wrapper_rig()
        self.preflight_consult(env, "prompt-role")
        args = (Path(env["CAPTURE_DIR"]) / "args-1").read_text(encoding="utf-8")
        self.assertIn("never RED/GREEN or production proof", args, marker)
        self.assertIn(BOUNDARY_SEAM, args, marker)
        self.assertIn("inside the asserted contract", args, marker)

    def commit_fixtures(self, files: dict[str, bytes]) -> None:
        for relative, content in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixtures")

    def extend_test(self) -> None:
        target = self.repo / "tests" / "test_app.py"
        target.write_text(target.read_text(encoding="utf-8") + ADDED_ASSERTION, encoding="utf-8")

    def diff_section(self, payload: str) -> str:
        """The diff the wrapper sent, with its line framing removed."""
        body = payload.split(DIFF_SECTION + "\n", 1)[1].split("\n=== Consult\n", 1)[0]
        lines = body.split("\n")[1:]  # the untrusted-data framing line
        return "".join(line[len("diff> "):] + "\n" for line in lines if line.startswith("diff> "))

    def test_a_changed_test_hunk_arrives_inside_its_enclosing_definition(self) -> None:
        # GitNexus #12's wording pass: an assertion added inside an existing test
        # reached the advisor as a three-line hunk with no Seam invocation.
        marker = "TEST_HUNK_LACKS_ENCLOSING_DEFINITION"
        env = self.wrapper_rig()
        self.commit_fixtures({"tests/test_app.py": TEST_MODULE.encode()})
        payload = self.final_consult(env, "test-context", edit=self.extend_test)
        # Exactly once at the outgoing boundary: the recorded repro's promise.
        self.assertEqual(payload.count("diff> +" + ADDED_ASSERTION.rstrip("\n")), 1, marker)
        self.assertEqual(payload.count("diff>      def test_compute_adds_one(self):"), 1, marker)
        self.assertEqual(payload.count("diff>          direct = compute(1)  # DIRECT-SEAM-INVOCATION"), 1, marker)
        candidate = json.loads(self.state("status").stdout)["activeCandidateTree"]
        blob = subprocess.run(["git", "rev-parse", f"{candidate}:tests/test_app.py"], cwd=self.repo,
                              env=self.env, text=True, capture_output=True, check=True).stdout.strip()
        index_line = re.search(r"^diff> index [0-9a-f]+\.\.([0-9a-f]+)", payload.split("b/tests/test_app.py", 1)[1], re.MULTILINE)
        self.assertIsNotNone(index_line, marker)
        self.assertTrue(blob.startswith(index_line.group(1)), marker + f": {blob} vs {index_line.group(1)}")

    def test_no_invoked_definitions_follow_the_changed_test(self) -> None:
        # #221 forwarded the setup and helper definitions a changed test hunk
        # invokes; git's function context is the whole contract now.
        marker = "INVOKED_DEFINITIONS_STILL_FORWARDED"
        env = self.wrapper_rig()
        self.commit_fixtures({"tests/test_app.py": TEST_MODULE.encode()})
        payload = self.final_consult(env, "no-defs", edit=self.extend_test)
        self.assertNotIn(DEFINITIONS_SECTION, payload, marker)
        self.assertNotIn("\ntest> ", payload, marker)

    def test_no_invoked_definitions_evidence_is_emitted(self) -> None:
        marker = "INVOKED_DEFINITIONS_EVIDENCE_STILL_EMITTED"
        env = self.wrapper_rig()
        self.commit_fixtures({"tests/test_app.py": TEST_MODULE.encode()})
        self.final_consult(env, "no-defs-evidence", edit=self.extend_test)
        self.assertIn("codex_advisor_evidence name=current-pass-diff", self.final_stderr, marker)
        self.assertNotIn("name=invoked-test-definitions", self.final_stderr, marker)

    def test_the_transport_returns_the_diff_the_payload_carries(self) -> None:
        marker = "TRANSPORT_RETURNS_DEFINITIONS_TUPLE"
        env = self.wrapper_rig()
        self.commit_fixtures({"tests/test_app.py": TEST_MODULE.encode()})
        payload = self.final_consult(env, "transport-diff", edit=self.extend_test)
        state = json.loads(self.state("status").stdout)
        produced = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]);"
             "from hooks.lib.advisor_diff import current_pass_evidence;"
             "sys.stdout.buffer.write(current_pass_evidence(*sys.argv[2:]))",
             str(ROOT), str(self.repo), state["passStartOid"] + "^{tree}", state["activeCandidateTree"]],
            cwd=ROOT, env=self.env, capture_output=True, check=False)
        self.assertEqual(produced.returncode, 0, marker + ": " + produced.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.diff_section(payload), produced.stdout.decode("utf-8", "surrogateescape"), marker)

    def test_a_production_rename_without_test_changes_is_gits_own_diff(self) -> None:
        marker = "NO_TEST_DIFF_CHANGED"
        env = self.wrapper_rig()
        self.commit_fixtures({"lib.py": ("value = 1\n" + "".join(f"pad_{i} = {i}\n" for i in range(40))).encode()})
        payload = self.final_consult(
            env, "prod-rename", edit=lambda: self.git("mv", "lib.py", "renamed_lib.py"), marker=marker)
        state = json.loads(self.state("status").stdout)
        expected = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", state["passStartOid"] + "^{tree}", state["activeCandidateTree"]],
            cwd=self.repo, env=self.env, text=True, capture_output=True, check=True).stdout
        self.assertEqual(self.diff_section(payload), expected, marker)

    def test_the_final_prompt_reserves_context_mismatch_for_identity_mismatch(self) -> None:
        # RCF #22's appeal: a measured rejection of an intent-wording claim was
        # answered with context-mismatch and zero findings, which advances nothing.
        marker = "CONTEXT_MISMATCH_NOT_RESERVED"
        env = self.wrapper_rig()
        payload = self.final_consult(env, "prompt-reserved")
        self.assertIn(RESERVED_MISMATCH, payload, marker)
        self.assertIn(WORDING_VERDICT, payload, marker)
        self.assertNotIn(RESERVED_MISMATCH, self.payload(env, 1), marker)

    def test_the_recorded_intent_reaches_the_final_review_exactly_once(self) -> None:
        marker = "INTENT_NOT_EXACTLY_ONCE"
        env = self.wrapper_rig()
        intent = "INTENT-ONCE-3f9c wording pass for the advisor transport"
        payload = self.final_consult(env, "intent-once", intent=intent)
        self.assertEqual(payload.count(intent), 1, marker)

    def test_production_hunks_keep_ordinary_context_beside_test_context(self) -> None:
        marker = "PRODUCTION_HUNK_EXPANDED"
        env = self.wrapper_rig()
        self.commit_fixtures({"app.py": PRODUCTION_MODULE.encode(), "tests/test_app.py": TEST_MODULE.encode()})

        def edit() -> None:
            self.extend_test()
            (self.repo / "app.py").write_text(PRODUCTION_MODULE.replace("value = 1\n", "value = 2\n"), encoding="utf-8")

        payload = self.final_consult(env, "mixed-change", edit=edit)
        self.assertIn("diff> +value = 2", payload, marker)
        self.assertNotIn("PRODUCTION-FAR-LINE", payload, marker)

    def test_every_changed_path_appears_once_across_the_partition(self) -> None:
        marker = "PATH_PARTITION_INCOMPLETE"
        env = self.wrapper_rig()
        self.commit_fixtures({
            "tests/test_app.py": TEST_MODULE.encode(),
            "tests/test_[glob].py": b"def test_glob():\n    assert True\n",
            "tests/fixtures/blob.bin": bytes(range(256)),
        })

        def edit() -> None:
            self.git("mv", "tests/test_app.py", "probe.py")  # test -> production
            (self.repo / "tests" / "test_[glob].py").write_text("def test_glob():\n    assert 1 == 1\n", encoding="utf-8")
            (self.repo / "tests" / "fixtures" / "blob.bin").write_bytes(bytes(reversed(range(256))))
            (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")

        payload = self.final_consult(env, "partition", edit=edit)
        headers = re.findall(r"^diff> diff --git a/(\S+) b/(\S+)$", payload, re.MULTILINE)
        self.assertEqual(len(headers), len(set(headers)), marker + f": {headers}")
        old_paths, new_paths = [pair[0] for pair in headers], [pair[1] for pair in headers]
        self.assertEqual(old_paths.count("tests/test_app.py"), 1, marker + f": {headers}")
        self.assertEqual(new_paths.count("probe.py"), 1, marker + f": {headers}")
        for path in ("app.py", "tests/test_[glob].py", "tests/fixtures/blob.bin"):
            self.assertEqual(new_paths.count(path), 1, marker + f": {path} in {headers}")

    def test_a_pass_without_test_changes_sends_gits_own_diff(self) -> None:
        marker = "NO_TEST_DIFF_CHANGED"
        env = self.wrapper_rig()
        payload = self.final_consult(
            env, "no-test-diff", edit=lambda: (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8"))
        state = json.loads(self.state("status").stdout)
        expected = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", state["passStartOid"] + "^{tree}", state["activeCandidateTree"]],
            cwd=self.repo, env=self.env, text=True, capture_output=True, check=True).stdout
        self.assertEqual(self.diff_section(payload), expected, marker)

    def ready_for_final_review(self, slug: str) -> str:
        """A pass at a ready final-review checkpoint that never consulted the advisor."""
        begun = self.state("begin", "--slug", slug)
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        self.record_preflight_evidence(slug, wid)
        self.owner_phase("tdd", "not-required")
        self.record_gate_evidence(slug, wid)
        passed = self.state("set-phase", "--phase", "implementation", "--status", "passed")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        return wid

    def test_a_refused_answer_is_still_emitted(self) -> None:
        marker = "REFUSED_ADVISOR_OUTPUT_DISCARDED"
        env = self.wrapper_rig()
        reply = '{"schemaVersion":1,"findings":[{"id":"SPEC-1","claim":"c"}],"verdict":"completed"}'
        env["ADVISOR_SHIM_REPLY"] = reply
        begun = self.state("begin", "--slug", "keep-output")
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        record_context_forge(self.repo, self.tmp)
        rig = Path(env["CAPTURE_DIR"]).parent
        result = self.run_advisor(env, "--slug", "keep-output", "--phase", "preflight-advice",
                                  "--design-file", str(rig / "design.md"), "--", "scope question")
        self.assertNotEqual(result.returncode, 0, marker + ": a malformed envelope was recorded")
        self.assertIn(reply, result.stdout, marker + ": " + result.stdout + result.stderr)
        self.assertNotIn("codex_advisor_complete", result.stderr, marker)

    def test_final_review_creates_its_session_without_a_preflight_consult(self) -> None:
        marker = "FINAL_REVIEW_NEEDS_PREFLIGHT_SESSION"
        env = self.wrapper_rig()
        env["ADVISOR_SHIM_REPLY"] = '{"schemaVersion":1,"findings":[],"verdict":"commit-ready"}'
        self.ready_for_final_review("review-first")
        rig = Path(env["CAPTURE_DIR"]).parent
        result = self.run_advisor(env, "--slug", "review-first", "--phase", "final-review",
                                  "--design-file", str(rig / "design.md"), "--", "completion question")
        self.assertEqual(result.returncode, 0, marker + ": " + result.stdout + result.stderr)
        args = (Path(env["CAPTURE_DIR"]) / "args-1").read_text(encoding="utf-8")
        self.assertIn("--session-id", args, marker)
        self.assertNotIn("--resume", args, marker)
        self.assertEqual(json.loads(self.state("status").stdout)["finalReview"]["status"], "commit-ready", marker)


    def test_a_created_final_review_session_is_persisted_and_resumed(self) -> None:
        marker = "CREATED_SESSION_NOT_REUSED"
        env = self.wrapper_rig()
        wid = self.ready_for_final_review("reuse-created")
        rig = Path(env["CAPTURE_DIR"]).parent
        consult = ("--slug", "reuse-created", "--phase", "final-review",
                   "--design-file", str(rig / "design.md"), "--", "completion question")
        first = self.run_advisor(env, *consult)
        self.assertNotEqual(first.returncode, 0, marker + ": the shim's create reply is not a final verdict")
        args = (Path(env["CAPTURE_DIR"]) / "args-1").read_text(encoding="utf-8").split()
        created = args[args.index("--session-id") + 1]
        state_root = env.get("DEVIN_WORKFLOW_STATE_ROOT") or f"{env['DEVIN_ESTATE_HOME']}/state"
        persisted = list((Path(state_root) / "_advisor-sessions").glob(f"*-reuse-created-{wid}.sid"))
        self.assertEqual([path.read_text(encoding="utf-8").strip() for path in persisted], [created], marker)
        second = self.run_advisor(env, *consult)
        self.assertEqual(second.returncode, 0, marker + ": " + second.stdout + second.stderr)
        resumed = (Path(env["CAPTURE_DIR"]) / "args-2").read_text(encoding="utf-8").split()
        self.assertEqual(resumed[resumed.index("--resume") + 1], created, marker)
        self.assertEqual(json.loads(self.state("status").stdout)["finalReview"]["status"], "commit-ready", marker)


class SkillTextTests(unittest.TestCase):
    """The skill Markdown agents read states each #211 slice-3 rule once."""

    def test_the_tdd_skill_states_the_probe_rule_once(self) -> None:
        marker = "PROBE_GUIDANCE_NOT_STATED_ONCE"
        tdd = (ROOT / "skills" / "tdd" / "SKILL.md").read_text(encoding="utf-8")
        workflow = (ROOT / "skills" / "repo-production-workflow" / "SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(tdd.count(PROBE_RULE), 1, marker)
        self.assertIn("never waives RED/GREEN", tdd, marker)
        self.assertNotIn(PROBE_RULE, workflow, marker)

    def test_the_advisor_skill_mirrors_the_reserved_verdict_rule(self) -> None:
        marker = "SKILL_VERDICT_TEXT_STALE"
        skill = (ROOT / "skills" / "codex-advisor" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("reserved for a candidate or projection identity mismatch", skill, marker)
        self.assertIn("literal wording", skill, marker)


class TollDeletionTests(HookHarness):
    """The governed pass with its bookkeeping tolls deleted: every step is a real
    action through the real gate, recorders, and Stop hook."""

    MAP = [pending_behavior("BM_HOOK", behavior="app value must be 2", seam="app module",
                            expected="app.value == 2", red_failure="VALUE_NOT_TWO")]

    def open_pass(self, slug: str, *, consult: bool = True) -> str:
        begun = self.state("begin", "--slug", slug)
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        if consult:
            for transition in (
                ("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                 "--source", "codex-advisor", "--verdict", "completed"),
                ("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                 "--findings", "none"),
            ):
                result = self.state(*transition)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return wid

    def tdd(self, slug: str, phase: str) -> subprocess.CompletedProcess[str]:
        (self.repo / "test_probe.py").write_text(
            "import app, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_value(self): self.assertEqual(app.value, 2, 'VALUE_NOT_TWO')\n",
            encoding="utf-8",
        )
        return self.workflow("tdd", "--slug", slug, "--phase", phase, "--behavior-id", "BM_HOOK",
                             "--", sys.executable, "-m", "unittest", "test_probe")

    def workflow(self, *args: str) -> subprocess.CompletedProcess[str]:
        """workflow.py with --repo ahead of any `--` command separator."""
        return subprocess.run(
            [sys.executable, str(WORKFLOW), args[0], "--repo", str(self.repo), *args[1:]],
            cwd=ROOT, env=self.env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def verify(self, slug: str, *extra: str) -> subprocess.CompletedProcess[str]:
        return self.workflow("verify", "--slug", slug, *extra)

    def final_intake(self, slug: str, wid: str, findings: list, verdict: str = "commit-ready") -> subprocess.CompletedProcess[str]:
        envelope = self.tmp / f"{slug}-final.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "findings": findings, "verdict": verdict}), encoding="utf-8")
        return self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                          "--source", "codex-advisor", "--input", str(envelope))

    def advance_to_review(self, slug: str) -> str:
        wid = self.open_pass(slug)
        self.record_preflight_evidence(slug, wid)
        self.owner_phase("tdd", "not-required")
        self.record_gate_evidence(slug, wid)
        passed = self.state("set-phase", "--phase", "implementation", "--status", "passed")
        self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        return wid

    def test_a_pass_completes_through_real_actions_only(self) -> None:
        marker = "TOLL_STILL_REQUIRED"
        slug = "no-tolls"
        wid = self.open_pass(slug, consult=False)
        self.record_preflight_evidence(slug, wid, behavior_map=self.MAP)
        red = self.tdd(slug, "red")
        self.assertEqual(red.returncode, 0, marker + ": " + red.stdout + red.stderr)
        admitted = self.intake("app.py")
        self.assertNotIn("deny", admitted.stdout, marker + ": " + admitted.stdout)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.post_edit("app.py")
        green = self.tdd(slug, "green")
        self.assertEqual(green.returncode, 0, marker + ": " + green.stdout + green.stderr)
        record_context_forge(self.repo, self.tmp)  # the post-edit graph revalidation
        verified = self.verify(slug, "--", sys.executable, "-m", "unittest", "test_probe")
        self.assertEqual(verified.returncode, 0, marker + ": " + verified.stdout + verified.stderr)
        gate = self.verify(slug, "--kind", "quality-gate", "--base-ref", "HEAD")
        self.assertEqual(gate.returncode, 0, marker + ": " + gate.stdout + gate.stderr)
        self.owner_phase("code-review", "not-required", findings="none")
        final = self.final_intake(slug, wid, [])
        self.assertEqual(final.returncode, 0, marker + ": " + final.stdout + final.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 0, marker + ": " + completed.stdout + completed.stderr)

    def late_pass_to_review(self, slug: str) -> str:
        """A pass whose contract RED ran after a production edit, driven to final-review readiness."""
        wid = self.open_pass(slug, consult=False)
        self.record_preflight_evidence(slug, wid, behavior_map=self.MAP)
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")  # production edited before its RED
        red = self.tdd(slug, "red")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        self.post_edit("app.py")
        green = self.tdd(slug, "green")
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        record_context_forge(self.repo, self.tmp)
        verified = self.verify(slug, "--", sys.executable, "-m", "unittest", "test_probe")
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        gate = self.verify(slug, "--kind", "quality-gate", "--base-ref", "HEAD")
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        self.owner_phase("code-review", "not-required", findings="none")
        return wid

    def test_a_late_pass_still_completes(self) -> None:
        marker = "LATE_PASS_REFUSED_AT_COMPLETE"
        slug = "late-pass"
        wid = self.late_pass_to_review(slug)
        final = self.final_intake(slug, wid, [])
        self.assertEqual(final.returncode, 0, marker + ": " + final.stdout + final.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 0, marker + ": " + completed.stdout + completed.stderr)

    def test_the_advisor_wrapper_forwards_late_reds_to_the_final_review(self) -> None:
        marker = "WRAPPER_DROPS_LATE_RED"
        slug = "late-wrapper"
        self.late_pass_to_review(slug)
        # The provider is the wrapper's outgoing process boundary: a capture there
        # is the real Seam for what the wrapper sends the final review.
        rig = self.tmp / "rig"
        for name in ("bin", "home", "capture"):
            (rig / name).mkdir(parents=True)
        (rig / "home" / ".bashrc").write_text(
            "alias claudex='ANTHROPIC_BASE_URL=https://transport.invalid ANTHROPIC_AUTH_TOKEN=offline-token "
            "CLAUDE_CODE_SUBAGENT_MODEL=offline-model \\\nclaude --model offline-model'\n", encoding="utf-8")
        provider = rig / "bin" / "claude"
        provider.write_text('#!/usr/bin/env bash\ncat >"$CAPTURE_DIR/payload"\n'
                            "printf '%s\\n' '{\"schemaVersion\":1,\"findings\":[],\"verdict\":\"commit-ready\"}'\n", encoding="utf-8")
        provider.chmod(0o755)
        env = {**self.env, "PATH": f"{rig / 'bin'}:{os.environ['PATH']}", "HOME": str(rig / "home"),
               "DEVIN_ESTATE_HOME": str(rig / "claude"), "CAPTURE_DIR": str(rig / "capture")}
        consult = subprocess.run(
            [str(ADVISOR), "--slug", slug, "--phase", "final-review", "--cwd", str(self.repo),
             "--design-absent", "hook-suite rig", "--", "completion question"],
            cwd=ROOT, env=env, text=True, capture_output=True, check=False)
        self.assertEqual(consult.returncode, 0, consult.stdout + consult.stderr)
        payload = (rig / "capture" / "payload").read_text(encoding="utf-8")
        self.assertIn("--- late RED: contract items whose RED or baseline ran after production had changed ---", payload,
                      marker + ": " + consult.stderr)
        self.assertIn('"id": "BM_HOOK"', payload, marker)
        self.assertIn("codex_advisor_evidence name=late-red", consult.stderr, marker)

    def test_a_late_baseline_pass_completes(self) -> None:
        marker = "LATE_BASELINE_PASS_REFUSED_AT_COMPLETE"
        slug = "late-baseline-pass"
        wid = self.open_pass(slug, consult=False)
        self.record_preflight_evidence(slug, wid, behavior_map=self.MAP)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")  # the behavior landed before its RED ran
        self.post_edit("app.py")
        baseline = self.tdd(slug, "red")
        self.assertEqual(baseline.returncode, 0, marker + ": " + baseline.stdout + baseline.stderr)
        self.assertIn('"already-satisfied"', baseline.stdout, marker)
        self.assertIn("Late RED: BM_HOOK", self.state("summary").stdout, marker + ": " + self.state("summary").stdout)
        record_context_forge(self.repo, self.tmp)
        verified = self.verify(slug, "--", sys.executable, "-m", "unittest", "test_probe")
        self.assertEqual(verified.returncode, 0, marker + ": " + verified.stdout + verified.stderr)
        gate = self.verify(slug, "--kind", "quality-gate", "--base-ref", "HEAD")
        self.assertEqual(gate.returncode, 0, marker + ": " + gate.stdout + gate.stderr)
        self.owner_phase("code-review", "not-required", findings="none")
        final = self.final_intake(slug, wid, [])
        self.assertEqual(final.returncode, 0, marker + ": " + final.stdout + final.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 0, marker + ": " + completed.stdout + completed.stderr)

    def test_verification_runs_while_a_material_finding_is_pending(self) -> None:
        marker = "VERIFY_REFUSED_ON_PENDING_FINDING"
        slug = "verify-pending"
        wid = self.open_pass(slug, consult=False)
        intake = self.tmp / "material-intake.json"
        intake.write_text(json.dumps({"schemaVersion": 1, "verdict": "completed", "findings": [
            {"id": "SPEC-1", "claim": "unattacked promise", "material": True, "kind": "behavioral"}]}), encoding="utf-8")
        consulted = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                               "--source", "codex-advisor", "--input", str(intake))
        self.assertEqual(consulted.returncode, 0, consulted.stdout + consulted.stderr)
        intake_id = json.loads(self.state("status").stdout)["advisorPreflight"]["intakeEvidence"]
        owned = [{**self.MAP[0], "sourceRefs": [{"type": "finding", "evidenceId": intake_id, "id": "SPEC-1"}]}]
        self.record_preflight_evidence(slug, wid, behavior_map=owned)
        red = self.tdd(slug, "red")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.tdd(slug, "green")
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        verified = self.verify(slug, "--", sys.executable, "-m", "unittest", "test_probe")
        self.assertEqual(verified.returncode, 0, marker + ": " + verified.stdout + verified.stderr)
        gate = self.verify(slug, "--kind", "quality-gate", "--base-ref", "HEAD")
        self.assertEqual(gate.returncode, 0, marker + ": " + gate.stdout + gate.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 2, marker + ": " + completed.stdout)
        self.assertIn("SPEC-1", completed.stderr, marker + ": " + completed.stderr)

    def test_a_lead_review_records_while_a_material_finding_is_pending(self) -> None:
        marker = "REVIEW_REFUSED_ON_PENDING_FINDING"
        slug = "review-pending"
        wid = self.open_pass(slug, consult=False)
        intake = self.tmp / "review-material-intake.json"
        intake.write_text(json.dumps({"schemaVersion": 1, "verdict": "completed", "findings": [
            {"id": "SPEC-1", "claim": "unattacked promise", "material": True, "kind": "behavioral"}]}), encoding="utf-8")
        consulted = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                               "--source", "codex-advisor", "--input", str(intake))
        self.assertEqual(consulted.returncode, 0, consulted.stdout + consulted.stderr)
        intake_id = json.loads(self.state("status").stdout)["advisorPreflight"]["intakeEvidence"]
        owned = [{**self.MAP[0], "sourceRefs": [{"type": "finding", "evidenceId": intake_id, "id": "SPEC-1"}]}]
        self.record_preflight_evidence(slug, wid, behavior_map=owned)
        red = self.tdd(slug, "red")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        (self.repo / "app.py").write_text("value = 2\n", encoding="utf-8")
        green = self.tdd(slug, "green")
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        self.assertEqual(self.verify(slug, "--", sys.executable, "-m", "unittest", "test_probe").returncode, 0)
        self.assertEqual(self.verify(slug, "--kind", "quality-gate", "--base-ref", "HEAD").returncode, 0)
        review_input = self.tmp / "pending-review.json"
        review_input.write_text('{"findings": []}', encoding="utf-8")
        review = self.workflow("record-review", "--slug", slug, "--workflow-id", wid, "--resolved-model", "test-model",
                               "--review-context-id", "pending-finding", "--input", str(review_input))
        self.assertEqual(review.returncode, 0, marker + ": " + review.stdout + review.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 2, marker + ": " + completed.stdout)
        self.assertIn("SPEC-1", completed.stderr, marker + ": " + completed.stderr)

    def test_an_empty_intake_closes_at_recording(self) -> None:
        marker = "EMPTY_INTAKE_NEEDS_ACKNOWLEDGEMENT"
        slug = "empty-intake"
        wid = self.open_pass(slug, consult=False)
        intake = self.tmp / "empty-intake.json"
        intake.write_text('{"schemaVersion":1,"findings":[],"verdict":"completed"}', encoding="utf-8")
        consulted = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                               "--source", "codex-advisor", "--input", str(intake))
        self.assertEqual(consulted.returncode, 0, consulted.stdout + consulted.stderr)
        self.assertEqual(json.loads(self.state("status").stdout)["advisorPreflight"]["findings"], "none", marker)
        self.record_preflight_evidence(slug, wid)
        self.owner_phase("tdd", "not-required")
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        final = self.final_intake(slug, wid, [{"id": "REVIEW-1", "claim": "advice", "material": False, "kind": "nonbehavioral"}])
        self.assertEqual(final.returncode, 0, marker + ": " + final.stdout + final.stderr)
        self.assertEqual(json.loads(self.state("status").stdout)["finalReview"]["findings"], "none", marker)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 0, marker + ": " + completed.stdout + completed.stderr)

    def test_a_material_finding_survives_a_later_review_and_empty_intake(self) -> None:
        marker = "MATERIAL_FINDING_CLOSED_BY_EMPTY_INTAKE"
        slug = "material-survives"
        wid = self.advance_to_review(slug)
        first = self.final_intake(slug, wid, [{"id": "FINAL-1", "claim": "real gap", "material": True, "kind": "behavioral"}], "fix-before-commit")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (self.repo / "app.py").write_text("value = 3\n", encoding="utf-8")
        self.post_edit("app.py")
        record_context_forge(self.repo, self.tmp)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        second = self.final_intake(slug, wid, [{"id": "REVIEW-1", "claim": "advice", "material": False, "kind": "nonbehavioral"}])
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 2, marker + ": " + completed.stdout)
        self.assertIn("FINAL-1", completed.stderr, marker + ": " + completed.stderr)

    def test_an_unproved_map_cannot_complete(self) -> None:
        marker = "UNPROVED_MAP_COMPLETED"
        slug = "unproved"
        wid = self.open_pass(slug)
        self.record_preflight_evidence(slug, wid, behavior_map=self.MAP)
        red = self.tdd(slug, "red")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 2, marker + ": " + completed.stdout)
        self.assertIn("BM_HOOK", completed.stderr, marker + ": " + completed.stderr)

    def test_a_context_mismatch_still_blocks_completion(self) -> None:
        marker = "CONTEXT_MISMATCH_IGNORED"
        slug = "mismatch-blocks"
        wid = self.advance_to_review(slug)
        mismatch = self.final_intake(slug, wid, [], "context-mismatch")
        self.assertEqual(mismatch.returncode, 0, mismatch.stdout + mismatch.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 2, marker + ": " + completed.stdout)

    def test_a_final_result_against_a_changed_tree_is_refused(self) -> None:
        marker = "STALE_REVIEW_ACCEPTED"
        slug = "stale-review"
        wid = self.advance_to_review(slug)
        (self.repo / "app.py").write_text("value = 4\n", encoding="utf-8")
        refused = self.final_intake(slug, wid, [])
        self.assertEqual(refused.returncode, 2, marker + ": " + refused.stdout)
        self.assertIn("changed after the lead review", refused.stderr, marker + ": " + refused.stderr)

class RedFirstTests(HookHarness):
    """Contract items are proved test-first: every contract RED lands on the clean
    tree before the first production edit, and only GREEN through that RED is proof."""

    def two_items(self) -> list:
        return [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO"),
                pending_behavior("BM_B", behavior="b is two", seam="app module", expected="app.b == 2", red_failure="B_NOT_TWO")]

    def open_pass(self, slug: str, behavior_map: list | None = None) -> str:
        (self.repo / "app.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "two attributes")
        begun = self.state("begin", "--slug", slug)
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        consulted = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                               "--source", "codex-advisor", "--verdict", "completed")
        self.assertEqual(consulted.returncode, 0, consulted.stdout + consulted.stderr)
        self.record_preflight_evidence(slug, wid, behavior_map=behavior_map or self.two_items())
        return wid

    def workflow(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKFLOW), args[0], "--repo", str(self.repo), *args[1:]],
            cwd=ROOT, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )

    def tdd(self, slug: str, phase: str, item: str, attr: str) -> subprocess.CompletedProcess[str]:
        (self.repo / f"test_probe_{attr}.py").write_text(
            "import app, unittest\n"
            f"class Probe(unittest.TestCase):\n"
            f"    def test_value(self): self.assertEqual(app.{attr}, 2, '{attr.upper()}_NOT_TWO')\n",
            encoding="utf-8",
        )
        return self.workflow("tdd", "--slug", slug, "--phase", phase, "--behavior-id", item,
                             "--", sys.executable, "-m", "unittest", f"test_probe_{attr}")

    def map_status(self) -> dict[str, str]:
        state = json.loads(self.state("status").stdout)
        document = json.loads(self.state("evidence", "--evidence-id", str(state["tddEvidence"])).stdout)
        items = document.get("behaviorMap") or (document.get("document") or {}).get("behaviorMap")
        return {str(entry["id"]): str(entry["status"]) for entry in items}

    def tdd_document(self) -> dict:
        """The recorded TDD document, read back through a fresh workflow.py process."""
        state = json.loads(self.state("status").stdout)
        return json.loads(self.state("evidence", "--evidence-id", str(state["tddEvidence"])).stdout)["document"]

    def latest_run(self) -> dict:
        return (self.tdd_document().get("runs") or [{}])[-1]

    def map_item(self, identifier: str) -> dict:
        return next(entry for entry in self.tdd_document()["behaviorMap"] if entry["id"] == identifier)

    def summary(self) -> str:
        return self.state("summary").stdout

    def rev_parse(self, ref: str) -> str:
        return subprocess.run(["git", "rev-parse", ref], cwd=self.repo, env=self.env, text=True,
                              stdout=subprocess.PIPE, check=True).stdout.strip()

    def events(self) -> int:
        return len(json.loads(self.state("history").stdout)["events"])

    def rewrite_latest_evidence(self, update) -> None:
        """Rewrite the persisted TDD evidence document the way an older producer left it."""
        identity = resolve_repo_identity(self.repo)
        evidence_id = json.loads(self.state("status").stdout)["tddEvidence"]
        database = Path(self.env["DEVIN_WORKFLOW_STATE_ROOT"]) / identity.key / "workflow.sqlite3"
        connection = sqlite3.connect(database)
        try:
            document = json.loads(connection.execute(
                "SELECT document_json FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()[0])
            update(document)
            connection.execute("UPDATE evidence SET document_json = ? WHERE evidence_id = ?",
                               (json.dumps(document, sort_keys=True, separators=(",", ":")), evidence_id))
            connection.commit()
        finally:
            connection.close()

    def test_a_finding_with_an_extra_field_or_unknown_kind_still_records(self) -> None:
        marker = "TOLERABLE_ENVELOPE_REFUSED"
        slug = "tolerant-envelope"
        (self.repo / "app.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "two attributes")
        begun = self.state("begin", "--slug", slug)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        envelope = self.tmp / "tolerant.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "verdict": "completed", "findings": [
            {"id": "SPEC-1", "claim": "an unattacked promise", "material": True, "kind": "other", "severity": "high"}]}), encoding="utf-8")
        recorded = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                              "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(recorded.returncode, 0, marker + ": " + recorded.stdout + recorded.stderr)
        [entry] = json.loads(self.state("status").stdout)["findingStates"]
        self.assertEqual((entry["findingId"], entry["kind"], entry["status"]), ("SPEC-1", "behavioral", "pending"), marker)

    def test_a_nonmaterial_final_finding_does_not_steer_the_next_action(self) -> None:
        marker = "NONMATERIAL_FINDING_STEERS_NEXT_ACTION"
        slug = "nonmaterial-next"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        record_context_forge(self.repo, self.tmp)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        envelope = self.tmp / "nonmaterial.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "verdict": "commit-ready", "findings": [
            {"id": "SCOPE-1", "claim": "promises restated", "material": False, "kind": "nonbehavioral"}]}), encoding="utf-8")
        final = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                           "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
        self.assertEqual(json.loads(self.state("status").stdout)["nextAction"], "complete-workflow", marker)
        self.assertEqual(self.state("complete").returncode, 0, marker)

    def rejection_document(self, wid: str, intake_id: str, measurement: str) -> Path:
        status = json.loads(self.state("status").stdout)
        path = self.tmp / f"reject-{len(measurement)}.json"
        path.write_text(json.dumps({"context": {"workflowId": wid, "candidateTree": status["activeCandidateTree"]},
            "intakeEvidenceId": intake_id, "dispositions": [{"finding_id": "F-1", "status": "rejected-with-evidence", "kind": "behavioral",
            "premise": {"claim": "the operation is attacked by test_probe_a", "command": measurement, "result": "false"},
            "occurrence": {"domain": "every caller of the operation", "count": 0, "complete": True, "command": measurement, "result": "count=0"},
            "materialConsequence": {"claim": "the promise could break unseen", "command": measurement, "result": "attacked"},
            "evidence": "test_probe_a executes the operation"}]}), encoding="utf-8")
        return path

    def rejected_then_re_raised(self, slug: str) -> tuple[str, str]:
        """A rejected final finding the advisor re-raises as material in its one response."""
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        record_context_forge(self.repo, self.tmp)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        finding = {"id": "F-1", "claim": "an operation is unattacked", "material": True, "kind": "behavioral"}
        envelope = self.tmp / "final-f1.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "verdict": "fix-before-commit", "findings": [finding]}), encoding="utf-8")
        first = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                           "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        intake_id = [e["intakeEvidenceId"] for e in json.loads(self.state("status").stdout)["findingStates"] if e["findingId"] == "F-1"][0]
        rejected = self.state("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                              "--findings", "addressed", "--input", str(self.rejection_document(wid, intake_id, "python -m unittest test_probe_a")))
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        self.first_rejection = self.finding_entry()["dispositionEvidenceId"]
        appeal = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                            "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(appeal.returncode, 0, appeal.stdout + appeal.stderr)
        return wid, intake_id

    def test_a_material_re_raise_is_judged_once_more_then_stands(self) -> None:
        marker = "REAPPEAL_NOT_REJUDGED"
        slug = "re-raise-judged"
        wid, intake_id = self.rejected_then_re_raised(slug)
        [entry] = [e for e in json.loads(self.state("status").stdout)["findingStates"] if e["findingId"] == "F-1"]
        self.assertEqual((entry["status"], entry.get("appealStatus")), ("pending", "disagreement"), marker + ": " + json.dumps(entry))
        premature = self.state("complete")
        self.assertEqual(premature.returncode, 2, marker + ": complete ignored the re-raised measurement")
        self.assertIn("F-1", premature.stderr, marker)
        judged = self.state("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                            "--findings", "addressed", "--input", str(self.rejection_document(wid, intake_id, "python -m unittest test_probe_a -v")))
        self.assertEqual(judged.returncode, 0, marker + ": " + judged.stdout + judged.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 0, marker + ": " + completed.stdout + completed.stderr)

    def test_a_measured_rejection_after_the_appeal_stands(self) -> None:
        marker = "ADJUDICATION_DEAD_END"
        slug = "rejection-stands"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        record_context_forge(self.repo, self.tmp)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        finding = {"id": "F-1", "claim": "an operation is unattacked", "material": True, "kind": "behavioral"}
        envelope = self.tmp / "final-f1.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "verdict": "fix-before-commit", "findings": [finding]}), encoding="utf-8")
        first = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                           "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        status = json.loads(self.state("status").stdout)
        intake_id = [e["intakeEvidenceId"] for e in status["findingStates"] if e["findingId"] == "F-1"][0]
        measurement = {"claim": "the operation is attacked by test_probe_a", "command": "python -m unittest test_probe_a", "result": "false"}
        rejection = self.tmp / "reject-f1.json"
        rejection.write_text(json.dumps({"context": {"workflowId": wid, "candidateTree": status["activeCandidateTree"]},
            "intakeEvidenceId": intake_id, "dispositions": [{"finding_id": "F-1", "status": "rejected-with-evidence", "kind": "behavioral",
            "premise": measurement, "occurrence": {"domain": "every caller of the operation", "count": 0, "complete": True,
            "command": "python -m unittest test_probe_a", "result": "count=0"},
            "materialConsequence": {"claim": "the promise could break unseen", "command": "python -m unittest test_probe_a", "result": "attacked"},
            "evidence": "test_probe_a executes the operation"}]}), encoding="utf-8")
        rejected = self.state("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                              "--findings", "addressed", "--input", str(rejection))
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        # The advisor's one response re-raises the same finding as material.
        appeal = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                            "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(appeal.returncode, 0, appeal.stdout + appeal.stderr)
        after = json.loads(self.state("status").stdout)
        self.assertNotEqual(after["nextAction"], "needs-human-owner-adjudication", marker + ": " + after["nextAction"])
        judged = self.state("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                            "--findings", "addressed", "--input", str(self.rejection_document(wid, intake_id, "python -m unittest test_probe_a -v")))
        self.assertEqual(judged.returncode, 0, marker + ": " + judged.stdout + judged.stderr)
        completed = self.state("complete")
        self.assertEqual(completed.returncode, 0, marker + ": " + completed.stdout + completed.stderr)

    def test_no_stop_hook_is_registered(self) -> None:
        marker = "STOP_HOOK_STILL_REGISTERED"
        hooks = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertNotIn("Stop", hooks, marker)
        self.assertFalse((ROOT / "hooks" / "post-edit-blast-radius.py").exists(), marker)

    def test_a_second_red_lands_on_the_clean_tree(self) -> None:
        marker = "SECOND_RED_REFUSED_ON_CLEAN_TREE"
        slug = "red-sweep"
        self.open_pass(slug)
        first = self.tdd(slug, "red", "BM_A", "a")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        second = self.tdd(slug, "red", "BM_B", "b")
        self.assertEqual(second.returncode, 0, marker + ": " + second.stdout + second.stderr)
        self.assertEqual(self.map_status(), {"BM_A": "red", "BM_B": "red"}, marker)

    def test_each_red_item_reaches_green_through_its_own_red(self) -> None:
        marker = "NON_ACTIVE_RED_GREEN_REFUSED"
        slug = "both-green"
        self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        green_a = self.tdd(slug, "green", "BM_A", "a")
        self.assertEqual(green_a.returncode, 0, marker + ": " + green_a.stdout + green_a.stderr)
        green_b = self.tdd(slug, "green", "BM_B", "b")
        self.assertEqual(green_b.returncode, 0, marker + ": " + green_b.stdout + green_b.stderr)
        self.assertEqual(self.map_status(), {"BM_A": "green", "BM_B": "green"}, marker)
        self.assertEqual(json.loads(self.state("status").stdout)["tdd"], "passed", marker)

    def test_green_without_a_red_is_refused(self) -> None:
        marker = "POST_EDIT_PASS_ACCEPTED"
        slug = "no-post-edit"
        self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        unearned = self.tdd(slug, "green", "BM_B", "b")
        self.assertEqual(unearned.returncode, 2, marker + ": " + unearned.stdout)
        self.assertEqual(self.map_status()["BM_B"], "pending", marker)

    def test_record_preflight_validates_the_text_sections(self) -> None:
        marker = "PROSE_SECTIONS_NOT_VALIDATED"
        slug = "prose-required"
        begun = self.state("begin", "--slug", slug)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                   "--source", "codex-advisor", "--verdict", "completed")
        doc = self.tmp / "prose.json"
        doc.write_text(json.dumps({"behaviorMap": self.two_items()}), encoding="utf-8")
        bare = self.state("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(doc))
        self.assertEqual(bare.returncode, 2, marker + ": a map-only document was recorded")
        self.assertIn("affectedSurface", bare.stderr, marker + ": " + bare.stderr)
        full = build_document("prose", behavior_map=self.two_items())
        full["openQuestions"] = "how should the seam be placed?"
        doc.write_text(json.dumps(full), encoding="utf-8")
        asking = self.state("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(doc))
        self.assertEqual(asking.returncode, 2, marker + ": an open question was recorded")
        full["openQuestions"] = "none"
        doc.write_text(json.dumps(full), encoding="utf-8")
        accepted = self.state("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(doc))
        self.assertEqual(accepted.returncode, 0, marker + ": " + accepted.stdout + accepted.stderr)

    def test_status_reports_the_earned_split(self) -> None:
        marker = "EARNED_SPLIT_MISSING"
        slug = "earned-split"
        self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        self.post_edit("app.py")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        self.assertIn("Contract green=1/2", self.state("summary").stdout, marker + ": " + self.state("summary").stdout)

    def test_items_added_after_implementation_each_take_their_red(self) -> None:
        marker = "LATER_ITEM_RED_DEADLOCKED"
        slug = "later-items"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\nc = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        added = self.tmp / "later.json"
        added.write_text(json.dumps({"sourceBehaviorId": "BM_A", "reassessment": "GREEN exposed two more promises", "items": [
            pending_behavior("BM_B", behavior="b is two", seam="app module", expected="app.b == 2", red_failure="B_NOT_TWO"),
            pending_behavior("BM_C", behavior="c is two", seam="app module", expected="app.c == 2", red_failure="C_NOT_TWO")]}), encoding="utf-8")
        grown = self.state("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(added))
        self.assertEqual(grown.returncode, 0, grown.stdout + grown.stderr)
        red_b = self.tdd(slug, "red", "BM_B", "b")
        self.assertEqual(red_b.returncode, 0, marker + ": " + red_b.stdout + red_b.stderr)
        red_c = self.tdd(slug, "red", "BM_C", "c")
        self.assertEqual(red_c.returncode, 0, marker + ": " + red_c.stdout + red_c.stderr)
        self.assert_obligations_only("BM_B", "BM_C")

    def test_a_skipped_green_is_not_proof(self) -> None:
        marker = "SKIPPED_RUN_ACCEPTED_AS_GREEN"
        slug = "skipped-green"
        self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        (self.repo / "test_probe_a.py").write_text(
            "import app, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    @unittest.skip('conditionally skipped after implementation')\n"
            "    def test_value(self): self.assertEqual(app.a, 2, 'A_NOT_TWO')\n",
            encoding="utf-8",
        )
        skipped = self.workflow("tdd", "--slug", slug, "--phase", "green", "--behavior-id", "BM_A",
                                "--", sys.executable, "-m", "unittest", "test_probe_a")
        self.assertEqual(skipped.returncode, 2, marker + ": " + skipped.stdout)
        self.assertEqual(self.map_status()["BM_A"], "red", marker)

    def test_an_early_exit_green_is_not_proof(self) -> None:
        marker = "EARLY_EXIT_ACCEPTED_AS_GREEN"
        slug = "early-exit"
        self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        (self.repo / "test_probe_a.py").write_text(
            "import app, os, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        os._exit(0)\n"
            "        self.assertEqual(app.a, 2, 'A_NOT_TWO')\n",
            encoding="utf-8",
        )
        cut_short = self.workflow("tdd", "--slug", slug, "--phase", "green", "--behavior-id", "BM_A",
                                  "--", sys.executable, "-m", "unittest", "test_probe_a")
        self.assertEqual(cut_short.returncode, 2, marker + ": " + cut_short.stdout)
        self.assertEqual(self.map_status()["BM_A"], "red", marker)

    def test_an_unhashable_kind_still_records(self) -> None:
        marker = "UNHASHABLE_KIND_DISCARDS_THE_CONSULT"
        slug = "list-kind"
        (self.repo / "app.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "two attributes")
        begun = self.state("begin", "--slug", slug)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        envelope = self.tmp / "list-kind.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "verdict": "completed", "findings": [
            {"id": "SPEC-1", "claim": "an unattacked promise", "material": True, "kind": []},
            {"id": "SPEC-2", "claim": "another", "material": False, "kind": {"type": "note"}}]}), encoding="utf-8")
        recorded = self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                              "--source", "codex-advisor", "--input", str(envelope))
        self.assertEqual(recorded.returncode, 0, marker + ": " + recorded.stdout + recorded.stderr)
        kinds = {e["findingId"]: e["kind"] for e in json.loads(self.state("status").stdout)["findingStates"]}
        self.assertEqual(kinds, {"SPEC-1": "behavioral", "SPEC-2": "behavioral"}, marker)

    def test_a_pre_upgrade_red_still_reaches_green(self) -> None:
        marker = "PRE_UPGRADE_RED_CANNOT_GREEN"
        slug = "pre-upgrade"
        self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)

        def strip_red_command(document: dict) -> dict:
            for entry in document.get("behaviorMap", []):
                entry.pop("redCommand", None)
                entry.pop("redProof", None)
            return document
        self.rewrite_latest_evidence(strip_red_command)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        green = self.tdd(slug, "green", "BM_A", "a")
        self.assertEqual(green.returncode, 0, marker + ": " + green.stdout + green.stderr)
        self.assertEqual(self.map_status()["BM_A"], "green", marker)

    def test_a_refused_preflight_or_green_leaves_state_unchanged(self) -> None:
        marker = "PREFLIGHT_REFUSAL_MUTATED_STATE"
        slug = "refusal-state"
        (self.repo / "app.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "two attributes")
        begun = self.state("begin", "--slug", slug)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "preflight",
                   "--source", "codex-advisor", "--verdict", "completed")
        before, events = self.state("status").stdout, self.events()
        doc = self.tmp / "refused.json"
        blank = build_document("prose", behavior_map=self.two_items()); blank["invariants"] = "   "
        doc.write_text(json.dumps(blank), encoding="utf-8")
        hollow = self.state("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(doc))
        self.assertEqual(hollow.returncode, 2, marker + ": a blank section was recorded")
        self.assertIn("invariants", hollow.stderr, marker)
        broken = build_document("prose", behavior_map=[{"id": "BROKEN"}])
        doc.write_text(json.dumps(broken), encoding="utf-8")
        invalid = self.state("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(doc))
        self.assertEqual(invalid.returncode, 2, marker + ": an invalid map was recorded")
        self.assertEqual((self.state("status").stdout, self.events()), (before, events), marker + ": a refusal mutated state")
        self.record_preflight_evidence(slug, wid, behavior_map=self.two_items())
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        borrowed = self.workflow("tdd", "--slug", slug, "--phase", "green", "--behavior-id", "BM_B",
                                 "--", sys.executable, "-m", "unittest", "test_probe_a")
        self.assertEqual(borrowed.returncode, 2, marker + ": GREEN accepted another item's RED surface")
        self.assertEqual(self.map_status()["BM_B"], "red", marker)

    def test_an_edit_before_any_review_records_no_invalidation(self) -> None:
        marker = "NOOP_INVALIDATION_RECORDED"
        slug = "no-noop"
        self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        before = self.events()
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        self.post_edit("app.py")
        self.assertEqual(self.events(), before, marker + ": an edit before any review appended an event")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        record_context_forge(self.repo, self.tmp)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        reviewed = self.events()
        (self.repo / "app.py").write_text("a = 2\nb = 3\n", encoding="utf-8")
        self.post_edit("app.py")
        self.assertEqual(self.events(), reviewed + 1, marker + ": an edit after review did not invalidate it")
        self.assertEqual(json.loads(self.state("status").stdout)["codeReview"]["status"], "pending", marker)

    def final_result(self, slug: str, wid: str, name: str, verdict: str, material: bool) -> subprocess.CompletedProcess[str]:
        envelope = self.tmp / f"{name}.json"
        envelope.write_text(json.dumps({"schemaVersion": 1, "verdict": verdict, "findings": [
            {"id": "F-1", "claim": "an operation is unattacked", "material": material, "kind": "behavioral"}]}), encoding="utf-8")
        return self.state("advisor-result", "--slug", slug, "--workflow-id", wid, "--stage", "final",
                          "--source", "codex-advisor", "--input", str(envelope))

    def finding_entry(self) -> dict:
        return [e for e in json.loads(self.state("status").stdout)["findingStates"] if e["findingId"] == "F-1"][0]

    def test_a_nonmaterial_finding_re_raised_as_material_blocks_completion(self) -> None:
        marker = "RERAISED_MATERIAL_IGNORED"
        slug = "reraise-material"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        record_context_forge(self.repo, self.tmp)
        self.run_verification(slug)
        self.owner_phase("code-review", "passed", findings="none")
        note = self.final_result(slug, wid, "final-note", "commit-ready", False)
        self.assertEqual(note.returncode, 0, note.stdout + note.stderr)
        rejected = self.state("advisor-disposition", "--slug", slug, "--workflow-id", wid, "--stage", "final", "--findings", "addressed",
                              "--input", str(self.rejection_document(wid, self.finding_entry()["intakeEvidenceId"], "python -m unittest test_probe_a")))
        self.assertEqual(rejected.returncode, 0, rejected.stdout + rejected.stderr)
        appeal = self.final_result(slug, wid, "final-reraise", "fix-before-commit", True)
        self.assertEqual(appeal.returncode, 0, appeal.stdout + appeal.stderr)
        entry = self.finding_entry()
        self.assertEqual((entry.get("status"), entry.get("material")), ("pending", True), marker + ": " + json.dumps(entry))
        self.assertNotEqual(self.state("complete").returncode, 0, marker + ": completed with an undispositioned material re-raise")

    def test_a_re_raise_archives_the_first_rejection(self) -> None:
        marker = "FIRST_REJECTION_POINTER_LOST"
        self.rejected_then_re_raised("reraise-history")
        entry = self.finding_entry()
        history = entry.get("dispositionHistory") or []
        self.assertEqual([h.get("status") for h in history], ["rejected-with-evidence"], marker + ": " + json.dumps(entry))
        self.assertTrue(history[0].get("evidenceId"), marker)

    def test_the_first_rejection_survives_the_second_disposition(self) -> None:
        marker = "FIRST_REJECTION_LOST_AFTER_SECOND"
        wid, intake_id = self.rejected_then_re_raised("reraise-second")
        entry = self.finding_entry()
        self.assertEqual([(h.get("status"), h.get("evidenceId")) for h in entry.get("dispositionHistory") or []],
                         [("rejected-with-evidence", self.first_rejection)], marker + ": " + json.dumps(entry))
        second = self.state("advisor-disposition", "--slug", "reraise-second", "--workflow-id", wid, "--stage", "final",
                            "--findings", "addressed", "--input", str(self.rejection_document(wid, intake_id, "python -m unittest -v test_probe_a")))
        self.assertEqual(second.returncode, 0, marker + ": " + second.stdout + second.stderr)
        entry = self.finding_entry()
        self.assertEqual([h.get("evidenceId") for h in entry.get("dispositionHistory") or []], [self.first_rejection], marker + ": " + json.dumps(entry))
        self.assertEqual(entry.get("status"), "rejected-with-evidence", marker)
        self.assertNotEqual(entry.get("dispositionEvidenceId"), self.first_rejection, marker)

    def test_green_accepts_a_verbosity_flag_on_the_recorded_red_surface(self) -> None:
        marker = "GREEN_REFUSED_FOR_VERBOSITY_FLAG"
        slug = "green-flags"
        self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        green = self.workflow("tdd", "--slug", slug, "--phase", "green", "--behavior-id", "BM_A",
                              "--", sys.executable, "-m", "unittest", "-v", "test_probe_a")
        self.assertEqual(green.returncode, 0, marker + ": " + green.stdout + green.stderr)
        self.assertEqual(self.map_status()["BM_A"], "green", marker)

    def test_a_map_item_with_red_proof_but_no_red_command_is_refused(self) -> None:
        marker = "ORPHAN_RED_PROOF_ACCEPTED"
        slug = "orphan-redproof"
        (self.repo / "app.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "two attributes")
        begun = self.state("begin", "--slug", slug)
        wid = json.loads(begun.stdout)["workflowId"]
        record_context_forge(self.repo, self.tmp)
        orphan = {**pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO"),
                  "redProof": {"runner": "unittest", "marker": "A_NOT_TWO"}}
        path = self.tmp / "orphan.json"
        path.write_text(json.dumps(build_document("orphan redProof", behavior_map=[orphan])), encoding="utf-8")
        recorded = self.state("record-preflight", "--slug", slug, "--workflow-id", wid, "--input", str(path))
        self.assertNotEqual(recorded.returncode, 0, marker + ": " + recorded.stdout)

    def test_the_edit_gate_advises_a_pending_contract_item(self) -> None:
        marker = "GATE_STILL_DENIES_PENDING_ITEM"
        slug = "sweep-gate"
        self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        advised = self.intake("app.py")
        self.assertEqual(advised.returncode, 0, advised.stdout + advised.stderr)
        output = json.loads(advised.stdout)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output, marker + ": " + advised.stdout)
        self.assertIn("BM_B", output.get("additionalContext", ""), marker + ": " + advised.stdout)
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        self.assert_obligations_only("BM_A", "BM_B")

    def test_a_red_run_binds_the_tree_it_ran_on(self) -> None:
        marker = "RED_RUN_LACKS_TREE_BINDING"
        slug = "tree-binding"
        self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        head = self.rev_parse("HEAD")
        run = self.latest_run()
        self.assertEqual(run.get("productionChanged"), [], marker + ": " + json.dumps(run)[:300])
        self.assertEqual(run.get("headOid"), head, marker)
        self.assertEqual(run.get("passStartOid"), head, marker)
        self.assertNotIn("productionChanged", self.map_item("BM_A").get("redProof", {}), marker)

    def test_a_late_red_records_every_changed_production_path(self) -> None:
        marker = "LATE_RED_UNRECORDED"
        slug = "late-red"
        self.open_pass(slug)
        (self.repo / "app.py").write_text("a = 1\nb = 1\nc = 1\n", encoding="utf-8")
        (self.repo / "extra.py").write_text("d = 1\n", encoding="utf-8")
        self.git("add", "app.py")  # a staged edit is still a changed production path
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        self.assertEqual(self.latest_run().get("productionChanged"), ["app.py", "extra.py"], marker)
        self.assertEqual(self.map_item("BM_A").get("redProof", {}).get("productionChanged"), ["app.py", "extra.py"], marker)

    def test_a_commit_inside_the_pass_cannot_hide_a_late_red(self) -> None:
        marker = "LATE_RED_HIDDEN_BY_COMMIT"
        slug = "late-commit"
        self.open_pass(slug)
        start = self.rev_parse("HEAD")
        (self.repo / "app.py").write_text("a = 1\nb = 1\nc = 1\n", encoding="utf-8")
        self.git("commit", "-qam", "edited inside the pass")
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        run = self.latest_run()
        self.assertEqual(run.get("productionChanged"), ["app.py"], marker + ": " + json.dumps(run)[:300])
        self.assertEqual(run.get("passStartOid"), start, marker)
        self.assertNotEqual(run.get("headOid"), start, marker)

    def test_a_declared_contract_item_baselines_after_edits_with_the_change_recorded(self) -> None:
        marker = "DIRTY_BASELINE_STILL_REFUSED"
        slug = "declared-dirty"
        self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        baseline = self.tdd(slug, "red", "BM_B", "b")
        self.assertEqual(baseline.returncode, 0, marker + ": " + baseline.stdout + baseline.stderr)
        self.assertEqual(self.map_status()["BM_B"], "already-satisfied", marker)
        self.assertEqual(self.map_item("BM_B").get("baselineProof", {}).get("productionChanged"), ["app.py"], marker)
        self.assertIn("Late RED: BM_B", self.summary(), marker + ": a late baseline is labelled too: " + self.summary())

    def test_summary_names_a_late_red_and_keeps_it_after_green(self) -> None:
        marker = "LATE_RED_UNLABELED"
        slug = "late-summary"
        wid = self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        self.assertNotIn("Late RED", self.summary(), marker + ": " + self.summary())
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        self.assertIn("Late RED: BM_B", self.summary(), marker + ": " + self.summary())
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_B", "b").returncode, 0)
        self.assertIn("Late RED: BM_B", self.summary(), marker + ": " + self.summary())
        superseded = self.map_update(slug, wid, "supersede-b", {"sourceBehaviorId": "BM_B", "reassessment": "a sharper attack owns b",
            "items": [pending_behavior("BM_B2", behavior="b is two through the sharper probe", seam="app module", expected="app.b == 2", red_failure="B2_NOT_TWO")],
            "dispositions": [{"id": "BM_B", "status": "superseded", "supersededBy": "BM_B2", "evidence": "BM_B2 asserts the same outcome"}]})
        self.assertEqual(superseded.returncode, 0, superseded.stdout + superseded.stderr)
        self.assertIn("Late RED: BM_B", self.summary(), marker + ": lateness must survive supersession: " + self.summary())

    def test_the_final_review_checkpoint_carries_late_reds(self) -> None:
        marker = "LATE_RED_INVISIBLE_TO_FINAL_REVIEW"
        slug = "late-checkpoint"
        self.open_pass(slug)
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        checkpoint = json.loads(self.state("checkpoint", "--phase", "final-review").stdout)
        self.assertEqual(checkpoint.get("lateRed"), [], marker + ": " + json.dumps(checkpoint)[:400])
        (self.repo / "app.py").write_text("a = 1\nb = 1\nc = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        checkpoint = json.loads(self.state("checkpoint", "--phase", "final-review").stdout)
        self.assertEqual(checkpoint.get("lateRed"), [{"id": "BM_B", "productionChanged": ["app.py"]}], marker + ": " + json.dumps(checkpoint)[:400])

    def test_the_binding_describes_the_tree_the_red_was_launched_on(self) -> None:
        marker = "BINDING_MEASURED_AFTER_THE_RUN"
        slug = "launch-binding"
        self.open_pass(slug)
        start = self.rev_parse("HEAD")
        # A RED command that restores the file it was launched against before returning.
        (self.repo / "app.py").write_text("a = 1\nb = 1\nc = 1\n", encoding="utf-8")
        (self.repo / "test_probe_a.py").write_text(
            "import app, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    def tearDown(self): open('app.py', 'w').write('a = 1\\nb = 1\\n')\n"
            "    def test_value(self): self.assertEqual(app.a, 2, 'A_NOT_TWO')\n", encoding="utf-8")
        red = self.workflow("tdd", "--slug", slug, "--phase", "red", "--behavior-id", "BM_A",
                            "--", sys.executable, "-m", "unittest", "test_probe_a")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        self.assertEqual(self.latest_run().get("productionChanged"), ["app.py"], marker + ": " + json.dumps(self.latest_run())[:300])
        # A RED command that commits before returning.
        (self.repo / "test_probe_b.py").write_text(
            "import app, subprocess, unittest\n"
            "class Probe(unittest.TestCase):\n"
            "    def tearDown(self):\n"
            "        open('app.py', 'w').write('a = 1\\nb = 1\\nc = 3\\n')\n"
            "        subprocess.run(['git', 'commit', '-qam', 'committed by the probe'], check=True)\n"
            "    def test_value(self): self.assertEqual(app.b, 2, 'B_NOT_TWO')\n", encoding="utf-8")
        red = self.workflow("tdd", "--slug", slug, "--phase", "red", "--behavior-id", "BM_B",
                            "--", sys.executable, "-m", "unittest", "test_probe_b")
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        run = self.latest_run()
        self.assertEqual(run.get("headOid"), start, marker + ": " + json.dumps(run)[:300])
        self.assertEqual(run.get("productionChanged"), [], marker)

    def test_open_cycles_finish_after_a_late_baseline_lands_beside_them(self) -> None:
        marker = "OPEN_CYCLES_STRANDED_BY_LATE_BASELINE"
        slug = "open-cycles"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO"),
                                    pending_behavior("BM_C", behavior="c is two", seam="app module", expected="app.c == 2", red_failure="C_NOT_TWO"),
                                    pending_behavior("BM_D", behavior="d is two", seam="app module", expected="app.d == 2", red_failure="D_NOT_TWO")])
        (self.repo / "app.py").write_text("a = 1\nb = 1\nc = 1\nd = 1\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "four attributes")
        for item, attr in (("BM_A", "a"), ("BM_C", "c"), ("BM_D", "d")):
            self.assertEqual(self.tdd(slug, "red", item, attr).returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 2\nc = 1\nd = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        # Two cycles still red when the review-discovered item lands on the dirty tree.
        added = self.map_update(slug, wid, "add-b-open", {"sourceBehaviorId": "BM_A", "reassessment": "review found b must be two as well",
            "items": [pending_behavior("BM_B", behavior="b is two", seam="app module", expected="app.b == 2", red_failure="B_NOT_TWO")]})
        self.assertEqual(added.returncode, 0, marker + ": " + added.stdout + added.stderr)
        baseline = self.tdd(slug, "red", "BM_B", "b")
        self.assertEqual(baseline.returncode, 0, marker + ": " + baseline.stdout + baseline.stderr)
        self.assert_obligations_only("BM_C", "BM_D")
        (self.repo / "app.py").write_text("a = 2\nb = 2\nc = 2\nd = 2\n", encoding="utf-8")
        for item, attr in (("BM_C", "c"), ("BM_D", "d")):
            green = self.tdd(slug, "green", item, attr)
            self.assertEqual(green.returncode, 0, marker + ": " + green.stdout + green.stderr)
        self.assertEqual(self.map_status(), {"BM_A": "green", "BM_C": "green", "BM_D": "green", "BM_B": "already-satisfied"}, marker)
        # The fixture's own in-pass commit makes every item late; the late baseline must be among them.
        self.assertIn("BM_B", self.summary().rsplit("Late RED: ", 1)[-1], marker + ": " + self.summary())

    def test_a_late_red_stays_late_when_rerun_on_a_clean_tree(self) -> None:
        marker = "LATE_MARKER_LOST_ON_RERUN"
        slug = "sticky-late"
        self.open_pass(slug)
        (self.repo / "app.py").write_text("a = 1\nb = 1\nc = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        self.assertIn("Late RED: BM_A", self.summary())
        (self.repo / "app.py").write_text("a = 1\nb = 1\n", encoding="utf-8")  # reverted
        (self.repo / "extra.py").write_text("d = 1\n", encoding="utf-8")  # a different path is dirty for the rerun
        rerun = self.tdd(slug, "red", "BM_A", "a")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertEqual(self.map_item("BM_A").get("redProof", {}).get("productionChanged"), ["app.py", "extra.py"],
                         "UNION_DROPS_A_RECORDED_PATH: " + json.dumps(self.map_item("BM_A"))[:300])
        (self.repo / "extra.py").unlink()  # the next rerun launches clean
        rerun = self.tdd(slug, "red", "BM_A", "a")
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertEqual(self.map_item("BM_A").get("redProof", {}).get("productionChanged"), ["app.py", "extra.py"],
                         marker + ": " + json.dumps(self.map_item("BM_A"))[:300])
        self.assertIn("Late RED: BM_A", self.summary(), marker + ": " + self.summary())
        # A clean RED followed by a clean rerun stays unlabelled.
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        self.assertEqual(self.summary().rsplit("Late RED: ", 1)[-1].split(".")[0], "BM_A", marker + ": " + self.summary())

    def map_update(self, slug: str, wid: str, name: str, document: dict) -> subprocess.CompletedProcess[str]:
        path = self.tmp / f"{name}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return self.workflow("tdd-map", "--slug", slug, "--workflow-id", wid, "--input", str(path))

    def test_a_failing_added_surface_runs_red_then_green(self) -> None:
        marker = "ADDED_RED_GREEN_BROKEN"
        slug = "added-red-green"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 1\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        added = self.map_update(slug, wid, "add-b-failing", {"sourceBehaviorId": "BM_A", "reassessment": "review found b must be two as well",
            "items": [pending_behavior("BM_B", behavior="b is two", seam="app module", expected="app.b == 2", red_failure="B_NOT_TWO")]})
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        red = self.tdd(slug, "red", "BM_B", "b")
        self.assertEqual(red.returncode, 0, marker + ": " + red.stdout + red.stderr)
        self.assertEqual(self.map_status()["BM_B"], "red", marker + ": " + json.dumps(self.map_status()))
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        green = self.tdd(slug, "green", "BM_B", "b")
        self.assertEqual(green.returncode, 0, marker + ": " + green.stdout + green.stderr)
        self.assertEqual(self.map_status()["BM_B"], "green", marker)

    def test_a_baseline_owner_cannot_close_a_finding_as_fixed(self) -> None:
        marker = "BASELINE_OWNER_CLOSED_FIXED"
        slug = "baseline-owner"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        (self.repo / "app.py").write_text("a = 2\nb = 2\n", encoding="utf-8")
        self.assertEqual(self.tdd(slug, "green", "BM_A", "a").returncode, 0)
        record_context_forge(self.repo, self.tmp)
        self.run_verification(slug)
        intake = self.tmp / "review-intake.json"
        intake.write_text(json.dumps({"findings": [{"id": "SPEC-1", "axis": "Spec", "severity": "high", "material": True, "kind": "behavioral",
            "location": "app.py:2", "claim": "b must be two", "evidence": "b was one", "consequence": "callers read one", "smallest_action": "set b"}]}), encoding="utf-8")
        recorded = self.state("record-review", "--slug", slug, "--workflow-id", wid, "--resolved-model", "test-model",
                              "--review-context-id", "ctx", "--input", str(intake))
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        summary_id = json.loads(recorded.stdout)["summaryId"]
        added = self.map_update(slug, wid, "add-b-owner", {"sourceBehaviorId": "BM_A", "reassessment": "SPEC-1 needs an owning attack",
            "items": [{**pending_behavior("BM_B", behavior="b is two", seam="app module", expected="app.b == 2", red_failure="B_NOT_TWO"),
                       "sourceRefs": [{"type": "finding", "evidenceId": summary_id, "id": "SPEC-1"}]}]})
        self.assertEqual(added.returncode, 0, added.stdout + added.stderr)
        self.assertEqual(self.tdd(slug, "red", "BM_B", "b").returncode, 0)
        self.assertEqual(self.map_status()["BM_B"], "already-satisfied")
        tree = json.loads(self.state("status").stdout)["activeCandidateTree"]
        closure = self.tmp / "review-fixed.json"
        measurement = {"claim": "b is two", "command": "python -m unittest test_probe_b", "result": "OK"}
        closure.write_text(json.dumps({"context": {"workflowId": wid, "candidateTree": tree}, "intakeEvidenceId": summary_id,
            "dispositions": [{"finding_id": "SPEC-1", "status": "fixed", "kind": "behavioral", "premise": measurement,
                              "occurrence": {"domain": "every reader of b", "count": 0, "complete": True,
                                             "command": measurement["command"], "result": measurement["result"]},
                              "materialConsequence": measurement, "evidence": "BM_B baselined"}]}), encoding="utf-8")
        refused = self.state("record-review", "--slug", slug, "--workflow-id", wid, "--resolved-model", "test-model",
                             "--review-context-id", "ctx", "--input", str(closure))
        self.assertNotEqual(refused.returncode, 0, marker + ": " + refused.stdout + refused.stderr)
        self.assertIn("baseline alone", refused.stdout + refused.stderr, marker + ": " + refused.stdout + refused.stderr)

    def test_a_map_update_is_admitted_while_a_cycle_is_red(self) -> None:
        marker = "MAP_UPDATE_REFUSED_DURING_RED"
        slug = "map-during-red"
        wid = self.open_pass(slug, [pending_behavior("BM_A", behavior="a is two", seam="app module", expected="app.a == 2", red_failure="A_NOT_TWO")])
        self.assertEqual(self.tdd(slug, "red", "BM_A", "a").returncode, 0)
        added = self.map_update(slug, wid, "add-during-red", {"reassessment": "the RED exposed a sibling attribute that must change too",
            "items": [pending_behavior("BM_B", behavior="b is two", seam="app module", expected="app.b == 2", red_failure="B_NOT_TWO")]})
        self.assertEqual(added.returncode, 0, marker + ": " + added.stdout + added.stderr)
        self.assertEqual(self.map_status()["BM_B"], "pending", marker)


class ProducerMaskGuardTests(unittest.TestCase):
    def test_the_masked_producer_tests_skip_without_bwrap(self) -> None:
        """CI has no bwrap: the masking class must skip there, not error on spawn."""
        marker = "BWRAP_ABSENT_TESTS_ERROR"
        tools = Path(tempfile.mkdtemp(prefix="no-bwrap-")) / "bin"
        tools.mkdir()
        for tool in ("git", "python3"):
            (tools / tool).symlink_to(shutil.which(tool) or self.fail(f"suite requires {tool}"))
        run = subprocess.run(
            [sys.executable, "-m", "unittest", "hooks.tests.test_workflow_hooks.RevalidateWithoutProducerTests"],
            cwd=ROOT, env={**os.environ, "PATH": str(tools)}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertNotIn("FileNotFoundError", run.stderr, marker + ": " + run.stderr[-600:])
        self.assertIn("skipped", run.stderr, marker + ": " + run.stderr[-600:])
        self.assertEqual(run.returncode, 0, marker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
