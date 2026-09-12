#!/usr/bin/env python3
"""Compare two trusted estate refs through their real CLI and hook processes.

Run from inside the source repository. Each arm gets a disposable clone pinned to an
exact commit, an arm-owned Claude estate built from that checkout's tracked files, and
its own workflow-state root, selected through environment redirection. Afterwards the
run checks monitored live-estate digests and arm-key traces under monitored state roots.

Those measures reduce accidental cross-arm and live-estate writes and detect the ones
they cover; they are not a filesystem sandbox. Both refs execute with this user's
permissions and can modify any path this user can, so untrusted refs are unsupported
without OS-enforced containment. Exit status answers behavioural correctness and the
monitored isolation checks only; timing never changes it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
REPETITIONS = 5
ESTATE = ("CLAUDE.md", "settings.json", "hooks", "skills")
# Split so this file never carries the marker it plants. The arm's own gate rejecting
# the edit is the only thing that proves the gate ran; a clean tree exits zero either way.
ESCAPE = "TO" + "DO"
STATE_FIELDS = ("phase", "nextAction", "slug", "intent", "repoContextForge", "gitnexus", "preflight",
                "tdd", "productionCode", "implementation", "verification", "advisorPreflight.status",
                "advisorPreflight.findings", "codeReview.status", "codeReview.findings",
                "finalReview.source", "finalReview.status", "finalReview.findings")
CHECKPOINT_FIELDS = ("phase", "slug", "ready", "missing", "tdd", "codeReviewStatus")
# The documents the recorders validate before they will record anything, written once per
# arm. They are benchmark stimulus and nothing else: the replay proves that two estate refs
# persist identical state from identical input and shows what each operation costs. It is
# not evidence that a preflight, an advisor consult or a code review ever happened.
REPLAY_INPUTS: dict[str, object] = {
    "preflight.json": {**{section: "workflow-state replay stimulus" for section in (
        "affectedSurface", "authoritativeContract", "invariants", "proofPlan", "reusePath",
        "chosenApproach", "rejectedAlternatives", "touchpoints", "verify", "update",
        "modularityPlan", "riskChecks")}, "openQuestions": "none"},
    "design-absent.json": {"schemaVersion": 1, "status": "absent", "reason": "benchmark replay has no governing design artifact"},
    "review.json": {"findings": [], "dispositions": []},
}
def _fields(names: tuple[str, ...]):
    """Project a step's last command, which is the one that reports the state it reached."""
    return lambda runs: project(runs[-1].stdout, names)


def _quality_gate(runs: list[subprocess.CompletedProcess[str]]) -> tuple[dict[str, object], list[str]]:
    """The typed run's outcome read from the status that follows it, not from its own stdout.

    Two reasons, both measured. The typed verify prints the bundled gate's raw JSON before
    its own envelope, so its combined stdout does not parse at all. And the two gate fields
    are per-run identifiers, so comparing them by value would make two capable arms differ
    on identity alone; presence is the contract, so presence is what is projected.
    """
    state, keys = project(runs[-1].stdout, ("verification", "qualityGateEvidence", "qualityGateManifestId"))
    return {"supported": True, "verification": state["verification"],
            "qualityGateEvidence": bool(state["qualityGateEvidence"]),
            "qualityGateManifestId": bool(state["qualityGateManifestId"])}, keys


def _retired_transition(runs: list[subprocess.CompletedProcess[str]]) -> tuple[dict[str, object], list[str]]:
    """The refusal's own reason, because an exit code is not one.

    `gitnexus` already reads `passed` before this step runs — the seed recorded Repo
    Context Forge evidence once per arm — so the status that follows the transition
    cannot testify about the transition. Without the reason, an arm that failed for an
    unrelated cause is indistinguishable from one that correctly refused a retired step.
    """
    state, keys = project(runs[-1].stdout, STATE_FIELDS)
    refused = runs[0].returncode == 2 and "gitnexus is no longer a workflow step" in runs[0].stderr
    return {**state, "retiredTransitionRefused": refused}, keys


# Every governed operation downstream of Repo Context Forge, in order, each driven through
# the arm's own shipped producer rather than a reimplementation of its contract. Each entry
# builds the commands to run, projects what they reached, and names the transition it must
# observe, so a producer that exits zero without advancing the state it owns fails the run
# instead of timing fast. A builder returning None means this arm does not ship the step.
REPLAY = (
    # An arm that records graph evidence with the packet has retired this transition and
    # must refuse it; an arm that still ships it must perform it. The state read that
    # follows decides either way: both arms have to arrive at the same next step with the
    # same compatibility reading, and the refusing arm must not have moved anything. The
    # exit codes and phase legitimately differ, and compare() reports that difference for
    # the operator rather than this predicate hiding it.
    ("replay-gitnexus", lambda c: [[*c["cli"], "set-phase", *c["bound"], "--phase", "gitnexus",
                                   "--status", "passed"],
                                   [*c["cli"], "status", "--repo", c["repo"]]],
     _retired_transition,
     lambda p: (p["exits"] == [0, 0] or (p["exits"] == [2, 0] and p["retiredTransitionRefused"]))
     and p["gitnexus"] == "passed" and p["nextAction"] == "advisor-preflight"),
    ("replay-advisor-preflight", lambda c: [[*c["cli"], "advisor-result", *c["bound"], "--stage", "preflight",
                                            "--source", "codex-advisor", "--verdict", "completed",
                                            "--design-declaration", str(c["inputs"] / "design-absent.json")]],
     _fields(STATE_FIELDS), lambda p: p["advisorPreflight.status"] == "completed"),
    ("replay-advisor-disposition", lambda c: [[*c["cli"], "advisor-disposition", *c["bound"],
                                              "--stage", "preflight", "--findings", "none"]],
     _fields(STATE_FIELDS), lambda p: p["advisorPreflight.findings"] == "none"),
    ("replay-preflight", lambda c: [[*c["script"]("production-preflight", "record-preflight.py"), *c["bound"],
                                    "--input", str(c["inputs"] / "preflight.json")]],
     _fields(("status",)), lambda p: p["exits"] == [0] and p["status"] == "passed"),
    ("replay-tdd", lambda c: [[*c["script"]("tdd", "tdd-run.py"), "--repo", c["repo"], "--slug", "estate-benchmark",
                              "--not-required", "workflow-state replay records no behaviour change"]],
     _fields(("status",)), lambda p: p["exits"] == [0] and p["status"] == "not-required"),
    ("replay-production-code", lambda c: [[*c["script"]("production-code", "record-production-code.py"),
                                          *c["bound"], "--input", str(c["inputs"] / "gate.json")]],
     _fields(("status",)), lambda p: p["exits"] == [0] and p["status"] == "passed"),
    ("replay-implementation", lambda c: [[*c["cli"], "set-phase", *c["bound"], "--phase", "implementation",
                                         "--status", "passed"]],
     _fields(STATE_FIELDS), lambda p: p["implementation"] == "passed"),
    ("replay-verification", lambda c: [[*c["script"]("repo-production-workflow", "verify-run.py"),
                                       "--repo", c["repo"], "--slug", "estate-benchmark", "--", "true"]],
     _fields(("verification", "exitCode")), lambda p: p["verification"] == "passed" and p["exitCode"] == 0),
    # Between verification and review because that is where the arm that ships it demands it.
    # Absent from main's grammar, so an arm without it records not-supported rather than a
    # transition it never made; compare() reports that asymmetry instead of calling it a
    # difference. The status read follows the typed run because the binding it must prove
    # lives in state, and status is the arm's own public CLI rather than its storage.
    ("replay-quality-gate", lambda c: [
        [*c["script"]("repo-production-workflow", "verify-run.py"), "--repo", c["repo"],
         "--slug", "estate-benchmark", "--kind", "quality-gate", "--base-ref", "HEAD"],
        [*c["cli"], "status", "--repo", c["repo"]]] if c["qualityGate"] else None,
     _quality_gate, lambda p: p["supported"] is False or (
         p["exits"] == [0, 0] and p["verification"] == "passed"
         and p["qualityGateEvidence"] and p["qualityGateManifestId"])),
    ("replay-code-review", lambda c: [[*c["script"]("code-review", "record-review.py"), *c["bound"],
                                      "--resolved-model", "workflow-state-replay",
                                      "--review-context-id", "workflow-state-replay",
                                      "--input", str(c["inputs"] / "review.json")]],
     _fields(("status",)), lambda p: p["exits"] == [0] and p["status"] == "passed"),
    ("replay-advisor-final", lambda c: [[*c["cli"], "advisor-result", *c["bound"], "--stage", "final",
                                        "--source", "codex-advisor", "--verdict", "commit-ready",
                                        "--design-declaration", str(c["inputs"] / "design-absent.json")]],
     _fields(STATE_FIELDS), lambda p: p["finalReview.source"] == "codex-advisor" and p["finalReview.status"] == "commit-ready"),
    ("replay-final-disposition", lambda c: [[*c["cli"], "advisor-disposition", *c["bound"], "--stage", "final",
                                            "--findings", "none"]],
     _fields(STATE_FIELDS), lambda p: p["finalReview.findings"] == "none"),
    ("replay-complete", lambda c: [[*c["cli"], "complete", *c["bound"]]],
     _fields(STATE_FIELDS), lambda p: p["exits"] == [0] and p["phase"] == "complete"),
)
# Each scenario must be shown to have done the thing it is named for. Parity alone
# would hold for two identically broken arms, so these decide the exit status too.
EXPECTED = {
    "begin": lambda p: p["exits"] == [0] and p["phase"] == "intake",
    "status-and-summary": lambda p: p["exits"] == [0, 0] and p["summary"]["slug"] == "estate-benchmark",
    "checkpoint-not-ready": lambda p: p["ready"] is False and "repo-context-forge" in (p["missing"] or []),
    "post-edit-hook": lambda p: p["exits"] == [2, 0] and p["gateRejected"] and p["after"]["phase"] == "implementation",
    "prune-report": lambda p: p["exits"] == [0] and p["applied"] is False and p["removableCount"] == 0,
    **{name: predicate for name, _, _, predicate in REPLAY},
}
# Shared keys only: the candidate's extra `quality-gate=` is a representation change,
# reported as a delta rather than compared.
SUMMARY_KEYS = ("slug", "phase", "next", "repo-context-forge", "advisor-preflight",
                "preflight", "tdd", "production-code", "implementation", "verification",
                "code-review", "final-review")


def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise SystemExit(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result.stdout.strip()


def run(argv: list[str], env: dict[str, str], *, cwd: Path | None = None,
        stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, text=True, input=stdin, timeout=300,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def declared_estates(settings: Path) -> set[Path]:
    """Estate roots the tracked settings name, read from the file rather than from this host.

    README records that settings.json hardcodes one absolute install path, so the
    prefix belongs to the file and a host whose home differs would match nothing.
    Every tracked command is `<estate>/hooks/<name>`, so that segment locates it.
    One owner for both callers: if what gets rewritten and what gets protected were
    derived separately they could disagree, and disagreement reads as isolation.
    """
    value = json.loads(settings.read_text(encoding="utf-8"))
    return {Path(token[: token.index("/hooks/")])
            for group in value.get("hooks", {}).values() for entry in group
            for hook in entry.get("hooks", []) for token in shlex.split(hook.get("command", ""))
            if "/hooks/" in token}


def monitored(source: Path) -> tuple[list[Path], list[Path]]:
    """Every estate and state root this run must leave untouched, and their state roots."""
    installed = Path.home() / ".config" / "devin"
    homes = {installed, Path(os.environ.get("DEVIN_ESTATE_HOME", installed)).expanduser()}
    homes |= declared_estates(source / "settings.json")
    roots = {home / "state" for home in homes}
    if override := os.environ.get("DEVIN_WORKFLOW_STATE_ROOT"):
        roots.add(Path(override).expanduser())
    return sorted(homes), sorted(roots)


def slot_names(root: Path) -> list[str]:
    """Repository slots directly under a state root; `sessions` and `_`-prefixed are shared."""
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir()
                  if path.is_dir() and path.name != "sessions" and not path.name.startswith("_"))


def arm_traces(root: Path, keys: set[str]) -> list[str]:
    """Anything an arm left under a live root, the shared `sessions/` tree included.

    Arm-scoped deliberately. A state root is where every other agent on this machine
    writes, so comparing its whole content would fail this run for somebody else's
    work — measured, not supposed. An arm can only appear here under its own
    repository key, so that key is the signal, at any depth rather than only as a
    top-level slot.
    """
    if not root.is_dir() or not keys:
        return []
    return sorted(str(path.relative_to(root)) for path in root.rglob("*")
                  if path.stem in keys or path.name in keys)


def digest(root: Path) -> str:
    """One hash over the repository-owned live estate: relative path, mode, content."""
    sha = hashlib.sha256()
    for target in (root / name for name in ESTATE):
        for path in sorted(target.rglob("*")) if target.is_dir() else [target]:
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            sha.update(f"{path.relative_to(root)}\0{path.stat().st_mode & 0o777}\0".encode())
            sha.update(hashlib.sha256(path.read_bytes()).digest())
    return sha.hexdigest()


def project_settings(clone: Path, home: Path) -> None:
    """This checkout's settings rewritten to the arm, minus entries naming a file it lacks.

    The drop removes what rewriting exposes: the tracked settings register a
    machine-generated hook this repository deliberately does not copy. Any command
    still absolute and outside the arm afterwards refuses the run, so a wrong anchor
    fails loudly instead of leaving the arm pointed at somebody else's estate.
    """
    settings = clone / "settings.json"
    raw = settings.read_text(encoding="utf-8")
    for estate in declared_estates(settings):
        raw = raw.replace(f"{estate}/", f"{home}/")
    value = json.loads(raw)
    for group in value.get("hooks", {}).values():
        for entry in group:
            kept = []
            for hook in entry.get("hooks", []):
                absolute = [token for token in shlex.split(hook.get("command", "")) if token.startswith("/")]
                if outside := [token for token in absolute if not token.startswith(f"{home}/")]:
                    raise SystemExit(f"arm settings still reach outside {home}: {outside}")
                if all(Path(token).exists() for token in absolute):
                    kept.append(hook)
            entry["hooks"] = kept
    (home / "settings.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def arm_env(root: Path, state_root: Path) -> dict[str, str]:
    # HOME as well as DEVIN_ESTATE_HOME: the estate's estate_home() falls back to Path.home().
    return {**os.environ, "HOME": str(root), "CLAUDE_CONFIG_DIR": str(root / "home"),
            "DEVIN_ESTATE_HOME": str(root / "home"), "DEVIN_WORKFLOW_STATE_ROOT": str(state_root),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1"}


def build_arm(source: Path, out: Path, name: str, ref: str, commit: str) -> dict[str, object]:
    root = out / name
    shutil.rmtree(root, ignore_errors=True)
    clone, home = root / "source", root / "home"
    home.mkdir(parents=True)
    git(source, "clone", "--quiet", str(source), str(clone))
    git(clone, "checkout", "--quiet", "--detach", commit)
    for item in ("CLAUDE.md", "hooks", "skills"):
        target = clone / item
        (shutil.copytree if target.is_dir() else shutil.copy2)(target, home / item)
    project_settings(clone, home)
    smoke = run(["claude", "doctor"], arm_env(root, root / "state" / "smoke"), cwd=root)
    return {"ref": ref, "commit": commit, "tree": git(clone, "rev-parse", f"{commit}^{{tree}}"),
            "root": str(root), "home": str(home), "stateRoot": str(root / "state"),
            "configSmokeExit": smoke.returncode}


def fixture(path: Path, env: dict[str, str]) -> Path:
    """A committed one-file repository, the shape this estate's own hook tests use."""
    path.mkdir(parents=True)
    git(path.parent, "init", "-q", path.name, env=env)
    git(path, "config", "user.email", "benchmark@example.invalid", env=env)
    git(path, "config", "user.name", "Benchmark Harness", env=env)
    (path / "app.py").write_text("value = 1\n", encoding="utf-8")
    git(path, "add", "app.py", env=env)
    git(path, "commit", "-q", "-m", "base", env=env)
    return path


def loaded(raw: str) -> dict[str, object]:
    # A refusing arm is the most important thing this command reports, so stdout that is
    # not an object must reduce to a value that compares unequal, never to an exception.
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"unparsed": raw.strip()}
    return value if isinstance(value, dict) else {"unparsed": raw.strip()}


def project(raw: str, fields: tuple[str, ...]) -> tuple[dict[str, object], list[str]]:
    # Fields drive the comparison; the raw key set only describes what the arm emitted,
    # which is what makes a representation change reportable instead of fatal.
    value = loaded(raw)
    picked: dict[str, object] = {}
    for field in fields:
        group, _, leaf = field.partition(".")
        nested = value.get(group)
        picked[field] = (nested if isinstance(nested, dict) else {}).get(leaf) if leaf else nested
    return picked, sorted(value)


def summary_projection(text: str) -> tuple[dict[str, object], list[str]]:
    pairs = dict(re.findall(r"([a-z-]+)=([^\s,.]+)", text))
    return {key: pairs.get(key) for key in SUMMARY_KEYS}, sorted(pairs)


def prune_projection(raw: str) -> tuple[dict[str, object], list[str]]:
    """Decisions, not containers or names.

    A slot is named by its arm-specific repository key, and #49 moves a slot's items
    from `entries` into `workflows`, so both are read and only the count of items this
    run would delete is compared. That count is what a live slot's retention means in
    either store, and zero is what an active workflow must report.
    """
    report = loaded(raw)
    slots = [slot for slot in report.get("slots", []) if isinstance(slot, dict)]
    items = [item for slot in slots for key in ("entries", "workflows")
             for item in slot.get(key, []) if isinstance(item, dict)]
    return ({"applied": report.get("applied"), "slotCount": len(slots),
             "slotStatuses": sorted(str(slot.get("status")) for slot in slots),
             "removableCount": sum(item.get("decision") in {"removable", "removed"} for item in items)},
            sorted({*report, *(key for slot in slots for key in slot)}))


def scenario(name: str, start: float, runs: list[subprocess.CompletedProcess[str]],
             projection: dict[str, object], keys: list[str]) -> dict[str, object]:
    return {"name": name, "seconds": round(time.perf_counter() - start, 6), "keys": keys,
            "projection": {"exits": [process.returncode for process in runs], **projection}}


def replay_seed(arm: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    """Advance one arm to the replay's starting line through its own real Repo Context Forge.

    Once per arm, not once per repetition. Every governed operation after it is gated
    behind repo-context-forge, and only that adapter can record the phase, so without
    this the twelve downstream operations cannot be reached at all. It costs seconds
    where they cost milliseconds, so each repetition continues a fresh copy of what it
    produced here; the copied root is that real path's output, not a substitute for it.
    """
    root = Path(str(arm["root"]))
    home = root / "replay"
    env = arm_env(root, home / "seed-state")
    repo = fixture(home / "repo", env)
    begun = run([*cli_for(arm), "begin", "--repo", str(repo), "--slug", "estate-benchmark",
                 "--intent", "workflow-state replay"], env)
    start = time.perf_counter()
    forged = run([sys.executable, str(root / "home/skills/repo-context-forge/scripts/bootstrap.py"),
                  "--repo", str(repo), "--workflow-slug", "estate-benchmark",
                  "--intent", "workflow-state replay"], env)
    seconds = round(time.perf_counter() - start, 6)
    inputs = home / "inputs"
    inputs.mkdir(parents=True)
    for name, document in REPLAY_INPUTS.items():
        (inputs / name).write_text(json.dumps(document), encoding="utf-8")
    workflow_id = str(loaded(begun.stdout).get("workflowId"))
    # The recorder refuses anything but this gate's own ok verdict, so the arm's gate is
    # run against the arm's fixture rather than a document describing one.
    gate = run([sys.executable, str(root / "home/skills/production-code/scripts/code_quality_gate.py"),
                "check", "--repo", str(repo), "--json"], env)
    (inputs / "gate.json").write_text(gate.stdout, encoding="utf-8")
    reached, _ = project(run([*cli_for(arm), "status", "--repo", str(repo)], env).stdout, STATE_FIELDS)
    # Asked of the arm's own runner, once, and answered from its help text rather than from
    # the ref it was built at: which grammar a ref speaks is a property of what it ships.
    # Once per arm and not per repetition, because a probe inside the loop would add a
    # subprocess to every timed operation and distort the numbers it exists to report.
    helped = run([sys.executable, str(root / "home/skills/repo-production-workflow/scripts/verify-run.py"),
                  "--help"], env)
    quality_gate = "--kind" in helped.stdout
    return ({"state": home / "seed-state", "repo": repo, "inputs": inputs, "qualityGate": quality_gate,
             "key": repo_key(root, repo, env), "instance": workflow_id},
            {"beginExit": begun.returncode, "exit": forged.returncode, "gateExit": gate.returncode,
             "seconds": seconds, "repoContextForge": reached["repoContextForge"],
             "qualityGate": quality_gate, "helpExit": helped.returncode,
             "blocker": forged.stderr.strip()[-300:]})


def repetition(arm: dict[str, object], index: int, seed: dict[str, object]) -> tuple[list[dict[str, object]], str]:
    """One ordered replay of every scenario against a fresh fixture and a fresh state root.

    Fresh both, every time: the candidate retains ledger history where the baseline
    overwrites one JSON file, so repeating a scenario in place would compare two
    different situations and drift the prune report.
    """
    root = Path(str(arm["root"]))
    state_root = root / "state" / str(index)
    env = arm_env(root, state_root)
    repo = fixture(root / "repos" / str(index), env)
    key = repo_key(root, repo, env)
    cli = cli_for(arm)
    hook = str(root / "home/hooks/code-quality-gate.py")
    scenarios = []

    start = time.perf_counter()
    begun = run([*cli, "begin", "--repo", str(repo), "--slug", "estate-benchmark",
                 "--intent", "isolated A/B estate benchmark"], env)
    scenarios.append(scenario("begin", start, [begun], *project(begun.stdout, STATE_FIELDS)))

    start = time.perf_counter()
    status = run([*cli, "status", "--repo", str(repo)], env)
    summarised = run([*cli, "summary", "--repo", str(repo)], env)
    state, state_keys = project(status.stdout, STATE_FIELDS)
    text, text_keys = summary_projection(summarised.stdout)
    scenarios.append(scenario("status-and-summary", start, [status, summarised],
                              {"status": state, "summary": text}, sorted({*state_keys, *text_keys})))

    start = time.perf_counter()
    checked = run([*cli, "checkpoint", "--repo", str(repo), "--phase", "preflight-advice"], env)
    scenarios.append(scenario("checkpoint-not-ready", start, [checked],
                              *project(checked.stdout, CHECKPOINT_FIELDS)))

    start = time.perf_counter()
    (repo / "app.py").write_text(f"value = 2  # {ESCAPE}: invalid production escape\n", encoding="utf-8")
    edited = run([hook], env, cwd=repo, stdin=json.dumps(
        {"session_id": "estate-benchmark", "tool_input": {"file_path": str(repo / "app.py")}}))
    after = run([*cli, "status", "--repo", str(repo)], env)
    invalidated, keys = project(after.stdout, STATE_FIELDS)
    scenarios.append(scenario("post-edit-hook", start, [edited, after], {
        "gateRejected": "production-code gate FAILED" in edited.stderr, "after": invalidated}, keys))

    start = time.perf_counter()
    pruned = run([*cli, "prune"], env)
    scenarios.append(scenario("prune-report", start, [pruned], *prune_projection(pruned.stdout)))

    # A private copy of the seeded root, so each repetition replays the governed sequence
    # from the same real Repo Context Forge output without paying for it again, and so a
    # step that mutates state cannot leak into the next repetition. Its own fixture too:
    # the post-edit-hook scenario above deliberately plants an escape marker in this
    # repetition's repo, which the arm's quality gate would then reject for an unrelated
    # reason when the production-code recorder asks for its verdict.
    replayed = root / "replay" / str(index)
    seeded = Path(str(seed["state"]))
    # An arm whose redirect failed never wrote the seeded root, so there is nothing to
    # copy. The replay then runs from an empty root and fails its own invariants, which
    # is the right report for an escaped arm; aborting here would produce no artifact
    # at all and hide the escape the run exists to catch.
    if seeded.is_dir():
        shutil.copytree(seeded, replayed)
    else:
        replayed.mkdir(parents=True)
    replay_env = arm_env(root, replayed)
    context = {
        "cli": cli, "repo": str(seed["repo"]), "inputs": Path(str(seed["inputs"])),
        "qualityGate": seed["qualityGate"],
        "script": lambda skill, name: [sys.executable, str(root / "home/skills" / skill / "scripts" / name)],
        "bound": ["--repo", str(seed["repo"]), "--slug", "estate-benchmark",
                  "--workflow-id", str(seed["instance"])],
    }
    for name, build, projected, _ in REPLAY:
        start = time.perf_counter()
        commands = build(context)
        # No runs at all for a step this arm does not ship: an empty exits list is the
        # honest record, where a synthesised success would be the fake green the
        # comparison exists to catch.
        stepped = [run(command, replay_env) for command in commands] if commands else []
        scenarios.append(scenario(name, start, stepped,
                                  *(projected(stepped) if stepped else ({"supported": False}, []))))

    return scenarios, key


def cli_for(arm: dict[str, object]) -> list[str]:
    return [sys.executable, str(Path(str(arm["root"])) / "home/skills/repo-production-workflow/scripts/pass-state.py")]


def repo_key(root: Path, repo: Path, env: dict[str, str]) -> str:
    """The arm's own identity owner asked for this fixture's key, from the fixture path.

    Reading it back from the arm's state root instead would make an arm whose redirect
    failed contribute no key at all, and the escape it caused invisible. No key means
    arm_traces searches for nothing and reports a clean root it never examined, so this
    refuses rather than returning empty: isolation.ok is a boolean and cannot say
    "not measured".
    """
    identity = run([sys.executable, str(root / "home/hooks/lib/repo_identity.py"),
                    "--field", "key", "--path", str(repo)], env)
    if identity.returncode or not identity.stdout.strip():
        raise SystemExit(f"repository key for {repo} is unavailable, so an escape could not "
                         f"be detected: exit {identity.returncode} {identity.stderr.strip()}")
    return identity.stdout.strip()


def stores_in(root: Path) -> list[str]:
    """Every storage file the slots hold, so the artifact records the engine rather than assuming it.

    All of them, not the first: a candidate that imports legacy JSON into SQLite leaves both,
    and reporting one name would hide the very conversion this comparison exists to show.
    """
    return sorted({path.name for slot in slot_names(root)
                   for path in (root / slot).iterdir() if path.suffix in {".json", ".sqlite3"}})


def migration(arms: dict[str, dict[str, object]], out: Path) -> dict[str, object]:
    """Continue one baseline-seeded state root on both arms, which is the only way a legacy import runs.

    repetition() gives every arm a private, empty state root, so nothing there can ever
    exercise a candidate's import of state an older version wrote. Here the baseline CLI
    seeds a root, the seed is copied so both continuations start byte-identical, and each
    arm continues its own copy. Runs in its own directory outside both arms' state roots,
    so the per-arm freshness guarantee is untouched.

    Agreement alone is not acceptance. Two arms can project every field identically and
    still leave that state in different engines, so each arm is also asked what it writes
    starting from nothing, and what it left on the seeded root must contain that. An arm
    that natively uses one store but silently keeps the seeded one fails; a same-ref run,
    where the two are the same store, passes. No engine is named here.
    """
    home = out / "migration"
    # Recreated per run like each arm root, so a rerun against the same --out cannot
    # inherit the previous run's seed or fail on an existing fixture.
    shutil.rmtree(home, ignore_errors=True)
    start = time.perf_counter()
    env = arm_env(Path(str(arms["baseline"]["root"])), home / "seed-state")
    repo = fixture(home / "repo", env)
    # Every arm's own name for this fixture, not just the baseline's. The migration
    # fixture is a separate repository, so without its key arm_traces could not see an
    # escape that happened only here; and an arm whose identity function disagrees writes
    # under a name the baseline's key would never match, which is the same blind spot
    # one key wider.
    keys = sorted({repo_key(Path(str(arm["root"])), repo, arm_env(Path(str(arm["root"])), home / f"{name}-state"))
                   for name, arm in arms.items()})
    seeded = run([*cli_for(arms["baseline"]), "begin", "--repo", str(repo),
                  "--slug", "estate-benchmark", "--intent", "migration seed"], env)
    seed_stores = stores_in(home / "seed-state")
    # A write, because #74 imports legacy state on first write rather than on read. `pause`
    # is the one state-advancing command a freshly begun workflow accepts: every phase in
    # WORKFLOW_SEQUENCE still has an unmet predecessor at this point.
    instance = str(loaded(seeded.stdout).get("workflowId"))

    projections, exits, stores, native = {}, [], {}, {}
    for name, arm in arms.items():
        cli, root, probed = cli_for(arm), home / f"{name}-state", home / f"{name}-native"
        # What this arm writes starting from nothing, measured rather than named. An arm
        # whose probe refuses reports no store at all: the empty set is a subset of
        # everything, so it would satisfy the comparison below without proving anything.
        probe = run([*cli, "begin", "--repo", str(repo), "--slug", "estate-benchmark",
                     "--intent", "native store probe"], arm_env(Path(str(arm["root"])), probed))
        native[f"{name}NativeStores"] = [] if probe.returncode else stores_in(probed)
        # Guarded: copying a seed that `begin` never created aborts the whole benchmark,
        # and a baseline that cannot seed is a result the operator should still read
        # alongside the per-arm scenarios.
        if seed_stores:
            shutil.copytree(home / "seed-state", root)
            continued = run([*cli, "pause", "--repo", str(repo), "--slug", "estate-benchmark",
                             "--workflow-id", instance, "--reason", "migration differential"],
                            arm_env(Path(str(arm["root"])), root))
            projections[name], _ = project(continued.stdout, STATE_FIELDS)
            exits.append(continued.returncode)
        stores[f"{name}Stores"] = stores_in(root)
    match = bool(projections) and projections["baseline"] == projections["candidate"]
    converted = all(native[f"{name}NativeStores"]
                    and set(native[f"{name}NativeStores"]) <= set(stores[f"{name}Stores"]) for name in arms)
    return {"keys": keys, "seedExit": seeded.returncode, "seedStores": seed_stores,
            "exits": exits, **stores, **native, "match": match,
            "seconds": round(time.perf_counter() - start, 6),
            "ok": seeded.returncode == 0 and bool(seed_stores) and exits == [0, 0] and match and converted,
            **projections}


def timings(runs: list[dict[str, object]]) -> dict[str, object]:
    seconds = [float(run["seconds"]) for run in runs]
    return {"seconds": seconds, "medianSeconds": round(statistics.median(seconds), 6),
            "maxSeconds": round(max(seconds), 6)}


def capability_gap(baseline: dict[str, object], candidate: dict[str, object]) -> bool:
    """The candidate ships this operation and the baseline does not.

    Directional, because only one direction is the difference under test: a candidate is
    allowed to extend the governed grammar, and comparing that strictly would make every
    such candidate unacceptable. The reverse is a regression - a candidate that drops an
    operation its baseline ships - and stays a reported difference. Reading it as a set
    would collapse the two, which is exactly how a removed step went unreported. Only this
    exact pair qualifies, so a projection missing the key, which is every other scenario,
    is compared as usual.
    """
    return baseline.get("supported") is False and candidate.get("supported") is True


def compare(baseline: list[dict[str, object]], candidate: list[dict[str, object]]) -> dict[str, object]:
    base, cand = timings(baseline), timings(candidate)
    base_keys = {key for run in baseline for key in run["keys"]}
    cand_keys = {key for run in candidate for key in run["keys"]}
    gapped = any(capability_gap(one["projection"], other["projection"])
                 for one, other in zip(baseline, candidate, strict=True))
    return {
        "name": baseline[0]["name"],
        # Reported for the operator's merge decision beside the representation deltas,
        # and deliberately not a failure.
        "capabilityDelta": gapped,
        # What the arms did, not only that they agreed: without it two identically
        # broken arms read as a clean pass. Every repetition replays fresh state,
        # so the first is representative.
        "observed": baseline[0]["projection"],
        # When the arms ran different operations, one arm's projection is not the record.
        # The capable arm's is the interesting half and would otherwise appear nowhere,
        # since a declared delta contributes no difference to read it from.
        **({"observedCandidate": candidate[0]["projection"]} if gapped else {}),
        "invariantsHeld": all(EXPECTED[str(run["name"])](run["projection"]) for run in (*baseline, *candidate)),
        "differences": [{"repetition": index, "baseline": one["projection"], "candidate": other["projection"]}
                        for index, (one, other) in enumerate(zip(baseline, candidate, strict=True))
                        if one["projection"] != other["projection"]
                        and not capability_gap(one["projection"], other["projection"])],
        # Not reported across a capability gap: representation deltas compare what two arms
        # emitted for the SAME operation, and an arm that never ran it emits nothing, so
        # every key would read as candidate-only. That is an artifact of the gap the row
        # already declares, not a representation change worth an operator's attention.
        "representationDeltas": {"candidateOnly": [] if gapped else sorted(cand_keys - base_keys),
                                 "baselineOnly": [] if gapped else sorted(base_keys - cand_keys)},
        "baseline": base, "candidate": cand,
        "deltaMedianSeconds": round(cand["medianSeconds"] - base["medianSeconds"], 6),
        "deltaMaxSeconds": round(cand["maxSeconds"] - base["maxSeconds"], 6),
    }


def render(artifact: dict[str, object], path: Path) -> str:
    isolation, scenarios, m = artifact["isolation"], artifact["scenarios"], artifact["migration"]
    seeds = artifact["replaySeed"]

    # Representation deltas ride on their own scenario's row: they are reported
    # for the operator's merge decision, never compared and never a failure.
    def row(item: dict[str, object]) -> str:
        return (f"{item['name']:26} "
                f"{'DIFF' if item['differences'] else 'ok' if item['invariantsHeld'] else 'BROKEN':6}  median "
                f"{item['baseline']['medianSeconds']:.3f}->{item['candidate']['medianSeconds']:.3f} "
                f"({item['deltaMedianSeconds']:+.3f})  max {item['baseline']['maxSeconds']:.3f}->"
                f"{item['candidate']['maxSeconds']:.3f} ({item['deltaMaxSeconds']:+.3f})"
                + (f"  +keys {','.join(item['representationDeltas']['candidateOnly'])}"
                   if item["representationDeltas"]["candidateOnly"] else "")
                + ("  +capability" if item["capabilityDelta"] else ""))

    replay = [item for item in scenarios if str(item["name"]).startswith("replay-")]
    # Collapsed to one line while they are clean, because twelve more rows would push a
    # passing summary past the length that makes it readable. Any replay step that
    # diverged, failed its own invariant, or ran on only one arm is printed in full
    # underneath: a capability delta passes both other tests, so without it the marker
    # would describe a row the operator never sees and the summary would disagree with
    # the artifact it points at.
    faulty = [item for item in replay if item["differences"] or not item["invariantsHeld"]]
    # Printed, not counted: a capability delta is a passing operation, so it belongs in the
    # rows without being subtracted from the tally above it.
    shown = [item for item in replay if item in faulty or item["capabilityDelta"]]
    return "\n".join([
        f"A/B estate benchmark  schema={artifact['schemaVersion']}  "
        f"claude={artifact['claudeCodeVersion']}  repetitions={artifact['repetitions']}",
        *(f"{name:9} {str(arm['ref'])[:18]:18} commit {str(arm['commit'])[:12]} "
          f"tree {str(arm['tree'])[:12]}  smoke-exit {arm['configSmokeExit']}"
          for name, arm in artifact["arms"].items()),
        *(row(item) for item in scenarios if not str(item["name"]).startswith("replay-")),
        f"workflow-state replay: {len(replay) - len(faulty)}/{len(replay)} operations ok; "
        f"repo-context-forge once per arm, exits "
        f"{[seed['exit'] for seed in seeds.values()]} in "
        f"{seeds['baseline']['seconds']:.2f}->{seeds['candidate']['seconds']:.2f}s; downstream median total "
        f"{sum(item['baseline']['medianSeconds'] for item in replay):.3f}->"
        f"{sum(item['candidate']['medianSeconds'] for item in replay):.3f}s",
        *(row(item) for item in shown),
        f"isolation: {'ok' if isolation['ok'] else 'FAILED'}; estate digest "
        f"{'unchanged' if isolation['estateDigestBefore'] == isolation['estateDigestAfter'] else 'CHANGED'}; "
        f"{len(isolation['armKeys'])} arm keys, {len(isolation['leakedKeys'])} under the live root",
        f"migration: seed {','.join(m['seedStores']) or 'NONE'} (exit {m['seedExit']}) -> baseline "
        f"{','.join(m['baselineStores']) or 'none'} | candidate {','.join(m['candidateStores']) or 'none'}",
        # Both arms, because acceptance judges both: a line reporting FAILED while showing
        # only one of them cannot tell the operator which arm left its own engine behind.
        f"  native baseline {','.join(m['baselineNativeStores']) or 'NONE'} | "
        f"candidate {','.join(m['candidateNativeStores']) or 'NONE'}; exits {m['exits']}; state "
        f"{'matches' if m['match'] else 'DIFFERS'}; {'ok' if m['ok'] else 'FAILED'} in {m['seconds']:.3f}s",
        f"result: {'PASS' if artifact['ok'] else 'FAIL'}   artifact: {path}",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A/B benchmark of two trusted estate refs. Arms are separated by environment "
                    "redirection and checked afterwards, not sandboxed: both refs run with this "
                    "user's permissions and can modify any path this user can.")
    parser.add_argument("--baseline", required=True, help="ref for the baseline arm")
    parser.add_argument("--candidate", required=True, help="ref for the candidate arm")
    parser.add_argument("--out", required=True, help="disposable output directory")
    args = parser.parse_args()

    source = Path(git(Path.cwd(), "rev-parse", "--show-toplevel"))
    out = Path(args.out).expanduser().resolve()
    homes, roots = monitored(source)
    # Before any mutation at all, creating `out` included: build_arm clears <out>/<arm>
    # unconditionally, so an --out aimed at a live path destroys it and still reports a
    # result. Both nestings are refused, and each side is resolved so a symlinked estate
    # cannot slip past a lexical comparison.
    for protected in (path.resolve() for path in (*homes, *roots, source)):
        if out == protected or protected in out.parents or out in protected.parents:
            raise SystemExit(f"--out {out} overlaps a protected path: {protected}")
    # That guard resolves `out` only, so an arm root that is itself a link still escapes:
    # rmtree(ignore_errors=True) leaves a symlink alone and the arm is then built through
    # it. Both roots are checked here, before either is built.
    for arm in (out / "baseline", out / "candidate", out / "migration"):
        if arm.is_symlink():
            raise SystemExit(f"arm root must not be a symlink: {arm}")
    out.mkdir(parents=True, exist_ok=True)
    estate_before = {str(home): digest(home) for home in homes}
    entries_before = {str(root): slot_names(root) for root in roots}

    # Each distinct ref resolved exactly once, before either arm is created: reading a
    # ref per arm let one that moved between the two reads pin the arms at different
    # revisions, so a run comparing a ref with itself reported a behavioural diff.
    named = (("baseline", args.baseline), ("candidate", args.candidate))
    # dict.fromkeys, not a comprehension over `named`: that would still run rev-parse per
    # arm and merely let the later result win, leaving the second read to observe a move.
    commits = {ref: git(source, "rev-parse", f"{ref}^{{commit}}")
               for ref in dict.fromkeys(ref for _, ref in named)}
    pinned = [(name, ref, commits[ref]) for name, ref in named]
    arms = {name: build_arm(source, out, name, ref, commit) for name, ref, commit in pinned}
    observed: dict[str, list[list[dict[str, object]]]] = {}
    arm_keys: set[str] = set()
    seeds = {}
    for name, arm in arms.items():
        seed, seeds[name] = replay_seed(arm)
        # The replay fixture is a repository of its own, so without its key arm_traces
        # could not see an escape that happened only during the replayed sequence.
        arm_keys.add(str(seed["key"]))
        replays = [repetition(arm, index, seed) for index in range(REPETITIONS)]
        observed[name] = [scenarios for scenarios, _ in replays]
        arm_keys.update(key for _, key in replays)

    scenarios = [compare([replay[index] for replay in observed["baseline"]],
                         [replay[index] for replay in observed["candidate"]])
                 for index in range(len(observed["baseline"][0]))]
    migrated = migration(arms, out)
    arm_keys.update(str(key) for key in migrated["keys"])
    estate_after = {str(home): digest(home) for home in homes}
    entries_after = {str(root): slot_names(root) for root in roots}
    leaked = sorted(trace for root in roots for trace in arm_traces(root, arm_keys))
    artifact = {
        "schemaVersion": SCHEMA_VERSION, "repetitions": REPETITIONS, "sourceRepo": str(source),
        "claudeCodeVersion": run(["claude", "--version"], dict(os.environ)).stdout.strip(),
        "arms": arms, "scenarios": scenarios, "migration": migrated, "replaySeed": seeds,
        "isolation": {
            "liveEstates": [str(home) for home in homes], "liveStateRoots": [str(root) for root in roots],
            "estateDigestBefore": estate_before, "estateDigestAfter": estate_after,
            "stateRootEntriesBefore": entries_before, "stateRootEntriesAfter": entries_after,
            "armKeys": sorted(arm_keys), "leakedKeys": leaked,
            "ok": estate_before == estate_after and not leaked,
        },
    }
    artifact["ok"] = (artifact["isolation"]["ok"] and all(arm["configSmokeExit"] == 0 for arm in arms.values())
                      # helpExit too: a probe that could not run leaves stdout empty, which
                      # reads as "this arm does not ship the step" and would silently drop
                      # the typed operation from a capable arm. Absent and unmeasured are
                      # not the same answer, so only a probe that actually answered counts.
                      and all(seed["exit"] == 0 and seed["beginExit"] == 0 and seed["helpExit"] == 0
                              for seed in seeds.values())
                      and all(item["invariantsHeld"] and not item["differences"] for item in scenarios)
                      and migrated["ok"])
    path = out / "benchmark.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(render(artifact, path))
    return 0 if artifact["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
