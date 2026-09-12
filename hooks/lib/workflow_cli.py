"""One public command-line Interface for repository workflow operations."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from ._workflow_db import LedgerError, history
from .preflight_document import validated_document
from .command_runner import emit_json as _emit_json, print_output as _print_output, run as _run, run_entry as _run_entry
from .repo_identity import RepoIdentity, RepoIdentityError, resolve_repo_identity
from .state_prune import prune
from .state_store import _active_candidate_tree, repo_state_dir, state_root, tree_manifest, utc_timestamp
from .workflow_documents import (
    advisor_disposition_document,
    advisor_envelope,
    design_declaration,
    gate_verdict,
    review_summary,
    validate_gate_result,
)
from .workflow_state import (
    NO_INSTANCE_ID,
    WorkflowError,
    advisor_disposition,
    begin,
    bound_state,
    checkpoint,
    commit_evidence_phase,
    commit_review,
    commit_verification,
    complete,
    evidence_document,
    evidence_record,
    instance_id,
    pause,
    public_status,
    read_workflow,
    record_advisor_result,
    safe_slug,
    set_phase,
    summary,
)

ROOT = Path(__file__).resolve().parents[2]
LEAD_PHASES = {"implementation", "code-review"}
PRODUCER_OWNED = {
    "repo-context-forge": "repo-context-forge is producer-owned; run the Repo Context Forge bootstrap",
    "gitnexus": "gitnexus is no longer a workflow step; Repo Context Forge records the graph "
                "evidence automatically, so there is nothing to transition",
    "preflight": "preflight is recorder-owned; use workflow record-preflight",
    "production-code": "production-code is recorder-owned; use workflow record-production-code",
    "verification": "verification is runner-owned; use workflow verify",
}


def _repo(command: argparse.ArgumentParser) -> None:
    command.add_argument("--repo", "--cwd", dest="repo", default=".")


def _instance(command: argparse.ArgumentParser) -> None:
    command.add_argument("--slug", required=True)
    command.add_argument("--workflow-id", required=True)


def _instance_command(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    help_text: str,
) -> argparse.ArgumentParser:
    command = commands.add_parser(name, help=help_text)
    _repo(command)
    _instance(command)
    return command


def _document_command(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
    command.add_argument("--input", required=True)
    return command


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="workflow", description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    command = commands.add_parser("begin", help="start and activate a workflow pass")
    _repo(command)
    command.add_argument("--slug", required=True)
    # Multi-KB request text does not survive being a shell argument, which is why
    # callers reach for a summary. The group refuses both at once rather than
    # inventing precedence between two sources of the same field.
    source = command.add_mutually_exclusive_group()
    source.add_argument("--intent", default="")
    source.add_argument("--intent-file")

    for name in ("status", "summary"):
        command = commands.add_parser(name)
        _repo(command)

    command = commands.add_parser(
        "paths",
        help="print the resolved workflow-state paths for this repository; "
             "with --workflow-id also prints the governing-design path",
    )
    _repo(command)
    command.add_argument("--workflow-id")

    command = commands.add_parser("history", help="read ordered accepted events")
    _repo(command)
    command.add_argument("--workflow-id")

    command = commands.add_parser("evidence", help="read a logical evidence record")
    _repo(command)
    command.add_argument("--evidence-id", required=True)

    command = commands.add_parser("set-phase", help="record a lead-owned phase")
    _repo(command)
    command.add_argument("--phase", required=True)
    command.add_argument("--status", required=True)
    command.add_argument("--findings")
    command.add_argument("--slug")
    command.add_argument("--workflow-id")

    command = _instance_command(commands, "advisor-result", "record a completed advisor result")
    command.add_argument("--stage", required=True)
    command.add_argument("--source", required=True)
    command.add_argument("--verdict")
    command.add_argument("--input")
    command.add_argument("--findings")
    command.add_argument("--reason")
    command.add_argument("--design-declaration", required=True)
    command.add_argument("--expected-candidate-tree")

    command = _instance_command(commands, "advisor-disposition", "record lead disposition of advisor findings")
    command.add_argument("--stage", required=True)
    command.add_argument("--findings", required=True)
    command.add_argument("--input")

    command = _instance_command(commands, "pause", "record an instance-bound honest wait")
    command.add_argument("--reason", required=True)

    command = commands.add_parser("checkpoint", help="query advisor readiness without mutation")
    _repo(command)
    command.add_argument("--phase", required=True)

    command = commands.add_parser("complete", help="complete a ready workflow")
    _repo(command)
    command.add_argument("--slug")
    command.add_argument("--workflow-id")

    _document_command(_instance_command(commands, "record-preflight", "validate and record production preflight"))
    _document_command(_instance_command(commands, "record-production-code", "validate and record the pre-edit quality gate"))

    # Registered for top-level discovery only: the tdd verbs' flag surfaces are
    # owned by tdd_workflow, which main() routes to before this parser ever
    # sees the arguments. tdd-map is a completion-required producer, so its
    # discovery is a deliberate addition to the baseline listing.
    commands.add_parser("tdd", help="run and record one real RED/GREEN candidate")
    commands.add_parser("tdd-map", help="record Behavior Map reassessments and dispositions")

    command = commands.add_parser("verify", help="execute and record typed verification")
    _repo(command)
    command.add_argument("--slug", required=True)
    command.add_argument("--kind", choices=("generic", "quality-gate"), default="generic")
    command.add_argument("--base-ref")
    command.add_argument("--timeout", type=int, default=900)
    command.add_argument("runner_command", nargs=argparse.REMAINDER)

    command = _document_command(_instance_command(commands, "record-review", "validate and record the code review"))
    command.add_argument("--resolved-model", required=True)
    command.add_argument("--review-context-id", required=True)

    command = commands.add_parser("prune", help="report or apply workflow-state retirement")
    command.add_argument("--apply", action="store_true")

    return result





def _intent(args: argparse.Namespace) -> str:
    """The task text exactly as the caller sent it: no stripping, no truncation.

    Both file and stdin intake decode raw bytes as UTF-8 rather than reading text:
    the locale's codec would make the record depend on the environment that started
    the pass, and text mode would translate CRLF and lone CR to LF, so a request
    pasted from a Windows editor would be recorded as something it never said.
    U+0000 is refused rather than recorded: the advisor payload carries this text
    through a shell variable, which cannot hold that character, so accepting it
    would promise a custody the rest of the chain silently breaks.
    """
    if args.intent_file is not None:
        text = Path(args.intent_file).read_bytes().decode("utf-8")
    else:
        text = sys.stdin.buffer.read().decode("utf-8") if args.intent == "-" else args.intent
    if "\0" in text:
        raise ValueError("intent text cannot contain U+0000; the consult payload cannot carry it")
    return text


def _emit_mutation(
    identity: RepoIdentity, operation: Callable[[str], dict[str, object]],
) -> None:
    """Bind full-state output before its mutation can commit."""
    candidate = _active_candidate_tree(identity)
    _emit_json(public_status({**operation(candidate), "activeCandidateTree": candidate}, identity))


def _state(identity: RepoIdentity) -> dict[str, object]:
    value = read_workflow(identity)
    if value is None:
        raise WorkflowError("no active workflow")
    return value


def _workflow_id(state: dict[str, object]) -> str:
    value = instance_id(state)
    if value is None:
        raise WorkflowError(NO_INSTANCE_ID)
    return value


def _verify(args: argparse.Namespace, identity: RepoIdentity) -> int:
    state = bound_state(identity, safe_slug(args.slug))
    slug = str(state["slug"])
    workflow_id = _workflow_id(state)

    # The tree this run's result will describe. The recorder compares it with
    # the tree at commit; a run that could not sample it is recorded invalid.
    binding_error: str | None = None
    tree_before: dict[str, str] | None = None
    graph_evidence_id: str | None = None
    graph_context_path: str | None = None
    try:
        tree_before = tree_manifest(identity)
    except RuntimeError as exc:
        binding_error = str(exc)

    if args.kind == "quality-gate":
        if not args.base_ref:
            raise ValueError("quality-gate verification requires --base-ref")
        if args.runner_command:
            raise ValueError("quality-gate verification runs the bundled gate and accepts no command")
        command = [
            sys.executable,
            str(ROOT / "skills" / "production-code" / "scripts" / "code_quality_gate.py"),
            "check",
            "--repo",
            str(identity.root),
            "--base-ref",
            args.base_ref,
            "--json",
        ]
        # The pass's recorded Repo Context Forge evidence, handed to the gate
        # unchanged when it carries the producer's snapshot-bound gate context.
        # The gate's own binding check adjudicates match, stale, or absent; a
        # document without that context simply attaches nothing, and the gate
        # names the absence.
        recorded = state.get("repoContextForgeEvidence")
        graph_document = evidence_document(identity, recorded if isinstance(recorded, str) else None)
        graph_context = (
            graph_document.get("gateContext")
            if isinstance(graph_document, dict) and graph_document.get("workflowId") == workflow_id
            else None
        )
        if isinstance(graph_context, dict):
            graph_evidence_id = str(recorded)
            handle = tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", prefix="quality-gate-graph-", suffix=".json", delete=False,
            )
            with handle:
                json.dump(graph_context, handle)
            graph_context_path = handle.name
            command += ["--gitnexus-context-json", graph_context_path]
    else:
        if args.base_ref:
            raise ValueError("--base-ref belongs to --kind quality-gate")
        command = args.runner_command[1:] if args.runner_command and args.runner_command[0] == "--" else args.runner_command
        if not command:
            raise ValueError("a command is required after --")

    try:
        raw, exit_code, timed_out = _run(command, identity, args.timeout)
    finally:
        if graph_context_path is not None:
            os.unlink(graph_context_path)
    valid = binding_error is None and not timed_out and exit_code == 0
    gate: dict[str, object] | None = None
    if args.kind == "quality-gate":
        try:
            gate = validate_gate_result(json.loads(raw.decode("utf-8")))
            valid = valid and gate.get("ok") is True
            errors = gate.get("errors")
            capture = next(
                (
                    error for error in (errors if isinstance(errors, list) else ())
                    if isinstance(error, str) and error.startswith("candidate capture ")
                ),
                None,
            )
            if binding_error is None and capture is not None:
                # Drift and outright capture failure are one condition here: the gate
                # never held a tree still, so nothing it reports binds to one. Only the
                # drift shape has a settled name; the rest carry the gate's own words
                # rather than being attributed to a cause the runner cannot know.
                binding_error = (
                    "reviewable tree changed during the quality-gate run"
                    if capture.startswith("candidate capture drift:")
                    else f"the quality gate could not capture the reviewable tree: {capture}"
                )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            valid = False
            binding_error = str(exc)

    run = _run_entry(
        raw, exit_code, timed_out,
        kind=args.kind, command=shlex.join(command), valid=valid,
    )
    if args.kind == "quality-gate":
        run["baseRef"] = args.base_ref
        run["gate"] = gate
        run["bindingError"] = binding_error
        run["graphEvidenceId"] = graph_evidence_id
    elif binding_error is not None:
        run["bindingError"] = binding_error
    state, evidence_id, recorded = commit_verification(identity, slug, workflow_id, run, tree_before=tree_before)

    _print_output(raw)
    _emit_json({
        "evidenceId": evidence_id,
        "exitCode": exit_code,
        "kind": args.kind,
        "verification": state["verification"],
        "valid": recorded["valid"],
    })
    if recorded["valid"] is not True:
        reason = recorded.get("bindingError") or "verification command failed"
        print(f"{reason}; verification stays pending until its rerun is green", file=sys.stderr)
        return 2
    return 0


def _record_phase(args: argparse.Namespace, identity: RepoIdentity, phase: str, key: str, value: object) -> int:
    slug = safe_slug(args.slug)
    document = {
        "schemaVersion": 1,
        "slug": slug,
        "workflowId": args.workflow_id,
        key: value,
        "recordedAt": utc_timestamp(),
    }
    state, evidence_id = commit_evidence_phase(identity, slug, args.workflow_id, phase, document)
    recorded = {"evidenceId": evidence_id, "status": "passed"}
    if phase == "preflight":
        # The plan-commit gate re-presents the contract: the builder reads the recorded
        # task text back here instead of building the rest of the pass from recall.
        recorded["intent"] = state.get("intent")
    _emit_json(recorded)
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "prune":
        _emit_json(prune(apply=args.apply))
        return 0

    identity = resolve_repo_identity(args.repo)
    if args.command == "begin":
        _emit_json(public_status(begin(identity, args.slug, _intent(args))))
    elif args.command == "status":
        _emit_json(public_status(_state(identity), identity))
    elif args.command == "paths":
        directory = repo_state_dir(identity)
        out: dict[str, object] = {
            "stateRoot": str(state_root()),
            "repoKey": identity.key,
            "repoStateDir": str(directory),
        }
        if args.workflow_id:
            out["designPath"] = str(directory / "designs" / f"{args.workflow_id}.md")
        _emit_json(out)
    elif args.command == "summary":
        print(summary(identity))
    elif args.command == "history":
        _emit_json(history(identity, args.workflow_id))
    elif args.command == "evidence":
        value = evidence_record(identity, args.evidence_id)
        if value is None:
            raise WorkflowError("evidence not found")
        _emit_json(value)
    elif args.command == "set-phase":
        phase = args.phase
        if phase in PRODUCER_OWNED:
            raise ValueError(PRODUCER_OWNED[phase])
        if phase not in LEAD_PHASES:
            raise ValueError("set-phase is lead-owned only for implementation and code-review not-required")
        if phase == "code-review" and (args.status != "not-required" or args.findings != "none"):
            raise ValueError("code-review passed is recorder-owned; lead-owned set-phase permits only not-required with findings none")
        _emit_mutation(identity, lambda candidate: set_phase(
            identity,
            phase,
            args.status,
            findings=args.findings,
            slug=args.slug,
            workflow_id=args.workflow_id,
            expected_candidate_tree=candidate,
        ))
    elif args.command == "advisor-result":
        intake = None
        verdict = args.verdict
        if args.input is not None:
            if any(value is not None for value in (args.verdict, args.findings, args.reason)):
                raise ValueError("advisor-result --input derives verdict and findings; legacy verdict fields are incompatible")
            intake, verdict = advisor_envelope(
                args.input,
                slug=safe_slug(args.slug),
                workflow_id=args.workflow_id,
                stage=args.stage,
                producer=args.source,
            )
        elif verdict is None:
            raise ValueError("advisor-result requires --input or legacy --verdict")
        def record(candidate: str) -> dict[str, object]:
            expected = args.expected_candidate_tree
            if expected is not None and expected != candidate:
                raise WorkflowError("active candidate changed after the advisor checkpoint")
            return record_advisor_result(
                identity,
                args.slug,
                args.workflow_id,
                args.stage,
                args.source,
                verdict,
                findings=args.findings,
                reason=args.reason,
                design=design_declaration(args.design_declaration),
                intake=intake,
                expected_candidate_tree=expected or candidate,
            )

        _emit_mutation(identity, record)
    elif args.command == "advisor-disposition":
        if args.findings == "addressed" and args.input is None:
            raise ValueError("an addressed disposition requires --input with the lead's disposition document")
        if args.findings == "none" and args.input is not None:
            raise ValueError("--input records an addressed disposition; findings none carries no document")
        document = advisor_disposition_document(
            args.input,
            slug=safe_slug(args.slug),
            workflow_id=args.workflow_id,
            stage=args.stage,
        ) if args.input else None
        _emit_mutation(identity, lambda candidate: advisor_disposition(
            identity,
            args.slug,
            args.workflow_id,
            args.stage,
            args.findings,
            document=document,
            expected_candidate_tree=candidate,
        ))
    elif args.command == "pause":
        _emit_mutation(identity, lambda candidate: pause(
            identity, args.slug, args.workflow_id, args.reason,
            expected_candidate_tree=candidate,
        ))
    elif args.command == "checkpoint":
        _emit_json(checkpoint(identity, args.phase))
    elif args.command == "complete":
        _emit_mutation(identity, lambda candidate: complete(
            identity, slug=args.slug, workflow_id=args.workflow_id,
            expected_candidate_tree=candidate,
        ))
    elif args.command == "record-preflight":
        return _record_phase(args, identity, "preflight", "document", validated_document(args.input))
    elif args.command == "record-production-code":
        return _record_phase(args, identity, "production-code", "gate", gate_verdict(args.input))
    elif args.command == "verify":
        return _verify(args, identity)
    elif args.command == "record-review":
        slug = safe_slug(args.slug)
        if _workflow_id(bound_state(identity, slug)) != args.workflow_id:
            raise WorkflowError("--workflow-id does not match the active workflow instance")
        document, status, findings = review_summary(
            args.input,
            slug=slug,
            workflow_id=args.workflow_id,
            resolved_model=args.resolved_model,
            review_context_id=args.review_context_id,
        )
        state, evidence_id = commit_review(identity, slug, args.workflow_id, document, status, findings)
        _emit_json({"summaryId": evidence_id, "status": state["codeReview"]["status"]})
    else:
        raise ValueError(f"unsupported workflow command: {args.command}")
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    try:
        # The TDD verbs' parsing travels with their implementation: the domain
        # module owns both flag surfaces (mapped and imported-legacy), so they
        # are routed before this module's stricter argparse ever sees them.
        if values and values[0] == "tdd":
            from .tdd_workflow import run_tdd
            return run_tdd(values[1:])
        if values and values[0] == "tdd-map":
            from .tdd_workflow import run_map_update
            return run_map_update(values[1:])
        return _dispatch(parser().parse_args(values))
    except (RepoIdentityError, LedgerError, WorkflowError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
