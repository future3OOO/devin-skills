"""Shared scaffolding for the workflow test suites."""
from __future__ import annotations

import json
import os
import site
import subprocess
import sys
import time
from pathlib import Path

from hooks.lib.behavior_map import no_change_item
from hooks.lib.preflight_document import BEHAVIOR_MAP_SECTION, SECTIONS
from hooks.lib.repo_identity import RepoIdentity, resolve_repo_identity
from hooks.lib.state_store import _active_candidate_tree
from hooks.lib.workflow_documents import graph_evidence_document
from hooks.lib.workflow_state import (
    advisor_disposition,
    commit_evidence_phase,
    instance_id,
    read_workflow,
    record_advisor_result,
    set_phase,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / "skills" / "repo-production-workflow" / "scripts" / "workflow.py"
BOOTSTRAP = ROOT / "skills" / "repo-context-forge" / "scripts" / "bootstrap.py"
POST_EDIT = ROOT / "hooks" / "code-quality-gate.py"


def fixture_env(state_root: Path) -> dict[str, str]:
    """The environment a real-index fixture pass runs under: an isolated state
    root and home, and no ambient git or bytecode side effects.

    HOME is isolated because the producer's global state hangs off it: GitNexus's
    registry at ~/.gitnexus and the analysis cache at ~/.cache/repo-context-forge.
    Sharing the caller's home makes every fixture intake write into both and, once
    intakes are serialised, queue against the caller's own machine-wide lock.
    Anything a fixture starts that must see the same index inherits this.
    """
    env = os.environ.copy()
    # A parent Git routing or command-scope config variable (GIT_CONFIG_COUNT and
    # its GIT_CONFIG_KEY_*/VALUE_* pairs, GIT_CONFIG_PARAMETERS) would redirect the
    # fixture's own git and the producer it drives, so drop them all.
    for name in tuple(env):
        if name in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} or name.startswith(
                ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name, None)
    home = state_root.parent / "fixture-home"
    home.mkdir(parents=True, exist_ok=True)
    env.update({
        "DEVIN_WORKFLOW_STATE_ROOT": str(state_root),
        "HOME": str(home),
        # The interpreter resolves user site-packages under HOME, so a --user
        # install such as pytest would vanish with it; keep the real one importable.
        "PYTHONPATH": os.pathsep.join(
            path for path in (site.getusersitepackages(), env.get("PYTHONPATH")) if path
        ),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def run_git(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """One git command in the fixture repository; the caller asserts the result."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def run_workflow(repo: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """The real workflow CLI against the fixture repository."""
    return subprocess.run(
        [sys.executable, str(WORKFLOW), *args, "--repo", str(repo)],
        cwd=repo, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def run_post_edit(
    repo: Path, env: dict[str, str], relative: str, *, session: str | None,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The real PostToolUse edit-success hook on one repository file; the caller
    asserts the result. session=None omits the field entirely rather than
    blanking it: an anonymous payload is one that never carried the key."""
    payload: dict[str, object] = {"tool_input": {"file_path": str(repo / relative)}}
    if session is not None:
        payload["session_id"] = session
    return subprocess.run(
        [str(POST_EDIT)], cwd=repo, env={**env, **(env_extra or {})}, text=True,
        input=json.dumps(payload),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )


def run_intake(
    repo: Path, env: dict[str, str], slug: str, intent: str, *extra: str, timeout: int = 900
) -> subprocess.CompletedProcess[str]:
    """One real governed intake against a dirty dependent, so the index is real.

    Local mode needs a dirty dependent; the overlay becomes part of the indexed
    baseline, so it never touches the fixture's own changed symbol.
    """
    (repo / "caller.py").write_text(
        "from app import compute\n\n\ndef run():\n    return compute(2)\n", encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable, str(BOOTSTRAP), "--repo", str(repo),
            "--workflow-slug", slug, "--mode", "local", "--intent", intent,
            "--map-build", "never", "--gitnexus-mode", "auto", "--top", "5",
            "--out", os.devnull, *extra,
        ],
        cwd=repo, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout,
    )


def wait_for_trace_writes(path: Path, count: int = 2) -> int:
    deadline, found = time.monotonic() + 10, 0
    while found < count and time.monotonic() < deadline:
        found = path.read_text(encoding="utf-8").count('"name":"write-tree"') if path.exists() else 0
        if found < count: time.sleep(0.01)
    return found


def pending_behavior(
    identifier: str = "BM_TEST",
    *,
    behavior: str = "value becomes two",
    seam: str = "public application behavior",
    expected: str = "value is two",
    red_failure: str = "VALUE_NOT_TWO",
    basis: str = "test contract",
    kind: str = "contract",
) -> dict[str, object]:
    return {
        "id": identifier,
        "kind": kind,
        "basis": basis,
        "behavior": behavior,
        "seam": seam,
        "expected": expected,
        "redFailure": red_failure,
        "status": "pending",
        "sourceRefs": [],
    }


def build_document(
    fill: str,
    *,
    behavior_map: list[dict[str, object]],
) -> dict[str, object]:
    """A structurally valid preflight document with explicit TDD scope."""
    document: dict[str, object] = {
        name: "none" if name == "openQuestions" else f"{name}: {fill}"
        for name in SECTIONS
    }
    document[BEHAVIOR_MAP_SECTION] = [
        {**item, "sourceRefs": item.get("sourceRefs", [])} for item in behavior_map
    ]
    return document


def build_no_change_document(fill: str) -> dict[str, object]:
    """A preflight fixture that explicitly declares no production behavior work."""
    return build_document(
        fill,
        behavior_map=[
            no_change_item("test fixture declares no production behavior change")
        ],
    )


def graph_packet(root: str, candidate: str, head: str) -> dict[str, object]:
    """A machine packet shaped exactly as the canonical producer emits one.

    Suites that are about workflow policy rather than the producer contract advance
    the context step with this, the way they already advance other steps through the
    library. The producer contract itself is proved against the real Repo Context
    Forge, real GitNexus, and a real repository in test_repoforge_workflow.py.
    """
    return {
        "target_state": {"source_repo": root, "head_sha": head},
        "git": {"merge_base": head},
        "gitnexus": {
            "analysis": {
                "status": "resolved",
                "entries": [
                    {
                        "kind": "symbol_context",
                        "file": "app.py",
                        "target": "compute",
                        "direction": "",
                        "status": "resolved",
                        "resolved_identity": "Function:app.py:compute",
                        "callers": [
                            {
                                "identity": "Function:caller.py:run",
                                "name": "run",
                                "file": "caller.py",
                            }
                        ],
                    }
                ],
                "unresolved_checks": [],
                "elapsed_ms": 1,
                "process_count": 1,
                "graph_call_count": 1,
                "output_bytes": 1,
                "estimated_output_tokens": 1,
                "omitted_check_count": 0,
                "authority": {"source_repository": root},
                "producer_revision": {"commit": "0" * 40, "dirty": False},
            }
        },
        "advisorProjection": {
            "schemaVersion": 1,
            "producerRevision": {"commit": "0" * 40, "dirty": False},
            "sourceRepo": "example.invalid/workflow-fixture",
            "sourceBaseOid": head,
            "committedHeadOid": head,
            "expectedCandidateTree": candidate,
            "indexedCandidateTree": candidate,
            "targets": [],
            "graph": {
                "status": "resolved",
                "references": ["gitnexus.analysis.entries[0]"],
                "requiredOmissions": [],
                "optionalOmissionCount": 0,
            },
            "coverageGaps": [],
        },
    }


def advance_to_final_review(repo: Path, tmp: Path, design=None) -> RepoIdentity:
    """Drive one pass from intake to a ready final-review checkpoint.

    Producer-owned steps go through the real recorders because that is the only way
    to obtain their evidence; lead-owned phases advance through the library, the way
    these suites already advance steps they are not testing.
    """
    identity = record_context_forge(repo, tmp)
    state = read_workflow(identity)
    slug, workflow_id = str(state["slug"]), str(instance_id(state))

    def producer(command: str, document: object) -> None:
        path = tmp / f"{command}-input.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(WORKFLOW),
                command,
                "--repo",
                str(repo),
                "--slug",
                slug,
                "--workflow-id",
                workflow_id,
                "--input",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    record_advisor_result(
        identity, slug, workflow_id, "preflight", "codex-advisor", "completed", design=design
    )
    advisor_disposition(identity, slug, workflow_id, "preflight", "none")
    producer(
        "record-preflight", build_no_change_document("advance to final review")
    )
    set_phase(identity, "tdd", "not-required")
    gate = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "skills"
                / "production-code"
                / "scripts"
                / "code_quality_gate.py"
            ),
            "check",
            "--repo",
            str(repo),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert gate.returncode == 0, gate.stdout + gate.stderr
    producer("record-production-code", json.loads(gate.stdout))
    set_phase(identity, "implementation", "passed")
    for extra in (
        ("--", sys.executable, "-c", "pass"),
        ("--kind", "quality-gate", "--base-ref", "HEAD"),
    ):
        subprocess.run(
            [
                sys.executable,
                str(WORKFLOW),
                "verify",
                "--repo",
                str(repo),
                "--slug",
                slug,
                *extra,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    set_phase(identity, "code-review", "passed", findings="none")
    return identity


def record_context_forge(repo: Path, tmp: Path) -> RepoIdentity:
    """Advance repo-context-forge the way the bootstrap Adapter does."""
    identity = resolve_repo_identity(repo)
    state = read_workflow(identity)
    packet = tmp / "graph-packet.json"
    packet.write_text(json.dumps(graph_packet(
        str(identity.root), str(_active_candidate_tree(identity)), str(state["passStartOid"]),
    )), encoding="utf-8")
    commit_evidence_phase(
        identity,
        str(state["slug"]),
        instance_id(state),
        "repo-context-forge",
        graph_evidence_document(
            str(packet),
            slug=str(state["slug"]),
            workflow_id=str(instance_id(state)),
            source_root=str(identity.root),
            canonical_source_repo="example.invalid/workflow-fixture",
        ),
    )
    return identity
