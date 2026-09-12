# Governing Design Template

Copy the fenced design body to the repository's workflow-state `designs/` directory, not into the Git checkout. Resolve the path with `workflow.py paths --repo "$PWD" --workflow-id <id>` — do not compute the state root by hand. Remove unused optional sections.

````md
# <Title>

## Design Identity

- workflow ID:
- workflow slug:
- design path: `<workflow-state-root>/<repo-key>/designs/<workflowId>.md`
- created:
- deepening rule: append-only corrections in the same unpushed workflow; the ledger keeps every prior declaration

## Objective

- primary goal:
- success condition:

## Source Of Truth

- authority:
- trusted base:
- linked evidence:

## Chosen Architecture

- selected family:
- rationale:
- Module / Interface / Seam:

## Explored Architecture Families

| Family | Selected / Rejected | Technical Reason | Measurement |
|---|---|---|---|
| ... | ... | ... | ... |

## Verified Constraints

- finding:
- measurement:
- design consequence:

## Preservation Obligations

- <observable behavior that must remain true>

## Load-Bearing Assumptions

- <falsifiable load-bearing assumption and its real-Seam measurement>

## Affected Surface

- changed behavior:
- adjacent consumers/callers:
- no-change surfaces:

## Affected Transaction System

Use only for transaction-sensitive work. Apply the installed `production-code/references/transaction-doctrine.md`; these fields record its result rather than redefining it.

- authoritative records:
- mutation boundary:
- adjacent interleavings:
- projection paths:
- replay paths:
- recovery paths:
- stale-secondary paths:
- no-op paths:
- helper semantic splits:

## Contract And Proof Model

- authoritativeContract:
- invariants:
- proofPlan:

## Scope In

- ...

## Scope Out

- ...

## Authority And Conflict Rule

- authorities:
- escalation path:

## Delivery Map

- plan type:
- PR count:
- stack depth:
- regroup rule:

## PR Plan

| PR | Branch | Base | Owner Slice | Commit Structure | Verification | Entry | Exit |
|---|---|---|---|---|---|---|---|
| A | `...` | `main` | ... | ... | ... | ... | ... |

## Verification Plan

- targeted tests:
- combined workflow proof:
- focused invariant checks:
- full gate:
- post-merge checks:

## Execution Order

1. ...
2. ...

Pass lifecycle, evidence, blockers, and findings live in repository-scoped workflow history. Commit, push, review, and merge status live in GitHub PR state when applicable. Tasks are session-local convenience only, never durable authority.

## Linked Evidence

- ...

````
