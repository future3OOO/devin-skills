---
name: production-preflight
description: Produce the required before-edit proof for production code changes — reuse path, chosen approach, touchpoints, verify vs update surfaces, module shape, risks, Behavior Map, and honest openQuestions. Use before writing code on implementation, refactor, bug-fix, or review-comment passes.
---

# Production Preflight

Use this skill before making tracked edits on preflight-required code turns.

## Core Doctrine

- Prefer root-cause fixes over band-aids.
- Follow the canonical review-comment doctrine in the repo's `CLAUDE.md` (or `AGENTS.md` if that is what the repo uses).
- Treat review comments as evidence to verify against current code and the repo contract, not authority to obey blindly.
- If you do not know, verify before editing.
- For behavior bugs, require the reproduced symptom, traced root cause, and testable hypothesis before editing; if any are missing, use `/diagnose`.
- No tracked edits before completed preflight.
- If the preflight finds unresolved blockers, stop and surface them honestly in `openQuestions`.
- If a tracked governing plan or review artifact exists for the current work and includes an execution checklist, anchor the preflight to that artifact instead of freehanding a new execution path.

## Governing Artifact Alignment

When the current work is governed by a tracked plan or review artifact under `docs/plans/` or `docs/reviews/`:

- name that artifact explicitly in the preflight
- stay inside the owner slice defined by that artifact
- use the artifact's PR order, scope, and verification as the starting execution boundary
- if the requested change no longer fits the governing artifact, refresh the artifact or block in `openQuestions` before editing

Do not use preflight to silently fork away from the governing execution document.

## Existing PR Checkout Rule

When the turn edits code on an already-open PR:

- treat the live GitHub PR head as authoritative
- verify the exact checkout path that will be edited, not some other review worktree
- record the PR number, branch name, checkout path, live PR head SHA, local `HEAD` SHA, and whether the checkout is branch-attached or detached
- if the checkout is stale, detached, or on the wrong SHA, fetch and realign it before the first tracked edit
- do not treat routine realignment as a blocker; only block if the checkout cannot actually be realigned
- do not commit from a stale detached review worktree

## Affected Surface Rule

Apply [Production Code’s Minimum Implementation Decision](../production-code/SKILL.md#minimum-implementation-decision) before edits. Record its affected guarantees, contracts, invariants and distinguishing operations in the preflight document and Behavior Map; preflight owns that initial record.

## Behavior Bug Root-Cause Gate

For behavior bugs, preflight proof must name:

- reproduced symptom: the exact failure observed
- traced root cause: the source trigger, not only the visible error
- testable hypothesis: why the proposed edit fixes the source
- source-level fix: why the edit is not merely a symptom guard

If any item is missing, use `/diagnose` before editing. If the trace crosses scattered shallow helpers/modules or no clean test seam exists, use `/improve-codebase-architecture` before forcing a bad test or broad patch.

## Module Shape Gate

When the change proposes a new production Module, public Seam, or change to a
public Interface, invoke `codebase-design` before completing this gate.

Before production edits, name the module shape:

- `publicInterface`: the caller-facing interface, CLI, IPC, UI flow, or module seam the proof crosses
- `testSurface`: the public behavior surface the test or smoke check exercises
- `moduleShape`: deepen existing module | create new module
- `reusePath`: existing module/path being extended
- `newModuleJustification`: required only when adding a new production module, public seam, wrapper, service, manager, or adapter
- `rejectedShallowPath`: shallow helper/wrapper/module split deliberately avoided

Prefer deepening an existing module. Apply Ousterhout's deep-module test: does this hide meaningful complexity behind a small, stable public interface, or create a shallow helper/wrapper split? A new module must earn its interface by hiding complexity, improving locality, or creating a real seam used by more than one caller, adapter, or test surface.

Touched shallow helpers/modules are in-scope debt: absorb, delete, or record a concrete blocker in the preflight.

Block if the public test surface cannot be named, or if a new module is proposed without a concrete reason existing modules cannot absorb the behavior.

## Affected Transaction System Rule

For transaction-sensitive work, load and apply the mandatory [canonical
transaction doctrine](../production-code/references/transaction-doctrine.md).
Preflight owns the before-edit map and must place any unnamed authoritative
record, mutation boundary, interleaving, shared projection/recovery path,
contract, invariant, or proof surface in blocking `openQuestions`.

## What To Produce

Produce a short preflight with these exact sections:

- `affectedSurface`
- `authoritativeContract`
- `invariants`
- `proofPlan`
- `reusePath`
- `chosenApproach`
- `rejectedAlternatives`
- `touchpoints`
- `verify`
- `update`
- `modularityPlan`
- `riskChecks`
- `openQuestions`
- `behaviorMap`

The first thirteen sections are concise text. `behaviorMap` is authoritative for the behavior obligations TDD must reconcile, not for choosing the architecture. A plan may reference the map but does not own another copy.

For ordinary local work, keep `affectedSurface`, `authoritativeContract`, `invariants`, and `proofPlan` short.
For transaction-sensitive work, these sections must be explicit enough to govern the full surrounding surface.

## Section Rules

### `affectedSurface`

- State the real changed boundary or behavior.
- Name the adjacent consumers, callers, and no-change surfaces that must remain correct.
- Do not reduce the surface to the edited file path.

### `authoritativeContract`

- State the rule that must remain true after the change.
- If more than one rule matters, list the small set that actually governs the surface.
- Do not hide the contract inside general prose about files or implementation shape.

### `invariants`

- List the observable conditions that prove the contract still holds.
- Include adjacent no-change expectations, not just the direct branch behavior.
- For transaction-sensitive work, include replay/recovery/projection and interleaving invariants when relevant.

### `proofPlan`

- Name the proof you will run for the affected surface.
- Include one combined workflow proof when the work is stateful or control-loop sensitive.
- Focused invariant checks may supplement the combined proof, not replace it.
- For each production-writing pass, name the targeted correctness operation that also measures/asserts the chosen resource limit. Run it through ordinary `workflow.py verify`; its output names scale, fixed limit, observed value, and actual target identity. Reuse its selected receipt, not a new benchmark suite or cost-only map item.

### `reusePath`

- Identify the existing code path, utility, module, or pattern to extend.
- If no safe reuse path exists, say that explicitly and explain why a new path is justified.
- Do not claim reuse without naming the actual files or components.

### `chosenApproach`

- State the intended implementation in direct terms, including a meaningful resource limit, scale, and command **before measurement**. Compare only alternatives satisfying the same Interface; before a real mechanism reversal, deepen the governing design with the measured reason and reassess affected guarantees.
- State each material implementation assumption and its evidence. An unresolved architecture-selection or contract question - one whose answer could change the chosen approach - moves to `openQuestions` and blocks recording until resolved. A settled choice whose behavioral consequence still needs falsification is not an open question: record it as a pending `behaviorMap` item and drive it through TDD.
- Explain why it is the shortest correct path.
- Keep the approach aligned with fail-closed behavior, boundary validation, and minimal diff size.
- If a governing plan or review artifact exists, state how this pass fits its current owner slice and checklist progression.
- Carry forward the governing design artifact's `PRES-n`/`ASSUMP-n` labels that constrain this pass, so the reconciled contract and later proof reference the same obligations.

### `rejectedAlternatives`

- List the realistic alternatives considered.
- Reject them with technical reasons, not taste.
- Prefer 1 to 3 rejected options, not a brainstorm dump.
- Name every architecture family this pass's exploration or planning produced. A family rejected on a falsifiable prediction about existing behavior, tests, compatibility, or runtime semantics carries the resolving real-Seam measurement, or the rejection is unresolved and belongs in `openQuestions` — the canonical imaginary-risk ban in the repo's `AGENTS.md` or `CLAUDE.md` governs; the `codex-advisor` skill owns the consult-time procedure.

### `touchpoints`

- Name the files, modules, tests, docs, scripts, and runtime surfaces likely to change.
- Include cross-boundary surfaces when the change affects contracts, persistence, auth, queues, or external integrations.
- If the change is intentionally narrow, say what you will not touch.
- If a governing artifact exists, include the tracked plan or review doc when this pass will materially change its checklist or execution state.

### `verify`

- List coupled surfaces that must be checked but should not change if the current implementation is correct.
- Include adjacent flows, invariants, and consumers that could regress even if untouched.
- Prefer explicit tests, fixtures, or commands when known.
- For all code work, include the no-change surfaces that would prove the change is not only locally correct.
- For transaction-sensitive work, include close/closing, replay/recovery, projection-only, stale secondary execution, and helper-sharing no-change surfaces as applicable.

### `update`

- List coupled surfaces that must be updated in the same change to keep the system coherent.
- Include tests, docs, schemas, decision records, and runtime references when the change affects them.
- If a reviewer comment would require an update that conflicts with the contract, block and surface it in `openQuestions`.
- If a governing plan or review artifact exists and this pass materially advances or reshapes execution, include that artifact here.
- If helper semantics differ between real mutation and projection/recovery paths, either split the helper or constrain its usage in the same change.

### `modularityPlan`

- State how the change stays small and production-grade.
- Include public interface, test surface, module shape, reuse path, rejected shallow path, and new-module justification when applicable.
- Call out file-growth risk, duplicate-path risk, and whether extraction is needed.
- Prefer extending an existing path over adding a new wrapper, helper, or abstraction.

### `riskChecks`

- Name the concrete failure modes to guard against, including violation of the declared resource bound. Do not relax that bound after a failure merely to pass; the same verification command must subsequently pass. Declaration adequacy remains review judgment, not universal automatic enforcement.
- Cover at least the relevant subset of: data integrity, cleanup, retries, auth, race conditions, cross-surface regressions, compatibility, and observability.
- If a risk cannot be evaluated yet, say so and move it to `openQuestions`.
- For all code work, include adjacent-surface regression risk, not just the direct edited branch.
- For transaction-sensitive work, include mutation-boundary drift, helper semantic drift, adjacent state races, and replay/finalize version drift.

### `openQuestions`

Use a three-way decision for every material unknown:

1. **Resolve from evidence.** Inspect the packet, repository, runtime,
   governing artifact, or verified source and record the answer.
2. **Interactive architecture interview.** When the unknown can change Module
   shape, public Interface, Seam placement, data contract, or irreversible
   scope, invoke `/grilling` and ask one question at a time.
3. **Block honestly.** If the fact cannot be resolved, the session is
   non-interactive, or safe implementation depends on it, keep the named
   question here and mark preflight blocked.

Do not pause for ceremonial approval after evidence has resolved the decision.

### `behaviorMap`

Record a non-empty JSON array. Every item has these eight required fields:

```json
[
  {
    "id": "BM_ATOMICITY",
    "kind": "preservation",
    "basis": "touched-Seam preservation",
    "behavior": "a caught inner failure remains atomic under the new transaction path",
    "seam": "the public operation through that path",
    "expected": "no partial inner write survives",
    "redFailure": "PARTIAL_INNER_WRITE_SURVIVED",
    "status": "pending"
  }
]
```

- IDs are stable uppercase identifiers used by RED/GREEN evidence.
- `kind` is `contract` for the requested behavior and `preservation` for what the change must keep true. List contract items first. A map with any pending item carries at least one contract item; `basis` is prose and carries no authority.
- `redFailure` names the product failure: a behavior-specific assertion marker or the product's own exception or diagnostic. A RED is valid only when the failure is that mapped product failure; failing earlier is evidence for no item. The first RED of a new Seam asserts the Seam's existence (`assert hasattr(db, "x"), MARKER`).
- A contract item starts `pending`. A preservation item starts `pending`, `already-satisfied`, or `omitted`; `evidence` is required for `already-satisfied` and `omitted`, and forbidden for `pending`.
- Every item is a concrete falsifier: an adversarial attack on one load-bearing public promise through its real production Seam. Derive attacks from what the design promises, not from a universal checklist: rollback/atomicity implies success, ordinary failure, supported interruption/cancellation, nested ownership, and every caller-reachable transaction-ending path; cleanup/resource ownership implies interruption and repeated or finalized lifecycle operations; persistence implies close/reopen and a second connection or process; parsers and matchers imply malformed boundaries plus the captured production corpus; shared mutable state implies every writer and material interleaving; lifecycle state machines imply repeated, out-of-order, nested, superseded, and terminal operations the Interface admits. If the Interface deliberately excludes an implication, narrow the promise explicitly instead of contradicting it.
- Map every category the tdd skill's [Record the Behavior Map in Preflight](../tdd/SKILL.md) section lists; read it before writing the map.
- Only runtime behavior is mappable: delivery line accounting, budget measurement, and other non-runtime bookkeeping never become items.
- A pending behavioral finding is owned by giving an attack item a finding entry in `sourceRefs`; the recorder refuses a map that leaves one unowned. No preservation-only item is needed when existing focused regression evidence already owns the obligation — reference that evidence in an `already-satisfied` disposition instead.
- Use TDD's one-item-per-independently-failing-outcome rule, including finding-owned attacks. Parameterized forms can share an operation; separate missing guarantees stay visible. Prose cannot widen the domain the retained attacks actually prove.
- Proof gaps stay in `openQuestions`; they are not omissions.

## Execution Gate

- Preflight must happen before the first tracked edit on the governed pass.
- Do not make tracked edits, stage files, or resolve review threads before preflight is complete.
- Do not treat a retrospective preflight summary as valid compliance.
- Do not pause for approval unless the user explicitly asked for approval or `openQuestions` contains a real blocker that prevents safe editing.
- If new facts invalidate the preflight after editing has started, stop, refresh the affected sections, and then continue from the corrected preflight.

## Recording

In the governed workflow this preflight records only through
`python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" record-preflight --repo "$PWD" --slug "<task>" --workflow-id "<active-workflowId>" --input "/path/to/preflight.json"`, which demands the full thirteen text sections plus `behaviorMap`
as JSON (every text section non-empty, `openQuestions` exactly `none`) and refuses
without mutating state. Write the document to a file and pass it with
`--input`; response prose is not evidence.

## Output Shape

Use the exact JSON section names above. For a visible summary, use the compact text shape - one bold-labelled line per section, in order:

```md
`affectedSurface`: ...
`authoritativeContract`: ...
`invariants`: ...
`proofPlan`: ...
`reusePath`: ...
`chosenApproach`: ...
`rejectedAlternatives`: ...
`touchpoints`: ...
`verify`: ...
`update`: ...
`modularityPlan`: ...
`riskChecks`: ...
`openQuestions`: none
`behaviorMap`: BM_... contract|preservation, pending | already-satisfied | omitted (with evidence)
```

If blocked, say so explicitly and keep the block reason inside `openQuestions`.
