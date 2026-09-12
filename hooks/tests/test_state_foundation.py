#!/usr/bin/env python3
"""Public contracts for repository identity and atomic workflow state."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hooks.lib.repo_identity import resolve_repo_identity
from hooks.lib.state_store import (
    atomic_write_json,
    read_json,
    repo_state_dir,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.rstrip("\n")


class StateFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="workflow-state-foundation-"))
        self.estate_home = self.tmp / "claude-home"
        self.estate_home.mkdir(mode=0o700)
        self.previous_home = os.environ.get("DEVIN_ESTATE_HOME")
        os.environ["DEVIN_ESTATE_HOME"] = str(self.estate_home)

    def tearDown(self) -> None:
        if self.previous_home is None:
            os.environ.pop("DEVIN_ESTATE_HOME", None)
        else:
            os.environ["DEVIN_ESTATE_HOME"] = self.previous_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_repo(self) -> Path:
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Workflow Harness")
        (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        git(repo, "add", "app.py")
        git(repo, "commit", "-q", "-m", "base")
        return repo

    def test_identity_is_stable_across_subdirectories_and_symlinks(self) -> None:
        repo = self.make_repo()
        subdirectory = repo / "nested"
        subdirectory.mkdir()
        link = self.tmp / "repo-link"
        link.symlink_to(repo, target_is_directory=True)
        expected = resolve_repo_identity(repo)
        self.assertEqual(resolve_repo_identity(subdirectory), expected)
        self.assertEqual(resolve_repo_identity(link), expected)

    def test_candidate_capture_preserves_the_real_index_and_raw_manifest(self) -> None:
        from hooks.lib.state_store import _active_candidate_tree, tree_manifest

        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        identity = resolve_repo_identity(repo)
        app, index = repo / "app.py", repo / ".git/index"
        app.write_text("value = 1\n", encoding="utf-8")
        unborn = _active_candidate_tree(identity)
        self.assertFalse(index.exists(), "capture created the real index")
        self.assertIn("app.py", git(repo, "ls-tree", unborn))
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Workflow Harness")
        (repo / ".gitattributes").write_text("app.py text\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "base")
        original_index = index.read_bytes()
        app.write_bytes(b"value = 2\n")
        candidate, raw = _active_candidate_tree(identity), tree_manifest(identity)
        self.assertEqual(git(repo, "show", f"{candidate}:app.py"), "value = 2")
        self.assertEqual(git(repo, "show", ":app.py"), "value = 1")
        app.write_bytes(b"value = 2\r\n")
        self.assertEqual(_active_candidate_tree(identity), candidate, "Git text filtering changed")
        self.assertNotEqual(tree_manifest(identity), raw, "raw drift disappeared behind Git filtering")
        with self.subTest(capability="owner-execute bit"):
            git(repo, "config", "core.filemode", "true")
            old_mode = app.stat().st_mode
            app.chmod(old_mode ^ stat.S_IXUSR)
            if (app.stat().st_mode ^ old_mode) & stat.S_IXUSR == 0:
                self.skipTest("fixture filesystem cannot change the owner-execute bit")
            self.assertNotEqual(_active_candidate_tree(identity), candidate)
            mode = "100755" if app.stat().st_mode & stat.S_IXUSR else "100644"
            self.assertTrue(tree_manifest(identity)["app.py"].startswith(mode + " "))
        self.assertEqual(index.read_bytes(), original_index)
        # An existing HEAD whose tree cannot be read is not an unborn repository.
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        (repo / ".git/objects" / tree[:2] / tree[2:]).unlink()
        with self.assertRaisesRegex(OSError, "read-tree"):
            _active_candidate_tree(identity)
        self.assertEqual(index.read_bytes(), original_index)

    def test_atomic_state_is_private_and_round_trips(self) -> None:
        identity = resolve_repo_identity(self.make_repo())
        path = repo_state_dir(identity) / "record.json"
        atomic_write_json(path, {"status": "passed", "count": 2})
        self.assertEqual(read_json(path), {"count": 2, "status": "passed"})
        self.assertEqual(stat.S_IMODE(repo_state_dir(identity).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

if __name__ == "__main__":
    unittest.main(verbosity=2)
