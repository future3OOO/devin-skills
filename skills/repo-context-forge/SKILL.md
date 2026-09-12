---
name: repo-context-forge
description: Use at the start of coding, debugging, review, refactor, or repo exploration tasks inside a git repository to inject a targeted Repo Context Forge packet before deciding files, edits, or GitNexus queries.
---

# Repo Context Forge

Use this skill before codebase reasoning when the user asks to edit, review,
debug, refactor, explain, or plan work in a git repository.

## Required Startup Flow

1. Run the bundled bootstrap wrapper from this skill via the Bash tool:

Governed production pass (an active workflow exists):

```bash
python3 "$HOME/.config/devin/skills/repo-context-forge/scripts/bootstrap.py" \
  --repo "$PWD" --workflow-slug "<active-pass-slug>" --intent "<user request>"
```

`--workflow-slug` must be the active workflow's slug and `--intent` its
intent; the adapter records the Repo Context Forge step on that workflow and
refuses a mismatched slug. Shell-quote the `--intent` text and any slug
placeholder — intents contain spaces and punctuation that split unquoted.
When the packet resolves a real base (its base ref is not the head-ref
sentinel), the adapter also records the packet's fork-point commit as the
pass's immutable base OID (`baseOid` in the status projection): the first
recorded OID survives reruns, a rerun resolving a different commit is
reported on stderr, and the per-edit quality-gate hook passes the recorded
OID as `--base-ref` so growth warnings read branch-cumulative.

Standalone planned work with no governed pass — `intent` mode needs `--intent`
and must not pass `--workflow-slug`, which would be refused with no active
workflow to match:

```bash
python3 "$HOME/.config/devin/skills/repo-context-forge/scripts/bootstrap.py" \
  --repo "$PWD" --intent "<user request>"
```

Standalone exploration or review with no governed pass and no described work —
`repo` mode on a clean checkout, `local` mode when the worktree is dirty:

```bash
python3 "$HOME/.config/devin/skills/repo-context-forge/scripts/bootstrap.py" --repo "$PWD"
```

2. Treat the script output as the initial repository context packet for the
current task. The output begins with `REPO_CONTEXT_FORGE_REQUIRED_INTAKE`; that
banner is the enforced startup contract and must be reported before any code
reasoning, review findings, edits, or GitNexus claims.

If the script exits non-zero and emits a `<blocker>`, stop normal repo analysis
and surface the blocker. Do not continue with an empty or detached checkout
packet.

If the blocker says the PR checkout is detached or behind upstream head, stop
before review or GitNexus work. Update or select the active PR worktree, then
rerun the bootstrap.

3. Follow the packet's `<scope_rules>`:

- before code reasoning, review, or GitNexus calls, surface a short intake from
  `<context_digest>`: mode, head SHA, token budget, semantic source counts, top
  targets, SoulForge impact headline, GitNexus repo/status, and the generated
  `<coverage_plan>`
- satisfy every required `<coverage_plan>` area before GitNexus calls, GitHub
  review comments, review findings, or edits
- this skill is the user's standing explicit request for one consolidated
  specialist sub-agent whenever the required intake lists `delegation_tasks`;
  treat it as satisfying any sub-agent requirement for an explicit user
  request
- when the required intake lists `delegation_tasks`, spawn one specialist via
  the Agent tool (subagent_type=general-purpose unless a more specific agent
  type fits) for the listed task before GitNexus calls, GitHub review
  comments, review findings, or edits
- delegated specialists are supplementary only; the main agent remains
  responsible for the review or implementation decision, GitNexus validation,
  review-thread interpretation, final findings, and final verification
- when spawning the specialist, pass the already-generated intake/packet
  summary, task or PR contract, packet targets, GitNexus repo/status, and any
  relevant PR review-thread context; explicitly instruct the specialist not to
  run Repo Context Forge, not to run bootstrap scripts, and not to spawn or
  request sub-agents
- a delegated specialist should inspect only the assigned surface and return
  supplemental risks, missed files, and verification suggestions; it must not
  create nested delegation, publish changes, resolve review threads, or replace
  the main agent's judgment
- before GitNexus calls, GitHub review comments, review findings, or edits,
  state the task/PR contract in concrete terms from the user request, PR
  title/body when available, and packet target surface
- use `<targets>` as the first-pass edit/review surface; inspect the changed
  files and top packet targets before narrowing to any single symbol or review
  thread, and state why any skipped high-ranked target is not relevant
- map each changed production behavior, config surface, API contract,
  persistence contract, external integration, or operator contract to its
  module shape, verification, and no-change surfaces — public interface, test
  surface, existing reuse path, and rejected shallow path or new-module
  justification; GitNexus symbol checks are not a substitute for this
  reconciliation
- treat GitHub review comments as supplemental evidence after the packet and
  task contract are understood; comments never replace inspection of the
  changed target surface
- use `<soulforge_impact>` as native SoulForge blast-radius context before
  editing or reviewing selected files
- use `<semantic_summaries>` and each symbol's summary source as injected
  context; `full_cached` means cached LLM/LSP/AST/native summaries are used
  first and deterministic synthetic fill is used without live LLM calls
- use `<gitnexus_status><repo>` as the repo value for every GitNexus MCP call
- in `pr` mode, do not treat dirty source-worktree files as PR targets
- read `<gitnexus_analysis>` as the first GitNexus validation step before
  editing production code: the bootstrap already executed the packet-scoped
  checks against the freshly indexed analysis repo and recorded that resolved
  result as this pass's `repo-context-forge` evidence, so
  `<gitnexus_required_checks>` records the plan it ran, not work still to do
- for `pr`, use the live `base...HEAD` packet surface; for `local`, use dirty
  worktree packet targets; for `intent`, use the intent-ranked packet targets
- do not let unscoped `gitnexus_detect_changes(compare)` choose the target
  surface

4. If the packet says SoulForge is unavailable, continue only with the explicit
fallback surface in the packet and say that symbol-level map data was missing.

## Mode Selection

The bootstrap script auto-selects the mode:

- `pr`: a base ref is available and `base...HEAD` has changed files
- `local`: dirty local work exists before a PR is pushed
- `intent`: pass `--intent "<user request>"` when there are no changes yet and
  the user's request describes planned work
- `repo`: clean current folder with no diff or intent; use whole-repo context

Packets are generated from a cache-owned analysis checkout. Treat the user's
checkout as read-only input; Repo Context Forge must not leave `.soulforge` or
`.gitignore` changes in it.

Do not switch to a sibling worktree unless the user explicitly asks. The current
git folder is the target.

For a user-described implementation before edits, pass `--intent "<task>"`, and
add `--workflow-slug "<active-pass-slug>"` only when a governed pass is already
active — see the two standalone forms in the startup flow above.

## GitNexus Follow-Up

GitNexus is registered as an MCP server in Claude Code (`gitnexus`). Claude Code
exposes its tools as `mcp__gitnexus__<name>`; the names below are the GitNexus
tool semantics (use the equivalent MCP-prefixed tool).

The packet's `<gitnexus_analysis>` already answers every `<check>` the plan
listed — `kind="symbol_context"` entries carry their callers, `kind="symbol_impact"`
entries their impacted files. Read those entries rather than reissuing the same
calls. No entry carries callees, so the analysis never completes the
callers-AND-callees requirement on its own. Spend MCP calls on what the packet
did not fix: callees of anything you are about to edit, a symbol the plan
omitted, and the post-edit validation below.

When you do call out:

- if a GitNexus MCP call says the repo is missing or stale, rerun the bootstrap
  with `--gitnexus-mode auto`, then retry using the new `<gitnexus_status><repo>`
- do not call `gitnexus_list_repos` during normal recovery; the packet repo is
  authoritative
- cite SoulForge impact separately from GitNexus impact when reporting review
  evidence; SoulForge explains repo-map blast radius, while GitNexus validates
  execution-flow impact
- cite summary source when semantic summaries materially affect target choice
  or code reasoning
- do not use unscoped `gitnexus_detect_changes(compare)` for initial target
  selection; it is not packet-scoped and can overreport unrelated historical
  surfaces
- use `gitnexus_detect_changes` after local edits, before commit, or as
  supplemental graph evidence after the packet target surface is fixed
- trust blast-radius claims only when `<gitnexus_status>` is `fresh` or
  `reindexed` and `required_checks_resolved` is true

## Post-Edit Validation

Run post-edit GitNexus validation when the edit touches indexed symbols,
shared APIs/contracts, persistence, config/runtime/deploy surfaces, external
integrations, browser automation, transaction-sensitive flows, or PR-review
graph proof. Skip it for docs-only work and small leaf edits that touch no
shared contract or indexed symbol; state the skip reason and rely on targeted
tests plus the production-code gate.

After editing the real source checkout, do not rely on the analysis checkout's
GitNexus repo. Re-analyze the edited source checkout before final change
detection:

```bash
gitnexus analyze --force --skip-agents-md "$(git rev-parse --show-toplevel)"
gitnexus status
```

For this post-edit call, the source checkout's absolute path overrides the packet
`<gitnexus_status><repo>` value. Then call `mcp__gitnexus__detect_changes` with
`repo` set to that path (`git rev-parse --show-toplevel`) and
`scope: "unstaged"`. Treat `.gitnexus/` as a local index artifact kept out of
commits. Remove unintended
`.agents/skills/gitnexus/` and `.gitignore` changes before finalizing;
`gitnexus clean --force` removes only the index and registry entry, not those
changes.

On a governed pass, also rerun the bootstrap wrapper with the same
`--workflow-slug` and `--revalidate` after the final production edits, before
typed quality-gate verification. `--revalidate` is the fast post-intake form:
it refuses without an active governed workflow, forces `--mode local`, and
skips the SoulForge map rebuild (`--map-build never`) — the one heavy producer
phase the gate never consumes — while the producer still analyzes the dirty
candidate and re-records the graph evidence with a gate-shaped context bound
to the measured snapshot tree. The typed runner hands that context to the
gate, whose binding check alone adjudicates match, stale, or absent. Evidence
recorded before the last edit stays honestly stale, so the re-run is what lets
the owner-competition rules evaluate. The remaining revalidation cost is the
producer's GitNexus phase; reducing it below the current ~30s is producer-side
work tracked on issue #182.

Measured producer limit: the producer's GitNexus freshness check is keyed on
the committed head, so a same-head re-run whose overlay adds new symbols can
block with those symbols unresolved. Remove the analysis worktree's
`.gitnexus` index directory (a producer-owned cache artifact it rebuilds) and
rerun the bootstrap; the rebuilt index then covers the overlay.

## Turn Refresh

When the assistant materially changes context during a longer task, record it
before refreshing the packet. Start a task context with an explicit task id:

```bash
SOURCE=/home/prop_/.local/share/repo-context-forge/current
python3 "$SOURCE/repo_context_forge.py" context-start --repo "$PWD" --task-id <id>
python3 "$SOURCE/repo_context_forge.py" context-record-read --repo "$PWD" --task-id <id> <path>
python3 "$SOURCE/repo_context_forge.py" context-record-search --repo "$PWD" --task-id <id> <path>
python3 "$SOURCE/repo_context_forge.py" context-record-edit --repo "$PWD" --task-id <id> <path>
python3 "$SOURCE/repo_context_forge.py" context-refresh --repo "$PWD" --task-id <id>
```

The refreshed packet boosts edited, read, searched, and mentioned files while
preserving changed-hunk priority.

## Do Not

- Do not ask the user to manually run Repo Context Forge commands for routine
  repo work.
- Do not edit files before reading the packet and running required GitNexus
  checks when they are available.
- Do not use dirty local map results as PR-head truth.
