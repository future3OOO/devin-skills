"""Atomic repository-scoped workflow state primitives."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import fcntl

from .repo_identity import CanonicalRoot, RepoIdentity

_PATH_POLICY_FILE = (
    Path(__file__).resolve().parents[2]
    / "skills" / "production-code" / "scripts" / "_quality_gate" / "path_policy.py"
)
_path_policy = None

CODE_SUFFIXES = {
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".sh", ".bash",
    ".go", ".rs", ".rb", ".java", ".kt", ".kts", ".swift", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".cs", ".php", ".scala", ".sql", ".proto",
}
DOC_SUFFIXES = {".md", ".markdown", ".rst", ".adoc"}
SCRATCH_PARTS = {"scratchpad", ".scratch", ".gitnexus"}


def estate_home() -> Path:
    return Path(os.environ.get("DEVIN_ESTATE_HOME", Path.home() / ".config" / "devin")).expanduser()


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def state_root() -> Path:
    override = os.environ.get("DEVIN_WORKFLOW_STATE_ROOT")
    return secure_dir(Path(override).expanduser() if override else estate_home() / "state")


def repo_state_dir(identity: RepoIdentity) -> Path:
    return secure_dir(state_root() / identity.key)


def _session_dir(session: str) -> Path:
    """Where one session's repository associations live.

    The shared parent is secured here rather than left to the atomic writer:
    that writer secures only the directory it writes into, and `mkdir` with
    `parents=True` would create `sessions` itself under the process umask,
    leaving every session id in this estate world-listable. Repository keys are
    numeric checksums, so the literal name cannot collide with a
    `repo_state_dir`.
    """
    return secure_dir(state_root() / "sessions") / session


def atomic_write_bytes(path: Path, value: bytes) -> None:
    secure_dir(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_text(path: Path, value: str) -> None:
    atomic_write_bytes(path, value.encode("utf-8"))


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


@contextlib.contextmanager
def _flock(path: Path, *, blocking: bool = True) -> Iterator[bool]:
    """Hold this lock file's exclusive flock, yielding whether it was acquired.

    One implementation for every holder, so a second caller cannot serialize on
    a different contract. Non-blocking callers get False rather than an
    exception, and the file is never unlinked: releasing by unlink would let a
    waiting writer lock a fresh inode at the same path and lose exclusion.
    """
    handle = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        if not blocking:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield True
    finally:
        os.close(handle)


@contextlib.contextmanager
def state_lock(identity: RepoIdentity) -> Iterator[None]:
    """Serialize every writer for one repository's workflow state."""
    with _flock(repo_state_dir(identity) / ".workflow.lock"):
        yield


def read_json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def stop_session_swap(identity: RepoIdentity, session: str, key: str, value: str) -> str | None:
    """Compare-and-set one per-session Stop-feedback key under the state lock.

    Returns the previous value. Fail-soft: Stop feedback must never break the
    hook, so storage errors surface on stderr and read as no-previous-value.
    """
    try:
        with state_lock(identity):
            path = repo_state_dir(identity) / "stop" / f"{session}.json"
            session_state = read_json(path) or {}
            previous = session_state.get(key)
            session_state.update({"schemaVersion": 1, key: value})
            atomic_write_json(path, session_state)
            return previous if isinstance(previous, str) else None
    except OSError as exc:
        print(f"Stop session state unavailable: {exc}", file=sys.stderr)
        return None


def record_session_association(session: str, identity: RepoIdentity) -> None:
    """Record that this session edited in this repository, once.

    One file per repository per session, so every write has a single writer and
    needs no lock. The marker is never rewritten: it says the session worked
    here, which cannot become less true, and rewriting it on every edit would
    churn the file for nothing. Fail-soft like all Stop feedback: an association
    only routes that feedback, so a storage failure must never change the edit
    hook's exit status, its review invalidation, or the quality gate it runs.
    """
    try:
        path = _session_dir(session) / f"{identity.key}.json"
        if not path.exists():
            atomic_write_json(path, {"schemaVersion": 1, "repo": identity.as_dict(), "at": utc_timestamp()})
    except OSError as exc:
        print(f"session association unavailable: {exc}", file=sys.stderr)


def session_associations(session: str) -> list[RepoIdentity]:
    """Every repository this session recorded an edit in.

    The identity is read back from the marker rather than re-derived, so no
    reader needs Git. A marker that does not parse is skipped rather than
    raising: it can only cost this repository its Stop feedback, and one
    unreadable file must not silence the others.

    Fail-soft on the directory too, symmetrically with the writer. Reaching the
    markers has to secure their parent first, which is a real filesystem call
    that can fail for reasons of its own, and this is the first thing Stop does:
    an unreadable store must degrade to the no-association fallback, never take
    the hook down with it.
    """
    try:
        markers = sorted(_session_dir(session).glob("*.json"))
    except OSError as exc:
        print(f"session associations unavailable: {exc}", file=sys.stderr)
        return []
    identities = []
    for marker in markers:
        repo = (read_json(marker) or {}).get("repo")
        if isinstance(repo, dict) and isinstance(repo.get("root"), str) and isinstance(repo.get("key"), str):
            identities.append(RepoIdentity(CanonicalRoot(repo["root"]), repo["key"]))
    return identities


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(
    identity: RepoIdentity,
    *args: str,
    stdin: bytes | None = None,
    env: dict[str, str] | None = None,
) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(identity.root), *args],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env={**os.environ, **env} if env else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"git {args[0]} timed out after {exc.timeout} seconds") from exc
    if result.returncode:
        message = (result.stderr or result.stdout or b"git command failed").decode("utf-8", errors="replace").strip()
        raise RuntimeError(message)
    return result.stdout


def _write_candidate_tree(identity: RepoIdentity) -> str:
    handle = tempfile.NamedTemporaryFile(prefix="workflow-candidate-index-", delete=False)
    handle.close()
    env = {"GIT_INDEX_FILE": handle.name}
    step = "read-tree"
    try:
        try:
            _git(identity, "read-tree", "HEAD", env=env)
        except RuntimeError:
            # Normal HEAD needs no existence probe. Preserve the empty seed
            # fallback for unresolvable HEAD; a valid HEAD's seed failure refuses.
            try:
                _git(identity, "rev-parse", "--verify", "HEAD^{commit}")
            except RuntimeError:
                _git(identity, "read-tree", "--empty", env=env)
            else:
                raise
        step = "add"
        _git(identity, "add", "-A", ".", env=env)
        step = "write-tree"
        return _git(identity, "write-tree", env=env).decode("utf-8").strip()
    except RuntimeError as exc:
        raise OSError(f"candidate capture failed at git {step}: {exc}") from exc
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _active_candidate_tree(identity: RepoIdentity) -> str:
    first = _write_candidate_tree(identity)
    second = _write_candidate_tree(identity)
    if first != second:
        raise OSError(
            "candidate capture drift: worktree changed during capture "
            f"({first[:12]} then {second[:12]})"
        )
    return first


def _paths(identity: RepoIdentity, *args: str) -> list[str]:
    return sorted(os.fsdecode(item) for item in _git(identity, *args).split(b"\0") if item)


def untracked_paths(identity: RepoIdentity) -> list[str]:
    return _paths(identity, "ls-files", "--others", "--exclude-standard", "-z")


def production_changes(identity: RepoIdentity, base: str) -> list[str]:
    """Production (non-test reviewable) paths that differ from ``base``, tracked or untracked."""
    changed = [*_paths(identity, "diff", "--name-only", "-z", base), *untracked_paths(identity)]
    return sorted({path for path in changed if is_reviewable_path(path) and not is_test_path(path)})


def is_docs_or_scratch(path: str) -> bool:
    normalized = path.replace("\\", "/")
    parts = set(Path(normalized).parts)
    return Path(normalized).suffix.lower() in DOC_SUFFIXES or bool(parts & SCRATCH_PARTS) or normalized.startswith("docs/")


def is_code_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return not is_docs_or_scratch(normalized) and Path(normalized).suffix.lower() in CODE_SUFFIXES


def is_reviewable_path(path: str) -> bool:
    return not is_docs_or_scratch(path.replace("\\", "/"))


def is_test_path(path: str) -> bool:
    """Test-like per the quality gate's single classifier; unclassifiable fails closed as production."""
    global _path_policy
    if _path_policy is None:
        try:
            spec = importlib.util.spec_from_file_location("_workflow_path_policy", _PATH_POLICY_FILE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except (OSError, AttributeError, ImportError, SyntaxError):
            return False
        _path_policy = module
    return bool(_path_policy.is_test_like_path(path.replace("\\", "/")))


def is_governance_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        Path(normalized).name in {"AGENTS.md", "CLAUDE.md"}
        or normalized.startswith("skills/")
        or normalized.startswith("docs/agents/")
    )


def reviewable_paths(paths: Iterable[str]) -> list[str]:
    return sorted({path for path in paths if is_reviewable_path(path)})


def code_paths(paths: Iterable[str]) -> list[str]:
    return sorted({path for path in paths if is_code_path(path)})


def _file_mode(path: Path) -> str | None:
    """The Git file mode of a working-tree path, or None when it has none.

    `lstat`, so a symlink is the link itself rather than whatever it resolves to:
    re-pointing a reviewed link is then visible, and a referent outside the
    repository can never drift the manifest. A vanished or otherwise non-regular
    path has no mode here, so it is absent and reads as removed rather than
    silently unchanged. Submodules are directories and land here as None;
    `_gitlink_entries` records them instead. A regular file that exists but
    cannot be read still has a mode — the later `hash-object` fails and the pass
    stays pending, deliberately: omitting it would let a path clear the gates
    with no content identity at all.
    """
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return "120000"
    if not stat.S_ISREG(info.st_mode):
        return None
    # Owner execute bit only: that is git's own rule for 100755, so a group or
    # other execute flip — which git will never record — cannot drift the manifest.
    return "100755" if info.st_mode & stat.S_IXUSR else "100644"


def _symlink_entry(identity: RepoIdentity, path: str) -> str | None:
    """A symlink's manifest entry: its target hashed the way Git stores it.

    Git keeps the target path as the blob's content, so hashing those bytes over
    stdin records the link without ever opening what it points at.
    """
    try:
        target = os.readlink(Path(identity.root) / path)
    except OSError:
        return None
    digest = _git(identity, "hash-object", "--stdin", stdin=os.fsencode(target)).decode("utf-8").strip()
    return f"120000 {digest}"


def _gitlink_entries(identity: RepoIdentity, listing: list[bytes]) -> dict[str, str]:
    """Each tracked submodule's checked-out commit, as `160000 <sha>`.

    The index says which paths are gitlinks; what a gitlink currently points at
    lives in the submodule's own working tree, so an unstaged submodule move is
    visible here and would not be in the parent's staged object id. A submodule
    that is not initialised has nothing checked out and stays absent, and one on
    a path the reviewable classifier excludes never enters the manifest at all.

    Only the commit is recorded. Uncommitted content inside an initialised
    submodule belongs to that repository, not to this one's reviewable surface.
    """
    paths = reviewable_paths(
        os.fsdecode(entry.split(b"\t", 1)[1]) for entry in listing if entry.startswith(b"160000 ")
    )
    entries = {}
    for path in paths:
        try:
            head = _git(identity, "-C", path, "rev-parse", "HEAD").decode("utf-8").strip()
        except RuntimeError:
            continue
        if head:
            entries[path] = f"160000 {head}"
    return entries


def tree_manifest(identity: RepoIdentity) -> dict[str, str]:
    """Working-tree mode and content hash per reviewable path, tracked and untracked alike.

    Hashes what is on disk, not what is staged: an index object id represents
    staged content and would miss the unstaged shell edit this exists to catch.
    The mode rides along because a content hash alone is blind to `chmod`, which
    is a shell mutation of a reviewed file like any other, a symlink is recorded
    as the link rather than its referent, and a submodule is recorded by the
    commit it currently points at.
    """
    listing = _git(identity, "ls-files", "-s", "-z").split(b"\0")
    tracked = [os.fsdecode(entry.split(b"\t", 1)[1]) for entry in listing if entry]
    candidates = reviewable_paths([*tracked, *untracked_paths(identity)])
    modes = {path: _file_mode(Path(identity.root) / path) for path in candidates}
    present = [path for path in candidates if modes[path] in {"100644", "100755"}]
    manifest = _gitlink_entries(identity, listing)
    for path in (path for path in candidates if modes[path] == "120000"):
        entry = _symlink_entry(identity, path)
        if entry:
            manifest[path] = entry
    if not present:
        return manifest
    # --no-filters: without it a configured clean filter normalises content before
    # hashing, and a line-ending-only rewrite keeps the digest a reviewer already saw.
    hashes = _git(identity, "hash-object", "--no-filters", "--", *present).decode("utf-8").split()
    manifest.update({path: f"{modes[path]} {digest}" for path, digest in zip(present, hashes, strict=True)})
    return manifest


def manifest_diff(recorded: dict[str, str], current: dict[str, str]) -> dict[str, list[str]]:
    """Bidirectional comparison; a for-each-current-path loop would miss deletions."""
    return {
        "added": sorted(set(current) - set(recorded)),
        "changed": sorted(path for path in set(recorded) & set(current) if recorded[path] != current[path]),
        "removed": sorted(set(recorded) - set(current)),
    }
