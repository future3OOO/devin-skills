#!/usr/bin/env bash
# Contract and composition diagnostics for the sole advisor transport.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)"
WRAPPER="$ROOT/skills/codex-advisor/scripts/ask-codex-advisor.sh"
WORKFLOW="$ROOT/skills/repo-production-workflow/scripts/workflow.py"
pass=0
fail=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [[ "$actual" == *"$expected"* ]]; then
    printf 'PASS  %s\n' "$name"; pass=$((pass + 1))
  else
    printf 'FAIL  %s\n      expected: %s\n      got: %s\n' "$name" "$expected" "${actual:0:240}"
    fail=$((fail + 1))
  fi
}

check_absent() {
  local name="$1" rejected="$2" actual="$3"
  if [[ "$actual" != *"$rejected"* ]]; then
    printf 'PASS  %s\n' "$name"; pass=$((pass + 1))
  else
    printf 'FAIL  %s\n      unexpected: %s\n' "$name" "$rejected"
    fail=$((fail + 1))
  fi
}

check_status() {
  local name="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf 'PASS  %s (exit %s)\n' "$name" "$actual"; pass=$((pass + 1))
  else
    printf 'FAIL  %s: expected exit %s, got %s\n' "$name" "$expected" "$actual"
    fail=$((fail + 1))
  fi
}

count_exact() {
  python3 - "$1" "$2" <<'PY'
import sys
print(open(sys.argv[1], encoding="utf-8").read().count(sys.argv[2]))
PY
}

write_design() {
  cat >"$1" <<'EOF'
UNIQUE-DESIGN-BODY-MARKER
Chosen architecture preserves PRES-1 and records ASSUMP-1.
<!-- governed-design-labels:v1 -->
```json
{"schemaVersion":1,"labels":[{"id":"PRES-1","kind":"preservation"},{"id":"ASSUMP-1","kind":"assumption","behavioral":false}]}
```
EOF
}

printf '== static and argument contract\n'
out=$(bash -n "$WRAPPER" 2>&1); check_status "wrapper parses" 0 "$?"
[[ -x "$WRAPPER" ]] && { printf 'PASS  wrapper is executable\n'; pass=$((pass + 1)); } || { printf 'FAIL  wrapper is not executable\n'; fail=$((fail + 1)); }
[[ ! -e "$ROOT/skills/repo-production-workflow/scripts/codex-advisor.sh" ]] \
  && { printf 'PASS  no second advisor transport exists\n'; pass=$((pass + 1)); } \
  || { printf 'FAIL  second advisor transport exists\n'; fail=$((fail + 1)); }

out=$(CODEX_ADVISOR_ACTIVE=1 "$WRAPPER" --slug t --cwd "$PWD" -- q 2>&1); status=$?
check_status "nested consult refused" 3 "$status"; check "nested refusal names cause" "you ARE the advisor delegate" "$out"
out=$(ADVISOR_ACTIVE=1 "$WRAPPER" --slug t --cwd "$PWD" -- q 2>&1); status=$?
check_status "shared nested marker refused" 3 "$status"
out=$("$WRAPPER" --cwd "$PWD" -- q 2>&1); status=$?
check_status "missing slug refused" 2 "$status"; check "missing slug named" "--slug is required" "$out"
out=$("$WRAPPER" --slug t --phase bogus --cwd "$PWD" -- q 2>&1); status=$?
check_status "unknown phase refused" 2 "$status"
out=$("$WRAPPER" --slug t --budget 1201 --cwd "$PWD" -- q 2>&1); status=$?
check_status "oversized budget refused" 2 "$status"
out=$("$WRAPPER" --slug t --cwd /definitely/not/a/dir -- q 2>&1); status=$?
check_status "bad cwd refused" 2 "$status"

grep -Fq 'provider_tools="Read,Grep,Glob,Skill,Bash,WebSearch,WebFetch"' "$WRAPPER" \
  && { printf 'PASS  phase-less direct-measurement tools retained\n'; pass=$((pass + 1)); } \
  || { printf 'FAIL  phase-less direct-measurement tools changed\n'; fail=$((fail + 1)); }
grep -Fq 'phase_args=(--safe-mode --strict-mcp-config)' "$WRAPPER" \
  && { printf 'PASS  phased customizations and MCP config are disabled\n'; pass=$((pass + 1)); } \
  || { printf 'FAIL  phased startup isolation missing\n'; fail=$((fail + 1)); }
grep -Fq 'provider_tools=""' "$WRAPPER" \
  && { printf 'PASS  phased tool allowlist is empty\n'; pass=$((pass + 1)); } \
  || { printf 'FAIL  phased tool allowlist is not empty\n'; fail=$((fail + 1)); }
grep -Fq 'disallowed_tools="Read Grep Glob Skill Bash WebSearch WebFetch Edit Write NotebookEdit Task mcp__gitnexus__*"' "$WRAPPER" \
  && { printf 'PASS  phased built-ins and GitNexus remain blocked\n'; pass=$((pass + 1)); } \
  || { printf 'FAIL  phased process-boundary deny changed\n'; fail=$((fail + 1)); }
grep -q -- '--expected-candidate-tree "$candidate"' "$WRAPPER" \
  && { printf 'PASS  checkpoint candidate reaches advisor-result\n'; pass=$((pass + 1)); } \
  || { printf 'FAIL  checkpoint candidate is not recorded with the result\n'; fail=$((fail + 1)); }
if grep -q 'workflow_cli" status\|repo context packet\|Repo Context Forge graph evidence\|--- unstaged diff ---\|--- staged diff ---\|--- untracked diff ---' "$WRAPPER"; then
  printf 'FAIL  superseded phased payload owner remains\n'; fail=$((fail + 1))
else
  printf 'PASS  superseded phased payload owners deleted\n'; pass=$((pass + 1))
fi

argtmp=$(mktemp -d)
trap 'rm -rf "$argtmp"' EXIT
mkdir -p "$argtmp/repo"
git -C "$argtmp/repo" init -q
git -C "$argtmp/repo" -c user.email=test@example.invalid -c user.name=Harness commit -q --allow-empty -m base
write_design "$argtmp/design.md"

out=$("$WRAPPER" --slug t --phase preflight-advice --cwd "$argtmp/repo" -- q 2>&1); status=$?
check_status "phased consult requires design declaration" 2 "$status"; check "design requirement named" "--design-file or --design-absent" "$out"
out=$("$WRAPPER" --slug t --phase preflight-advice --design-file "$argtmp/missing" --cwd "$argtmp/repo" -- q 2>&1); status=$?
check_status "missing design refused" 2 "$status"
out=$("$WRAPPER" --slug t --phase preflight-advice --design-file "$argtmp/design.md" --design-absent no --cwd "$argtmp/repo" -- q 2>&1); status=$?
check_status "two design declarations refused" 2 "$status"
out=$("$WRAPPER" --slug t --design-absent no --cwd "$argtmp/repo" -- q 2>&1); status=$?
check_status "phase-less design refused" 2 "$status"

printf '{}' >"$argtmp/packet.json"
for args in \
  '--fresh' \
  "--packet $argtmp/packet.json" \
  '--base-ref HEAD'; do
  read -r -a extra <<<"$args"
  out=$("$WRAPPER" --slug t --phase preflight-advice --design-absent no --cwd "$argtmp/repo" "${extra[@]}" -- q 2>&1); status=$?
  check_status "phased caller choice refused ($args)" 2 "$status"
done
check "phased fresh names checkpoint ownership" "checkpoint stage owns create or resume mode" "$("$WRAPPER" --slug t --phase preflight-advice --design-absent no --cwd "$argtmp/repo" --fresh -- q 2>&1)"
check "phased anchor refusal names checkpoint ownership" "checkpoint owns projection and current-pass anchors" "$("$WRAPPER" --slug t --phase preflight-advice --design-absent no --cwd "$argtmp/repo" --packet "$argtmp/packet.json" -- q 2>&1)"

printf '== checkpoint and path identity\n'
state="$argtmp/state"
out=$(DEVIN_WORKFLOW_STATE_ROOT="$state" "$WRAPPER" --slug orphan --phase preflight-advice --design-absent no --cwd "$argtmp/repo" -- q 2>&1); status=$?
check_status "phased consult without workflow refused" 2 "$status"; check "missing workflow named" "requires an active workflow" "$out"
DEVIN_WORKFLOW_STATE_ROOT="$state" python3 "$WORKFLOW" begin --repo "$argtmp/repo" --slug real-pass >/dev/null
out=$(DEVIN_WORKFLOW_STATE_ROOT="$state" "$WRAPPER" --slug wrong-pass --phase preflight-advice --design-absent no --cwd "$argtmp/repo" -- q 2>&1); status=$?
check_status "mismatched slug refused" 2 "$status"; check "slug mismatch named" "does not match the active workflow" "$out"
out=$(DEVIN_WORKFLOW_STATE_ROOT="$state" "$WRAPPER" --slug real-pass --phase preflight-advice --design-absent no --cwd "$argtmp/repo" -- q 2>&1); status=$?
check_status "not-ready checkpoint refused" 2 "$status"; check "missing graph step named" "repo-context-forge" "$out"

idtmp=$(mktemp -d)
mkdir -p "$idtmp/home" "$idtmp/repo/sub"
git -C "$idtmp/repo" init -q
for cwd in "$idtmp/repo" "$idtmp/repo/sub"; do
  HOME="$idtmp/home" DEVIN_ESTATE_HOME="$idtmp/claude" DEVIN_WORKFLOW_STATE_ROOT="$idtmp/state" \
    "$WRAPPER" --slug path-identity --cwd "$cwd" -- q >/dev/null 2>&1
done
ln -s "$idtmp/repo" "$idtmp/link"
HOME="$idtmp/home" DEVIN_ESTATE_HOME="$idtmp/claude" DEVIN_WORKFLOW_STATE_ROOT="$idtmp/state" \
  "$WRAPPER" --slug path-identity --cwd "$idtmp/link" -- q >/dev/null 2>&1
sid_count=$(ls "$idtmp/state/_advisor-sessions" 2>/dev/null | wc -l | tr -d ' ')
check_status "one phase-less SID across canonical paths" 1 "$sid_count"
rm -rf "$idtmp"

printf '== scoped payload and session diagnostics\n'
rigtmp=$(mktemp -d)
mkdir -p "$rigtmp/home" "$rigtmp/repo" "$rigtmp/bin" "$rigtmp/capture"
git -C "$rigtmp/repo" init -q
git -C "$rigtmp/repo" config user.email test@example.invalid
git -C "$rigtmp/repo" config user.name Harness
git -C "$rigtmp/repo" remote add origin https://example.invalid/advisor-rig.git
printf 'value = 1\n' >"$rigtmp/repo/app.py"
git -C "$rigtmp/repo" add app.py
git -C "$rigtmp/repo" commit -q -m base
write_design "$rigtmp/design.md"
cat >"$rigtmp/home/.bashrc" <<'BASHRC'
alias claudex='ANTHROPIC_BASE_URL=https://transport.invalid ANTHROPIC_AUTH_TOKEN=offline-token CLAUDE_CODE_SUBAGENT_MODEL=offline-model \
CLAUDE_CODE_MAX_CONTEXT_TOKENS=272000 CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000 \
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80 claude --model offline-model'
BASHRC
cat >"$rigtmp/bin/claude" <<'PROVIDER'
#!/usr/bin/env bash
set -u
count_file="$CAPTURE_DIR/count"
count=0; [[ -f "$count_file" ]] && count=$(cat "$count_file")
count=$((count + 1)); printf '%s\n' "$count" >"$count_file"
printf '%s\n' "$PWD" >"$CAPTURE_DIR/pwd-$count"
printf '%s\n' "$*" >"$CAPTURE_DIR/args-$count"
printf 'CLAUDE_CODE_MAX_CONTEXT_TOKENS=%s\nCLAUDE_CODE_AUTO_COMPACT_WINDOW=%s\nCLAUDE_AUTOCOMPACT_PCT_OVERRIDE=%s\n' \
  "${CLAUDE_CODE_MAX_CONTEXT_TOKENS:-unset}" "${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-unset}" \
  "${CLAUDE_AUTOCOMPACT_PCT_OVERRIDE:-unset}" >"$CAPTURE_DIR/env-$count"
cat >"$CAPTURE_DIR/payload-$count"
if [[ "${FAIL_PROVIDER:-0}" == 1 ]]; then exit 7; fi
if [[ " $* " == *" --resume "* ]]; then
  printf '%s\n' '{"schemaVersion":1,"findings":[],"verdict":"commit-ready"}'
else
  printf '%s\n' '{"schemaVersion":1,"findings":[{"id":"SPEC-1","claim":"the public reader returns the wrong value","kind":"behavioral","material":true},{"id":"SPEC-2","claim":"another reader returns the wrong value","kind":"behavioral","material":true}],"verdict":"completed"}'
fi
PROVIDER
chmod +x "$rigtmp/bin/claude"
rigstate="$rigtmp/state"
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" begin --repo "$rigtmp/repo" --slug scoped-rig --intent 'scoped advisor transport' >/dev/null
printf 'value = 2\n' >"$rigtmp/repo/app.py"
cat >"$rigtmp/repo/test_transport_probe.py" <<'PY'
import unittest
import app
class Reader(unittest.TestCase):
    def test_value(self):
        self.assertEqual(app.value, 2, "READER_VALUE_WRONG")
PY
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 - "$ROOT" "$rigtmp/repo" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hooks.tests.support import record_context_forge
record_context_forge(Path(sys.argv[2]), Path(sys.argv[2]).parent)
PY

run_wrapper() {
  PATH="$rigtmp/bin:$PATH" HOME="$rigtmp/home" DEVIN_ESTATE_HOME="$rigtmp/claude" \
    DEVIN_WORKFLOW_STATE_ROOT="$rigstate" CAPTURE_DIR="$rigtmp/capture" \
    "$WRAPPER" --cwd "$rigtmp/repo" "$@"
}
preflight_out=$(run_wrapper --slug scoped-rig --phase preflight-advice --design-file "$rigtmp/design.md" -- 'scope question' 2>"$rigtmp/preflight.err"); status=$?
check_status "controlled preflight composition exits 0" 0 "$status"
check "provider runs in selected repository" "$rigtmp/repo" "$(cat "$rigtmp/capture/pwd-1")"
preflight_args=$(cat "$rigtmp/capture/args-1")
preflight_sid=$(printf '%s\n' "$preflight_args" | sed -n 's/.*--session-id \([0-9a-f-]*\).*/\1/p')
check_status "preflight session id parsed" 36 "${#preflight_sid}"
check "preflight creates provider session" "--session-id $preflight_sid" "$preflight_args"
check_absent "preflight does not resume" "--resume" "$preflight_args"
check "preflight disables customizations" "--safe-mode --strict-mcp-config" "$preflight_args"
check "preflight pins xhigh reasoning" "--effort xhigh" "$preflight_args"
check "preflight denies GitNexus tools" "mcp__gitnexus__*" "$preflight_args"
check_absent "preflight role has no GitNexus guidance" "configured GitNexus" "$(cat "$rigtmp/capture/payload-1")"
check "configured max-context knob reaches the provider" "CLAUDE_CODE_MAX_CONTEXT_TOKENS=272000" "$(cat "$rigtmp/capture/env-1")"
check "configured auto-compact window reaches the provider" "CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000" "$(cat "$rigtmp/capture/env-1")"
check "configured autocompact percent reaches the provider" "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=80" "$(cat "$rigtmp/capture/env-1")"
check "design body is attached as framed evidence" "design> UNIQUE-DESIGN-BODY-MARKER" "$(cat "$rigtmp/capture/payload-1")"
check_status "one design narrative section" 1 "$(count_exact "$rigtmp/capture/payload-1" '--- governed-design narrative evidence')"
check "design evidence names line framing" "framing=design-line-prefix" "$(cat "$rigtmp/capture/payload-1")"
check "design telemetry emitted" "codex_advisor_evidence name=governing-design" "$(cat "$rigtmp/preflight.err")"
check "canonical design declaration is retained" '"sha256"' "$(cat "$rigtmp/capture/payload-1")"
check "current-pass diff carries the changed value" "diff> +value = 2" "$(cat "$rigtmp/capture/payload-1")"
check "projection is framed as untrusted data" "Untrusted repository-derived projection data follows" "$(cat "$rigtmp/capture/payload-1")"
check "diff is framed as untrusted data" "Untrusted repository diff data follows" "$(cat "$rigtmp/capture/payload-1")"
check_status "one projection section" 1 "$(count_exact "$rigtmp/capture/payload-1" '--- advisor projection (schemaVersion 1) ---')"
check_status "one current-pass diff section" 1 "$(count_exact "$rigtmp/capture/payload-1" '--- current-pass diff: passStartOid^{tree} -> activeCandidateTree ---')"
for old in 'repo context packet' 'Repo Context Forge graph evidence' '--- unstaged diff ---' '--- staged diff ---' '--- untracked diff ---' 'current Behavior Map' 'recorded TDD summary' 'recorded code-review summary'; do
  check_absent "old payload absent ($old)" "$old" "$(cat "$rigtmp/capture/payload-1")"
done

wid=$(DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" status --repo "$rigtmp/repo" | python3 -c 'import json,sys; print(json.load(sys.stdin)["workflowId"])')
# The controlled intake owns real runner-backed evidence, not authored GREEN.
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 - "$ROOT" "$rigtmp/repo" "$rigtmp/preflight.json" <<'PY'
import json, sys
sys.path.insert(0, sys.argv[1])
from hooks.lib.repo_identity import resolve_repo_identity
from hooks.lib.workflow_state import read_workflow
from hooks.tests.support import build_document, pending_behavior
state = read_workflow(resolve_repo_identity(sys.argv[2]))
intake = state["advisorPreflight"]["intakeEvidence"]
refs = [{"type":"finding", "evidenceId":intake, "id":identifier} for identifier in ("SPEC-1", "SPEC-2")]
contract = {**pending_behavior("BM_READER", red_failure="READER_VALUE_WRONG"), "sourceRefs":refs}
keep = {**contract, "id":"BM_KEEP", "kind":"preservation", "sourceRefs":[
    *refs, {"type":"design", "evidenceId":state["governedDesignEvidence"], "id":"PRES-1"}]}
doc = build_document("scoped wrapper diagnostic", behavior_map=[contract, keep])
doc["chosenApproach"] = "Reuse the hook correctness operation at 82 rows; limit 2048 UTF-8 bytes and 2 seconds per hook, no extra DB reads or subprocesses versus old."
doc["riskChecks"] = "Keep fixed bounds after measurement; compare the same real-hook Interface."
doc["proofPlan"] = "workflow.py verify -- python3 -m unittest hooks.tests.test_behavior_map_workflow.BehaviorMapWorkflowTests.test_consecutive_hook_obligations_are_bounded_without_extra_edit_work; report actual source target, scale, limit and observed value."
open(sys.argv[3], "w", encoding="utf-8").write(json.dumps(doc))
PY
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" record-preflight --repo "$rigtmp/repo" --slug scoped-rig --workflow-id "$wid" --input "$rigtmp/preflight.json" >/dev/null
for behavior in BM_KEEP BM_READER; do
  out=$(DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" tdd --repo "$rigtmp/repo" --slug scoped-rig --phase red --behavior-id "$behavior" -- python3 -m unittest test_transport_probe 2>&1); status=$?
  check_status "$behavior receives an executed baseline" 0 "$status"
done
PYTHONDONTWRITEBYTECODE=1 python3 "$ROOT/skills/production-code/scripts/code_quality_gate.py" check --repo "$rigtmp/repo" --json >"$rigtmp/gate.json"
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" record-production-code --repo "$rigtmp/repo" --slug scoped-rig --workflow-id "$wid" --input "$rigtmp/gate.json" >/dev/null
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" set-phase --repo "$rigtmp/repo" --phase implementation --status passed >/dev/null
selected_receipt=$(PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" verify --repo "$rigtmp/repo" --slug scoped-rig -- python3 -m unittest hooks.tests.test_behavior_map_workflow.BehaviorMapWorkflowTests.test_consecutive_hook_obligations_are_bounded_without_extra_edit_work 2>"$rigtmp/measurement.err"); status=$?
check_status "declared hook resource operation verifies through the real runner" 0 "$status"
check "selected operation reports its fixed byte limit" '"limitBytes": 2048' "$selected_receipt"
check "selected operation reports its retained scale" '"scale": 82' "$selected_receipt"
printf '%s\n' "$selected_receipt" | tee "$rigtmp/selected-receipt.txt"
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" verify --repo "$rigtmp/repo" --slug scoped-rig --kind quality-gate --base-ref HEAD >/dev/null
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" set-phase --repo "$rigtmp/repo" --phase code-review --status not-required --findings none >/dev/null

# One report-only finding is settled; the other remains pending. Reassessment
# blocks final transport, but measured rejection remains reachable in that pass.
DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 - "$ROOT" "$rigtmp/repo" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hooks.lib.repo_identity import resolve_repo_identity
from hooks.lib.workflow_state import read_workflow
from hooks.lib.state_store import _active_candidate_tree
repo = Path(sys.argv[2])
state = read_workflow(resolve_repo_identity(repo))
for identifier, status in (("SPEC-1", "report-only"), ("SPEC-2", "rejected-with-evidence")):
    doc = {"context":{"workflowId":state["workflowId"], "candidateTree":_active_candidate_tree(resolve_repo_identity(repo))},
           "intakeEvidenceId":state["advisorPreflight"]["intakeEvidence"], "dispositions":[{
               "finding_id":identifier, "status":status, "kind":"behavioral",
               "premise":{"claim":"reader value differs", "command":"python3 -m unittest test_transport_probe", "result":"false" if status == "rejected-with-evidence" else "true: reported reader differs"},
               "occurrence":{"domain":"all public app.value reads", "count":0, "complete":True, "command":"python3 -m unittest test_transport_probe", "result":"count=0"},
               "materialConsequence":{"claim":"callers see the wrong value", "command":"python3 -m unittest test_transport_probe", "result":"false"},
               "evidence":"retained public-reader operation passed"}]}
    (repo.parent / f"{identifier}.json").write_text(json.dumps(doc), encoding="utf-8")
(repo.parent / "reassess.json").write_text(json.dumps({"reassessment":"reader preservation affected", "dispositions":[{"id":"BM_KEEP", "revalidate":True, "evidence":"re-execute retained reader before closure"}]}), encoding="utf-8")
PY
out=$(DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" advisor-disposition --repo "$rigtmp/repo" --slug scoped-rig --workflow-id "$wid" --stage preflight --findings addressed --input "$rigtmp/SPEC-1.json" 2>&1); status=$?
check_status "executed owning evidence settles report-only" 0 "$status"
out=$(DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" tdd-map --repo "$rigtmp/repo" --slug scoped-rig --workflow-id "$wid" --input "$rigtmp/reassess.json" 2>&1); status=$?
check_status "settled owner enters reassessment" 0 "$status"
out=$(run_wrapper --slug scoped-rig --phase final-review --design-file "$rigtmp/design.md" -- 'final question' 2>&1); status=$?
check_status "pending preservation blocks ordinary final transport" 2 "$status"
check "refusal names the unresolved owner" "BM_KEEP" "$out"
check_status "blocked final never invokes the provider" 1 "$(cat "$rigtmp/capture/count")"
out=$(DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" advisor-disposition --repo "$rigtmp/repo" --slug scoped-rig --workflow-id "$wid" --stage preflight --findings addressed --input "$rigtmp/SPEC-2.json" 2>&1); status=$?
check_status "measured rejection remains reachable with pending preservation" 0 "$status"
out=$(run_wrapper --slug scoped-rig --phase final-review --design-file "$rigtmp/design.md" -- 'final question' 2>&1); status=$?
check_status "rejecting a finding does not erase pending preservation" 2 "$status"
check_status "unrelated pending work still prevents provider invocation" 1 "$(cat "$rigtmp/capture/count")"
out=$(DEVIN_WORKFLOW_STATE_ROOT="$rigstate" python3 "$WORKFLOW" tdd --repo "$rigtmp/repo" --slug scoped-rig --phase red --behavior-id BM_KEEP -- python3 -m unittest test_transport_probe 2>&1); status=$?
check_status "same retained operation closes preservation without a fabricated RED" 0 "$status"

FAIL_PROVIDER=1 run_wrapper --slug scoped-rig --phase final-review --design-file "$rigtmp/design.md" -- 'final question' >/dev/null 2>"$rigtmp/resume-fail.err"; status=$?
check_status "resume provider failure propagates" 7 "$status"
resume_args=$(cat "$rigtmp/capture/args-2")
check "final resumes same SID" "--resume $preflight_sid" "$resume_args"
check_absent "resume failure has no cold-start fallback" "--session-id" "$resume_args"

sid_file=$(ls "$rigstate/_advisor-sessions"/*-scoped-rig-"$wid".sid)
rm "$sid_file"
FAIL_PROVIDER=1 run_wrapper --slug scoped-rig --phase final-review --design-file "$rigtmp/design.md" -- 'final question' >/dev/null 2>&1; status=$?
check_status "missing SID starts a session and the provider failure propagates" 7 "$status"
created_sid=$(cat "$sid_file" 2>/dev/null)
check_status "missing SID persists a new workflow-bound session id" 36 "${#created_sid}"
check_absent "missing SID does not reuse the preflight session" "$preflight_sid" "$created_sid"
missing_args=$(cat "$rigtmp/capture/args-3")
check "missing SID creates the persisted session" "--session-id $created_sid" "$missing_args"
check_absent "missing SID does not resume" "--resume" "$missing_args"

final_out=$(run_wrapper --slug scoped-rig --phase final-review --design-file "$rigtmp/design.md" -- "Reconcile ownership and preservation with this selected verification receipt only: $selected_receipt" 2>"$rigtmp/final.err"); status=$?
check_status "controlled final composition exits 0" 0 "$status"
final_args=$(cat "$rigtmp/capture/args-4")
check "successful final resumes the created session" "--resume $created_sid" "$final_args"
check "final disables customizations" "--safe-mode --strict-mcp-config" "$final_args"
check "resumed final pins xhigh reasoning" "--effort xhigh" "$final_args"
check "final denies GitNexus tools" "mcp__gitnexus__*" "$final_args"
check_status "final has one design narrative section" 1 "$(count_exact "$rigtmp/capture/payload-4" '--- governed-design narrative evidence')"
check "final carries framed design body" "design> UNIQUE-DESIGN-BODY-MARKER" "$(cat "$rigtmp/capture/payload-4")"
check_status "final has one projection section" 1 "$(count_exact "$rigtmp/capture/payload-4" '--- advisor projection (schemaVersion 1) ---')"
check_status "final has one current-pass diff section" 1 "$(count_exact "$rigtmp/capture/payload-4" '--- current-pass diff: passStartOid^{tree} -> activeCandidateTree ---')"
check "final design telemetry emitted" "codex_advisor_evidence name=governing-design" "$(cat "$rigtmp/final.err")"
check "projection telemetry emitted" "codex_advisor_evidence name=advisor-projection" "$(cat "$rigtmp/final.err")"
check "diff telemetry emitted" "codex_advisor_evidence name=current-pass-diff" "$(cat "$rigtmp/final.err")"
check "completion marker emitted" "codex_advisor_complete status=0 provider=codex" "$(cat "$rigtmp/final.err")"
python3 - "$rigtmp/capture/payload-4" "$rigtmp/selected-receipt.txt" "$rigtmp/SPEC-1.json" <<'PY'
import json, sys
payload, receipt, disposition = [open(path, encoding="utf-8").read() for path in sys.argv[1:]]
assert receipt.strip() in payload, "SELECTED_RECEIPT_DROPPED"
assert json.loads(disposition)["intakeEvidenceId"] in payload, "EXACT_INTAKE_ID_DROPPED"
for value in ('"executedCommands"', 'python3 -m unittest test_transport_probe', '"revalidationRequired": false', 'all public app.value reads', 'the public reader returns the wrong value'):
    assert value in payload, "OWNERSHIP_OR_MEASUREMENT_DROPPED: " + value
PY
check_status "outgoing final payload retains exact ownership, commands and selected receipt" 0 "$?"

cat >"$rigtmp/home/.bashrc" <<'BASHRC'
alias claudex='ANTHROPIC_BASE_URL=https://transport.invalid ANTHROPIC_AUTH_TOKEN=offline-token CLAUDE_CODE_SUBAGENT_MODEL=offline-model \
CLAUDE_CODE_MAX_CONTEXT_TOKENS=272000 claude --model offline-model'
BASHRC
unphased_out=$(CLAUDE_CODE_AUTO_COMPACT_WINDOW=999111 CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=77 \
  run_wrapper --slug scoped-unphased --fresh -- 'unphased question' 2>"$rigtmp/unphased.err"); status=$?
check_status "controlled unphased consult exits 0" 0 "$status"
unphased_args=$(cat "$rigtmp/capture/args-5")
check "unphased consult retains direct-measurement tools" "--tools Read,Grep,Glob,Skill,Bash,WebSearch,WebFetch" "$unphased_args"
check_absent "unphased consult keeps customizations" "--safe-mode" "$unphased_args"
check_absent "unphased consult keeps configured MCP tools" "mcp__gitnexus__*" "$unphased_args"
check "the alias-configured max-context knob reaches the provider" "CLAUDE_CODE_MAX_CONTEXT_TOKENS=272000" "$(cat "$rigtmp/capture/env-5")"
check "a parent-exported unconfigured window is cleared" "CLAUDE_CODE_AUTO_COMPACT_WINDOW=unset" "$(cat "$rigtmp/capture/env-5")"
check "a parent-exported unconfigured percent is cleared" "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=unset" "$(cat "$rigtmp/capture/env-5")"

cat >"$rigtmp/home/.bashrc" <<'BASHRC'
alias claudex='ANTHROPIC_BASE_URL=https://transport.invalid ANTHROPIC_AUTH_TOKEN=offline-token CLAUDE_CODE_SUBAGENT_MODEL=offline-model \
CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000 claude --model offline-model'
BASHRC
omitted_max_out=$(CLAUDE_CODE_MAX_CONTEXT_TOKENS=888222 CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=77 \
  run_wrapper --slug scoped-unphased-max-isolation --fresh -- 'unphased question' 2>"$rigtmp/unphased-max.err"); status=$?
check_status "controlled max-context isolation consult exits 0" 0 "$status"
check "a parent-exported unconfigured max-context is cleared" "CLAUDE_CODE_MAX_CONTEXT_TOKENS=unset" "$(cat "$rigtmp/capture/env-6")"
check "the alias-configured window still reaches the provider" "CLAUDE_CODE_AUTO_COMPACT_WINDOW=240000" "$(cat "$rigtmp/capture/env-6")"
check "a parent-exported unconfigured percent remains cleared" "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=unset" "$(cat "$rigtmp/capture/env-6")"
rm -rf "$rigtmp"

if [[ "${LIVE:-0}" == 1 ]]; then
  printf '== live unphased transport\n'
  live_out=$("$WRAPPER" --slug wrapper-contract-live --cwd "$PWD" --budget 40 --fresh -- 'Reply with exactly LIVE_OK and nothing else. Do not use tools.' 2>"$argtmp/live.err")
  status=$?; check_status "live consult exits 0" 0 "$status"; check "live answer" "LIVE_OK" "$live_out"
  check "live completion marker" "codex_advisor_complete status=0 provider=codex" "$(cat "$argtmp/live.err")"
else
  printf 'SKIP  live consult (set LIVE=1 to run)\n'
fi

rm -rf "$argtmp"
trap - EXIT
printf '\n%s passed, %s failed\n' "$pass" "$fail"
[[ "$fail" -eq 0 ]]
