# Production workflow map

The maintained boundary is workflow sequencing and completion. Git remains an
ordinary delivery tool.

```mermaid
flowchart LR
    B[begin] --> R[Repo Context Forge records the packet graph evidence]
    R --> D{bug or regression?}
    D -->|yes| DG[diagnose]
    D -->|no| A1[advisor preflight]
    DG --> A1
    A1 --> P[production preflight records the Behavior Map]
    P --> M{map has a pending item?}
    M -->|yes| TR[mapped contract RED, preservation items settled first]
    M -->|no: every item already-satisfied or omitted| NR[tdd --not-required]
    TR --> PC[production-code]
    NR --> PC
    PC --> I[implementation]
    I -->|every contract RED| TG[mapped GREEN]
    I -->|not-required map| V
    TG --> TM[map update when a proof exposes a new obligation]
    TM -->|new obligation| TR
    TM -->|map resolved| V[verification]
    V --> CR[code-review delegate review when non-trivial]
    CR --> A2[final Codex Advisor review]
    A2 --> C{context matched and effective findings terminal?}
    C -->|context mismatch| A2
    C -->|behavioral correction| TM2[tdd-map adds the item]
    TM2 --> TR
    C -->|non-behavioral correction| I
    C -->|evidence-backed rejection| AP[one appeal on the same session]
    AP --> C
    C -->|yes| WC[workflow complete]
    WC --> DL[delivery]
    DL --> PR[reviewer completion]
```

## State Interface

One repository-scoped SQLite event ledger records accepted transitions, logical evidence, review manifests, and complete canonical resulting state. A disposable projection names the active workflow and latest event; reads repair it from the ledger when it is missing, dangling, or stale. See [Workflow state root](../../README.md#workflow-state-root) for which root holds it.

```text
workflow begin                 # assigns and activates a random workflowId
workflow status|summary        # active canonical state
workflow history               # ordered accepted events and logical references
workflow evidence              # read one logical evidence record
workflow set-phase             # lead-owned implementation and trivial review
workflow record-preflight      # validates 13 text sections + Behavior Map
workflow record-production-code # validates the bundled gate verdict (optional; nothing waits on it)
workflow tdd                   # mapped RED/GREEN or records not-required
workflow tdd-map               # dispositions and post-GREEN map updates
workflow verify                # generic commands or typed final-tree quality gate
workflow record-review         # delegate review intake, lead dispositions, tree manifest
workflow advisor-result|advisor-disposition
workflow pause|checkpoint|complete|prune
```

A documentation-only pull request takes the CI job's cheap lane, decided by
`.github/scripts/pr_scope.py` from the pull-request delta and governed by step 9.

### `workflow status` contract

`workflow status` is a public JSON Interface, not a dump of persistence internals.
It returns the active canonical `schemaVersion: 1` projection. Callers may rely
on the semantic workflow fields: repository identity, `slug`, `workflowId`,
`phase`, `nextAction`, phase statuses, advisor/review records, logical evidence
identities, and the optional `paused` and `revalidation` state. The projection
never includes a database path, SQLite table or column name, journal detail, or
other storage mechanism. With no authoritative workflow it prints no JSON,
returns exit 2, names `no active workflow`, and creates no state.

Repo Context Forge, preflight, production-code, TDD, verification, review, and
addressed advisor dispositions record only with their native validated documents
as logical evidence, inserted in the same SQLite transaction as the accepted event; a
findings-none advisor disposition intentionally carries no document, and a
refusal names the missing evidence and mutates nothing. The preflight document
owns the initial Behavior Map; mapped TDD evidence carries its stable IDs,
RED/GREEN runs and current dispositions. A plan may show
the map but is not an evidence owner.

Exit 2 alone does not prove a refusal: the verification, TDD, and review
producers each document a path that commits first and returns 2 after — a
command that failed after being recorded, an invalid TDD run recorded as
`reopen` or `in-progress`, and a review whose material findings remain
unresolved. Repo Context Forge, preflight, production-code, and verification keep their
accepted reference only while producer-recorded as passed — every other transition drops
it, so a bare replay can never resurrect prior evidence. TDD and code review
instead keep a current producer reference across their own non-passed states —
TDD while in-progress and when not-required, code review while pending — so a
later run can validate or supersede it. Only TDD's in-progress reference serves
GREEN's validation of the RED it follows. TDD entry demands recorded preflight
evidence and, for new governed passes, a mapped behavior ID. Each producer stamps
the workflow instance into its evidence and the ledger keeps its logical
identity, so a passed Repo Context Forge, preflight, production-code, or
verification phase without one — legacy state, or a bare library claim — reads
pending at completion, never success. Evidence proves the output exists, not
that the analysis is good; fabrication remains deception and stays covered by
the transcript audit.

The database and its containing directory are private and agent-writable. Committed transactions provide continuity across process restart and compaction; it is not tamper-proof and does not authorize Git. A normal
commit or HEAD change does not invalidate it. The edit hook advises, never
refuses (hook table below). Every RED-phase run records the production paths
changed since the pass began, so a late RED or baseline is labelled in
`summary` and shown to the final review; nothing refuses on it. A `tdd-map`
update is needed only when a GREEN exposes a new obligation. A normally
completed workflow is terminal: every mutation except `begin` is rejected.

After a successful production edit in an active pass, the PostToolUse edit
hook runs one GitNexus `detect-changes` against the pass-start index and names
in its `additionalContext` the impacted tests the current map's recorded
selections do not own, or a short gap when that cannot be decided against this
pass's index. Complete ownership is silent, an identical result repeats neither
notice nor write, a completed or revalidating pass gets no scan, and the
advisory never changes the edit's outcome or the workflow state; a mapped GREEN
issues no second scan. It is advisory and incomplete by nature, and full-map
reconciliation stays the completeness authority. The installed
automatic advisory replaces the manual pre-commit detect-changes step.

A governance-document edit after completion is the sole controlled revalidation exception: it opens a window in
which only verification, code review, the final advisor review, and completion
are accepted, production editing stays closed, and completing again restores
the terminal state. The read-only `checkpoint` query reports consult
readiness for the advisor phases without mutating anything.

`complete` requires:

- Repo Context Forge completed, carrying its producer graph evidence;
- advisor preflight completed with findings dispositioned, or explicitly
  unavailable with a measured reason;
- production preflight completed with a non-empty Behavior Map;
- every contract map item GREEN, baseline `already-satisfied`, or `withdrawn`;
- every preservation map item GREEN, already satisfied, or omitted with evidence (the recorder validates the evidence structurally; its truth is a lead-owned obligation the reviews check) - a superseded item of either kind instead needs a GREEN terminal replacement - judged by `behavior_map` inside `complete()`'s transaction;
- no pending proof gap;
- TDD passed or not required;
- production-code recorded;
- implementation and verification passed;
- preflight, production-code, and verification each carrying their producer's
  evidence reference;
- code review recorded (delegate intake, or not required) with material findings addressed;
- a context-matched final review from `codex-advisor` whose effective findings
  are terminal: the immutable raw verdict remains evidence, but
  `fix-before-commit` is not a veto after closure;
- no pending final rejection appeal and no re-raised finding awaiting its second disposition;
- the reviewable working tree unchanged since the recorded lead review.

The historical `pass-state.py`, recorder, TDD, and verification scripts are temporary compatibility adapters. They call the same unified CLI Module and contain no persistence or path logic; new callers use `workflow.py` directly.

## Edit invalidation

```text
production Edit/Write/NotebookEdit
  -> implementation = in-progress
  -> verification = pending
  -> codeReview = pending
  -> finalReview = pending
  -> nextAction = implementation when a resolved map was touched,
                  otherwise implementation/correction
```

Invalidation occurs before quality feedback, so a failing quality check cannot
leave stale readiness behind. Ordinary documentation and scratch edits are
exempt; governance docs reset verification, code review, and final review, and
resume at the first unsatisfied phase in the same ordered workflow. A
governance-first pass therefore returns to TDD, while a completed
implementation returns to verification.

Behavioral findings from the `code-review` delegate or final Codex Advisor against the current unpushed tree return to mapped TDD under the active `workflowId`: add the Behavior Map item, drive its behavior-specific RED, then fix it. Only genuinely non-behavioral corrections return directly to implementation, with the reason recorded. The behavioral/non-behavioral classification is a lead-owned obligation, not a machine-validated edge: the recorder validates the reassessment's structure and blocks completion until one is recorded, but it cannot judge the classification itself - a behavioral defect routed through a why-only reassessment is a doctrine violation the reviews are expected to catch, not a state the hooks can refuse. A legitimate reviewer signal on a pushed PR head, or a bug/regression outside the active workflow intent, instead starts a new workflow with `begin`.

A finding envelope is one correction batch. A pending behavioral finding rides
the pass as a map-owned attack obligation; dispositions may cover any subset,
and later documents preserve append-only history. Until every material finding is dispositioned and the map is closed, the final
checkpoint and `complete` refuse; verification, the typed gate, and lead
review run regardless. Targeted TDD and direct changed-Seam
probes remain available. Final rejections use one context-matched appeal response:
omission or same-ID `material:false` concedes, a material re-raise reopens
the finding for one more lead disposition, which then stands, and new IDs form
their own immutable intake.

## Approval freshness

A file written through the shell emits no editor event, so nothing invalidates
mid-stream, and the pre-edit gate does not see that write either — an accepted
gap, because the failure model is drift rather than deception. Freshness is
recovered at the later gates instead. Recording the lead review stores a
per-path manifest of the reviewable surface: each path's working-tree file mode
and content hash, and for a tracked submodule the commit it currently points at.
The index is read only to learn which paths are tracked and which of them are
submodules; every recorded value comes from the working tree, never from an
index object id, which records staged content and would miss an unstaged edit.
Four things follow from that: the bytes are hashed unfiltered, so a normalising
clean filter cannot hide a line-ending rewrite; the mode rides along — git's owner
execute bit, the only one a tree entry records — because a content hash alone
is blind to `chmod`; a symlink is recorded as the link rather
than its referent, so re-pointing one is visible and a file outside the
repository can never drift the manifest; and a submodule is read from its own
checked-out `HEAD`, so an unstaged submodule move is visible. A submodule is
recorded by that commit alone: uncommitted content inside it belongs to that
repository's own review, not this one's. Each later gate recomputes it:

```text
final advisor recording refuses -> the tree changed after the lead review
final-review checkpoint reports not ready -> same, before a paid consult is spent
complete refuses -> the tree changed after the final review
```

The refusal site is the window attribution, and every refusal names the added,
changed, and removed paths. Re-recording the lead review refreshes the manifest
and resets the final review to pending, so a refreshed tree always costs a fresh
final consult. A missing or uncomputable manifest is pending, never success: a
pass in flight when this shipped cannot complete until its lead review is
re-recorded. A mutation racing the completion call itself stays uncatchable —
the state lock serializes state writers, not the filesystem.

## Hook roles

This section is the canonical operational documentation for hook behavior.
`~/.config/devin/config.json` and the hook scripts remain the executable Interface:
where they disagree with this table, the code is correct and the table is the
defect. `AGENTS.md` §9 keeps only the facts that change lead action each
session and defers the rest here.

| Hook | Role |
|---|---|
| `PreToolUse(Edit\|Write\|NotebookEdit)` | Advise, never refuse: name what the pass has not recorded and admit the edit; docs, scratch, and non-repository paths are silent; test-like paths skip only the RED advice |
| `PostToolUse(Edit\|Write\|NotebookEdit)` | Invalidate downstream readiness, record the session's repository association where a pass exists, then return quality feedback — the gate run carries the pass's recorded base OID as `--base-ref` when bootstrap recorded one, so growth warnings read branch-cumulative per edit; with no recorded base the hook derives nothing and the gate reports the base-binding gap |
| `SessionStart(compact\|resume)` | Restore the full workflow chain and bounded current summary from committed SQLite state |

The session association marker: `PostToolUse` records one immutable marker per repository
per session under `sessions/<session>/<repo-key>.json` in the state root,
written only where a workflow already exists, and identity comes from the edited
path through the same resolver the edit gate uses — no hook gains Git awareness,
and a storage failure only prints to stderr, never changing a hook's exit
status, its review invalidation, or the quality gate it runs. Those associations
replace the candidate set rather than extending it: the session `cwd` slot is
consulted only by a session that recorded no association at all, whose behaviour
is unchanged. A payload whose `session_id` is missing, null, not a string, empty,
or only whitespace belongs to no session: it is rejected before any key is
derived, so it records no association and reads none, and therefore keeps that
`cwd` fallback — the association key is never defaulted to a shared literal,
because every anonymous payload would then share one identity and one
repository's pass could reach another's Stop.

For an admitted non-blank string id the key is `safe_slug(session_id)[:40]`, and
that transform is lossy rather than injective. In order it trims surrounding
whitespace, replaces each run of characters outside `[A-Za-z0-9._-]` with a
single `-`, strips leading and trailing `-`, `.` and `_`, lowercases, caps at 80
characters, substitutes the literal `unnamed-workflow` when nothing survives, and
is then cut to 40. Distinct ids therefore **can** collide — by case, by any
character outside that allowed set (`.`, `_` and internal `-` are preserved), by
edge characters alone, beyond 40 characters, and, for non-blank ids whose whole
content is removed by that replacement and edge stripping, on the
`unnamed-workflow` literal itself. A blank id never reaches this transform at
all; it is refused by the admission check above.

Per-session isolation is thus a property of the ids this harness supplies, not a
guarantee of the key: they are lowercase hexadecimal UUIDs and so are fixed
points of the whole transform — measured across 644 recorded sessions, none
altered by it and none sharing a key. Any future id source must be injective
under the transform exactly as written above, or introduce a collision-resistant
encoding here before it is trusted. The repository-scoped per-session feedback file keeps its own `unknown` display name, which cannot cross
repositories.

A pass this session never edited in is not reported; the feedback path emits bounded context
containing changed-code status and the workflow summary per consulted slot, and
deduplicates identical rendered context per session and slot. When Git reports
no changed code and no workflow state exists, that path emits nothing. The
payload it reads was captured on Claude Code 2.1.220 and is kept as a test
fixture; the delegate release is the `CODEX_ADVISOR_ACTIVE` environment
variable.

There is no Bash command matcher, Git hook, command classifier, protected-path
parser, candidate-tree gate, approval marker, nonce, or evidence graph.

## Ordinary summaries

Repo Context Forge output, mapped TDD runs and map updates, and code-review
findings may be retained as bounded summaries for the next agent. They carry no
HEAD/tree/hash identity and are never substitutes for the real packet, recorded
Behavior Map, test command, or live review. The review-time manifest above is
workflow state rather than one of these summaries, and it identifies working-tree
file mode and content, plus each submodule's checked-out commit — never this
repository's own HEAD, and never an attestation.

## Delivery is separate

Workflow completion means the production process reached a final ready review.
Delivery follows only when integration is intended; the no-PR route (local-only
work, estate syncs, work the user said not to push) completes the workflow,
reports the change and its verification, and names why no PR exists.
Commit, push, PR creation, CI, reviewer comments, and mergeability remain normal
delivery/reviewer-loop concerns after that point.
