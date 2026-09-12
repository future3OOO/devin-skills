# Governed TDD Recorder

Use this reference only when governed workflow continuity is active. The recorded preflight owns the initial Behavior Map; the recorder binds real RED/GREEN executions and reassessments to its stable IDs. It is evidence, not authorization.

## RED and GREEN

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" tdd \
  --repo "$PWD" --slug "<task>" --phase red --behavior-id "BM_..." \
  -- <targeted-command>
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" tdd \
  --repo "$PWD" --slug "<task>" --phase green --behavior-id "BM_..." \
  -- <targeted-command>
```

The map owns the behavior, Seam, expected outcome, and behavior-specific `redFailure` - an assertion marker or the product's own exception or diagnostic. For directly invoked pytest and unittest, RED is valid only when collection/loading/setup reaches at least one executed test and the marker is carried by that test's own failure exception (`redProof.quality` `assertion-reached`, the line kept in `observedFailure`). Printed output is never the failure: captured output is excluded, and a pytest run whose FAILURES section carries more header-shaped lines than failed tests is unattributable and refuses, naming both counts. For any other command, a non-zero exit whose output carries the marker opens the RED with `redProof.reach` `unresolved` (`quality` `failure-observed`); review establishes that the observed failure is the mapped promise. Identifiable pre-Interface failures refuse either way, with the reason retained in the run's `redProofFailure`: a command that could not start, the interpreter's missing-target report, a loader failure, a collection/setup error, a zero-test run, or an import or syntax exception as the final diagnostic or the marker-carrying line. Every mapped run is retained, refused or not; a refused attempt binds the item to nothing, and a later differing command is admitted until a valid RED opens the cycle.

A valid RED records its item red and opens its cycle whatever the map's other items are doing; the edit hook names any contract item still without its RED instead of refusing. Mapped proof surfaces must resolve inside the repository: unittest selectors, discover start directories, and pytest targets that do not resolve under the repository root refuse at cycle-open. That promise is target-name resolution, not executed-source attestation — the ledger is continuity, and deliberately routing executed test source from outside the repository through an in-repo re-export, `load_tests`, or conftest delegation is fabricated proof in the audited deception class. A passing runner RED baselines the item; a non-runner operation exiting 0 on a pending item is refused, because it can be GREEN only through the item's own recorded RED, recorded as the operation succeeding (`passProof.quality` `operation-succeeded`) and never as assertion execution. GREEN must rerun the same normalized test surface, not merely the same spelling. For directly invoked stdlib unittest or pytest, fail-fast and verbosity aliases may differ; selectors, target, config, runner, behavior ID, and Seam remain load-bearing. Unknown runners remain exact-command bound.

The recorder counts valid cycle-opening REDs only as a coarse granularity smell. Cycle count is never a coverage target.

## Map updates

`tdd-map` changes existing obligations or adds uncovered outcomes. A no-op writes nothing. Pass the document on stdin:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" tdd-map \
  --repo "$PWD" --slug "<task>" --workflow-id "<active-workflowId>" --input - <<'JSON'
{"sourceBehaviorId": "BM_...", "reassessment": "what the proof exposed", "items": [...], "dispositions": [...]}
JSON
```

`sourceBehaviorId`, when given, names the GREEN whose consequence the update records. New items use the initial preflight schema; runtime proof and `revalidationRequired` are reserved. Dispositions follow [SKILL.md](SKILL.md). Missing replacement targets, cycles, impossible terminal replacements, and foreign finding references refuse the whole update atomically.

Reassess affected preservation with `{"id":"BM_KEEP","revalidate":true,"evidence":"Name the affected guarantee and change"}`. `revalidate` and `status` are mutually exclusive; repeated flagged requests are idempotent. `status:pending` reopening sets the same flag. Settled preservation loses present settlement/baseline authority, while GREEN retains historical RED/GREEN fields. Governing omission retains the flag through reopening; finding closure still requires current owning proof.

Flagged pending uses ordinary `tdd --phase red`: a passing pytest/unittest baseline clears reassessment without a cycle; a genuine failure opens RED, and GREEN later clears it. Flagged GREEN reruns `tdd --phase green` against its **producer-recorded `redCommand`**, including direct operations, with the same normalized-surface rules. Success refreshes evidence without a new cycle or invalidating unrelated receipts. Failure, timeout, skipped-only/setup output, and candidate drift retain the run and unresolved flag for retry. Positively identified non-executing rechecks on the unchanged candidate also preserve unrelated receipts; genuine regressions and ambiguous failures invalidate downstream checks. Missing producer binding stays unresolved unless governing omission or a valid current GREEN replacement settles the obligation. Neither authored `proofCommand` nor prose supplies that binding. Do not fabricate RED or wrap an operation just to change parser classification.

Reassessment runs retain `candidateTree` sampled before execution and compare the candidate at commit, outside the child execution lock. Drift records `bindingError`, never accepted proof. RED's existing `productionChanged`, `passStartOid`, and `headOid` remain pass-relative; lateness is sticky and historical documents remain immutable.

Add ownership without another execution: `{"id":"BM_KEEP","sourceRefs":[{"type":"finding","evidenceId":"<actual intake>","id":"SPEC-1"}]}`. References union in order by full identity, including historical intakes in the same workflow; withdrawn items cannot acquire new references. They cannot remove/reassign ownership. Reference-only updates preserve lifecycle, verification, review, cycle count, and active command/surface/runs; repeated unions write nothing. Mixed updates commit atomically, transitioning only for actual obligations.

Any map update, refusal, baseline, or recheck beside A's open RED keeps A's binding unless another RED genuinely opens a cycle. Repeated RED as well as GREEN must match the item's own recorded `redCommand` before execution. The admitted RED sweep remains available.

Already fixed/report-only owners can obtain reassessment evidence without first possessing it. Strict behavioral `fixed` still needs current owning evidence and at least one genuine GREEN-through-RED; baseline alone never claims a repair. When reassessment invalidates an existing terminal proof claim, a measured rejection or report-only correction remains possible through the existing disposition command, retaining its history. It is not permission to relabel a still-supported terminal finding.

## No behavior change

Use `--not-required` only when every map item is already satisfied or omitted by governing evidence:

```bash
python3 "$HOME/.config/devin/skills/repo-production-workflow/scripts/workflow.py" tdd \
  --repo "$PWD" --slug "<task>" \
  --not-required "<specific reason no production behavior edit is required>"
```

Proof gaps and pending items forbid this path. The CLI separately refuses to replace existing valid RED/GREEN evidence.

Before completion, report the applicable map IDs and evidence: RED, GREEN, already satisfied, omitted, proof gaps, map updates, broader regression proof, and refactoring performed while GREEN.
