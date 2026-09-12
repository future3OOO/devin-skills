---
name: execution-planning
description: Create durable advisor-bound governing designs outside the Git checkout. Use when planning multi-PR implementation, branch strategy, recovery, or review remediation; the design owns scope, PR ownership and order, verification gates, preservation obligations, and load-bearing assumptions, and deepens append-only in the same unpushed workflow.
---

# Execution Planning

Use this skill for non-trivial planning in this repo.

This skill does not replace the repo's `CLAUDE.md` (or `AGENTS.md`).
It turns planning and remediation into durable governing designs under workflow state, outside the Git candidate.

## Mandatory Workflow Position

For new non-trivial planning work, use this order:

1. [repo-production-workflow](../repo-production-workflow/SKILL.md) to begin the workflow state
2. [repo-context-forge](../repo-context-forge/SKILL.md) to establish repository context
3. delivery-governance skill, when planning needs delivery-shape decisions
4. execution-planning (this skill)

If delivery-governance does not apply, proceed from Repo Context Forge directly to execution-planning. After the design is written and validated, continue the same workflow pass through the remaining repo-production-workflow steps for the first implementation.

If the work is already governed by [repo-large-implementation](../repo-large-implementation/SKILL.md), use this skill as the durable-design step inside that workflow.

For later implementation against an existing design, do **not** invoke execution-planning again. Execute each pass through [repo-production-workflow](../repo-production-workflow/SKILL.md), follow the governing design, and use the durable progress authorities defined below.

The design owns architecture, scope, and delivery order. Mutable execution status never goes into the advisor-bound design.

## Core Rule

Do not leave the governing design only in chat, and do not add it to the Git candidate by default.

Before implementation starts, save one Markdown design under the selected workflow state root. Use the canonical repository identity owner to place it at:

`<workflow-state-root>/<repo-key>/designs/<workflowId>.md`

The workflow's public status Interface supplies `<workflowId>`; callers do not derive or normalize another workflow identity. Resolve the design path through the installed workflow CLI — never compute the state root by hand:

```bash
python3 <estate>/skills/repo-production-workflow/scripts/workflow.py paths --repo "$PWD" --workflow-id <workflowId>
```

`paths` prints the resolved `designPath`, `repoStateDir`, and `stateRoot`. The prose description of that resolution (`DEVIN_WORKFLOW_STATE_ROOT`, then `${DEVIN_ESTATE_HOME:-$HOME/.config/devin}/state`) is context only; `~/.local/share/devin` is the CLI's data dir and is not the workflow state root.

## Governing Design Format

Create exactly one advisor-bound governing design for the planned work. Before the first advisor consult, the design must contain:

- the complete decided design and delivery map
- every material preservation obligation and load-bearing assumption, stated in prose the Behavior Map can turn into concrete attacks
- every unverified falsifiable prediction explicitly marked unresolved

The wrapper records the design's declaration (its SHA-256, or a stated absence) as workflow evidence at each consult.

## Semantic Ownership

- The lead owns semantic completeness: every material preservation obligation and load-bearing assumption must be expressed.
- The Preflight Advisor derives the load-bearing promises from the original request and challenges omissions and missing attacks.
- Production preflight turns those obligations into Behavior Map falsifiers; findings own their attacks through finding `sourceRefs`.
- The Final Advisor re-derives the attack surface from the original request and public Interface before checking declared evidence.

The design is a falsifiable hypothesis, not an immutable authority. Deepen it append-only in the same unpushed workflow and carry the current file to each consult; a changed declaration records as new workflow evidence and never by itself requires another `begin`, another preflight consult, or a Repo Context Forge rerun.

Create a tracked document under `docs/plans/` or `docs/reviews/` only when the user explicitly requests that document as a deliverable. A tracked deliverable is not the advisor-bound design and is never updated merely to reflect execution progress.

## Existing Tracked Artifacts

An existing tracked governing artifact that already controls in-flight work remains authoritative under its existing contract. Do not migrate, rename, or rewrite it merely to adopt the workflow-state design policy.

## Required Planning Workflow

### 1. Choose the authority set

Name:

- source of truth
- trusted base branch
- branch or PR references
- conflict rule

Use repo authorities explicitly when relevant:

- the repo's `CLAUDE.md` or `AGENTS.md`
- the repo's canonical implementation spec under `docs/specs/`
- the repo's `DECISIONS.md`
- any pinned donor, production, or review evidence document

### 2. Define scope before structure

State:

- scope in
- scope out
- known non-goals
- whether the work is implementation, recovery, consolidation, remediation, or review-only

If scope cannot say what is excluded, it is too loose.

### 3. Define the delivery map

Name:

- PR count
- PR order
- branch names
- owner slice per PR
- commit structure per PR
- estimated net-line budget per PR (review-budget measurement rules live in the delivery-governance skill)
- scope breaker or regroup rule

If the design cannot answer “which PR owns this behavior,” it is not ready.

If any PR slice is likely to run past the review-budget target (~500 net lines), record the reason and shrink it where practical. If any slice is likely to exceed the split threshold (1,000 net lines), split it before implementation or record explicit user approval for the exception.

If the work updates an existing PR, also name:

- PR number
- branch name
- exact checkout path the execution agent must edit
- trusted base branch

And require this execution rule:

- before tracked edits, fetch the PR branch and realign that exact checkout to the live PR head SHA
- do not edit from a stale detached review worktree

### 4. Define verification

List:

- targeted tests per PR or per remediation pass
- full gate commands
- merge readiness conditions
- post-merge or follow-up classification work when required

### 4a. Map the affected surface for all code work

Every code-governing design must define the real affected surface, not just the local diff.

At minimum, name:

- changed boundary or behavior
- adjacent consumers or dependents that must remain correct
- upstream triggers or callers when relevant
- no-change surfaces that could regress and therefore need proof

Use practical repo evidence such as entrypoints, direct imports/callers, proving tests, and cheap co-change history when needed to map this surface. Do not rely on memory or the cited comment alone.

Keep this proportional for ordinary work.

### 4b. Map the affected transaction system when the work is transaction-sensitive

Load and apply the [canonical transaction doctrine](../production-code/references/transaction-doctrine.md).
The design must expose its authoritative records, mutation boundary,
interleavings, shared projection, replay, recovery, stale-secondary, and no-op paths, helper semantic splits,
contract, invariants, and proof plan; planning does not redefine them.

### 5. Initialize durable execution state

Begin repository-scoped workflow history for each PR slice or remediation pass. Use it for pass lifecycle evidence, blockers, and findings rather than rewriting the design. Use GitHub PR state for committed, pushed, review, and merge status when the work has a PR. Tasks may mirror immediate work inside one session but are not a handoff record.

For an existing PR branch, completion still requires committed and pushed changes before review threads are resolved as fixed. Local uncommitted or unpushed changes do not count as completed remediation.

### 6. Critique the draft before finalizing

Challenge the design before calling it ready.

If sub-agents are appropriate for the task, spawn one critique agent (Agent tool, subagent_type=general-purpose) after the first full draft and before finalizing the design. The critique pass should check at minimum:

- authority model and conflict handling
- scope in / scope out clarity
- PR count and PR order
- owner-slice boundaries
- verification completeness
- whether the implementation prompt incorrectly tells the execution agent to re-plan

Integrate real critique findings into the design before validating and binding it.

If delegation is not available or not authorized, do the same critique pass yourself before final validation.

## Minimum Readiness Rule

Do not call the design ready unless it names all of the following:

- objective
- source of truth
- scope in
- scope out
- trusted base
- PR ownership
- PR order
- commit structure
- net-line budget per PR, with no unapproved slice above the split threshold
- verification commands
- regroup or consolidation rule
- every material preservation obligation and load-bearing assumption in prose

For all code work, also require:

- affected surface
- no-change proof surfaces

For transaction-sensitive work, also require:

- authoritative records
- mutation boundary
- adjacent interleavings
- projection paths
- replay paths
- recovery paths
- stale-secondary paths
- no-op paths
- helper semantic splits
- authoritativeContract
- invariants
- proofPlan
- combined workflow proof

## Deepening And Progress Discipline

Authority is divided deliberately:

- The governing design owns architecture, scope, PR ownership, and execution order, and deepens append-only in the same unpushed workflow; the ledger keeps every prior declaration.
- Repository-scoped workflow history owns each pass's durable lifecycle, evidence, blockers, and findings.
- GitHub PR state owns committed, pushed, review, and merge status for delivered slices.
- The Task list is a session-local convenience, never durable authority.

Correcting the design or its attack set never by itself requires a replacement design, a new workflow pass, another preflight consult, or a Repo Context Forge rerun; only changed production behavior invalidates the proof it can affect. Mutable execution status still never goes into the design.

## Required Handoff Prompt

After validating the governing design, provide a compact copy-paste prompt for the execution agent.

Rules for that prompt:

- point to the design path under workflow state
- say explicitly: do not create a new plan or re-plan this pass
- say explicitly: deepen the design append-only when new obligations surface; use workflow history and GitHub PR state for durable progress, with Tasks only as session-local convenience
- when the work targets an existing PR, require realignment of the exact checkout to the live PR head before edits, then commit and push before resolving review threads as fixed
- keep the PR near the review-budget target unless the user approved a concrete exception
- require [Production Code’s Minimum Implementation Decision](../production-code/SKILL.md#minimum-implementation-decision) before edits and completion, including its [transaction doctrine](../production-code/references/transaction-doctrine.md) when applicable
- direct each execution pass through [repo-production-workflow](../repo-production-workflow/SKILL.md), which records this prompt verbatim as that pass's intent
- keep the prompt compact; do not duplicate the design

## Output Shape

Use [references/plan-template.md](references/plan-template.md) as the governing-design starting point. Use [references/remediation-map-template.md](references/remediation-map-template.md) only for a tracked remediation document the user explicitly requested as a deliverable.

Keep the final design lean. Do not fill sections with boilerplate.

## Repo-Specific Rules

- Use WSL-native paths and tools as authoritative for this repo.
- Keep advisor-bound designs under workflow state, outside Git.
- Do not create or update `docs/plans/` or `docs/reviews/` for ceremony; use them only for an explicitly requested deliverable.
- If recovery or donor reconciliation is involved, record pinned SHAs and evidence paths explicitly.
- If review comments drive the work, classify them as real, stale, no-change, or deferred instead of blindly converting comments into tasks.

## Completion Rule

This skill is complete only when:

- the governing design exists under the repository's workflow state directory
- the design has the required structure
- PR ownership, order, and verification are explicit
- workflow history and, when applicable, GitHub PR state carry the durable execution facts
- later agents could execute without reconstructing the design from chat or session-local Tasks
