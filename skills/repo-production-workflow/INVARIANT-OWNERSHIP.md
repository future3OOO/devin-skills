# Workflow ownership map

Single ownership prevents contract drift without hiding hard constraints from
an isolated delegate.

A contract belongs in this table only when it has demonstrated competing
ownership, observed drift, or cross-file consumption. This is not an index of
every ownership statement in the estate; an exhaustive registry would be a
second source of truth that drifts on its own.

| Contract | Canonical owner | Other consumers |
|---|---|---|
| Mock ban / real-Seam proof | `AGENTS.md` Hard Production Invariants | Local consequences and pointers only; one delegate copy in the advisor wrapper |
| Imaginary-risk rule | `AGENTS.md` Hard Production Invariants | Local consequences and one isolated-delegate copy |
| Root-cause-first | `AGENTS.md` Hard Production Invariants | `diagnose` owns the tracing procedure |
| GitNexus context/impact doctrine | `AGENTS.md` §9 | The workflow supplies packet-specific facts |
| Execution sequence and phase order | `repo-production-workflow/SKILL.md` | `AGENTS.md` §7 owns only when skills fire |
| Review ownership | `repo-production-workflow/SKILL.md` | `code-review` is the forked delegate's task; the lead records and dispositions; the final Codex Advisor review follows |
| Terminal state and the governance-revalidation exception | `WORKFLOW-MAP.md` | `workflow.py` exposes the operator-facing Interface; `hooks/lib/workflow_state.py` implements shared transitions consumed by the CLI and hooks; legacy scripts are compatibility shims |
| Public workflow status JSON | `WORKFLOW-MAP.md` | `workflow.py status` emits the canonical `schemaVersion: 1` projection; hooks and advisor automation consume semantic fields only |
| SQLite ledger schema and transaction mechanics | `hooks/lib/_workflow_db.py` | `workflow_state.py` supplies policy mutations; `state_prune.py` supplies estate retention decisions through the ledger's private inventory/apply Interface |
| Hook operational documentation | `WORKFLOW-MAP.md` | `config.json` and the hook scripts remain the executable Interface; `AGENTS.md` §9 keeps only what changes lead action |
| Repo Context Forge downstream contract | `repo-context-forge/SKILL.md` | `AGENTS.md` §8 owns the intake gate |
| Review-budget measurement | `delivery-governance/SKILL.md` | `AGENTS.md` §7 states the ~500 net-line target |
| Advisor transport | `codex-advisor/SKILL.md` | The workflow retains the stable slug, phase, and terminal-marker contract |
| Transaction doctrine | `production-code/references/transaction-doctrine.md` | Preflight, production-code, and planning skills point to it |
| Quality-review vocabulary | `code-quality/SKILL.md` | `production-code` extends it for implementation |
| Module shape rules | `production-preflight/SKILL.md` | Deep modules, reuse-before-new, shallow-helper debt; `codebase-design` owns the vocabulary |
| Module / Interface / Seam vocabulary | `codebase-design/SKILL.md` | Architecture and preflight skills consume it |

A markdown owner owns the canonical description of a contract, never its
executable behavior. Where a row names both, the code is authoritative and the
document must be corrected to match it.

Workflow state records continuity only. It owns no Git, tree, attestation,
approval, or security contract.
