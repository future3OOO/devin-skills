#!/usr/bin/env python3
"""Fail-closed intake proof for malformed mapped TDD evidence."""
from __future__ import annotations

import json
import os
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

from hooks.lib._workflow_db import database_path  # noqa: E402
from hooks.lib.workflow_state import (  # noqa: E402
    advisor_disposition,
    read_workflow,
    record_advisor_result,
)
from hooks.tests.support import (  # noqa: E402
    build_document,
    pending_behavior,
    record_context_forge,
)

WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"
QUALITY_GATE = ROOT / "skills" / "production-code" / "scripts" / "code_quality_gate.py"
INTAKE = ROOT / "hooks" / "rcf-intake-gate.py"


class MappedIntakeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mapped-intake-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.previous_state_root = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.tmp / "state")
        self.env = os.environ.copy()
        self.env.update(
            {
                "DEVIN_WORKFLOW_STATE_ROOT": str(self.tmp / "state"),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
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

    def command(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(WORKFLOW),
                args[0],
                "--repo",
                str(self.repo),
                *args[1:],
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_malformed_map_evidence_emits_advisory_context(self) -> None:
        begun = self.command(
            "begin", "--slug", "malformed-map", "--intent", "change app value"
        )
        self.assertEqual(begun.returncode, 0, begun.stdout + begun.stderr)
        state = json.loads(begun.stdout)
        slug = str(state["slug"])
        workflow_id = str(state["workflowId"])
        identity = record_context_forge(self.repo, self.tmp)
        record_advisor_result(
            identity, slug, workflow_id, "preflight", "codex-advisor", "completed"
        )
        advisor_disposition(identity, slug, workflow_id, "preflight", "none")
        preflight = self.tmp / "preflight.json"
        preflight.write_text(
            json.dumps(
                build_document(
                    "malformed-map intake",
                    behavior_map=[pending_behavior("BM_VALUE")],
                )
            ),
            encoding="utf-8",
        )
        recorded = self.command(
            "record-preflight",
            "--slug",
            slug,
            "--workflow-id",
            workflow_id,
            "--input",
            str(preflight),
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)

        (self.repo / "test_app.py").write_text(
            "import app, unittest\n"
            "class ValueTests(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(app.value, 2, 'VALUE_NOT_TWO')\n",
            encoding="utf-8",
        )
        red = self.command(
            "tdd",
            "--slug",
            slug,
            "--phase",
            "red",
            "--behavior-id",
            "BM_VALUE",
            "--",
            sys.executable,
            "-m",
            "unittest",
            "test_app.ValueTests.test_value",
        )
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)

        gate = subprocess.run(
            [sys.executable, str(QUALITY_GATE), "check", "--repo", str(self.repo), "--json"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        gate_path = self.tmp / "gate.json"
        gate_path.write_text(gate.stdout, encoding="utf-8")
        production_code = self.command(
            "record-production-code",
            "--slug",
            slug,
            "--workflow-id",
            workflow_id,
            "--input",
            str(gate_path),
        )
        self.assertEqual(
            production_code.returncode,
            0,
            production_code.stdout + production_code.stderr,
        )

        current = read_workflow(identity)
        evidence_id = str(current["tddEvidence"])
        connection = sqlite3.connect(database_path(identity))
        try:
            envelope = json.loads(
                connection.execute(
                    "SELECT document_json FROM evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()[0]
            )
            envelope["behaviorMap"] = [{"id": "BROKEN"}]
            connection.execute(
                "UPDATE evidence SET document_json = ? WHERE evidence_id = ?",
                (json.dumps(envelope), evidence_id),
            )
            connection.commit()
        finally:
            connection.close()

        intake = subprocess.run(
            [sys.executable, str(INTAKE)],
            cwd=self.repo,
            env=self.env,
            text=True,
            input=json.dumps(
                {"tool_input": {"file_path": str(self.repo / "app.py")}}
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(intake.returncode, 0, intake.stdout + intake.stderr)
        marker = "GATE_STILL_DENIES_UNREADABLE_EVIDENCE"
        output = json.loads(intake.stdout)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", output, marker + ": " + intake.stdout)
        self.assertIn("workflow evidence is unreadable", output["additionalContext"], marker)
        self.assertNotIn("Traceback", intake.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
