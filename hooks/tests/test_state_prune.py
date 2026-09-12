#!/usr/bin/env python3
"""Public contract for retiring workflow state: the real prune CLI over a synthetic root.

Every test builds its own state root under a temporary directory and points
DEVIN_WORKFLOW_STATE_ROOT at it. Pruning is destructive, so no test may ever
reach the estate's live root.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib.repo_identity import resolve_repo_identity
from hooks.lib.state_prune import RETAINED_HISTORIES

WORKFLOW_CLI = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"


def digests(directory: Path) -> dict[str, str]:
    """Every file under a directory by relative path and content hash."""
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


class StatePruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-state-prune-"))
        self.root = self.tmp / "state"
        self.root.mkdir(mode=0o700)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def prune(self, *args: str) -> dict[str, object]:
        """Run the real CLI against this test's synthetic root and parse its report."""
        environment = {
            **os.environ,
            "DEVIN_WORKFLOW_STATE_ROOT": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, str(WORKFLOW_CLI), "prune", *args],
            capture_output=True, text=True, encoding="utf-8", env=environment, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def slot(self, key: str, *, root: Path | None = None, workflow_id: str = "w0") -> Path:
        """A repository slot whose workflow points at a repository root that exists."""
        directory = self.root / key
        directory.mkdir(mode=0o700, exist_ok=True)
        if root is None:
            root = self.tmp / f"repo-{key}"
            root.mkdir(exist_ok=True)
        (directory / "workflow.json").write_text(json.dumps({
            "schemaVersion": 1,
            "repo": {"root": str(root), "key": key},
            "slug": "active-pass",
            "workflowId": workflow_id,
        }), encoding="utf-8")
        return directory

    def evidence(self, slot: Path, kind: str, slug: str, workflow_id: str, stamp: str) -> Path:
        """One producer-written evidence document owned by a workflow instance."""
        path = slot / f"{kind}-{slug}.json"
        field = "updatedAt" if kind == "tdd" else "recordedAt"
        path.write_text(json.dumps({
            "schemaVersion": 1, "slug": slug, "workflowId": workflow_id, field: stamp,
        }), encoding="utf-8")
        return path

    def test_a_held_lock_skips_the_slot_without_unlinking_or_deleting(self) -> None:
        """A busy slot is reported and left byte-identical; the lock survives.

        The lock is held by a second OS process against the real flock the
        workflow writers use, so this crosses the production mutual-exclusion
        contract rather than simulating contention.
        """
        slot = self.slot("busy-slot")
        # More superseded instances than the retention window, so the slot has a
        # genuinely removable candidate: without one, prune never needs the lock
        # and the skip path would go unexercised.
        for index in range(RETAINED_HISTORIES + 2):
            self.evidence(slot, "preflight", f"pass-{index}", f"w-{index}",
                          f"2026-01-{index + 1:02d}T00:00:00+00:00")
        lock = slot / ".workflow.lock"
        lock.touch(mode=0o600)
        before = digests(slot)

        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent("""
                import fcntl, sys, time
                handle = open(sys.argv[1], "w")
                fcntl.flock(handle, fcntl.LOCK_EX)
                print("held", flush=True)
                time.sleep(30)
            """), str(lock)],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            report = self.prune("--apply")
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()

        busy = [entry for entry in report["slots"] if entry["slot"] == "busy-slot"]
        self.assertEqual([entry["status"] for entry in busy], ["skipped"])
        self.assertIn("busy", busy[0]["reason"])
        self.assertTrue(lock.exists(), "a held lock must never be unlinked")
        self.assertEqual(digests(slot), before, "a busy slot must stay byte-identical")

    def decisions(self, report: dict[str, object], slot: str) -> dict[str, str]:
        entries = next(item for item in report["slots"] if item["slot"] == slot)["entries"]
        return {entry["path"]: entry["decision"] for entry in entries}

    def test_reporting_classifies_without_changing_anything(self) -> None:
        """Report-only decides exactly what apply would, and touches no bytes."""
        slot = self.slot("report-slot")
        for index in range(RETAINED_HISTORIES + 1):
            self.evidence(slot, "review", f"pass-{index}", f"w-{index}",
                          f"2026-02-{index + 1:02d}T00:00:00+00:00")
        before = digests(slot)

        reported = self.prune()
        self.assertEqual(digests(slot), before, "report-only must not change the state root")
        self.assertEqual(reported["applied"], False)

        applied = self.prune("--apply")
        self.assertEqual(
            {name for name, decision in self.decisions(reported, "report-slot").items() if decision == "removable"},
            {name for name, decision in self.decisions(applied, "report-slot").items() if decision == "removed"},
            "apply must remove exactly what the report called removable",
        )

    def test_reporting_does_not_create_a_missing_root(self) -> None:
        """A dry run must not bring into existence the state it describes."""
        absent = self.tmp / "never-created"
        self.root = absent
        self.assertEqual(self.prune()["slots"], [])
        self.assertFalse(absent.exists(), "report-only must not create the state root")

    def test_a_dead_slot_keeps_telemetry_and_unknown_files(self) -> None:
        """Death authorizes removing known workflow artifacts, never a blind sweep."""
        slot = self.root / "dead-slot"
        slot.mkdir(mode=0o700)
        (slot / "workflow.json").write_text(json.dumps({
            "schemaVersion": 1, "workflowId": "w-dead",
            "repo": {"root": str(self.tmp / "gone"), "key": "dead-slot"},
        }), encoding="utf-8")
        self.evidence(slot, "preflight", "old", "w-dead", "2026-01-01T00:00:00+00:00")
        telemetry = slot / "stop-latch-log.jsonl"
        telemetry.write_text('{"event":"latched"}\n', encoding="utf-8")
        unknown = slot / "quality-deadbeef.json"
        unknown.write_text('{"legacy":true}\n', encoding="utf-8")
        telemetry_before, unknown_before = digests(slot)["stop-latch-log.jsonl"], digests(slot)["quality-deadbeef.json"]

        decisions = self.decisions(self.prune("--apply"), "dead-slot")
        self.assertEqual(decisions["preflight-old.json"], "removed")
        self.assertEqual(decisions["workflow.json"], "removed",
                         "a valid snapshot whose repository is gone is itself a dead workflow artifact")
        self.assertEqual(decisions["stop-latch-log.jsonl"], "retained")
        self.assertEqual(decisions["quality-deadbeef.json"], "retained")
        self.assertEqual(digests(slot)["stop-latch-log.jsonl"], telemetry_before, "telemetry is never removed")
        self.assertEqual(digests(slot)["quality-deadbeef.json"], unknown_before, "unknown files are never removed")

    def untrusted_snapshot_case(self, name: str, arrange) -> None:
        """One untrusted-snapshot shape: the whole slot survives byte-identically."""
        slot = self.root / name
        slot.mkdir(mode=0o700)
        arrange(slot)
        for index in range(RETAINED_HISTORIES + 2):
            self.evidence(slot, "review", f"pass-{index}", f"w-{index}",
                          f"2026-09-{index + 1:02d}T00:00:00+00:00")
        (slot / "active-pass.json").write_text(json.dumps({"slug": "p"}), encoding="utf-8")
        before = digests(slot)

        report = self.prune("--apply")
        entries = next(item for item in report["slots"] if item["slot"] == name)["entries"]
        self.assertEqual({entry["decision"] for entry in entries}, {"retained"},
                         f"{name}: an untrusted snapshot must retain every artifact")
        self.assertEqual({entry["reason"] for entry in entries}, {"indeterminate-workflow"})
        self.assertEqual(digests(slot), before, f"{name}: the slot must stay byte-identical")

    def test_a_present_but_invalid_snapshot_preserves_the_whole_slot(self) -> None:
        """A snapshot that exists but cannot be trusted is indeterminate, not dead.

        Unparsable data may be corruption; an unknown schema may be a newer
        estate; a snapshot binding no instance would turn every artifact into a
        superseded history. None confirms the slot is retired - dead-slot
        pruning is reserved for an absent snapshot or a valid one whose
        repository root is confirmed gone.
        """
        def snapshot(slot: Path, text: str) -> None:
            (slot / "workflow.json").write_text(text, encoding="utf-8")

        self.untrusted_snapshot_case(
            "indeterminate-malformed", lambda slot: snapshot(slot, "{ corrupted"))
        self.untrusted_snapshot_case(
            "indeterminate-newer-schema", lambda slot: snapshot(slot, json.dumps(
                {"schemaVersion": 2, "repo": {"root": str(self.tmp)}})))
        self.untrusted_snapshot_case(
            "indeterminate-no-instance", lambda slot: snapshot(slot, json.dumps(
                {"schemaVersion": 1, "repo": {"root": str(self.tmp)}})))
        self.untrusted_snapshot_case(
            "indeterminate-coerced-schema", lambda slot: snapshot(slot, json.dumps(
                {"schemaVersion": True, "workflowId": "w-c",
                 "repo": {"root": str(self.tmp / "gone")}})))
        self.untrusted_snapshot_case(
            "indeterminate-nul-root", lambda slot: snapshot(slot, json.dumps(
                {"schemaVersion": 1, "workflowId": "w-n",
                 "repo": {"root": "bad\u0000root"}})))

        def symlinked(slot: Path) -> None:
            target = self.tmp / "elsewhere.json"
            target.write_text(json.dumps({
                "schemaVersion": 1, "workflowId": "w-x",
                "repo": {"root": str(self.tmp / "gone")},
            }), encoding="utf-8")
            (slot / "workflow.json").symlink_to(target)

        self.untrusted_snapshot_case("indeterminate-symlinked", symlinked)

    def test_a_live_slot_keeps_the_active_pass_and_four_recent_histories(self) -> None:
        """The active workflow, what it references, and the four newest survive."""
        slot = self.slot("live-slot", workflow_id="w-active")
        active = self.evidence(slot, "preflight", "active-pass", "w-active", "2026-03-09T00:00:00+00:00")
        workflow = json.loads((slot / "workflow.json").read_text())
        workflow["verificationEvidence"] = str(active)
        (slot / "workflow.json").write_text(json.dumps(workflow), encoding="utf-8")
        for index in range(6):
            self.evidence(slot, "review", f"old-{index}", f"w-{index}",
                          f"2026-03-{index + 1:02d}T00:00:00+00:00")
        unreadable = slot / "tdd-corrupt.json"
        unreadable.write_text("{ not json", encoding="utf-8")

        decisions = self.decisions(self.prune("--apply"), "live-slot")
        self.assertEqual(decisions["workflow.json"], "retained")
        self.assertEqual(decisions["preflight-active-pass.json"], "retained")
        self.assertEqual(decisions["tdd-corrupt.json"], "retained", "unreadable owner is preserved, not guessed")
        # Six superseded instances, newest four kept: w-2..w-5 survive, w-0/w-1 go.
        self.assertEqual([decisions[f"review-old-{index}.json"] for index in range(6)],
                         ["removed", "removed", "retained", "retained", "retained", "retained"])

    def test_each_invocation_replans_from_the_current_state(self) -> None:
        """An earlier report never authorises a later apply; each run re-decides.

        This is deliberately NOT proof of the in-run stale-plan guard, which
        covers the window between classification and lock acquisition inside a
        single invocation. That window has no deterministic seam and is
        untested; see the module's plan/apply revalidation.
        """
        slot = self.slot("racing-slot")
        for index in range(RETAINED_HISTORIES + 1):
            self.evidence(slot, "review", f"pass-{index}", f"w-{index}",
                          f"2026-04-{index + 1:02d}T00:00:00+00:00")
        oldest = slot / "review-pass-0.json"
        self.assertEqual(self.decisions(self.prune(), "racing-slot")[oldest.name], "removable")

        # Rewrite the planned candidate as a live instance would, then re-plan:
        # the fresh read reclassifies it, and its bytes no longer match.
        oldest.write_text(json.dumps({
            "schemaVersion": 1, "slug": "pass-0", "workflowId": "w-0",
            "recordedAt": "2026-12-31T00:00:00+00:00",
        }), encoding="utf-8")
        rewritten = digests(slot)[oldest.name]
        self.prune("--apply")
        self.assertTrue(oldest.exists(), "a candidate rewritten as the newest history must survive")
        self.assertEqual(digests(slot)[oldest.name], rewritten)

    def test_an_unverifiable_candidate_is_never_deleted(self) -> None:
        """A file that stops being readable survives, whichever guard catches it.

        Classification preserves it as an unreadable owner; the delete-time
        digest recheck is the second guard, for a file that turns unreadable
        after the plan is made. Either way the invariant is the same: prune
        never unlinks what it cannot identify.
        """
        slot = self.slot("unreadable-slot")
        for index in range(RETAINED_HISTORIES + 1):
            self.evidence(slot, "review", f"pass-{index}", f"w-{index}",
                          f"2026-05-{index + 1:02d}T00:00:00+00:00")
        oldest = slot / "review-pass-0.json"
        self.assertEqual(self.decisions(self.prune(), "unreadable-slot")[oldest.name], "removable")

        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("permission-denied behavior requires an unprivileged user")
        oldest.chmod(0o000)
        try:
            decision = self.decisions(self.prune("--apply"), "unreadable-slot")[oldest.name]
            self.assertIn(decision, {"retained", "skipped"})
            self.assertNotEqual(decision, "removed")
            self.assertTrue(oldest.exists(), "an unreadable candidate must survive")
        finally:
            oldest.chmod(0o600)

    def test_the_active_pass_marker_follows_slot_liveness(self) -> None:
        """A slug-only marker binds no instance, so only slot death retires it."""
        live = self.slot("marker-live")
        (live / "active-pass.json").write_text(json.dumps({
            "schemaVersion": 1, "slug": "some-pass", "updatedAt": "2026-01-01T00:00:00+00:00",
        }), encoding="utf-8")
        dead = self.root / "marker-dead"
        dead.mkdir(mode=0o700)
        (dead / "workflow.json").write_text(json.dumps({
            "schemaVersion": 1, "workflowId": "w-dead",
            "repo": {"root": str(self.tmp / "gone"), "key": "marker-dead"},
        }), encoding="utf-8")
        (dead / "active-pass.json").write_text(json.dumps({
            "schemaVersion": 1, "slug": "some-pass", "updatedAt": "2026-01-01T00:00:00+00:00",
        }), encoding="utf-8")

        report = self.prune("--apply")
        self.assertEqual(self.decisions(report, "marker-live")["active-pass.json"], "retained")
        self.assertEqual(self.decisions(report, "marker-dead")["active-pass.json"], "removed")
        self.assertTrue((live / "active-pass.json").exists())
        self.assertFalse((dead / "active-pass.json").exists())

    def test_retention_decides_whole_instances_and_breaks_ties_by_id(self) -> None:
        """An instance's artifacts share one fate, and tied stamps order by id.

        Every artifact of a retained instance survives and every artifact of a
        retired one goes, ranked by the instance's newest stamp; identical
        stamps fall back to the workflow id so the order is total and
        independent of directory iteration.
        """
        slot = self.slot("grouped-slot", workflow_id="w-active")
        stamp = "2026-08-01T00:00:00+00:00"
        # Six instances, deliberately unsorted ids, all tied on the same stamp;
        # each carries two artifact kinds so grouping is observable.
        for identifier in ("w-b", "w-f", "w-a", "w-d", "w-c", "w-e"):
            self.evidence(slot, "review", identifier, identifier, stamp)
            self.evidence(slot, "tdd", identifier, identifier, stamp)

        decisions = self.decisions(self.prune("--apply"), "grouped-slot")
        for identifier in ("w-f", "w-e", "w-d", "w-c"):
            self.assertEqual(decisions[f"review-{identifier}.json"], "retained", identifier)
            self.assertEqual(decisions[f"tdd-{identifier}.json"], "retained", identifier)
        for identifier in ("w-b", "w-a"):
            self.assertEqual(decisions[f"review-{identifier}.json"], "removed", identifier)
            self.assertEqual(decisions[f"tdd-{identifier}.json"], "removed", identifier)

    def test_a_naive_timestamp_is_preserved_not_crashed_on(self) -> None:
        """An offset-less stamp cannot be ordered against the aware ones every
        current writer emits; the artifact is preserved, and prune still runs.
        """
        slot = self.slot("naive-slot", workflow_id="w-active")
        for index in range(RETAINED_HISTORIES + 1):
            self.evidence(slot, "review", f"pass-{index}", f"w-{index}",
                          f"2026-07-{index + 1:02d}T00:00:00+00:00")
        naive = slot / "review-naive.json"
        naive.write_text(json.dumps({
            "schemaVersion": 1, "slug": "naive", "workflowId": "w-naive",
            "recordedAt": "2026-07-20T00:00:00",
        }), encoding="utf-8")

        decisions = self.decisions(self.prune("--apply"), "naive-slot")
        self.assertEqual(decisions["review-naive.json"], "retained",
                         "an unorderable stamp must preserve the artifact")
        self.assertTrue(naive.exists())

    def test_a_symlinked_slot_is_never_traversed(self) -> None:
        """Apply must not follow a slot symlink and delete outside the root."""
        outside = self.tmp / "outside"
        outside.mkdir()
        for index in range(RETAINED_HISTORIES + 1):
            (outside / f"review-out-{index}.json").write_text(json.dumps({
                "workflowId": f"w-{index}", "recordedAt": f"2026-06-{index + 1:02d}T00:00:00+00:00",
            }), encoding="utf-8")
        (self.root / "linked").symlink_to(outside)
        before = digests(outside)

        report = self.prune("--apply")
        self.assertNotIn("linked", [entry["slot"] for entry in report["slots"]],
                         "a symlinked slot must not be classified at all")
        self.assertEqual(digests(outside), before, "files outside the root must survive")

    def test_apply_is_rejected_for_every_other_action(self) -> None:
        """A destructive-mode flag must not be silently ignored elsewhere."""
        environment = {
            **os.environ,
            "DEVIN_WORKFLOW_STATE_ROOT": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, str(WORKFLOW_CLI), "status", "--repo", str(self.tmp), "--apply"],
            capture_output=True, text=True, encoding="utf-8", env=environment, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unrecognized arguments: --apply", result.stderr)


    def test_advisor_pointers_follow_the_workflow_decision(self) -> None:
        """A classifiable pointer shares its workflow's fate; the rest survive.

        Classifiable means the filename carries the owning slot's key prefix
        and the instance id of a workflow this run retired. A foreign-prefix,
        suffix-less, or unknown-instance pointer retains fail-closed.
        """
        slot = self.slot("1111", workflow_id="a" * 32)
        for index in range(6):
            self.evidence(slot, "review", f"old-{index}", f"{index:032x}",
                          f"2026-03-{index + 1:02d}T00:00:00+00:00")
        sessions = self.root / "_advisor-sessions"
        sessions.mkdir(mode=0o700)
        retired_sid = sessions / f"1111-old-pass-{0:032x}.sid"
        retained_sid = sessions / f"1111-old-pass-{5:032x}.sid"
        foreign_sid = sessions / f"9999-old-pass-{1:032x}.sid"
        unowned_sid = sessions / "1111-legacy-pass.sid"
        newline_sid = sessions / f"1111-old-pass-{1:032x}.sid\n"
        for sid in (retired_sid, retained_sid, foreign_sid, unowned_sid, newline_sid):
            sid.write_text("session\n", encoding="utf-8")

        report = self.prune("--apply")
        fates = {entry["path"]: entry["decision"] for entry in report["advisorSessions"]}
        self.assertEqual(fates[retired_sid.name], "removed",
                         "a pointer to a retired history follows it out")
        self.assertEqual(fates[retained_sid.name], "retained")
        self.assertEqual(fates[foreign_sid.name], "retained",
                         "a foreign-prefix pointer never follows another repository's decision")
        self.assertEqual(fates[unowned_sid.name], "retained")
        self.assertEqual(fates[newline_sid.name], "retained",
                         "a malformed newline-suffixed filename is never an owned pointer")
        self.assertFalse(retired_sid.exists())
        self.assertTrue(retained_sid.exists() and foreign_sid.exists()
                        and unowned_sid.exists() and newline_sid.exists())

    def test_an_inaccessible_repository_root_preserves_the_slot(self) -> None:
        """A stat failure that is not absence is indeterminate, not dead or a crash."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("permission-denied behavior requires an unprivileged user")
        guarded = self.tmp / "guarded"
        repo_root = guarded / "repo"
        repo_root.mkdir(parents=True)
        slot = self.slot("blocked-slot", root=repo_root)
        self.evidence(slot, "review", "old", "w-old", "2026-01-01T00:00:00+00:00")
        before = digests(slot)

        guarded.chmod(0o000)
        try:
            report = self.prune("--apply")
        finally:
            guarded.chmod(0o700)
        entries = next(item for item in report["slots"] if item["slot"] == "blocked-slot")["entries"]
        self.assertEqual({entry["reason"] for entry in entries}, {"indeterminate-workflow"})
        self.assertEqual(digests(slot), before, "an unreachable root must preserve the slot")

    def test_one_unreadable_slot_does_not_abort_the_estate_run(self) -> None:
        """A slot whose directory cannot be read is skipped; siblings still run."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("permission-denied behavior requires an unprivileged user")
        blocked = self.slot("aa-blocked")
        readable = self.slot("bb-readable")
        self.evidence(readable, "review", "ok", "w-ok", "2026-01-01T00:00:00+00:00")

        blocked.chmod(0o000)
        try:
            report = self.prune("--apply")
        finally:
            blocked.chmod(0o700)
        statuses = {entry["slot"]: entry["status"] for entry in report["slots"]}
        self.assertEqual(statuses["aa-blocked"], "skipped")
        self.assertIn("classification-failed", next(
            entry["reason"] for entry in report["slots"] if entry["slot"] == "aa-blocked"))
        self.assertIn("bb-readable", statuses, "a readable sibling must still be classified")

    def test_one_unreadable_sqlite_slot_does_not_abort_the_estate_run(self) -> None:
        """The SQLite probe and report path stay inside the per-slot failure boundary."""
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("permission-denied behavior requires an unprivileged user")
        blocked = self.root / "aa-sqlite-blocked"
        blocked.mkdir(mode=0o700)
        sqlite3.connect(blocked / "workflow.sqlite3").close()
        readable = self.slot("bb-readable-sqlite")
        self.evidence(readable, "review", "ok", "w-ok", "2026-01-01T00:00:00+00:00")

        blocked.chmod(0o000)
        try:
            report = self.prune("--apply")
        finally:
            blocked.chmod(0o700)
        slots = {entry["slot"]: entry for entry in report["slots"]}
        self.assertEqual(slots["aa-sqlite-blocked"]["status"], "skipped")
        self.assertIn("classification-failed", slots["aa-sqlite-blocked"]["reason"])
        self.assertIn("bb-readable-sqlite", slots,
                      "an unreadable SQLite slot aborted a readable sibling")

    def test_a_directory_shaped_lock_skips_only_that_slot(self) -> None:
        """A lock that cannot even be opened skips its slot; siblings still run."""
        malformed = self.slot("aa-badlock")
        for index in range(RETAINED_HISTORIES + 1):
            self.evidence(malformed, "review", f"pass-{index}", f"w-{index}",
                          f"2026-10-{index + 1:02d}T00:00:00+00:00")
        (malformed / ".workflow.lock").mkdir()
        readable = self.slot("bb-goodlock")
        self.evidence(readable, "review", "ok", "w-ok", "2026-01-01T00:00:00+00:00")
        before = digests(malformed)

        report = self.prune("--apply")
        statuses = {entry["slot"]: entry["status"] for entry in report["slots"]}
        self.assertEqual(statuses["aa-badlock"], "skipped")
        self.assertIn("lock-failed", next(
            entry["reason"] for entry in report["slots"] if entry["slot"] == "aa-badlock"))
        self.assertEqual(digests(malformed), before, "a slot with an unopenable lock loses nothing")
        self.assertEqual(statuses["bb-goodlock"], "applied", "a readable sibling must still process")

    def test_a_dead_snapshots_own_pointer_follows_it_out(self) -> None:
        """A dead slot holding only its snapshot still retires its pointer.

        The snapshot is the sole carrier of the instance id there, so its
        removable entry must keep that id or the pointer strands forever.
        """
        dead = self.root / "2222"
        dead.mkdir(mode=0o700)
        dead_wid = "d" * 32
        (dead / "workflow.json").write_text(json.dumps({
            "schemaVersion": 1, "workflowId": dead_wid,
            "repo": {"root": str(self.tmp / "gone"), "key": "2222"},
        }), encoding="utf-8")
        sessions = self.root / "_advisor-sessions"
        sessions.mkdir(mode=0o700)
        sid = sessions / f"2222-dead-pass-{dead_wid}.sid"
        sid.write_text("session\n", encoding="utf-8")

        report = self.prune("--apply")
        fates = {entry["path"]: entry["decision"] for entry in report["advisorSessions"]}
        self.assertEqual(fates[sid.name], "removed",
                         "a snapshot-only dead workflow must still retire its pointer")
        self.assertFalse(sid.exists())

    def real_repo_identity(self, name: str):
        """A real temporary git repository resolved through the production identity."""
        from hooks.lib.repo_identity import resolve_repo_identity
        repo = self.tmp / name
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        return resolve_repo_identity(repo)

    def dead_stop_slot(self, name: str = "stoprepo"):
        """A classifiably dead slot holding real producer-written Stop documents.

        Both documents are created through the real stop_session_swap writer,
        so the persisted shape is the producer's, and the repository is then
        deleted to make the slot dead.
        """
        from hooks.lib.state_store import stop_session_swap
        identity = self.real_repo_identity(name)
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.root)
        try:
            # state_root() reads the override per call, so the real writer
            # lands in this test's synthetic root with no reload tricks.
            stop_session_swap(identity, "sess-a", "blockFingerprint", "abc123")
            # The resolution path writes an empty fingerprint, so an empty
            # string is a real producer payload and must stay removable.
            stop_session_swap(identity, "sess-b", "blockFingerprint", "")
        finally:
            os.environ.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
        slot = self.root / identity.key
        (slot / "workflow.json").write_text(json.dumps({
            "schemaVersion": 1, "workflowId": "w-dead",
            "repo": identity.as_dict(),
        }), encoding="utf-8")
        shutil.rmtree(self.tmp / name)
        return identity, slot

    def test_dead_slot_stop_documents_are_item_reported_and_removed(self) -> None:
        """Only current-producer Stop documents follow their dead slot out.

        Everything the producer contract does not cover - malformed JSON and a
        name no producer writes - is preserved with its own reported reason.
        """
        identity, slot = self.dead_stop_slot()
        malformed = slot / "stop" / "malformed.json"
        malformed.write_text("{not json", encoding="utf-8")
        unknown = slot / "stop" / "notes.txt"
        unknown.write_text('{"schemaVersion": 1, "message": "valid json, wrong name"}', encoding="utf-8")

        report = self.prune("--apply")
        entries = {e["path"]: (e["decision"], e["reason"]) for s in report["slots"]
                   for e in s["entries"] if s["slot"] == identity.key}
        self.assertEqual(entries.get("stop/sess-a.json"), ("removed", "dead-slot"),
                         "a producer-written stop document must be item-reported and removed")
        self.assertEqual(entries.get("stop/sess-b.json"), ("removed", "dead-slot"),
                         "an empty fingerprint is a real producer payload, not an unknown shape")
        self.assertEqual(entries.get("stop/malformed.json"), ("retained", "unknown-shape"),
                         "malformed JSON must survive with its own reason")
        self.assertEqual(entries.get("stop/notes.txt"), ("retained", "unknown-kind"),
                         "a name no producer writes must survive with its own reason")
        self.assertTrue(malformed.exists() and unknown.exists())

    def test_apply_mutates_only_what_the_report_planned(self) -> None:
        """What apply deletes is exactly what report-only mode called removable.

        The plan is taken from a separate report run, so an apply-time
        mutation the plan never contained - an emptied stop/ directory - is
        visible as a difference rather than reported into existence.
        """
        _, slot = self.dead_stop_slot("fidelityrepo")
        planned = {f"{s['slot']}/{e['path']}" for s in self.prune()["slots"]
                   for e in s["entries"] if e["decision"] == "removable"}
        before = {str(path.relative_to(self.root)) for path in self.root.rglob("*")}

        self.prune("--apply")
        after = {str(path.relative_to(self.root)) for path in self.root.rglob("*")}
        self.assertEqual(before - after, planned,
                         "apply must mutate exactly the paths report mode planned")
        self.assertTrue((slot / "stop").is_dir(),
                        "an emptied stop directory is not the report's to remove")

    def test_session_associations_follow_their_repositorys_liveness(self) -> None:
        """Producer-written associations retire with a confirmed-absent root only.

        Both markers are created through the real record_session_association
        writer; one repository is then deleted. Malformed session data is
        preserved untouched.
        """
        from hooks.lib.state_store import record_session_association
        dead = self.real_repo_identity("deadrepo")
        live = self.real_repo_identity("liverepo")
        os.environ["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.root)
        try:
            record_session_association("sess-b", dead)
            record_session_association("sess-b", live)
        finally:
            os.environ.pop("DEVIN_WORKFLOW_STATE_ROOT", None)
        shutil.rmtree(self.tmp / "deadrepo")
        malformed = self.root / "sessions" / "sess-b" / "marker.json"
        malformed.write_text('{"kept":true}\n', encoding="utf-8")

        report = self.prune("--apply")
        fates = {entry["path"]: entry["decision"] for entry in report["sessions"]}
        self.assertEqual(fates[f"sess-b/{dead.key}.json"], "removed",
                         "an association whose repository root is confirmed absent retires")
        self.assertEqual(fates[f"sess-b/{live.key}.json"], "retained")
        self.assertEqual(fates["sess-b/marker.json"], "retained")
        self.assertFalse((self.root / "sessions" / "sess-b" / f"{dead.key}.json").exists())
        self.assertTrue((self.root / "sessions" / "sess-b" / f"{live.key}.json").exists())
        self.assertTrue(malformed.exists())

    def test_the_real_wrapper_pointer_reaches_prune_under_a_shared_root(self) -> None:
        """The wrapper's sid, written under the override root, is visible to prune.

        The real wrapper runs offline (it dies at its alias-parse stage, after
        the pointer is written); nothing may land under the distinct
        DEVIN_ESTATE_HOME fallback.
        """
        wrapper = ROOT / "skills" / "codex-advisor" / "scripts" / "ask-codex-advisor.sh"
        repo = self.tmp / "wrapperrepo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        fallback = self.tmp / "fallback-home"
        subprocess.run(
            [str(wrapper), "--slug", "shared-root", "--cwd", str(repo), "--", "q"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(self.tmp / "home"),
                 "DEVIN_ESTATE_HOME": str(fallback),
                 "DEVIN_WORKFLOW_STATE_ROOT": str(self.root)},
        )
        pointers = list((self.root / "_advisor-sessions").glob("*.sid"))
        self.assertEqual(len(pointers), 1, "the wrapper must write its pointer under the shared root")
        self.assertFalse((fallback / "state" / "_advisor-sessions").exists(),
                         "nothing may land under the DEVIN_ESTATE_HOME fallback")

        report = self.prune()
        names = {entry["path"] for entry in report["advisorSessions"]}
        self.assertIn(pointers[0].name, names,
                      "prune must see the pointer the real wrapper just wrote")

    def test_unknown_session_shapes_are_preserved(self) -> None:
        """Files directly under sessions/ fit no association shape and survive."""
        shared = self.root / "sessions"
        shared.mkdir(mode=0o700)
        (shared / "marker.json").write_text('{"kept":true}\n', encoding="utf-8")
        before = digests(self.root)
        report = self.prune("--apply")
        self.assertEqual([entry["slot"] for entry in report["slots"]], [])
        self.assertEqual(digests(self.root), before)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.rstrip("\n")


def digest_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class SQLiteStatePruneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-prune-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Workflow Harness")
        (self.repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        git(self.repo, "add", "app.py")
        git(self.repo, "commit", "-q", "-m", "base")
        self.state_root = self.tmp / "state"
        self.env = os.environ.copy()
        self.env["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.state_root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(WORKFLOW_CLI), *args], cwd=self.repo, env=self.env,
            text=True, encoding="utf-8", stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )

    def begin(self, slug: str) -> dict[str, object]:
        result = self.cli("begin", "--repo", str(self.repo), "--slug", slug)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_report_only_does_not_create_or_mutate_state(self) -> None:
        missing = self.cli("prune")
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertFalse(self.state_root.exists())

        self.begin("active")
        before = digest_tree(self.state_root)
        reported = self.cli("prune")
        self.assertEqual(reported.returncode, 0, reported.stderr)
        self.assertFalse(json.loads(reported.stdout)["applied"])
        self.assertEqual(digest_tree(self.state_root), before)

    def test_sqlite_apply_retains_active_and_four_recent_histories(self) -> None:
        passes = [self.begin(f"pass-{index}") for index in range(6)]
        slot = self.state_root / resolve_repo_identity(self.repo).key
        oldest = str(passes[0]["workflowId"])
        pointer_dir = self.state_root / "_advisor-sessions"
        pointer_dir.mkdir(mode=0o700)
        pointer = pointer_dir / f"{slot.name}-pass-0-{oldest}.sid"
        pointer.write_text("session\n", encoding="utf-8")

        report = json.loads(self.cli("prune").stdout)
        decisions = {
            item["workflowId"]: item["decision"]
            for item in report["slots"][0]["workflows"]
        }
        self.assertEqual(decisions[oldest], "removable")
        self.assertEqual(sum(value == "retained" for value in decisions.values()), 5)
        self.assertTrue(pointer.exists(), "report-only prune must not remove advisor pointers")

        applied = self.cli("prune", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        result = json.loads(applied.stdout)
        self.assertEqual(result["advisorSessions"][0]["decision"], "removed")
        self.assertFalse(pointer.exists())
        history = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertNotIn(oldest, {event["workflowId"] for event in history})
        self.assertEqual(len({event["workflowId"] for event in history}), 5)
        self.assertEqual(json.loads(self.cli("status", "--repo", str(self.repo)).stdout)["workflowId"], passes[-1]["workflowId"])

    def test_sqlite_busy_apply_refuses_without_deleting(self) -> None:
        passes = [self.begin(f"pass-{index}") for index in range(6)]
        slot = self.state_root / resolve_repo_identity(self.repo).key
        database = slot / "workflow.sqlite3"
        connection = sqlite3.connect(database, timeout=0, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = self.cli("prune", "--apply")
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)["slots"][0]
        self.assertEqual(report["status"], "skipped")
        self.assertTrue(any(item["reason"] == "busy-database" for item in report["workflows"]))
        history = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)["events"]
        self.assertEqual(len({event["workflowId"] for event in history}), len(passes))

    def test_copied_authoritative_database_is_not_pruned_under_the_wrong_slot(self) -> None:
        self.begin("active")
        source = self.state_root / resolve_repo_identity(self.repo).key / "workflow.sqlite3"
        copied = self.state_root / "copied-slot"
        copied.mkdir(mode=0o700)
        shutil.copy2(source, copied / "workflow.sqlite3")

        result = self.cli("prune", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = next(
            slot for slot in json.loads(result.stdout)["slots"]
            if slot["slot"] == "copied-slot"
        )
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["store"], "unknown")
        self.assertIn("repository identity", report["reason"])
        self.assertTrue((copied / "workflow.sqlite3").exists())

    def test_unsupported_event_schema_is_not_pruned(self) -> None:
        """An authoritative but unreadable ledger is preserved whole."""
        for index in range(6):
            self.begin(f"pass-{index}")
        slot = self.state_root / resolve_repo_identity(self.repo).key
        database = slot / "workflow.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "UPDATE workflow_events SET state_schema_version = 999 "
                "WHERE event_id = (SELECT MAX(event_id) FROM workflow_events)"
            )
            connection.commit()
        finally:
            connection.close()
        before = hashlib.sha256(database.read_bytes()).hexdigest()

        result = self.cli("prune", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = next(
            item for item in json.loads(result.stdout)["slots"]
            if item["slot"] == slot.name
        )
        self.assertEqual(report["store"], "unknown")
        self.assertEqual(report["status"], "skipped")
        self.assertIn("event schema or policy", report["reason"])
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_configured_symlinked_state_root_is_traversed(self) -> None:
        self.begin("active")
        linked = self.tmp / "linked-state"
        linked.symlink_to(self.state_root, target_is_directory=True)
        previous = self.env["DEVIN_WORKFLOW_STATE_ROOT"]
        self.env["DEVIN_WORKFLOW_STATE_ROOT"] = str(linked)
        try:
            result = self.cli("prune")
        finally:
            self.env["DEVIN_WORKFLOW_STATE_ROOT"] = previous
        self.assertEqual(result.returncode, 0, result.stderr)
        slots = json.loads(result.stdout)["slots"]
        self.assertTrue(slots, "a trusted configured state-root symlink was treated as empty")
        self.assertEqual(slots[0]["store"], "sqlite")

    def test_question_mark_state_root_keeps_authoritative_sqlite_classification(self) -> None:
        """A URI-reserved state-root path still opens its authoritative database."""
        self.state_root = self.tmp / "state?reserved"
        self.env["DEVIN_WORKFLOW_STATE_ROOT"] = str(self.state_root)
        self.begin("active")
        slot_key = resolve_repo_identity(self.repo).key
        database = self.state_root / slot_key / "workflow.sqlite3"
        self.assertTrue(database.is_file())

        result = self.cli("prune")
        self.assertEqual(result.returncode, 0, result.stderr)
        slot = next(
            item for item in json.loads(result.stdout)["slots"]
            if item["slot"] == slot_key
        )
        self.assertEqual(slot["store"], "sqlite")

    def test_database_apply_rolls_back_if_delete_is_aborted(self) -> None:
        for index in range(6):
            self.begin(f"pass-{index}")
        slot = self.state_root / resolve_repo_identity(self.repo).key
        database = slot / "workflow.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("""
            CREATE TRIGGER refuse_workflow_delete BEFORE DELETE ON workflows
            BEGIN SELECT RAISE(ABORT, 'forced prune abort'); END
        """)
        connection.commit()
        connection.close()

        before = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)
        result = self.cli("prune", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(self.cli("history", "--repo", str(self.repo)).stdout)
        self.assertEqual(after, before)
        report = json.loads(result.stdout)["slots"][0]
        self.assertEqual(report["status"], "skipped")
        self.assertTrue(any("forced prune abort" in item["reason"] for item in report["workflows"] if item["decision"] == "skipped"))



if __name__ == "__main__":
    unittest.main(verbosity=2)
