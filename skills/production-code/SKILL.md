---
name: production-code
description: Enforce production-only implementation standards for this repo. Use when implementing, refactoring, fixing bugs, reviewing code, or tightening tests where the outcome must stay minimal, direct, duplication-averse, fail closed, strictly typed, boundary-validated, cleanup-safe, and fully verified before being called complete.
---

# Production Code

Apply this skill before writing any repository code or file content change, keep it active while implementing, and run its bundled gate before finalizing.
Use the production-preflight skill first on before-edit turns that require explicit preflight. `code-quality` owns the seven quality principles and wins on conflict; this skill extends them with implementation procedure.

In a governed production workflow, invoke this skill after the RED or
not-required TDD decision and before production, configuration, or runtime
implementation edits; the test edit that establishes RED may precede it. Run
the bundled gate over the pre-implementation tree as the clean baseline, then keep
this doctrine active through implementation and final verification.

Before editing, use the standards below to choose the smallest production-safe implementation path. Run the bundled non-mutating gate from the target repository before finalizing:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 "$HOME/.config/devin/skills/production-code/scripts/code_quality_gate.py" check --repo "$PWD"
```

Use `--base-ref <ref>` when a review base is known; without it the gate
measures the worktree against `HEAD` only and reports its cumulative-growth
claim as incomplete. In a governed pass the PostToolUse gate hook supplies the
base OID recorded at Repo Context Forge bootstrap automatically, so per-edit
warnings already read branch-cumulative. Existing Repo Context Forge
or GitNexus evidence can be supplied with `--repo-context-packet <path-or->`
and `--gitnexus-context-json <path-or->`. A bare run supplies no graph
evidence, so the `QG54-OWNER-COMPETITION-*` rules report incomplete there; in
the governed workflow the typed verification run
(`workflow.py verify --kind quality-gate`) attaches the pass's recorded
snapshot-bound Repo Context Forge evidence automatically. Load
[references/gate-policy.md](references/gate-policy.md) when interpreting the
gate's JSON contract.

## Minimum Implementation Decision

Before choosing a repair or mechanism, complete the affected decision:

- Derive requested and preserved guarantees from the original contract, base, and reachable callers/docs/tests independently of the map. Inspect supported input forms, interactions, known defects, and successful cases a new guard could exclude. Separate intentional contract changes from regressions; keep unrelated behavior outside the repair.
- Simplify the shared decision rather than adding symptom guards. Ask what materially wrong behavior would pass the retained checks. Reuse the smallest real-Interface operations that distinguish it, observing required results, data, identity, state, and cleanup; add only uncovered outcomes.
- Replay applicable retained failing and passing operations unchanged on the candidate. For a bug or suspected regression, use the same operation/assertions against identified old and candidate implementations. Reconcile the map with this evidence before returning for review.

Use the request/map already in context; load missing evidence once at implementation entry and refresh only on material change. An edit-hook reminder cannot supply reasoning for already-generated edit arguments.

Resolve ownership placement inside that decision:

1. Prove whether the required behavior already exists. If a named Interface already provides it and real test-surface evidence verifies the requirement, make no production change.
2. Choose the responsible owner. Consume production preflight's `moduleShape` decision. When the turn required no preflight, deepen the existing Module; proposing a new Module or Seam requires preflight first. Delete every surface the change supersedes.
3. Inside that owner, reuse a capability whose Interface already owns the required semantics, invariant, or failure policy: standard library; native platform, runtime, datastore, or protocol; or an already-installed dependency. These are peers; choose by authority, not list order.
4. Treat the changed Implementation as bloated. **Reduce it first.** Delete duplication and consolidate existing owners before adding code. Every change targets fewer lines; justify necessary growth against the actual requirement. Preserve production behaviour and useful assertions. Moving complexity or compressing formatting does not count.

Implementation mechanism never chooses placement: a library or native capability does not justify a new Module or Seam. Every choice must preserve required behavior, boundary validation, security, accessibility, data-loss protection, cleanup, and affected-surface proof.

The decision is complete only when one outcome is recorded:

- Existing behavior: name its owning Interface and real test-surface evidence; plan no production change.
- Change required: name the responsible owner, preflight's selected `moduleShape` when preflight ran, Interface and test surface, existing capability to reuse or why custom Implementation is required, minimum changed surface, and every superseded surface to delete.

## Core Standard

- Ship production code only.
- Make the smallest correct change.
- Delete lines that do not directly serve the requirement.
- Remove dead code instead of hiding it behind flags or wrappers.
- Apply [codebase-design](../codebase-design/SKILL.md) when judging Module depth and consolidation; file size is not the measure.
- Do not add orchestration layers, control-plane hops, or indirection that the requirement does not need.
- Prefer readable, direct code over verbose generated patterns.
- Smallest change means the smallest final diff, not the smallest tool call: prepare coherent multi-hunk edits per file and batch independent edits in one message; consecutive single-line edits to one file are the smell this rule prevents.
- If the current work is governed by a tracked plan or review artifact that includes an execution checklist, follow that artifact during implementation instead of drifting to an unwritten plan.

## Non-Negotiable Rules

- Eliminate duplication.
- Reuse existing utilities when behavior is equivalent.
- Keep one implementation per behavior.
- Delete new helper functions, loops, or adapters that reimplement an existing path; call or narrowly extend the existing path instead.
- Do not add shallow helper, service, manager, wrapper, or adapter modules that only pass through, rename, split, expose many public names, or orchestrate existing behavior.
- If a shallow helper/module is inside the changed behavior path, absorb it into the deeper module or record a concrete blocker explaining why it cannot be safely changed in this PR.
- Keep private helpers behind the existing module interface unless preflight justifies a new public seam.
- Preserve direct data flow.
- Keep I/O and control flow explicit and traceable.
- Apply the canonical mock ban and fake-green rules in `~/.config/devin/AGENTS.md`; no local procedure creates an exception.
- Never use `|| true`, swallow-and-continue flows, blanket catch/pass, or suppression that hides a real failure.
- Never leave `TODO`, `FIXME`, `HACK`, placeholder stubs, dummy implementations, fake adapters, or temporary bypasses in shippable code.
- Treat uncertainty as a stop-and-verify condition, not a reason to guess.
- Treat review comments as evidence to verify against the code and contract, not authority to obey blindly.
- Apply the canonical imaginary-risk ban in `~/.config/devin/AGENTS.md` before adding any guard, fallback, retry, configuration, abstraction, or code.
- Stay on task: if the cumulative diff grows past roughly 3× what the task implies, stop and justify the overrun before continuing.
- For behavior proof invoke `tdd`; the canonical mock ban governs every claimed RED/GREEN result.

## Data, Types, and Boundaries

- Validate untrusted inputs at the boundary.
- Treat network, HTTP, env, Firestore, Pub/Sub, Chat, Gmail, Cloud Tasks, and third-party payloads as `unknown` until validated.
- Validate once at the boundary with one consistent schema path, then convert to typed internal DTOs.
- Re-check invariants at state-mutation boundaries.
- Do not pass raw provider payloads deeper into the system.
- Do not silently default required fields.

## Stack-Specific Rules

For TypeScript or JavaScript changes, load and apply [references/typescript.md](references/typescript.md). Do not load that reference for unrelated stacks.

## State Mutation Discipline

- Keep business rules separate from persistence calls.
- Do not spray ad-hoc persistence updates through handlers.
- Route critical state changes through dedicated transition helpers or structured service methods.
- Enforce preconditions and postconditions inside each transition helper.
- Use transactions for critical multi-document state changes when needed.
- Update `updated_at` on every successful transition.
- Make failed transitions observable in logs and audit paths where appropriate.

## Transaction-Sensitive Work

Load [references/transaction-doctrine.md](references/transaction-doctrine.md) for transaction-sensitive changes. Its canonical proof requirements are part of the Minimum Implementation Decision, not a separate repair or review stage.

## Retries, Cleanup, and Dependencies

- Bound every retry by attempts or time.
- Make retries observable with logs and counters.
- Provide an explicit fail path or dead-letter path for retry loops.
- Leave no orphaned temp state, leaked leases, or silent leftovers.
- Keep startup, pre-task, and post-task cleanup deterministic.
- Treat a new dependency as a separate justified decision, never as reuse; do not add one when an existing capability satisfies the requirement cleanly.
- Do not introduce a second package manager or second lockfile.

## Execution Checklist

- Complete the Minimum Implementation Decision before writing code, including untracked files, scratch implementation files, generated source, or a new worktree.
- Inspect the delta and remove unnecessary additions.
- Scan for common quality escapes such as `TODO`, `FIXME`, `eslint-disable`, `@ts-ignore`, and broad catch/pass patterns.
- Run the bundled production code quality gate.
- If the gate reports errors or actionable warnings, go back to the code, remove the bloat or quality escape, and rerun the gate.
- If the gate reports a `QG54-OWNER-COMPETITION-*` warning, it has named both competing owners with their evidence class. Deepen, replace, or consolidate same-responsibility owners until one owner remains and delete the competing surface; a `candidate` or `confirmed-unresolved` state left behind is unfinished work, not a passing verdict. `resolved` telemetry requires a parent-bound disposition record and complete scope; same-responsibility repairs additionally require the one-owner predicate.
- If the gate reports a `QG54-DUPLICATE-*` warning, it has named every region carrying that exact implementation. Keep one owner and delete the copies, or call the survivor. These rules are warning-only; a copy left behind is unfinished work, not a passing verdict.
- For owner-competition warnings, inspect the named regions' callers/callees with GitNexus MCP or local search before deciding; distinct authorities, real adapters, and genuinely different lifecycles are the legitimate negative cases the disposition contract records.
- Treat touched shallow modules as in-scope debt: absorb, delete, or record the blocker before finalizing.
- Do not finish the turn while duplicate added code, reimplemented existing helpers, unnecessary growth, fake-green suppressions, broad catch/pass, temp artifacts, or cleanup failures remain in the changed production surface.
- Treat the gate as changed-scope evidence, not as a substitute for the repo's own lint, typecheck, tests, build, and domain-specific quality gates.
- If a tracked governing plan or review artifact exists for the current work and includes an execution checklist, update it when execution state materially changes.
- Material changes include:
  - checklist progress
  - active branch or PR state
  - superseded or dropped items
  - changed execution order
  - remaining blockers or follow-ups
- Do not create busywork edits for every tiny code change, but do not leave the governing artifact stale after a meaningful implementation pass either.
- If the current work targets an existing PR branch, do not treat local changes as complete:
  - commit the changes
  - push the branch
  - only then resolve review threads as fixed
- Reconcile closure through the Minimum Implementation Decision. Compare the final diff against the preflight module shape; delete or inline shallow wrappers/helpers and verify tests cross the public interface.
- Run the repo's canonical install, lint, typecheck, unit, integration, build, and quality gates for touched areas before calling work complete.
- Keep changed code paths at or above the repo coverage gate.
- Add explicit tests for critical control loops even if coverage already passes.
- For bugs and regressions, compare the implementation to the canonical root-cause-first gate and the `/diagnose` trace.
- Do not mark work done while blockers, follow-ups, dead-letter gaps, retry gaps, or state-regression risks remain.
- Do not present PR remediation as complete while the fix exists only locally or while review threads were resolved ahead of the pushed fix.
- Closure notes must include: summary, commands run, key outcomes, test classes exercised, and blockers or follow-ups.

## Bundled Gate Policy

Load [references/gate-policy.md](references/gate-policy.md) when running or interpreting the bundled gate. The gate is non-mutating.
