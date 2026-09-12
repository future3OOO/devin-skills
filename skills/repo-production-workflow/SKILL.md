---
name: repo-production-workflow
description: Orchestrate production repository changes from context through final review, workflow completion, delivery, and reviewer completion. State is continuity only and never authorizes Git.
---

# Repo production workflow

Use this skill for production code, configuration, runtime, deploy, generated
source, and behavior-changing repository work. `CLAUDE.md` owns the hard
invariants and GitNexus doctrine; [INVARIANT-OWNERSHIP.md](INVARIANT-OWNERSHIP.md)
maps the remaining owners.

## One stable workflow

Choose one short slug for the whole pass and begin state before bootstrap:

```bash
printf '%s' "$request_text" | python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" begin \
  --repo "$PWD" --slug "<task>" --intent -
# or, when the caller already has the request in a file:
#   ... begin --repo "$PWD" --slug "<task>" --intent-file "<path>"
```

Pass the request text, not a summary: build `$request_text` in a file from the
message, append the verbatim body of any issue or spec it names, and feed that
file — shell quoting mangles a long request passed inline. The recorded intent
is the contract the rest of the pass is answerable to, so it is stored exactly as
given (valid UTF-8; U+0000 refused) and read back at the plan-commit gate and in
every advisor consult; a paraphrase written here is the paraphrase those steps
will enforce. `--intent "<text>"` still takes a literal argument, and
`--intent`/`--intent-file` are mutually exclusive.

The repository-scoped SQLite event ledger remembers accepted transitions, logical evidence, phase, and next action across process restarts. Its disposable active projection is repaired from that history. It is agent-writable workflow continuity, not an attestation, approval, audit credential, or Git boundary.

`workflow.py status` is the public `schemaVersion: 1` JSON projection consumed by
hooks and advisor automation. It exposes semantic workflow facts and logical
evidence identities only; database paths, table names, journals, and other
storage mechanics are private. Missing authoritative state returns exit 2 with
`no active workflow` and creates nothing.

## Mandatory order

### 1. Repo Context Forge

Invoke `repo-context-forge`, then run its adapter with the same slug and intent:

```bash
python3 "$HOME/.config/devin/skills/repo-context-forge/scripts/bootstrap.py" \
  --repo "$PWD" --workflow-slug "<task>" --intent "<user request>"
```

Stop on packet blockers. The packet fixes the initial target and coverage
surface. When the packet resolves a real base, the adapter also records its
fork-point commit as the pass's immutable base OID (`baseOid` in the status
projection); the per-edit gate hook passes it as `--base-ref` so growth reads
branch-cumulative throughout implementation.

### 2. Task contract and diagnosis

State the changed behavior, authority, packet targets, skipped targets,
no-change surfaces, and review-budget fit. Invoke `diagnose` for bugs,
regressions, flaky failures, or performance problems before any fix.

### 3. Packet-scoped GitNexus

Repo Context Forge executes the packet's required context/impact checks and its
adapter records that resolved graph result as `repo-context-forge` evidence, in
the same transaction as the step. There is no separate transition to record, and
`set-phase --phase gitnexus` refuses as an obsolete step. Read the packet's graph
result; run further MCP checks when they widen the surface the packet fixed.

### 4. Advisor scope check

Invoke `codex-advisor` with phase `preflight-advice` through its sole wrapper,
preferably in a dedicated chat pane. It attaches the recorded graph evidence
itself. Supply the contract, packet, intended proof, and no-change surfaces. Invoke `codebase-design` first
when adding/changing a Module, public Interface, or Seam.

The consult is optional: skip it when the request raises no design or scope
question. The wrapper emits the completed answer, then records it; an intake
with no material finding is closed at recording and needs no disposition.
A material behavioral finding rides the pass as a map-owned attack and is
dispositioned once that attack is GREEN; a nonbehavioral or measured-false
finding is dispositioned whenever its measurement exists. Findings block
completion, never an edit, a verification run, or a review:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  advisor-disposition --repo "$PWD" --slug "<task>" --workflow-id "<active-workflowId>" --stage preflight --findings addressed --input <document>
```

The active `workflowId` comes from `workflow.py status`. A disposition is
bound to that instance and cannot create or alter immutable advisor intake.
For strict findings, `--findings addressed --input <document>` carries current
workflow/candidate context, intake identity, and measured dispositions at either stage.
A material behavioral finding needs no disposition to proceed: leave it pending
and it rides the pass as a direct attack obligation — `record-preflight` refuses
a map that does not own it through a finding `sourceRefs` attack item, and
`tdd-map` adds owners later in the same pass. `fixed` for a behavioral finding
requires an owning attack GREEN through its recorded RED plus a zero-count
complete-domain occurrence over the finding's recorded surface; a narrowed
Interface or a measured false premise is recorded as `rejected-with-evidence`.
`report-only` requires false material consequence. The legacy inline form
remains compatible for measured nonbehavioral results. Refusal mutates nothing.
An unavailable consult requires `--reason` with the measured transport failure
and needs no disposition.

### 5. Production preflight

Invoke `production-preflight` before tracked production edits. Anchor it to the
packet, graph, advisor findings, and governing artifact. Resolve, interview, or
block on every material unknown. For transaction-sensitive work, load the
[transaction doctrine](../production-code/references/transaction-doctrine.md).

The recorded preflight owns the initial Behavior Map; read the tdd skill's [Record the Behavior Map in Preflight](../tdd/SKILL.md) section before writing it. It is authoritative for proof obligations, not architecture selection; a plan may reference it but is not a second proof owner.

Record a completed preflight only through its recorder, which demands the
skill's structured document (thirteen non-empty text sections plus a non-empty
`behaviorMap`, with `openQuestions` exactly `none`) and refuses without mutating state:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  record-preflight --repo "$PWD" --slug "<task>" \
  --workflow-id "<active-workflowId>" --input <preflight.json>
```

### 6. Mapped TDD RED or not-required

For behavior changes invoke `tdd` and select one pending Behavior Map ID. The RED is an attack vector test through the item's recorded real Seam that fails with that item's declared `redFailure` - an assertion marker or the product's own exception or diagnostic. A missing API/import, setup, syntax, fixture, or collection failure is not RED for a later product behavior and does not unlock production edits.

The recorder's acceptance and refusal rules for runner-backed and non-runner attacks are owned by the tdd skill's [recorder.md](../tdd/recorder.md).

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" tdd \
  --repo "$PWD" --slug "<task>" --phase red --behavior-id "BM_..." \
  -- <targeted-command>
```

In this governed workflow the public TDD producers are required; `set-phase` does not accept the `tdd` phase. They keep bounded evidence and advance state but are not proof by themselves. For genuinely non-behavioral work, `--not-required` is available only after every map item is already satisfied or omitted by governing evidence:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  tdd --repo "$PWD" --slug "<task>" \
  --not-required "<specific non-behavioral reason>"
```

The edit hook advises, never refuses; `WORKFLOW-MAP.md` owns its role. A RED or baseline taken after production changed is late: labelled in `summary` and the final review, never refused at `complete`. A refactor that changes behavior adds its item with `tdd-map` and proves it. Current unresolved obligations block closure. Reference-only updates and successful or positively identified nonexecuting rechecks on an unchanged candidate preserve completed downstream checks; genuine regressions and ambiguous failures invalidate them. Cycle count remains a coarse granularity smell, never a coverage target.

### 7. Production code

Invoke `production-code` with the Skill tool and run its bundled gate over the
pre-implementation tree; the verdict is the lead's baseline and nothing waits
on a recording of it:

```bash
python3 "$HOME/.config/devin/skills/production-code/scripts/code_quality_gate.py" \
  check --repo "$PWD" --json > gate.json
```

This run passes no base ref on purpose: it proves
the pre-implementation tree is a clean baseline (worktree against `HEAD` — no
branch delta yet), so its cumulative-growth claim is intentionally incomplete.
Branch-cumulative growth against the review budget is measured per edit by the
PostToolUse gate hook using the base OID recorded at bootstrap, and again at
typed verification. Begin
production, configuration, and runtime implementation edits only once both TDD
and production-code are ready. The `production-code` skill owns the standards
themselves; this step owns only its place in the order. This bare baseline run
carries no graph evidence, so the `QG54-OWNER-COMPETITION-*` rules report their
incomplete gap here by design; their evidenced evaluation happens at the typed
verification run in step 9.

### 8. Implementation

Implement the smallest direct change and remove obsolete code created by the
change. PostToolUse marks implementation in-progress and resets downstream
readiness after every production edit; governance edits reset the downstream
review steps without reopening production editing.

After the smallest production edit, run GREEN on the same mapped surface:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" tdd \
  --repo "$PWD" --slug "<task>" --phase green --behavior-id "BM_..." \
  -- <same test surface>
```

Use Production Code's **Minimum Implementation Decision** for repair completion and TDD's [map-update and reassessment rules](../tdd/recorder.md). Batch affected preservation and additive finding ownership in the existing call:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  tdd-map --repo "$PWD" --slug "<task>" --workflow-id "<active-workflowId>" --input - <<'JSON'
{"reassessment":"Affected preservation and retained attack ownership","dispositions":[{"id":"BM_KEEP","revalidate":true,"evidence":"Changed shared decision"},{"id":"BM_ATTACK","sourceRefs":[{"type":"finding","evidenceId":"<actual intake>","id":"SPEC-1"}]}]}
JSON
# or, when the caller already has the document in a file: --input <path>
```

Terminal TDD proof opens verification directly; no implementation acknowledgement is recorded. Metadata-only reassessment is not another downstream review chain.

### 9. Verification

Run locally the changed-behavior RED/GREEN, the affected suites, preservation and
no-change checks, lint/typecheck/build, cleanup, the typed gate, and GitNexus
reanalysis/detect-changes when required. CI's `contracts` job owns the full runner here and step 13 waits for it; other repositories run it locally unless their CI supplies that coverage. Verification records only through the unified CLI runner, which executes the command it records and derives status
per-command-latest — any distinct command whose latest run failed keeps
verification pending until that same command reruns green, overlapping runs
record in completion order without rerunning, and a run whose reviewable tree
changed between its start and its commit is retained invalid naming the
drifted paths:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  verify --repo "$PWD" --slug "<task>" -- <verification command>
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  verify --repo "$PWD" --slug "<task>" --kind quality-gate --base-ref "<base>"
```

Use preflight's selected resource/correctness operation in the ordinary verification call. Reuse the returned evidence ID and operation output; a generic receipt does not retain a candidate-tree ID. Which commands suffice remains review judgment. Completion additionally requires the typed `quality-gate` run over the current reviewable tree.

Before the typed run, rerun the Repo Context Forge bootstrap with the same slug
so the recorded graph evidence is snapshot-bound to the edited candidate tree.
The typed runner reads that recorded evidence and hands its gate-shaped context
to the gate's `--gitnexus-context-json` input; the gate's own binding check
adjudicates match, stale, or absent. Without the post-edit re-run — or after any
further edit — the `QG54-OWNER-COMPETITION-*` rules honestly report the stale or
absent gap instead of evaluating.

### 10. Delegate code review

For a non-trivial change invoke `code-review` for a fresh, general-purpose
background delegate in this checkout. Wait without editing the candidate. It returns a
Standards/Spec review and a findings intake. Verify every finding and
disposition each one. A disposition is invalid
without its measurement; advisor agreement is not authorization; historical behavior
is contextual evidence only — a current Interface claim needs current documentation,
callers, tests, or another active authority. In this governed workflow `workflow.py record-review` is the required producer for non-trivial review state (`set-phase` cannot record a passed review); outside the governed
workflow it stays optional. For a genuinely trivial change, record
`set-phase --phase code-review --status not-required --findings none`.

Before recording, match checkout/workflow/tree against dispatch and
`workflow.py status`. Verify agent identity from `subagents/agent-<id>.meta.json`
and its forked-skill marker under `~/.local/share/devin/cli`; match model and effort
from harness receipts to the loaded `code-review` frontmatter. Missing or
mismatched evidence blocks recording: report it. Record
immutable intake first as `{"findings":[...]}` through the unified Interface. If it contains findings,
capture the returned `summaryId`, then call
the same command with `{"context":{"workflowId":"...","candidateTree":"...","prHead":"..."},"intakeEvidenceId":"<summaryId>","dispositions":[...]}`;
each disposition carries `kind`, `premise`, `occurrence`, and
`materialConsequence`. A document carrying both forms refuses. Print the
canonical disposition shape table, generated from its installed validator
declarations, with `python3 -I -c 'import sys; from pathlib import Path; sys.path.insert(0, str(Path.home() / ".config" / "devin")); from hooks.lib.workflow_documents import DOCUMENT_SHAPE_TABLE; print(DOCUMENT_SHAPE_TABLE)'`;
the `codex-advisor` skill's disposition section owns the recorder's other
refusals (temporary-directory paths, behavioral `report-only` without a proved
owning attack).

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  record-review --repo "$PWD" --slug "<task>" --workflow-id "<active-workflowId>" \
  --resolved-model "<model>" --review-context-id "<agent-id>" --input <review.json>
```

A no-finding intake binds the reviewed tree and passes immediately. A finding
intake stays pending until its appended dispositions resolve every material
finding. Dispositions may cover any subset of an intake; every material finding still
needs a terminal disposition before completion; a `material:false` note needs none. Verification, the typed gate, and a new review all run while findings
are open; open findings block completion only. A false premise records normalized `result`
exactly `false`; otherwise
rejection requires zero occurrence on a complete domain. `report-only` resolves
completion without authorizing an edit and cannot later become `fixed`. A
behavioral finding is fixed by owning it: add the attack item with its finding
`sourceRefs` through `tdd-map`, drive RED/GREEN, then record
`fixed` with the zero-count complete-domain occurrence; nonbehavioral
corrections record their current-tree evidence directly. A later map update
that would leave a fixed finding without its owning attack refuses.

### 11. Final Codex Advisor review

Before routine consult, reconcile known material sibling obligations and affected preservation through the existing correction blockers. In the existing final-consult question, quote only the selected resource receipt: evidence ID, command, scale, fixed limit, observed value, and the operation's actual target identity. Reuse returned evidence; read one document only if needed, not verification history. Known missing material acceptance belongs in a Spec finding.

The final Codex Advisor review judges the candidate, the delegate review, and
the lead's dispositions. Invoke it against the live diff with wrapper
phase `final-review`, the same slug, and the base ref. It re-derives the attack
surface before checking declared evidence: what the recorded original request
and public Interface promise, which operations can falsify each promise, which
of those are unattacked through the real Seam, and whether any disposition
narrowed its finding's domain — only then implementation detail and declared-map
closure. A promised load-bearing surface with no attack forbids `commit-ready`
even when every declared item is green. Address and disposition material findings. The
wrapper leaves final findings pending; the lead explicitly records `none` or
`addressed` only after validating the output. Any production edit repeats
verification, code review where required, and final review.

### 12. Complete the workflow

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" \
  complete --repo "$PWD"
```

`complete` refuses, from inside its transaction, unless every contract item is GREEN, baseline `already-satisfied`, or `withdrawn`, every preservation item is GREEN or validly dispositioned — a superseded item of either kind instead needs a GREEN terminal replacement — no proof gap remains, required phases are ready, material code-review findings are dispositioned, and the context-matched final `codex-advisor` intake has only effective terminal findings. The immutable raw verdict remains evidence but is not an indefinite veto after closure; `context-mismatch` or a pending one-response rejection appeal still blocks; a material re-raise reopens the finding as pending until the lead dispositions it once more against the new measurement; that second measured disposition stands. The reviewable working tree must match the manifest recorded by the lead review, and every evidence phase must carry its producer's evidence reference — a passed phase without one is a bare claim and reads pending, including legacy in-flight state at upgrade time. It changes workflow state only. It does not inspect, intercept, authorize, or execute Git.

### 13. Delivery and reviewer completion

Commit, push, and open/update the PR when intended for integration. For changed
paths mapped into the live estate: **install, motherfucker.** Follow the README's
scoped install contract and record the branch, commit, and path set. Then run the PR
Reviewer Completion Gate from `CLAUDE.md` on the current head. A reviewer-fix
round begins a new production pass; pushing is not completion.

When the completed work is intentionally not delivered as a PR — local-only
config, an estate sync, or work the user told you not to push — the no-PR
route is: complete the workflow, report the change and its verification in the
final response, and name why no PR exists. The completed state then simply
remains until the next `begin` replaces it; no reviewer gate applies.

## Compatibility shims

`pass-state.py`, `verify-run.py`, `tdd-run.py`, and the phase recorder scripts are temporary migration shims. They delegate to the same workflow CLI implementation and own no persistence, evidence-path, or policy behavior. New callers and documentation use `workflow.py`; the shims are retired after the installed estate has completed one verified migration cycle.

## Failure semantics

Missing or corrupt workflow state is pending, never success. Preflight advisor
transport may be recorded `unavailable` only with the measured reason; final
review has no unavailable exception. Ordinary documentation, scratch, and
non-repository work keeps the lightweight exception; governance docs still
reset downstream review readiness. There is no Stop hook; `workflow.py summary` reports the earned proof
(`Contract green=n/m`) and the next action on demand.
[WORKFLOW-MAP.md](WORKFLOW-MAP.md) owns the hook roles. Unavailable blast-radius impact is reported as `unknown`.

## Final response

Report Behavior Map dispositions, behavior-specific RED/GREEN proof, map updates, verification, review findings and dispositions, both advisor outcomes, workflow completion, reviewer-loop state, and any explicitly unverified surface. Never describe state summaries as proof, authorization, or tamper-resistant evidence.
