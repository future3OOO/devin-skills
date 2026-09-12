---
name: diagnose
description: Disciplined diagnosis loop for hard bugs and performance regressions. Reproduce → minimise → hypothesise → instrument → fix → regression-test. Use when user says "diagnose this" / "debug this", reports a bug, says something is broken/throwing/failing, or describes a performance regression.
---

# Diagnose

A discipline for hard bugs. Skip phases only when explicitly justified.

When exploring the codebase, use the project's domain glossary to get a clear mental model of the relevant modules, and check ADRs in the area you're touching.

## Iron Law

The canonical root-cause-first gate in `AGENTS.md` governs entry to a fix; this
skill owns the reproduction, tracing, and hypothesis procedure.

Seeing the symptom is not root cause. A stack trace line, failing assertion, or bad final state is the starting point. Trace the failure back to the original trigger before proposing code changes.

## Phase 1 — Build a feedback loop

**This is the skill.** Everything else is mechanical. If you have a fast, deterministic, agent-runnable pass/fail signal for the bug, you will find the cause — bisection, hypothesis-testing, and instrumentation all just consume that signal. If you don't have one, no amount of staring at code will save you.

Spend disproportionate effort here. **Be aggressive. Be creative. Refuse to give up.**

### Ways to construct one — try them in roughly this order

1. **Failing test** at whatever seam reaches the bug — unit, integration, e2e.
2. **Curl / HTTP script** against a running dev server.
3. **CLI invocation** with a fixture input, diffing stdout against a known-good snapshot.
4. **Headless browser script** (Playwright / Puppeteer) — drives the UI, asserts on DOM/console/network.
5. **Replay a captured trace.** Save a real network request / payload / event log to disk; replay it through the code path in isolation.
6. **Throwaway harness.** Spin up the smallest real subset that exercises the bug path. A temporary stand-in may isolate one diagnostic hypothesis only; label it non-proof and never use it for RED/GREEN or production verification.
7. **Property / fuzz loop.** If the bug is "sometimes wrong output", run 1000 random inputs and look for the failure mode.
8. **Bisection harness.** If the bug appeared between two known states (commit, dataset, version), automate "boot at state X, check, repeat" so you can `git bisect run` it.
9. **Differential loop.** Run the same input through old-version vs new-version (or two configs) and diff outputs.
10. **HITL bash script.** Last resort. If a human must click, drive _them_ with `scripts/hitl-loop.template.sh` so the loop is still structured. Captured output feeds back to you.

Build the right feedback loop, and the bug is 90% fixed.

### Iterate on the loop itself

Treat the loop as a product. Once you have _a_ loop, ask:

- Can I make it faster? (Cache setup, skip unrelated init, narrow the test scope.)
- Can I make the signal sharper? (Assert on the specific symptom, not "didn't crash".)
- Can I make it more deterministic? (Pin time, seed RNG, isolate filesystem, freeze network.)

A 30-second flaky loop is barely better than no loop. A 2-second deterministic loop is a debugging superpower.

### Non-deterministic bugs

The goal is not a clean repro but a **higher reproduction rate**. Loop the trigger 100×, parallelise, add stress, narrow timing windows, inject sleeps. A 50%-flake bug is debuggable; 1% is not — keep raising the rate until it's debuggable.

### When you genuinely cannot build a loop

Stop and say so explicitly. List what you tried. Ask the user for: (a) access to whatever environment reproduces it, (b) a captured artifact (HAR file, log dump, core dump, screen recording with timestamps), or (c) permission to add temporary production instrumentation. Do **not** proceed to hypothesise without a loop.

Do not proceed to Phase 2 until you have a loop you believe in.

## Phase 2 — Reproduce

Run the loop. Watch the bug appear.

Confirm:

- [ ] The loop produces the failure mode the **user** described — not a different failure that happens to be nearby. Wrong bug = wrong fix.
- [ ] The failure is reproducible across multiple runs (or, for non-deterministic bugs, reproducible at a high enough rate to debug against).
- [ ] You have captured the exact symptom (error message, wrong output, slow timing) so later phases can verify the fix actually addresses it.

Do not proceed until you reproduce the bug.

## Phase 3 — Trace Root Cause

Trace backward from the visible failure to the original trigger:

```text
symptom -> immediate cause -> caller -> bad input/state -> original trigger
```

Ask at each step:

- What directly caused the observed failure?
- What called this with that value, state, config, timing, or environment?
- Where did the bad value or invalid state first enter the system?
- Which seam should have owned this invariant?

Fix at the source, not where the error merely appears.

If the trace is unclear, add targeted instrumentation before the dangerous operation or state transition. Include the relevant value, cwd/environment/config when relevant, and a stack trace or caller context. Tag temporary logs with a unique `[DEBUG-...]` marker.

If tracing requires scattered helpers or replacing internal collaborators to
reach the behavior, or no Module exposes the behavior through a clean Seam,
record a module-shape risk using the vocabulary owned by `codebase-design`.
After the immediate bug is understood, escalate to
`improve-codebase-architecture` rather than normalizing the shallow path.

### Targeted GitNexus Check

For repo-based bugs, use GitNexus when available only after source tracing has named a suspected owning module/interface/seam. Retarget it to the active checkout, branch, or PR head before relying on graph evidence:

```bash
gitnexus analyze --force --skip-agents-md "$(git rev-parse --show-toplevel)"
gitnexus status
```

Use `gitnexus context <suspect>` and `gitnexus impact <suspect> --direction upstream` to challenge the trace for missed callers, affected processes, and shallow-helper domino paths. GitNexus may widen the regression surface or change the hypothesis; it does not replace reproduction, source reads, or the testable hypothesis.

## Phase 4 — Pattern Analysis

Before forming a fix hypothesis:

- Check recent changes: diffs, commits, dependencies, config, runtime, and environment.
- Find working examples in the same codebase with similar behavior.
- Compare working and broken paths; list material differences.
- Read relevant reference implementations completely before adapting their pattern.
- Identify assumptions the broken path makes about dependencies, state, ordering, or configuration.

Do not skip this because the fix feels obvious. Pattern mismatch is a common root cause.

## Phase 5 — Hypothesise

Generate **3–5 ranked hypotheses** before testing any of them. Single-hypothesis generation anchors on the first plausible idea.

Each hypothesis must be **falsifiable**: state the prediction it makes.

> Format: "If <X> is the cause, then <changing Y> will make the bug disappear / <changing Z> will make it worse."

If you cannot state the prediction, the hypothesis is a vibe — discard or sharpen it.

Test one active hypothesis at a time with the smallest useful probe. Do not bundle multiple fixes or instrumentation changes into one test.

After two failed hypotheses, stop and refresh the trace and pattern analysis before continuing. Do not layer guesses.

**Show the ranked list to the user before testing.** They often have domain knowledge that re-ranks instantly ("we just deployed a change to #3"), or know hypotheses they've already ruled out. Cheap checkpoint, big time saver. Don't block on it — proceed with your ranking if the user is AFK.

## Phase 6 — Instrument

Each probe must map to a specific prediction from Phase 5. **Change one variable at a time.**

Tool preference:

1. **Debugger / REPL inspection** if the env supports it. One breakpoint beats ten logs.
2. **Targeted logs** at the boundaries that distinguish hypotheses.
3. Never "log everything and grep".

**Tag every debug log** with a unique prefix, e.g. `[DEBUG-a4f2]`. Cleanup at the end becomes a single grep. Untagged logs survive; tagged logs die.

**Condition-based waiting.** For flaky async or timing failures, wait for the real condition: event, state, file, output, queue item, DOM change, process exit, or persisted record. Do not use arbitrary sleeps as guesses. A fixed delay is acceptable only when timing itself is the behavior under test; state the reason.

**Targeted defense-in-depth.** After the root cause is traced, add validation only where it makes the bug structurally harder to repeat: trust boundaries, state mutation boundaries, dangerous operations, or the exact boundary where the bad value crossed. Do not add blanket validation at every layer.

**Perf branch.** For performance regressions, logs are usually wrong. Instead: establish a baseline measurement (timing harness, `performance.now()`, profiler, query plan), then bisect. Measure first, fix second.

## Phase 7 — Fix + regression test

Write the regression test **before the fix** — but only if there is a **correct seam** for it.

A correct seam is one where the test exercises the **real bug pattern** as it occurs at the call site. If the only available seam is too shallow (single-caller test when the bug needs multiple callers, unit test that can't replicate the chain that triggered the bug), a regression test there gives false confidence.

**If no correct seam exists, that itself is the finding.** Note it. The codebase architecture is preventing the bug from being locked down. Flag this for the next phase.

If a correct seam exists:

1. Turn the minimised repro into a failing test at that seam.
2. Watch it fail.
3. Apply one source-level fix.
4. Watch it pass.
5. Re-run the Phase 1 feedback loop against the original (un-minimised) scenario.

No "while here" refactors during the fix. Keep refactoring for the green state.

If the fix does not work, stop and count attempts. After three failed fixes, treat the issue as an architecture/module-shape problem, not a debugging persistence problem. Use `/improve-codebase-architecture` before attempting another fix.

## Phase 8 — Cleanup + post-mortem

Required before declaring done:

- [ ] Original repro no longer reproduces (re-run the Phase 1 loop)
- [ ] Regression test passes (or absence of seam is documented)
- [ ] Root cause is stated as the traced source, not the visible symptom
- [ ] All `[DEBUG-...]` instrumentation removed (`grep` the prefix)
- [ ] Throwaway prototypes deleted (or moved to a clearly-marked debug location)
- [ ] The hypothesis that turned out correct is stated in the commit / PR message — so the next debugger learns

**Then ask: what would have prevented this bug?** If the answer involves architectural change (no good test seam, tangled callers, hidden coupling) hand off to the `/improve-codebase-architecture` skill with the specifics. Make the recommendation **after** the fix is in, not before — you have more information now than when you started.
