# Global Rules

Global guidance for every Devin session. Project-specific `AGENTS.md` or `CLAUDE.md` files override these where they conflict.

## 1. Think Before Coding

Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop, name what's confusing, and ask.

## 2. Simplicity First

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- Apply the canonical imaginary-risk ban below; do not turn an unobserved failure mode into code.
- If you write 200 lines and it could be 50, rewrite it.
- Ask yourself: "would a senior engineer say this is overcomplicated?" If yes, simplify.

## 2A. Hard Production Invariants

These are the canonical lead-context statements. Skills may point here and add
procedure, but may not weaken or duplicate them. The isolated advisor delegate
receives one necessary copy because it does not inherit this context.

<!-- HARD_INVARIANT_REAL_SEAM -->
- **Mock ban.** Tests, smokes, and verification must cross the real production Interface/Seam. A mock, stub, fake, fixture-substituted collaborator, invented gateway, or test-only adapter is never RED/GREEN or production proof. A capture at a Module's own outgoing process boundary is the real Seam for assertions about what that Module emits; the ban targets substituted collaborators inside the asserted contract. If the real seam cannot be driven, report the proof gap instead of manufacturing green evidence.
<!-- HARD_INVARIANT_DEMONSTRATED_RISK -->
- **Imaginary-risk ban.** An undemonstrated theoretical failure may be reported, but it cannot justify guards, fallbacks, retries, configuration, abstractions, or code. Require a verified mechanism and demonstrated occurrence before changing production behavior. A real-Seam reproduction of behavior admitted by the supported Interface is occurrence; caller enumeration proves absence only on a closed, complete execution surface.
<!-- HARD_INVARIANT_CONTRADICTORY_CONTRACT -->
- **Contradictory-contract gate.** An Interface that promises to accept arbitrary caller behavior cannot also require callers to avoid particular operations. When shipping needs a "do not call X while Y is active" caveat, that caveat is the defect: narrow the promise or redesign — no occurrence count is required.
<!-- HARD_INVARIANT_ROOT_CAUSE -->
- **Root-cause-first gate.** For a bug, regression, flaky failure, or performance regression, no production fix begins until the symptom is reproduced, the source trigger is traced, and the proposed cause is stated as a falsifiable hypothesis. Fix the source, not the visible symptom.

## 3. Surgical Changes

Touch only what you must. Clean up only your own mess.

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that *your* changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the user's request.

Smallest change means the smallest final diff, not the smallest tool call:
prepare coherent multi-hunk edits per file, and batch independent edits to
different files in one message. A run of consecutive single-line edits to the
same file is the smell this rule exists to prevent.

Cleanup loop, every pass:

- Before handoff, inspect the delta and remove bloat, duplication, redundant code, speculative flexibility, and unnecessary files.
- Remove imports, variables, helpers, comments, tests, docs, and artifacts made obsolete by your change.
- Delete code made obsolete by the change instead of hiding it behind flags or wrappers.
- Do not leave `TODO`, `FIXME`, `HACK`, placeholders, dummy adapters, temporary bypasses, broad catch/pass, blanket suppressions, fake-green code, `eslint-disable`, `@ts-ignore`, or `@ts-expect-error`.

## 4. Goal-Driven Execution

Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan with explicit verification per step. Strong success criteria let you loop independently; weak criteria ("make it work") require constant clarification.

For work on the devin-skills estate only, read
`/home/prop_/projects/devin-skills/decisions.md` at start and after resume or
compaction. Update decisions, reasons and status before reporting or handoff;
mark superseded decisions. All worktrees share this local-only, Git-excluded file.

## 5. Quiet Windows And Scheduled Follow-Ups

Do not handle quiet windows, review waits, deploy waits, or scheduled follow-ups with repeated passive `sleep` loops in the foreground. Run the wait as a backgrounded shell so the session stays responsive.

When a rule requires waiting until a specific time:

- Compute and state the exact deadline timestamp and current timestamp.
- Compute the remaining wait in seconds.
- Launch the wait in the background: `exec` with `timeout: 0` running `sleep <secs> && <the audit command>` — or a watcher loop that exits when the awaited line appears — and keep the returned `shell_id`.
- For genuinely recurring schedules, use the OS scheduler (cron/systemd timer) rather than re-arming sleep manually.
- When the wait returns, collect it with `get_output <shell_id>` and immediately run the required live audit or follow-up command.
- Do not start another long wait unless the live audit shows a new event that creates a new deadline.
- Do not emit repeated "still waiting" updates. State the deadline once, then act when it expires.

For PR merge quiet windows specifically:

- `deadline = latest reviewer/check event timestamp + required quiet window`.
- After `deadline`, immediately re-query head SHA, checks, merge state, and unresolved non-outdated review threads (`gh` CLI via `exec`, or `gh api graphql`).
- If the audit is clean, merge immediately.
- If a new reviewer/check event appears, state the new deadline once and repeat the single-wait process.

## 6. Delegated Agent Defaults

When spawning sub-agents via `run_subagent`, default to:

- `profile`: choose the most specific profile that matches the task (`subagent_explore` for codebase searches and planning-input work, `subagent_general` otherwise).
- `subagent_explore`: when reading production code, invoke `codebase-design` and discover deepening opportunities; do not stop at locating code.
- Model: `subagent_explore` runs on the default subagent model (SWE-1.6 via the org router); `subagent_general` inherits the parent session's model. Pin a `model:` in a custom `.devin/agents/` profile or a skill's frontmatter when the task clearly warrants a different tier. Verify the executed model from harness receipts, not environment settings.

Keep delegation bounded:

- **Fan-in before planning.** Every spawned delegate returns before the lead writes the design or plan.
- **Delegates provide planning inputs; the lead owns the design.** Explore delegates explore, compare alternatives, and invoke `codebase-design`; the lead reconciles their reports and authors the governing design.
- Do not run build, typecheck, or proof commands concurrently with a delegated agent unless the user explicitly asks for that level of parallel execution.

If the parent session needs an independent second opinion, spawn a fresh agent rather than asking the same context-laden agent to self-review.

## 7. Repo Production Skill Order

Use the `repo-production-workflow` skill as the default first skill for production repository work. It owns the execution sequence; this section owns only when skills fire.

The full chain, in order — every named skill is INVOKED with the `skill` tool by exact name (reading its `SKILL.md` does not satisfy the step):

`repo-production-workflow` → `repo-context-forge` (+ its `bootstrap.py`, which executes the packet-scoped GitNexus checks and records that graph result as workflow evidence — there is no separate transition to record) → `diagnose` (bugs/regressions/perf only) → `codex-advisor` scope check when the request raises a design or scope question (phase `preflight-advice`; its wrapper ONLY) → `production-preflight` → `tdd` failing test first for behavior changes → `production-code` (invoked; its gate run is the baseline) before implementation edits → implementation through final verification → `code-review` delegate review when non-trivial → final Codex Advisor review (wrapper phase `final-review`, same `--slug`) → workflow `complete` → commit/push/PR → reviewer completion gate.

Invocation policy:

- Escalate to `repo-large-implementation` for large planned work: anything likely to span multiple PRs, need a durable governing design, or exceed the review budget. Begin a `repo-production-workflow` pass, run Repo Context Forge, then pair `delivery-governance` with `execution-planning` before continuing that same pass for the first implementation. Later execution slices begin their own passes against the governing design. New advisor-bound designs are keyed by the workflow's public `workflowId` under the selected workflow state root, outside the Git checkout; create a tracked planning document only when the user explicitly requests that document as a deliverable, and never maintain one for progress or execution state.
- Use `diagnose` before fixing bugs, failures, flaky behavior, or performance regressions; the canonical root-cause-first gate above governs entry to a fix.
- Use `tdd` for behavior changes where a public-Interface failing test is practical; name the real production Seam and apply the canonical mock ban above without exception.
- Skill invocation is per execution pass, not per session: every new PR slice, every bug or regression outside the active pass's recorded intent, and every reviewer-fix round from signals on a pushed PR head begins a new pass by invoking `repo-production-workflow` with the `skill` tool before Repo Context Forge, `diagnose`, or any edit, then follows the rest of the chain. Findings raised by the `code-review` delegate or final Codex Advisor against the current unpushed tree stay in the active pass and follow the return-to-implementation path owned by `WORKFLOW-MAP.md`. Compaction or resume notes never waive re-invocation for a new pass. A harness compaction notice that says not to re-execute previously invoked skills governs their one-time setup actions only (scheduling, file creation) and is never a waiver of this rule. A pass that spans the compaction boundary keeps the invocations it already made; a new pass begun after compaction re-invokes its chain regardless of the notice's wording.
- Do not bypass `repo-production-workflow` by jumping from Repo Context Forge straight to edits.
- Do not re-invoke `execution-planning` for an execution-only pass when a governing design exists. The design is a falsifiable hypothesis, not an immutable authority: deepen it append-only in the same unpushed workflow, and record the changed declaration at the next advisor consult — the ledger keeps every prior version. Adding or correcting a design or attack obligation never by itself requires another `begin`, another preflight Advisor consultation, or a Repo Context Forge rerun; only changed production behavior invalidates the proof it can affect, and a pushed-head reviewer correction still begins the required new pass under existing delivery policy. The original user intent and public production Interface remain the completeness authority throughout. Repository-scoped workflow history and GitHub PR state carry durable progress; task lists (`todo_write`) are session-local convenience only. Existing tracked governing artifacts already controlling in-flight work remain authoritative under their existing contracts and are not migrated or rewritten merely to adopt the workflow-state design policy.
- Documentation-only changes follow the Repo Context Forge gate exception below.

The **review budget** targets ~500 net lines of code per PR (net = additions minus deletions in human-authored source; measurement and the 1,000-net-line split threshold live in the delivery-governance skill). Split, shrink, or consolidate scope before coding when a planned PR is likely to run past the target.

Module shape is a first-class production contract; the `production-preflight` skill owns its rules (deep modules, reuse-before-new, shallow-helper debt) and `codebase-design` owns the Module/Interface/Seam vocabulary.

Delivery and review discipline:

- Do not leave completed repo work stranded as local uncommitted changes. When work is intended for review or integration and a remote exists, verify branch/base/upstream alignment, commit the cleaned change, push, and open or update the PR unless the user or repo workflow explicitly says not to. Keep commits small, coherent, and reviewable.
- **Install, motherfucker.** Follow the estate repo README's scoped install contract; name the branch, commit, and path set, then re-sync to merged `main` once the PR lands.
- Treat reviewer comments as evidence to verify against code, contracts, tests, and edge cases; reviewers can be wrong. Fix valid issues with the smallest production change; explain with evidence when a requested change is unnecessary or unsafe.
- Before acting on ANY reviewer or automated finding, run two checks. They are unconditional and apply to one-line fixes, not just passes that invoke `production-preflight`. Severity labels are not a work queue: automated reviewers are reliable about what code *can* do and unreliable about whether it *does*.
  - **Premise**: name the finding's assumption about runtime, config, or installed state and verify it against the live system with a command. A false premise is rejected with the measurement quoted, and no code changes.
  - **Occurrence**: establish the failing shape through the supported Interface. Counts from data, logs, or reachable callers prove absence only when the measured domain is closed and complete; otherwise drive the real Seam. Only such a zero means report-only.
- Re-measure every re-raised finding and any finding whose premise, reachability, or measured domain changed during implementation. A disposition is never reusable across heads: re-run the premise and occurrence checks against the current code and quote the new numbers. Repetition is not evidence the finding is noise, and a cached conclusion is not a measurement.
- Validating a finding is not validating a fix. Before shipping a change to a parser, matcher, predicate, or anything consuming external text or markup, run the NEW code over values already captured in the system and require zero regressions. A test written from the same assumption that produced the fix cannot detect its error; only the corpus can.
- Give every finding a disposition with evidence — fixed, rejected-with-evidence, or reported-not-actioned — where the reviewer loop can see it. A rejection without a measurement is indistinguishable from one ignored.
- Resolve review threads only after the fix is pushed or the evidence has been posted. Merges and deploys remain subject to the repo's approval, quiet-window, and release gates.

PR Reviewer Completion Gate:

Commit/push/PR-update does not complete review work. Do not mark complete, switch slices, or start a new PRD until the reviewer loop is closed on the current PR head.

Steps:

- Enumerate every reviewer signal on the current head: review threads, inline and issue comments, check annotations, CI failures, automated-reviewer and human findings (live roster: the repo's `docs/agents/reviewers.md` when present), and PRD acceptance criteria.
- Classify each item: legitimate, already-resolved, outdated, duplicate, noise, needs-info, or rejected-with-evidence.
- For legitimate defects, regressions, flaky failures, or behavior mismatches, invoke `repo-production-workflow` first to open the fix pass, then follow its chain, using `diagnose` before any fix and updating the PRD or task contract when scope changes.
- After each push, wait for reviewers/checks on the new head, then re-query head SHA, checks, merge state, and unresolved non-outdated threads. Stale output from an older head is not evidence.

Complete only when: every legitimate signal is fixed or rejected-with-evidence; no unresolved non-outdated threads remain; required checks are green or unrelated failures are named as blockers; PRD reconciliation is done.

## 8. Repo Context Forge — Global Gate

For any coding, debugging, review, refactor, explanation, planning, or repository exploration task inside a git repository, run Repo Context Forge before choosing files, editing code, or running GitNexus analysis.

Exception: documentation-only changes do not require Repo Context Forge, even when they update behavior, deploy, runtime, operator, or governance docs. For docs-only work, use checkout/branch verification, `fff` MCP or direct file inspection, minimal edits, a cleanup loop, and lightweight docs/diff checks. If the work also changes code, config, runtime wiring, generated source, or executable behavior, Repo Context Forge is required before choosing files or editing.

Governance docs that change agent behavior, such as `CLAUDE.md`,
`AGENTS.md`, or `docs/agents/`, should also run `code-review` before handoff;
trivial docs edits can stay on the lightweight path. Before the first edit under
`skills/**` in a pass, invoke `writing-great-skills`.

Choose one stable task slug, begin its workflow state, then run the installed
bootstrap wrapper with the same slug:

```bash
printf '%s' "$request_text" | python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" begin \
  --repo "$PWD" --slug "<stable-task-slug>" --intent -
python3 "$HOME/.config/devin/skills/repo-context-forge/scripts/bootstrap.py" \
  --repo "$PWD" --workflow-slug "<stable-task-slug>" --intent "<user request>"
```

**Pass the request text, not a summary.** `repo-production-workflow` owns how that
intent is built and fed to `begin`; whatever reaches `begin` is what
`record-preflight` and both advisor consults enforce for the rest of the pass.

The SQLite event ledger and its active projection are workflow continuity only. They are not an attestation,
permission object, or Git authorization boundary.

The output must begin with `REPO_CONTEXT_FORGE_REQUIRED_INTAKE`. Treat that intake and the following packet as the initial repository context. If the packet emits a blocker, stop normal repo analysis and surface the blocker; do not continue with empty target context.

Delegated agents inherit the packet: spawn any sub-agent for repository exploration, planning, review, or implementation only after the intake completes, and pass the packet's targets, coverage plan, and GitNexus repo into the delegate's brief.

The `repo-context-forge` skill owns everything downstream of the intake: surface selection per mode, the consolidated-specialist delegation contract, surface reconciliation, packet-scoped GitNexus validation, and post-edit revalidation. For review-only tasks, do not edit code unless the user explicitly asks for a fix; report valid defects first and wait for an edit instruction.

The source checkout is input: Repo Context Forge must not leave `.soulforge`, `.codex`, `.claude`, `.devin`, or incidental `.gitignore` mutations in the user's checkout; an intentional `.gitnexus/` ignore rule is allowed when GitNexus indexes the source checkout.

## 9. GitNexus — Global Workflow

Inside an indexed repository, use GitNexus for structure, blast radius, and execution flow before making changes — packet-scoped per the Repo Context Forge gate above. Its tools appear as `mcp__gitnexus__<tool>`; use the MCP tools directly and reserve the CLI for indexing, status, and admin operations. The MCP `repo` selector is the packet's `<gitnexus_status><repo>` name: the packet indexes a cache-owned analysis worktree, and the source checkout has no index until the post-edit `gitnexus analyze` step. After that step, and for every CLI call, the selector is the checkout's absolute path (`git rev-parse --show-toplevel`), never its basename. A `Repository ... not found` reply means the selector named an unindexed checkout; switch to the packet name rather than retrying.

### Search Flow

- Use the `fff` MCP server (`mcp__fff__<tool>`) as the primary initial search layer for raw file and content discovery: file lookup, symbol lookup, text search, broad exploration, multi-pattern search.
- Use the `grep`/`find_file_by_name` tools or `exec rg` only when an exhaustive raw listing, exact count, machine-readable full output, or a missing `fff` tool makes it necessary.
- Do not use shell `grep` or `find` for repository search unless both `fff` and `rg` are unavailable, or the task specifically requires those commands.
- After locating the symbol or file, switch to GitNexus for meaning and safety:
  - `mcp__gitnexus__query` for architecture and execution flows
  - `mcp__gitnexus__context` for callers/callees and process participation
  - `mcp__gitnexus__impact` (direction `upstream`) before editing
- Before editing a symbol in an indexed repo, that symbol needs BOTH
  `mcp__gitnexus__context` on it (callers AND callees) and `mcp__gitnexus__impact`
  with `direction: upstream` and `includeTests: true`. A packet check that covered
  that symbol already answers the caller and upstream-impact halves — its entries
  carry `callers` and `impacted_files` — so read those from `<gitnexus_analysis>`
  rather than reissuing them. It carries no callee facts, so callee context is
  always your own call, as is every symbol the plan did not cover, which is the
  usual case because the plan ranks packet targets rather than your edit list. One
  call is never the full seam. `impact` walks callers only, so an impact-only pass is structurally
  blind to callees — and the thing a change actually breaks is usually a
  callee: the shared writer, lock, or transition helper the edited symbol
  calls. `includeTests` defaults to `false`, which hides the regression surface
  the change has to keep green; always pass it explicitly.
- Also run `impact` with `direction: downstream, includeTests: true` when
  behavior is moved, deepened, consolidated, or hidden behind an Interface.
- When the change touches shared state — the same table, row, file, lease, claim
  token, or transition helper — run `mcp__gitnexus__context` on EVERY symbol
  that mutates that state, not only the one being edited. Compare their risk
  ratings against each other. Two writers to one row is the exact case a single
  upstream impact call always misses, and the second writer is routinely the
  higher-risk one.
- Consuming an internal seam from a NEW file (tests, smokes, harnesses, scripts) requires `mcp__gitnexus__context` on that seam BEFORE writing the consumer — a new file has no indexed symbols, so the edit-time impact rule alone never fires for it. Import the existing tested owner of the behavior instead of writing a second parsing/lifecycle client.
- Reindex after structural changes or Git mutations when staleness is detected. Run `gitnexus analyze --force --skip-agents-md "$(git rev-parse --show-toplevel)"` and verify with `gitnexus status`. Rerunning the Repo Context Forge bootstrap re-records the packet-scoped graph result; neither it nor `gitnexus status` certifies a HEAD or tree.

### Hooks

Hook configuration lives in the `hooks` key of `~/.config/devin/config.json`. Five facts govern how hooks change what you do:

- Production edits are advised, never refused: `PreToolUse` on `edit|write|notebook_edit|apply_patch` names what the pass has not recorded and admits the edit; docs, scratch, and non-repository paths are silent. A RED taken after production changed is labelled late, never refused.
- Every production edit, and every governance edit, invalidates downstream review readiness before quality feedback returns, so review and final review must be earned again. A completed workflow stays terminal; an edit against it is advised to begin a new pass.
- `SessionStart` (resume/compact sources) and `PostCompaction` restore the chain from committed SQLite state; compaction never advances or waives a step.
- There is no Stop hook; `workflow.py summary` reports the earned proof (`Contract green=n/m`) and the next action on demand.
- No hook parses exec commands or authorizes Git.

The `repo-production-workflow` skill's `WORKFLOW-MAP.md` is the canonical operational documentation for per-hook roles.
