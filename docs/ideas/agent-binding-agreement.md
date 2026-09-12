# The Agent Binding Agreement

**Instruction files don't govern coding agents; enforced transactions do.**

An idea file. Copy-paste it to your own LLM agent and build it together. It runs our own production workflow loop. We're not trying to convince you to use ours, but there are parts you should consider adopting. We built it on Claude Code; any harness with lifecycle hooks can do the same. The specific skills, models, and scripts are ours, so swap in your own. The agreement is what transfers.

## The problem

The failure mode of AI coding isn't only wrong code. It's *shallow* code - thin helper modules, wrappers around wrappers, each fine in isolation, compounding into a codebase where one small change breaks everything. Models paper over it with mock tests that never touch a real seam. Some invent a *fake* seam just so the mock has something to pass. Green tests, done-looking PR, invisible debt.

The attempts before this idea are everywhere right now: workflow loops in markdown, agent "constitutions", glorified skill packs - instruction files telling the agent what process to follow. They all fail the same way: **no enforcement**. People cry that Opus 5 ignores instructions, that it's useless after compaction, which is amusing, because you were expecting a Claude model to be governed by md files. The agent reads the rules, sincerely agrees with them, and drifts anyway. Gradually, then completely. Compaction just finishes the job. CI can catch broken behavior; it rarely catches a codebase getting shallower one helper at a time.

Here is what we propose. We call it the Agent Binding Agreement (ABA).

## The agreement

The first thing we give up is raw speed. Slow and steady wins this race: each pass takes longer because the proof happens before the change is allowed to land.

In exchange, the process becomes a binding agreement, and the cleanest way to see it is to build it.

Suppose we just write the rules down as markdown clauses [1][9][10]: deepen modules instead of spawning them, check the existing tests before writing more, proof must cross a real production Seam, an admitted proof gap beats manufactured green, one owner per rule. Good rules. The agent reads them, sincerely agrees, and drifts. We established this.

So we add a ledger: one JSON file per repo recording each step, in order [4], living in Claude Code's state directory so the checkout stays clean, and five lifecycle hooks that read it [2][3][8]. Production edits are refused until the chain catches up; test files open once preflight is ready, so RED comes first. Every production edit resets verification and both reviews to pending, before quality feedback returns. The chain survives compaction. The turn cannot end with work incomplete unless an honest, named pause is recorded, and the latch finds that work by the repositories the session edited in, not the directory it was launched from, which is rarely the tree it works in. The order is now enforced rather than requested.

But a step is still just a mark, and a step an agent can mark done without doing anything will eventually be marked done without anything being done. We've all seen it happen: tests reported passing that never ran, skills invoked and nothing produced, an agent announcing a step it never took, or coming back from compaction with nothing but the marks to tell it what was actually done. Nothing malicious. The process simply permitted a step marked done to point at work that never happened. So the steps stop being marks: **a phase records only with its skill's own output as the evidence.** Almost every step already produces a native byproduct - a test run, a structured document, a gate verdict, a reviewer's findings - so nothing new is invented. Where the step is an execution, a runner executes the command it records; where it is an artifact, a recorder validates the artifact before the phase advances. The ordinary phase command cannot substitute for either. The evidence lands at a fixed path beside the ledger, keyed to the pass, so checking a claim against its artifact becomes a state read instead of transcript archaeology.

Two limits, kept on purpose. Recorder validation is structural: it proves the artifact exists in the required shape, not that the thinking behind it was good; substance stays with the reviewers. And a step whose native output is a tool call with no durable receipt stays a bare mark, because a pasted summary would be the agent's own claim wearing a producer's clothing. Laundered evidence is worse than an honest mark.

Now the steps are real, but the approvals still don't know what tree they approved. A shell edit after review leaves every approval standing. So when the review is recorded, the ledger snapshots a manifest, a content hash per reviewable file, and the later gates recompute and refuse on mismatch, naming what changed. And now we have an agreement that binds!

Each step enters the ledger the way a transaction enters a database: validated, atomic, refused if it doesn't hold. One readiness check powers both `complete` and the Stop latch, so they cannot disagree about whether the pass is finished. And the manifest's hashes are Git object ids. When the pass lands, the reviewed blobs settle into Git's own hash-linked history. The ledger validates the transaction; the merge is its inclusion in the chain. Tamper-evidence, not tamper-proofing: hashes detect change, and nothing signs them, on purpose.

One disclaimer, because it matters: this is a system against drift, not against deception. The ledger is honest memory, not security. It's agent-writable, the edit gate only sees the editor's own tools (a shell heredoc walks straight past it), and "test file" is a naming convention. Don't close those holes; closing them means parsing shell and authorizing Git, and the moment you build that, you've built theater. Drift is the failure that actually happens, and the mechanisms kill it. Deception is caught by reading the transcript against the ledger, and that audit is one grep.

## Architecture

Four layers. Keep the role, replace the contents.

1. **The map** (ours: RepoContextForge + GitNexus). An index that ranks what matters for this change, and a code graph that answers callers/callees/blast-radius for any symbol. The difference between editing a file that *looks* right and the seam that *is* right. Grep finds names; the graph finds the second writer to the same row, the callee your change actually breaks.
2. **The clauses** (ours: nine skill files [1] - seven load every pass, one joins for bugs, one for interface changes). Your standards as markdown the agent loads per pass. Write them once, they compound forever.
3. **The ledger + producers** (ours: one JSON file and a runner or recorder per load-bearing step [4][5][6][7]). Our chain: map → advisor scope check → preflight → failing test → standards loaded → implement → verify → self-review → independent review → done. The state machine refuses out-of-order phases. Preflight, TDD, the standards gate, verification, structured review, and advisor results enter through separate producer interfaces. `complete` refuses unless every required phase is ready and every producer-backed phase carries its evidence, material lead-review findings are resolved, and the final source is the rival advisor with `commit-ready` and no pending findings. Yours can differ; the ordering enforcement can't.
4. **The hooks** (ours: five Claude Code lifecycle hooks [2][8]). Gate, invalidate, preserve, latch. The layer that turns the other three from advice into refusals.

## The advisor - twice, not once

A different model family reviews the work at two points. Before any code: it challenges the plan - scope, design, what already exists. After implementation and lead review: it receives the current unstaged, staged, untracked, and base-to-HEAD diff, plus the recorded TDD and lead-review summaries [7]. It is read-only and must end with exactly one terminal verdict. Ours precedes the verdict with a machine-readable findings block, severity and a material flag on each finding. Only the verdict and the lead's disposition document are enforced; the advisor's own block is not yet a contract. The wrapper records the raw result; the lead records a separate disposition. A material finding becomes a trap when the advisor withholds `commit-ready`: completion refuses, and the fix re-earns verification, the self-review, and a fresh consult against the new evidence. A `commit-ready` verdict with cleared findings is the last gate before the ledger completes.

The principle is rivalry, not a particular model. Families share blind spots internally. Our pick (GPT-5.6 at max reasoning) over-engineers theoretical edge cases when it *writes* code, which is exactly the trait you want in an adversary *reading* code. Every finding gets a measured disposition: fixed with proof, rejected with the measurement quoted, or carried explicitly as follow-up. Material findings keep the gate open until fixed or rejected. Never argued, always measured.

## The ritual

1. **map** - index the repo, walk the graph, fix the real target seams
2. **ask** - first advisor consult: challenge the plan before any code exists
3. **prepare** - the preflight: name the affected surface, the contract that must hold, the proof plan, and every open question
4. **prove** - run one failing test through a real production Seam. RED must fail for the named product reason; GREEN must use the same behavior, command, and Seam, and cannot pass without that RED
5. **build** - load the standards, then the smallest change that makes the test pass
6. **check** - verify, then a structured self-review against the clauses; every finding gets one validated disposition
7. **review** - second advisor consult against the live evidence; its exact verdict plus your separate disposition gate the finish
8. **land** - pass the canonical completion check, push, answer every reviewer finding with evidence

It is done.

Nothing in this list is new. It's the process every good engineer already claims to follow. The invention is only the forcing. The state machine refuses the next phase until its predecessor is ready; every production edit rewinds downstream proof. A fix round is a new ritual, not a patch on the old one.

## The honest cost

Slower, and more tokens. A straightforward fix can take a full pass with two paid consults. That's the trade, taken with open eyes: the objective is code quality, not speed, not token efficiency.

Not every task takes the same proof path. Genuinely non-behavioral work can record TDD as not required, trivial changes can skip structured lead review, and documentation follows a lighter path. What cannot happen is silent omission: each gate is passed, explicitly not required, or still pending.

A single task is also the wrong place to measure the cost. Shallow code is cheap today and expensive forever; the debt lands on every task after this one. Measure across the life of the codebase or don't measure at all. Don't take our word for any of this. Run it on one of your own repos and watch what the reviewers find.

## What do you actually need to build?

The whole enforcement core. The chain may record a step as not required; it cannot silently omit one. Every gap is where the drift comes back in.

Ours: five hooks [2][8], the ledger with a producer per load-bearing step [4][5][6], nine clause files [1], one advisor wrapper [7], and the map. Every piece is a file you can read, and every producer-backed phase leaves a producer-recorded result you can check.

## Tips

- Prefer runners that do the work as they record. Where a recorder only validates an artifact, audit the artifact separately.
- Keep producer outputs separate from lead dispositions. A lead may resolve findings; it must not create the producer's result.
- Use one readiness check for both completion and stopping.
- A pause is a claim too. Audit pauses like any other claim.
- Invalidate on edit, mechanically. Stale approvals are worse than none.
- Re-measure every recurring reviewer finding fresh. Cached dispositions rot; we learned this the hard way.
- Docs describing runtime behavior: the code is the owner, the doc is the defect when they disagree.
- A code graph makes callable nodes only of the source types it parses. Naming a Python runner `.py` puts it in the graph; a shell runner stays out whatever you call it, so its callers have to be found by raw search rather than trusted to blast radius.
- Keep enforcement out of Git. Ledger and hooks, nothing else.
- Audit the transcript against the ledger sometimes. Count what was invoked versus what was recorded. It's the only check that catches a liar.

## TLDR

Instruction files don't govern agents; enforced transactions do. Write the process as a binding agreement: markdown clauses for the rules, an ordered state machine, runners and recorders that let each producer-backed phase record only with its skill's own output as evidence, hooks that gate edits and invalidate downstream proof, validated reviewer dispositions, one readiness check shared by completion and stopping, and a rival model family consulted twice, once to challenge the plan and once to gate the landing with an exact verdict. Slow and steady by design. Every piece is a file. The cost is paid per task; the intended return is less rework and less debt across the life of the codebase.

## References (our implementation)

1. [`skills/repo-production-workflow/SKILL.md`](../../skills/repo-production-workflow/SKILL.md) - the chain, step by step, with the exact recording commands
2. [`skills/repo-production-workflow/WORKFLOW-MAP.md`](../../skills/repo-production-workflow/WORKFLOW-MAP.md) - hook roles, Stop permit conditions, edit invalidation
3. [`hooks/`](../../hooks/) - the five lifecycle hook scripts
4. [`hooks/lib/workflow_state.py`](../../hooks/lib/workflow_state.py) - the ledger: ordering, producer transitions for every evidence-bearing phase, invalidation, and completion readiness
5. [`workflow.py tdd`](../../skills/repo-production-workflow/scripts/workflow.py) - the runner: executes RED/GREEN and binds both to the same behavior, command, and Seam
6. [`workflow.py record-review`](../../skills/repo-production-workflow/scripts/workflow.py) - the recorder: validates every finding and disposition before review advances
7. [`skills/codex-advisor/scripts/ask-codex-advisor.sh`](../../skills/codex-advisor/scripts/ask-codex-advisor.sh) - the advisor wrapper: read-only live-evidence consult, producer-recorded result, exact terminal verdict
8. [`config.json`](../../config.json) - where the hooks are registered
9. [`AGENTS.md`](../../AGENTS.md) - the canonical hard invariants: real-Seam proof, demonstrated risk, root-cause first
10. [`skills/tdd/mocking.md`](../../skills/tdd/mocking.md) - real boundary strategies and the honest proof-gap rule

## Open questions

Things we know are unfinished. If you build this, you'll hit them too.

- The manifest's hashes are unsigned by design - evidence, not proof. That's the right economy while the failure model is drift. But as agents get more capable, blind trust thins, and "the transcript audit covers deception" may stop being enough. The clean extension, when that day comes: the harness signs the ledger once at begin and once at complete, two signatures with nothing new in between, and tamper-evidence graduates to verifiable proof. We haven't built it; we've kept the seam.
- The lead's disposition carries its evidence, a structured document in the review recorder's shape, so the gate does not close on a bare flag. The advisor's own output is unpersisted: the findings the lead dispositions are transcribed rather than captured verbatim, so the two can drift and only the transcript shows it.
- The rival advisor sometimes serializes its findings across consults, one more discovery per paid round, instead of enumerating everything in the first pass. Reviewer calibration is a real cost lever.
- The production path is still heavier than some changes deserve. We have explicit not-required and documentation paths, but not yet a lighter production lane that cannot become the default escape hatch. The governance rule is the blunt case: every clause file counts, so correcting a single sentence in any of them reopens a finished pass and buys another verification, another review, and another paid consult.
- Completion demands no subtraction receipt. The quality gate runs after every edit; cleanup is also a hard rule. Yet nothing at `complete` records what the pass added against what it removed, so growth across a long fix round answers to no one.
- Auditing produced evidence is a state read. The fabrication check stays manual: whether the artifact came from the work. Native tool-invocation counts would make invocation matching mechanical; provenance would still be read by hand.
- We haven't ablated the layers. We believe the chain only works whole; we haven't proven which piece carries the most weight.
- This has only governed the estate that built it. The real test is a codebase that isn't about the contract.
