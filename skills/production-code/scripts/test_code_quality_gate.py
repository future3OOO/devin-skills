#!/usr/bin/env python3
"""Tests for the generic production code quality gate."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("code_quality_gate.py")
SCRIPT_DIR = Path(__file__).parent
ROOT = SCRIPT_DIR.parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The last commit before #75 reshaped classification. Its shipped predicate is
# the oracle for the standalone truth workflow state still depends on.
PINNED_PRE_75 = "9f01e5f"
POLICY_PATH = "skills/production-code/scripts/_quality_gate/path_policy.py"

# The captured PR #68 round-six corpus, not the merged PR's final head.
CORPUS_BASE = "4cfffcb8d5724bfc2b03dce505da8cf930fb49fa"
CORPUS_CANDIDATE = "28cf04e63fa6eb598b938d3a78d782969538d9a9"
CORPUS_DIFF_SHA256 = "885cd0f024eedcbb3c32e80ec6a41441cb0c82e2d227335c5d43e74105973d4a"


def _load_path_policy(path: Path):
    """Load a path_policy module standalone, the way workflow state loads it."""
    spec = importlib.util.spec_from_file_location(f"_path_policy_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SourceRepositoryUnavailable(Exception):
    """Raised only when no source checkout is found — a repository that is
    present but missing something a test needs stays a hard failure."""


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def source_repo() -> Path:
    """The repository whose history the characterization tests read; the
    estate installs these scripts outside any checkout, so ask Git."""
    res = run(["git", "rev-parse", "--show-toplevel"], SCRIPT_DIR)
    if res.returncode != 0:
        raise _SourceRepositoryUnavailable(f"{SCRIPT_DIR} is not inside a git checkout")
    return Path(res.stdout.strip())


def git(repo: Path, *args: str) -> None:
    res = run(["git", *args], repo)
    if res.returncode != 0:
        raise AssertionError(res.stderr or res.stdout)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def create_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="production-code-gate-"))
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    write(repo / "src" / "base.py", "def ok() -> int:\n    return 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    return repo


def run_gate(repo: Path, *args: str) -> tuple[int, dict[str, object], str]:
    res = run(["python3", str(SCRIPT), "check", "--repo", str(repo), "--json", *args], repo)
    payload = json.loads(res.stdout)
    return res.returncode, payload, res.stderr


def growth_totals(payload: dict[str, object]) -> dict[str, object]:
    return payload["evaluation"]["growth"]


def growth_finding(payload: dict[str, object]) -> dict[str, object]:
    findings = [item for item in payload["findings"] if item["ruleId"] == "QG54-GROWTH-CUMULATIVE"]
    assert len(findings) == 1, findings
    return findings[0]


def owner_rule_finding(payload: dict[str, object], rule: str) -> dict[str, object]:
    """One owner rule's per-evaluation state finding, never a candidate."""
    findings = [
        item for item in payload["findings"]
        if item["ruleId"] == rule and item["region"]["scope"] == "evaluation"
    ]
    assert len(findings) == 1, findings
    return findings[0]


def check_named(payload: dict[str, object], name: str) -> dict[str, object]:
    return next(item for item in payload["checks"] if item["name"] == name)


def duplicate_findings(payload: dict[str, object], rule: str = "") -> list[dict[str, object]]:
    """One finding per duplicated implementation, optionally one rule's.

    The per-rule state findings (`region.scope == "evaluation"`) carry the pass
    and completeness projection and are read through `check_named` instead.
    """
    return [
        item
        for item in payload["findings"]
        if item["ruleId"].startswith("QG54-DUPLICATE-")
        and item["region"]["scope"] == "duplicate"
        and (not rule or item["ruleId"] == rule)
    ]


EXACT_RULES = ("QG54-DUPLICATE-ADDED-SYMBOL", "QG54-DUPLICATE-ADDED-BLOCK", "QG54-DUPLICATE-BASELINE")

# The schema couples the two facts, so one place knows the pairing: an absence
# of findings alone proves nothing, because an incomplete rule produces one too.
_RULE_STATES = {"passed": (True, "passed"), "finding": (True, "finding"), "incomplete": (None, "incomplete")}


def assert_exact_rules(payload: dict[str, object], expected: dict[str, str]) -> None:
    """Assert the state of each named exact rule, never a bare absence."""
    for rule, state in expected.items():
        item = check_named(payload, rule)
        assert (item["passed"], item["status"]) == _RULE_STATES[state], (rule, state, item)


def duplicate_regions(finding: dict[str, object]) -> set[tuple[str, str]]:
    return {(region["path"], region["evidenceRole"]) for region in finding["region"]["regions"]}


def snapshot_paths(repo: Path) -> set[str]:
    return {
        str(path.relative_to(repo))
        for path in repo.rglob("*")
        if ".git" not in path.relative_to(repo).parts
    }


def in_repo(fn) -> None:
    """Run fn against a fresh real repository, always cleaned up."""
    repo = create_repo()
    try:
        fn(repo)
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def with_repo(fn):
    """Decorator form for the common single-scenario test."""
    def wrapper() -> None:
        in_repo(fn)

    wrapper.__name__ = fn.__name__
    return wrapper


@with_repo
def test_clean_pass(repo: Path) -> None:
    code, payload, _ = run_gate(repo)
    assert code == 0
    assert payload["ok"] is True
    assert payload["schemaVersion"] == 2, payload["schemaVersion"]
    assert set(payload["hardRules"]) == {
        "noDuplication",
        "cleanup",
        "noMergeConflictMarkers",
        "consequenceCoverage",
    }
    hard_rules = payload["hardRules"]
    evaluated = [tuple(item["checks"]) for item in hard_rules.values() if item["status"] == "evaluated"]
    assert len(evaluated) == len(set(evaluated))
    assert hard_rules["noMergeConflictMarkers"]["checks"] == ["no-merge-conflict-markers"]
    assert hard_rules["consequenceCoverage"]["status"] == "not_evaluated"
    assert hard_rules["consequenceCoverage"]["passed"] is None


@with_repo
def test_temp_artifact_fails_cleanup(repo: Path) -> None:
    write(repo / "tmp" / "debug.txt", "scratch\n")
    code, payload, _ = run_gate(repo)
    assert code == 2
    assert payload["hardRules"]["cleanup"]["passed"] is False
    assert "no-temp-artifacts" in payload["hardRules"]["cleanup"]["checks"]


@with_repo
def test_standalone_entrypoint_import_bootstrap_passes(repo: Path) -> None:
    # Hook entry points are invoked by absolute path, so each must put the repo
    # root on sys.path before importing shared code, and E402 is unavoidable.
    # The bootstrap is entrypoint mechanics, not duplicated behaviour.
    bootstrap = (
        "import sys\n"
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "if str(ROOT) not in sys.path:\n"
        "    sys.path.insert(0, str(ROOT))\n"
        "from lib.shared import helper  # noqa: E402\n"
    )

    for name in ("gate_one", "gate_two", "gate_three"):
        write(repo / "hooks" / f"{name}.py", bootstrap + f"\n\ndef {name}() -> int:\n    return helper()\n")
    code, payload, _ = run_gate(repo)
    assert code == 0, json.dumps(payload, indent=2)
    assert payload["ok"] is True


@with_repo
def test_gate_creates_no_repo_artifacts(repo: Path) -> None:
    # The non-mutation contract this pins: working tree, staged content, and
    # refs are untouched. Capture may refresh the index's cache-tree extension
    # and leave unreferenced loose objects that git gc prunes; nothing
    # references those, so they are out of scope here.
    write(repo / "src" / "candidate.py", "def candidate() -> int:\n    return 2\n")
    git(repo, "add", "src/candidate.py")
    worktree_only = "# TO" + "DO worktree-only text must not enter index evidence\n"
    write(repo / "src" / "candidate.py", worktree_only + "def candidate() -> int:\n    return 3\n")
    before = snapshot_paths(repo)
    status_before = run(["git", "status", "--porcelain=v1"], repo).stdout
    refs_before = run(["git", "for-each-ref"], repo).stdout
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD", "--staged-only")
    after = snapshot_paths(repo)
    assert code == 0
    assert payload["ok"] is True
    assert payload["candidateSource"] == "index"
    assert payload["candidateTree"] == run(["git", "write-tree"], repo).stdout.strip()
    assert after == before
    assert run(["git", "status", "--porcelain=v1"], repo).stdout == status_before
    assert run(["git", "for-each-ref"], repo).stdout == refs_before


@with_repo
def test_unparseable_python_is_incomplete_not_duplicate(repo: Path) -> None:
    # This file tokenizes but does not parse, so no parser can say where a
    # definition begins or ends. Guessing those edges reports two differently
    # decorated methods as one implementation; the honest answer is that the
    # file was not read.
    body = "\n".join((
        "        ceiling = int(config.get('max_attempts', 3))",
        "        remaining = max(0, ceiling - attempt)",
        "        if not remaining:",
        "            return 0.0",
        "        scaled = round(0.5 * (2 ** attempt), 3)",
        "        return scaled",
    ))
    write(
        repo / "src" / "broken.py",
        "x = = 2\n"
        f"class L:\n    @property\n    def budget(self, config, attempt):\n{body}\n"
        f"class R:\n    @staticmethod\n    def budget(self, config, attempt):\n{body}\n",
    )
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert not duplicate_findings(payload), json.dumps(duplicate_findings(payload), indent=2)
    assert_exact_rules(payload, dict.fromkeys(EXACT_RULES, "incomplete"))
    for rule in EXACT_RULES:
        gaps = check_named(payload, rule)["gaps"]
        assert any("src/broken.py" in gap and "parse" in gap for gap in gaps), (rule, gaps)
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_blank_line_inside_an_f_string_is_content_not_spacing(repo: Path) -> None:
    # A blank line inside a multi-line f-string is part of the literal. Python
    # 3.12 tokenizes f-strings as FSTRING_* rather than STRING, so protection
    # keyed on token type alone would drop it and merge two different strings.
    def helper(gap: str) -> str:
        return (
            "def render_banner(name, count):\n"
            '    text = f"""welcome {name}\n'
            f"{gap}"
            '    you have {count} items"""\n'
            "    trimmed = text.strip()\n"
            "    upper = trimmed.upper()\n"
            "    return upper\n"
        )
    write(repo / "src" / "left.py", helper("\n"))
    write(repo / "src" / "right.py", helper("\n\n"))
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert not duplicate_findings(payload), json.dumps(duplicate_findings(payload), indent=2)
    assert_exact_rules(payload, dict.fromkeys(EXACT_RULES, "passed"))
    assert code == 0, (code, payload["errors"])


@with_repo
def test_untokenizable_language_reports_incomplete_not_clean(repo: Path) -> None:
    # A duplicate the gate cannot read exactly is not an absence of duplicates.
    # No tokenizer proves what a comment or a string interior is in JavaScript,
    # so the rule names the scope it could not read instead of passing.
    block = "\n".join(
        [
            "    const payload = normalizeInput(value) + normalizeInput(other);",
            "    const result = payload.trim().toLowerCase().replaceAll('x', 'y');",
            "    return result.includes('ready') ? result : `${result}:ready`;",
        ]
    )

    write(repo / "src" / "dup.js", f"export function a(value, other) {{\n{block}\n}}\nexport function b(value, other) {{\n{block}\n}}\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    symbol_rule = check_named(payload, "QG54-DUPLICATE-ADDED-SYMBOL")
    assert symbol_rule["passed"] is None and symbol_rule["status"] == "incomplete", symbol_rule
    assert any("src/dup.js" in gap and "javascript" in gap for gap in symbol_rule["gaps"]), symbol_rule
    assert any(
        finding["evidence"]["affectedRuleId"] == "QG54-DUPLICATE-ADDED-SYMBOL"
        for finding in payload["findings"]
        if finding["ruleId"] == "QG54-ANALYSIS-INCOMPLETE"
    ), payload["findings"]
    # Incomplete is visible, never blocking: no QG54 rule may fail a run.
    assert code == 0 and payload["ok"] is True, json.dumps(payload["errors"], indent=2)


# One quality-escape payload per row: typed test fakes stay green, fake-green
# swallowed asserts do not. Markers are assembled so this file is not flagged.
_ESCAPE_ROWS = (
    ("bare-noqa", "src/sloppy.py",
     "import os  # no" + "qa\n\n\ndef sloppy() -> str:\n    return os.sep\n", True),
    ("js-ts-escape", "src/bad.ts",
     "export function bad(value: any) {\n  // es" + "lint-disable-next-line\n  return value as any;\n}\n", True),
    ("python-escape", "src/bad.py",
     "from typing import Any\n\ndef bad(value: Any):\n    try:\n        return value\n    except Exception:\n        pass\n", True),
    ("test-any-annotation-is-allowed", "tests/test_fake.py",
     "from typing import Any\n\nclass Fake:\n    value: Any\n", False),
    ("test-fake-green-still-fails", "tests/test_bad.py",
     "def test_bad():\n    try:\n        assert False\n    except Exception:\n        pass\n", True),
)


def test_quality_escape_verdict_holds_for_every_payload() -> None:
    for name, path, content, fails in _ESCAPE_ROWS:
        in_repo(lambda repo, p=path, c=content, f=fails, label=name: _escape_row(repo, p, c, f, label))


def _escape_row(repo: Path, path: str, content: str, fails: bool, name: str) -> None:
    write(repo / path, content)
    code, payload, _ = run_gate(repo)
    if fails:
        assert code == 2, (name, code, payload["errors"])
        assert payload["hardRules"]["cleanup"]["passed"] is False, (name, payload["hardRules"]["cleanup"])
    else:
        assert code == 0 and payload["ok"] is True, (name, code, payload["errors"])


# One merge-conflict payload per row. A conflict is evidenced by either outer
# marker alone, each carrying a trailing space and a label that markup never
# produces; a bare separator line is legitimate reStructuredText and is not
# evidence of anything.
# name, path, content, expected conflict failure.
_CONFLICT_ROWS = (
    ("rst-section-underline", "docs/cli-reference.rst", "Options\n=======\n\nRun a query.\n", False),
    # Half-resolved files: each outer marker is evidence on its own, so both are
    # asserted separately — the full-triad fixtures cannot catch the loss of
    # either alternation, because the surviving one still fails the file.
    ("open-marker-alone", "src/half.py", "<" * 7 + " HEAD\nA = 1\n", True),
    ("close-marker-alone", "src/half.py", ">" * 7 + " theirs\nA = 1\n", True),
)


def test_conflict_verdict_holds_for_every_marker_shape() -> None:
    for name, path, content, fails in _CONFLICT_ROWS:
        in_repo(lambda repo, p=path, c=content, f=fails, label=name: _conflict_row(repo, p, c, f, label))


def _conflict_row(repo: Path, path: str, content: str, fails: bool, name: str) -> None:
    write(repo / path, content)
    code, payload, _ = run_gate(repo)
    rule = payload["hardRules"]["noMergeConflictMarkers"]
    if fails:
        assert code == 2, (name, code, payload["errors"])
        assert rule["passed"] is False, (name, rule)
        assert any("merge conflict markers" in item for item in payload["errors"]), (name, payload["errors"])
    else:
        assert code == 0 and payload["ok"] is True, (name, code, payload["errors"])
        assert rule["passed"] is True, (name, rule)


@with_repo
def test_distant_edits_do_not_fabricate_an_empty_catch(repo: Path) -> None:
    # Added lines from separate hunks are not adjacent in the candidate: an
    # except header edited in one place and a real `pass` added far below must
    # not join into an empty-catch escape that exists nowhere in the file.
    original = (
        "def parse(text):\n"
        "    try:\n"
        "        return int(text)\n"
        "    except ValueError:\n"
        "        return 0\n"
        "\n"
        "\n"
        "def audit(flag):\n"
        "    if flag:\n"
        "        log(flag)\n"
        "    return flag\n"
    )
    write(repo / "src" / "loader.py", original)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "loader")
    edited = original.replace("except ValueError:", "except Exception:").replace("        log(flag)", "        pass")
    write(repo / "src" / "loader.py", edited)
    code, payload, _ = run_gate(repo)
    assert code == 0, (code, payload["errors"])
    assert payload["ok"] is True


@with_repo
def test_large_growth_is_warning_only(repo: Path) -> None:
    # The per-file bloat blockers are deleted by the binding architecture:
    # cumulative human-authored growth over the review budget warns, never
    # fails, and the active warning-only finding keeps its intrinsic pass.
    write(repo / "src" / "huge.py", "\n".join(f"VALUE_{i} = {i}" for i in range(801)) + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert code == 0, (code, payload["errors"])
    assert payload["ok"] is True
    assert any("QG54-GROWTH-CUMULATIVE" in warning for warning in payload["warnings"]), payload["warnings"]
    findings = [item for item in payload["findings"] if item["ruleId"] == "QG54-GROWTH-CUMULATIVE"]
    assert len(findings) == 1, payload.get("findings")
    assert findings[0]["status"] == "finding" and findings[0]["passed"] is True, findings[0]


@with_repo
def test_huge_fixture_growth_stays_warning_only(repo: Path) -> None:
    # The per-file bloat blockers are gone: a large test-support addition
    # contributes to the human-authored growth warning, never to a failure.
    write(repo / "tests" / "fixtures" / "huge.py", "\n".join(f"VALUE_{i} = {i}" for i in range(1200)) + "\n")
    code, payload, _ = run_gate(repo)
    assert code == 0
    assert payload["ok"] is True


_OWNER = "def normalize_user_id(value: str) -> str:\n    return value.strip().lower()\n"


@with_repo
def test_completeness_scopes_are_rule_specific(repo: Path) -> None:
    # An owner file the capture bound never read is unknown scope for the
    # owner rule only once a changed-side unit could pair against it: growth
    # keeps its measured claim, and a unit-free change stays complete.
    write(repo / "src" / "big.py", "# pad\n" * 130000)
    write(repo / "src" / "ids.py", _OWNER)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owners")
    write(repo / "src" / "new.py", "def fresh_candidate_helper(x):\n    return x.strip()\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert code == 0, (code, payload["errors"])
    assert growth_finding(payload)["completeness"]["complete"] is True, growth_finding(payload)["completeness"]
    gaps = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["completeness"]["gaps"]
    assert any("big.py" in gap for gap in gaps), gaps

    write(repo / "src" / "new.py", "NOTES = 2\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert code == 0, (code, payload["errors"])
    gaps = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["completeness"]["gaps"]
    assert not any("big.py" in gap for gap in gaps), gaps


@with_repo
def test_completeness_follows_each_rule_own_role_scope(repo: Path) -> None:
    # An unmeasured, unparseable test blob is inside the exact and TEST owner
    # rules' scope and outside the production owner rule's. Widening one
    # rule's roles must not dirty the other's.
    (repo / "tests").mkdir()
    (repo / "tests" / "blob.py").write_bytes(b"A = 1\x00\n")
    write(repo / "src" / "app.py", "VALUE = 1\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    escapes = check_named(payload, "no-quality-escapes")
    assert escapes["status"] == "incomplete", escapes
    assert_exact_rules(payload, {
        "QG54-DUPLICATE-ADDED-SYMBOL": "incomplete",
        "QG54-OWNER-COMPETITION-TEST": "incomplete",
    })
    # Role separation shows in the gap sets: the blob dirties the TEST rule's
    # scope while the production rule carries only the universal graph gap.
    assert any("blob.py" in gap for gap in owner_rule_finding(payload, "QG54-OWNER-COMPETITION-TEST")["completeness"]["gaps"])
    production_gaps = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["completeness"]["gaps"]
    assert production_gaps == ["no snapshot-bound external graph evidence: caller/callee scope is unestablished"], production_gaps
    assert code == 0, (code, payload["errors"])


POLLING_BLOCK = "\n".join(
    [
        "    deadline = time.monotonic() + timeout_seconds",
        "    while True:",
        "        current_url = page.url",
        "        body_text = page.locator('body').inner_text()",
        "        if check(current_url, body_text):",
        "            return current_url, body_text",
        "        if time.monotonic() >= deadline:",
        "            return current_url, body_text",
        "        wait_for_timeout = getattr(page, 'wait_for_timeout', None)",
        "        if callable(wait_for_timeout):",
        "            wait_for_timeout(poll_interval_ms)",
    ]
)


@with_repo
def test_repeated_added_block_is_one_grouped_warning(repo: Path) -> None:
    # Two differently named helpers share one identical polling body. The
    # symbols differ, so only the contiguous block inside them duplicates, and
    # the overlapping windows covering it collapse to that one block.
    write(
        repo / "src" / "polls.py",
        f"def a(page, timeout_seconds, poll_interval_ms):\n{POLLING_BLOCK}\n\n"
        f"def b(page, timeout_seconds, poll_interval_ms):\n{POLLING_BLOCK}\n",
    )
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    findings = duplicate_findings(payload, "QG54-DUPLICATE-ADDED-BLOCK")
    assert len(findings) == 1, json.dumps(payload["findings"], indent=2)
    assert [region["displayLine"] for region in findings[0]["region"]["regions"]] == [2, 15], findings[0]
    assert not duplicate_findings(payload, "QG54-DUPLICATE-ADDED-SYMBOL"), payload["findings"]
    assert_exact_rules(payload, {
        "QG54-DUPLICATE-ADDED-BLOCK": "finding", "QG54-DUPLICATE-ADDED-SYMBOL": "passed",
    })
    assert code == 0 and payload["ok"] is True, json.dumps(payload["errors"], indent=2)


RETRY_BLOCK = "\n".join(
    (
        "    ceiling = int(config.get('max_attempts', 3))",
        "    remaining = max(0, ceiling - attempt)",
        "    if not remaining:",
        "        return 0.0",
        "    scaled = round(0.5 * (2 ** attempt), 3)",
        "    return scaled",
    )
)

DUPLICATE_HELPER = "\n".join(
    (
        "def resolve_retry_budget(config, attempt):",
        "    ceiling = int(config.get('max_attempts', 3))",
        "    remaining = max(0, ceiling - attempt)",
        "    if not remaining:",
        "        return 0.0",
        "    return round(0.5 * (2 ** attempt), 3)",
    )
)


@with_repo
def test_two_exact_added_symbols_warn_once_naming_both_regions(repo: Path) -> None:
    # Two files add the same complete helper body. That is one duplication
    # defect with two source regions, not two findings and not a blocker.
    write(repo / "src" / "left.py", DUPLICATE_HELPER + "\n")
    write(repo / "src" / "right.py", DUPLICATE_HELPER + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    findings = duplicate_findings(payload, "QG54-DUPLICATE-ADDED-SYMBOL")
    assert len(findings) == 1, json.dumps(payload["findings"], indent=2)
    finding = findings[0]
    # Active QG54 warning: the schema keeps its intrinsic check passed and
    # carries activeness in `status`, so ok stays true either way.
    assert finding["severity"] == "warning" and finding["passed"] is True, finding
    assert finding["status"] == "finding", finding
    assert_exact_rules(payload, {"QG54-DUPLICATE-ADDED-SYMBOL": "finding"})
    assert duplicate_regions(finding) == {("src/left.py", "duplicate"), ("src/right.py", "duplicate")}, finding
    # Warning-only: an exact duplicate never fails the run, even when the
    # caller asks for warnings to be promoted.
    assert code == 0 and payload["ok"] is True, json.dumps(payload["errors"], indent=2)
    _, promoted_payload, _ = run_gate(repo, "--base-ref", "HEAD", "--fail-on-warnings")
    assert not any("QG54-DUPLICATE-" in error for error in promoted_payload["errors"]), promoted_payload["errors"]


@with_repo
def test_copy_of_retained_baseline_names_both_regions(repo: Path) -> None:
    # The candidate copies a test helper the base tree still owns. Both regions
    # are named: the new copy, and the baseline implementation still retained.
    # A test role also proves capture is no longer production-only, and keeps
    # the exit code clear of the production-only legacy reuse advisory.
    write(repo / "tests" / "owner_helpers.py", DUPLICATE_HELPER + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    write(repo / "tests" / "copy_helpers.py", DUPLICATE_HELPER + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    findings = duplicate_findings(payload, "QG54-DUPLICATE-BASELINE")
    assert len(findings) == 1, json.dumps(payload["findings"], indent=2)
    assert findings[0]["severity"] == "warning", findings[0]
    assert duplicate_regions(findings[0]) == {
        ("tests/copy_helpers.py", "duplicate"),
        ("tests/owner_helpers.py", "retained-baseline"),
    }, findings[0]
    assert_exact_rules(payload, {"QG54-DUPLICATE-BASELINE": "finding"})
    assert code == 0 and payload["ok"] is True, json.dumps(payload["errors"], indent=2)


# Each row is one consolidation outcome the baseline rule must reach. The
# baseline commit owns DUPLICATE_HELPER at tests/owner_helpers.py; the row
# describes what the candidate does to it, and whether a retained copy remains.
# name, candidate files, paths removed from the candidate, expected regions.
_CONSOLIDATION_ROWS = (
    ("copy-then-delete", {"tests/copy_helpers.py": DUPLICATE_HELPER + "\n"}, ("tests/owner_helpers.py",), set()),
    ("moved-to-one-owner", {"tests/moved_helpers.py": DUPLICATE_HELPER + "\n"}, ("tests/owner_helpers.py",), set()),
    ("calls-the-owner", {"tests/caller_helpers.py": "from tests.owner_helpers import resolve_retry_budget\n\n\ndef budget(config):\n    return resolve_retry_budget(config, 1)\n"}, (), set()),
    ("edited-past-the-anchor", {"tests/copy_helpers.py": DUPLICATE_HELPER + "\n",
                                "tests/owner_helpers.py": DUPLICATE_HELPER.replace("0.5", "0.75") + "\n"}, (), set()),
    # Retention is about the implementation, not the bytes around it: adding a
    # comment to the owner must not make a live duplicate disappear.
    ("owner-comment-only-edit", {"tests/copy_helpers.py": DUPLICATE_HELPER + "\n",
                                 "tests/owner_helpers.py": DUPLICATE_HELPER.replace(
                                     "    remaining =", "    # keep the ceiling honest\n    remaining =") + "\n"}, (),
     {("tests/copy_helpers.py", "duplicate"), ("tests/owner_helpers.py", "retained-baseline")}),
    # Git resolves one of the two new copies as the rename of the deleted
    # owner, so consolidation is only partial: the moved implementation is
    # still retained and the second copy repeats it.
    ("partial-consolidation", {"tests/copy_helpers.py": DUPLICATE_HELPER + "\n",
                               "tests/second_helpers.py": DUPLICATE_HELPER + "\n"}, ("tests/owner_helpers.py",),
     {("tests/copy_helpers.py", "retained-baseline"), ("tests/second_helpers.py", "duplicate")}),
    ("retained-owner", {"tests/copy_helpers.py": DUPLICATE_HELPER + "\n"}, (),
     {("tests/copy_helpers.py", "duplicate"), ("tests/owner_helpers.py", "retained-baseline")}),
)


def test_consolidation_clears_the_duplicate_and_a_retained_copy_does_not() -> None:
    for name, candidate, removed, expected in _CONSOLIDATION_ROWS:
        in_repo(lambda repo, c=candidate, r=removed, e=expected, label=name: _consolidation_row(repo, c, r, e, label))


def _consolidation_row(repo: Path, candidate, removed, expected, name: str) -> None:
    write(repo / "tests" / "owner_helpers.py", DUPLICATE_HELPER + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    for path, text in candidate.items():
        write(repo / path, text)
    for path in removed:
        (repo / path).unlink()
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    regions = {
        region
        for finding in duplicate_findings(payload)
        for region in duplicate_regions(finding)
    }
    assert regions == expected, (name, json.dumps(duplicate_findings(payload), indent=2))
    # A cleared row must be cleared, and a firing row must be fully read: an
    # incomplete rule reports no regions either way.
    # Every row in this table clears or fires the BASELINE rule; the other two
    # stay clean either way, so one map covers both branches.
    states = dict.fromkeys(EXACT_RULES, "passed")
    states["QG54-DUPLICATE-BASELINE"] = "finding" if expected else "passed"
    assert_exact_rules(payload, states)
    assert code == 0 and payload["ok"] is True, (name, payload["errors"])


@with_repo
def test_varying_scaffolding_is_not_an_exact_duplicate(repo: Path) -> None:
    # Five near-identical scenario tests differing only in literals and
    # expected values. Exactness preserves literals, so this is not a
    # duplicated implementation; fixture-lifecycle ownership belongs to #77.
    cases = "\n\n".join(
        "\n".join((
            f"def test_case_{index}(tmp_path):",
            f"    target = tmp_path / 'case_{index}.json'",
            f"    target.write_text('{{\"attempts\": {index}}}')",
            "    parsed = load_attempts(target)",
            f"    assert parsed['attempts'] == {index}",
            f"    assert target.name == 'case_{index}.json'",
        ))
        for index in range(5)
    )
    write(repo / "tests" / "test_scenarios.py", "from src.base import load_attempts\n\n\n" + cases + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert_exact_rules(payload, dict.fromkeys(EXACT_RULES, "passed"))
    assert code == 0, (code, payload["errors"])


def owner_findings(payload: dict[str, object], rule: str) -> list[dict[str, object]]:
    """One finding per owner-competition candidate, optionally one rule's."""
    return [
        item
        for item in payload["findings"]
        if item["ruleId"].startswith("QG54-OWNER-COMPETITION-")
        and item["region"]["scope"] == "candidate"
        and (not rule or item["ruleId"] == rule)
    ]


_LIFECYCLE_ROWS = (
    ("alpha", "src/alpha.py", "value = 1", "2", "is False"),
    ("beta", "src/beta.py", "value = 2", "0", "is True"),
    ("gamma", "config/gamma.py", "flag = 3", "2", "is False"),
    ("delta", "src/delta.py", "flag = 4", "0", "is True"),
    ("epsilon", "lib/epsilon.py", "count = 5", "2", "is False"),
)

# Five scenario tests owning one write -> run -> assert lifecycle through the
# same executor, varying only payload and expected-value slots — the pinned
# case-R shape. Helpers are same-file so every callee anchor resolves locally.
_LIFECYCLE_SCAFFOLDS = "\n".join((
    "import json",
    "import subprocess",
    "from pathlib import Path",
    "",
    "",
    "def write(path, text):",
    "    path.parent.mkdir(parents=True, exist_ok=True)",
    "    path.write_text(text)",
    "",
    "",
    "def run_gate(repo):",
    "    result = subprocess.run(['gate', 'check'], cwd=repo, capture_output=True, text=True)",
    "    return result.returncode, json.loads(result.stdout or '{}')",
    "",
    "",
    "def with_repo(body):",
    "    body(Path('/tmp/fixture'))",
    "",
    "",
    "\n\n\n".join(
        "\n".join((
            f"def test_{name}_verdict():",
            "    def body(repo):",
            f"        write(repo / '{path}', '{payload}\\n')",
            "        code, payload = run_gate(repo)",
            f"        assert code == {code}",
            f"        assert payload['ok'] {verdict}",
            "    with_repo(body)",
        ))
        for name, path, payload, code, verdict in _LIFECYCLE_ROWS
    ),
    "",
))


@with_repo
def test_repeated_inline_scaffolds_are_one_owner_candidate(repo: Path) -> None:
    # Varying literals, scenarios, and expected effects do not suppress the
    # candidate when the resolved layer, callees, and ordered lifecycle
    # signature match; and no duplicate finding is required for it.
    write(repo / "tests" / "test_lifecycle.py", _LIFECYCLE_SCAFFOLDS)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "five scaffolds")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    bound = graph_evidence(base, run(["git", "rev-parse", "HEAD"], repo).stdout.strip(), ("tests/test_lifecycle.py",))
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(bound))
    names = {item["name"] for item in payload["checks"]}
    assert "QG54-OWNER-COMPETITION-TEST" in names, sorted(names)
    assert_exact_rules(payload, {**dict.fromkeys(EXACT_RULES, "passed"), "QG54-OWNER-COMPETITION-TEST": "finding"})
    candidates = owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")
    assert len(candidates) == 1, candidates
    finding = candidates[0]
    assert finding["state"] == "candidate", finding
    expected = [
        ("tests/test_lifecycle.py", line_no)
        for line_no, line in enumerate(_LIFECYCLE_SCAFFOLDS.splitlines(), 1)
        if line.startswith("def test_")
    ]
    assert len(expected) == 5, expected
    regions = [(region["path"], region["displayLine"]) for region in finding["region"]["regions"]]
    assert regions == expected, (regions, expected)
    assert code == 0 and payload["ok"] is True, (code, payload["errors"])


def assert_no_lifecycle_candidates(repo: Path, base: str, bound: Path) -> None:
    """The shared negative tail: zero fixture-lifecycle candidates, exact rule state."""
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(bound))
    lifecycle = [item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")
                 if item["region"]["evidenceClass"] == "fixture-lifecycle"]
    regions = [[(region["path"], region["displayLine"]) for region in item["region"]["regions"]]
               for item in lifecycle]
    assert regions == [], regions
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-TEST": "passed"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_partition_boundaries_discriminate_lifecycles(repo: Path) -> None:
    # An order-preserving transfer across the try/else boundary changes the
    # exception scope, so the two scaffolds are different lifecycles and
    # never one fixture-lifecycle candidate.
    write(repo / "tests" / "test_partitions.py", (
        "def test_finalize_guarded():\n"
        "    write_marker('r', 'armed')\n"
        "    stage = prepare('cfg')\n"
        "    try:\n"
        "        apply(stage)\n"
        "        finalize(stage)\n"
        "    except OSError:\n"
        "        rollback(stage)\n\n\n"
        "def test_finalize_unguarded():\n"
        "    write_marker('r', 'armed')\n"
        "    stage = prepare('cfg')\n"
        "    try:\n"
        "        apply(stage)\n"
        "    except OSError:\n"
        "        rollback(stage)\n"
        "    else:\n"
        "        finalize(stage)\n"
    ))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "partition scaffolds")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    bound = graph_evidence(base, head, ("tests/test_partitions.py",))
    assert_no_lifecycle_candidates(repo, base, bound)


@with_repo
def test_partition_wrappers_do_not_satisfy_operation_floor(repo: Path) -> None:
    # Partition wrappers carry structure for signature equality, never
    # weight: a two-operation if/else scaffold stays under the lifecycle
    # floor no matter how many partitions enclose it.
    scaffold = (
        "def test_{n}_toggle():\n"
        "    if flag('mode'):\n"
        "        enable('mode')\n"
        "    else:\n"
        "        disable('mode')\n"
    )
    write(repo / "tests" / "test_left.py", scaffold.format(n="left"))
    write(repo / "tests" / "test_right.py", scaffold.format(n="right"))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "toggles")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    bound = graph_evidence(base, head, ("tests/test_left.py", "tests/test_right.py"))
    assert_no_lifecycle_candidates(repo, base, bound)


@with_repo
def test_unreferenced_nested_helpers_stay_with_their_owner(repo: Path) -> None:
    # Facts and weight belong to the scope that owns them: two unrelated
    # outer functions containing similar never-referenced nested helpers
    # share no owner evidence. The referenced with_repo(body) scaffold shape
    # stays a lifecycle candidate and is pinned by the scaffold tests above.
    outer = (
        "def {name}(config):\n"
        "    def resolve_defaults():\n"
        "        root = os.environ.get('APP_STATE_ROOT')\n"
        "        home = os.environ.get('APP_HOME')\n"
        "        return root or home\n"
        "    return {result}\n"
    )
    write(repo / "src" / "exporter.py", "import os\n\n\n" + outer.format(name="export_report", result="config['report']"))
    write(repo / "src" / "importer.py", "import os\n\n\n" + outer.format(name="import_report", result="config['import']"))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "unrelated outers")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    bound = graph_evidence(base, head, ("src/exporter.py", "src/importer.py"))
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(bound))
    candidates = [(item["region"]["evidenceClass"],
                   [(region["path"], region["displayLine"]) for region in item["region"]["regions"]])
                  for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert candidates == [], candidates
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "passed"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_changed_file_without_units_still_needs_coverage(repo: Path) -> None:
    # Changed-surface completeness comes from the changed role entries, not
    # from what the extractor could decompose: a changed module with only
    # module-level boundary reads still needs graph coverage.
    write(repo / "src" / "settings.py",
          "import os\n\nSTATE_ROOT = os.environ.get('APP_STATE_ROOT')\nHOME = os.environ.get('APP_HOME')\n")
    write(repo / "src" / "other.py", "import os\n\n\ndef untouched():\n    return os.environ.get('APP_CACHE')\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "settings")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    partial = graph_evidence(base, head, ("src/other.py",))
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(partial))
    rule = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert any("src/settings.py" in gap for gap in rule["completeness"]["gaps"]), rule
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "incomplete"})

    full = graph_evidence(base, head, ("src/other.py", "src/settings.py"))
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(full))
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "passed"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_partition_roles_discriminate_bare_except_and_finally(repo: Path) -> None:
    # Partition role is lifecycle identity: rollback on error only (bare
    # except) and rollback always (finally) never group, even with matching
    # operation sequences and empty headers.
    scaffold = (
        "def test_{n}_cleanup():\n"
        "    write_marker('r', 'armed')\n"
        "    stage = prepare('cfg')\n"
        "    try:\n"
        "        apply(stage)\n"
        "    {clause}:\n"
        "        rollback(stage)\n"
    )
    write(repo / "tests" / "test_left.py", scaffold.format(n="guarded", clause="except"))
    write(repo / "tests" / "test_right.py", scaffold.format(n="always", clause="finally"))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "cleanups")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    bound = graph_evidence(base, head, ("tests/test_left.py", "tests/test_right.py"))
    assert_no_lifecycle_candidates(repo, base, bound)


@with_repo
def test_a_dead_helper_chain_activates_nothing(repo: Path) -> None:
    # Activation flows through nesting recursion into referenced defs, never
    # through references living only inside sibling definitions: a helper
    # referenced solely from an unreferenced sibling helper stays dead, so
    # unrelated outers carrying the same dead chain share no lifecycle.
    outer = (
        "def {name}(config):\n"
        "    def resolve_defaults():\n"
        "        root = os.environ.get('APP_STATE_ROOT')\n"
        "        home = os.environ.get('APP_HOME')\n"
        "        return root or home\n"
        "    def probe_state():\n"
        "        return resolve_defaults()\n"
        "    return {result}\n"
    )
    write(repo / "src" / "exporter.py", "import os\n\n\n" + outer.format(name="export_report", result="config['report']"))
    write(repo / "src" / "importer.py", "import os\n\n\n" + outer.format(name="import_report", result="config['import']"))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "dead chains")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    bound = graph_evidence(base, head, ("src/exporter.py", "src/importer.py"))
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(bound))
    candidates = [(item["region"]["evidenceClass"],
                   [(region["path"], region["displayLine"]) for region in item["region"]["regions"]])
                  for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert candidates == [], candidates
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "passed"})
    assert code == 0, (code, payload["errors"])


_RESOLVER_A = (
    "import os\n\n\ndef resolve_state_root():\n"
    "    override = os.environ.get('APP_STATE_ROOT')\n"
    "    return override or os.environ.get('APP_HOME', '/var') + '/state'\n"
)
_RESOLVER_FILES = ("src/state.py", "src/advisor.py")
_RESOLVER_B = (
    "import os\n\n\ndef advisor_state_dir():\n"
    "    root = os.environ.get('APP_STATE_ROOT') or os.environ.get('APP_HOME', '/var')\n"
    "    return root + '/advisor'\n"
)


def state_root_record(**fields) -> dict[str, object]:
    """The manifest-shaped record for the two-resolver fixture, one owner."""
    return {
        "ruleId": "QG54-OWNER-COMPETITION-PRODUCTION",
        "responsibilityKey": "app-state-root-location",
        "disposition": "same-responsibility",
        "repair": "consolidate",
        "owners": [
            {"path": "src/state.py", "symbol": "resolve_state_root"},
            {"path": "src/advisor.py", "symbol": "advisor_state_dir"},
        ],
        "parentRecord": "future3OOO/claude-skills#54 comment 5251048442",
        **fields,
    }


def stamp_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Issuance emulation: v1 schema stamped and each record's digest
    computed over its canonical content, unless the record already carries
    explicit (possibly wrong-on-purpose) trust fields."""
    stamped = []
    for record in records:
        record = {"schemaVersion": 1, **record}
        if "validationRoot" not in record:
            canonical = json.dumps(record, sort_keys=True)
            record["validationRoot"] = {
                "identifier": record.get("parentRecord", ""),
                "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            }
        stamped.append(record)
    return stamped


def write_disposition(repo: Path, records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Records at the fixed out-of-tree carrier: a git-dir path is never part
    of any candidate tree, so the provenance is structural."""
    stamped = stamp_records(records)
    located = run(["git", "rev-parse", "--git-path", "qg54-dispositions.json"], repo).stdout.strip()
    carrier = Path(located) if os.path.isabs(located) else repo / located
    carrier.parent.mkdir(parents=True, exist_ok=True)
    carrier.write_text(json.dumps({"records": stamped}), encoding="utf-8")
    return stamped


def graph_evidence(base: str, candidate: str, files: tuple = ()) -> Path:
    """Snapshot-bound external graph evidence OUTSIDE the evaluated repo,
    carrying caller/callee symbol results for the named files."""
    document = Path(tempfile.mkdtemp(prefix="graph-evidence-")) / "graph.json"
    document.write_text(json.dumps({
        "base": base, "candidate": candidate,
        "symbols": [{"name": Path(item).stem, "file": item, "callers": []} for item in files],
    }), encoding="utf-8")
    return document


def two_resolvers(repo: Path) -> tuple[str, str]:
    """The committed two-file competing-resolver fixture; returns (base, head)."""
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "src" / "advisor.py", _RESOLVER_B)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two resolvers")
    return (run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip(),
            run(["git", "rev-parse", "HEAD"], repo).stdout.strip())


@with_repo
def test_absent_graph_evidence_leaves_caller_callee_scope_unestablished(repo: Path) -> None:
    # Parent #54 decision (2026-08-12): an absent graph input cannot establish
    # complete caller/callee scope, and the snapshot index is not a
    # substitute. Bound evidence restores completeness; unbound or stale
    # evidence does not.
    base, head = two_resolvers(repo)

    code, payload, _ = run_gate(repo, "--base-ref", base)
    assert_exact_rules(payload, {
        "QG54-OWNER-COMPETITION-PRODUCTION": "incomplete",
        "QG54-OWNER-COMPETITION-TEST": "incomplete",
    })
    rule = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert any("graph evidence" in gap for gap in rule["completeness"]["gaps"]), rule
    assert code == 0, (code, payload["errors"])

    bound = graph_evidence(base, head, _RESOLVER_FILES)
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(bound))
    assert_exact_rules(payload, {
        "QG54-OWNER-COMPETITION-PRODUCTION": "finding",
        "QG54-OWNER-COMPETITION-TEST": "passed",
    })

    stale = graph_evidence(base, base)
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(stale))
    rule = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert rule["status"] == "incomplete", rule
    assert any("graph evidence" in gap for gap in rule["completeness"]["gaps"]), rule
    assert code == 0, (code, payload["errors"])


@with_repo
def test_scalar_relationship_values_are_not_graph_coverage(repo: Path) -> None:
    # A relationship key must hold the provider's list-shaped result: a null
    # or scalar value is malformed input, not caller/callee coverage. The
    # empty-list validity half lives in the bound-evidence cases above.
    base, head = two_resolvers(repo)
    malformed = Path(tempfile.mkdtemp(prefix="graph-evidence-")) / "graph.json"
    malformed.write_text(json.dumps({
        "base": base, "candidate": head,
        "symbols": [{"name": Path(item).stem, "file": item, "references": "invalid"}
                    for item in _RESOLVER_FILES],
    }), encoding="utf-8")
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(malformed))
    rule = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert any("no caller/callee symbol results" in gap for gap in rule["completeness"]["gaps"]), rule
    assert_exact_rules(payload, {
        "QG54-OWNER-COMPETITION-PRODUCTION": "incomplete",
        "QG54-OWNER-COMPETITION-TEST": "incomplete",
    })
    assert code == 0, (code, payload["errors"])


@with_repo
def test_ambiguous_same_named_definitions_are_a_gap_not_a_binding(repo: Path) -> None:
    # Two classes define resolve_root and a caller references the name, so
    # closure evidence cannot bind one definition silently: the owner
    # evidence names the ambiguity and the rule reads incomplete.
    write(repo / "src" / "state.py", (
        "import os\n\n\n"
        "class DiskState:\n"
        "    def resolve_root(self):\n"
        "        return os.environ.get('APP_STATE_ROOT')\n\n\n"
        "class MemoryState:\n"
        "    def resolve_root(self):\n"
        "        return '/tmp/memory-state'\n\n\n"
        "def open_state(state):\n"
        "    return state.resolve_root()\n"
    ))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "ambiguous resolvers")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    bound = graph_evidence(base, head, ("src/state.py",))
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(bound))
    rule = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert any("ambiguous same-named definitions referenced in closure: resolve_root" in gap
               for gap in rule["completeness"]["gaps"]), rule
    assert_exact_rules(payload, {
        "QG54-OWNER-COMPETITION-PRODUCTION": "incomplete",
        "QG54-OWNER-COMPETITION-TEST": "passed",
    })
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_superset_state_writer_still_competes_and_binds_once(repo: Path) -> None:
    # Pairwise environment anchors: a resolver reading a superset of another
    # resolver's keys still shares a pair and competes, and three shared
    # pairs yield exactly one finding, never one per pair.
    write(repo / "src" / "sweeper.py", (
        "import os\n\n\ndef sweep_state():\n"
        "    root = os.environ.get('APP_STATE_ROOT')\n"
        "    home = os.environ.get('APP_HOME')\n"
        "    cache = os.environ.get('APP_CACHE')\n"
        "    return root or home or cache\n"
    ))
    write(repo / "src" / "pruner.py", (
        "import os\n\n\ndef prune_state():\n"
        "    root = os.environ.get('APP_STATE_ROOT')\n"
        "    home = os.environ.get('APP_HOME')\n"
        "    cache = os.environ.get('APP_CACHE')\n"
        "    keep = os.environ.get('APP_TMP')\n"
        "    return root or home or cache or keep\n"
    ))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two writers")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    bound = graph_evidence(base, head, ("src/sweeper.py", "src/pruner.py"))
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json", str(bound))
    candidates = owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert [item["state"] for item in candidates] == ["candidate"], candidates
    regions = [(region["path"], region["displayLine"]) for region in candidates[0]["region"]["regions"]]
    assert regions == [("src/pruner.py", 4), ("src/sweeper.py", 4)], regions
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "finding"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_same_responsibility_disposition_confirms_the_candidate(repo: Path) -> None:
    # A parent-bound same-responsibility record turns the mechanical candidate
    # into confirmed-unresolved while both owners remain; the record itself
    # never resolves anything.
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "lib" / "advisor.py", _RESOLVER_B)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two resolvers")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write_disposition(repo, [state_root_record(
        base=base, candidate=head,
        owners=[{"path": "src/state.py", "symbol": "resolve_state_root"},
                {"path": "lib/advisor.py", "symbol": "advisor_state_dir"}],
    )])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    confirmed = [
        item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
        if item["state"] == "confirmed-unresolved"
    ]
    assert len(confirmed) == 1, json.dumps(payload["findings"], indent=2)
    finding = confirmed[0]
    assert finding["evidence"]["responsibilityKey"] == "app-state-root-location", finding
    assert finding["evidence"]["repair"] == "consolidate", finding
    assert {region["path"] for region in finding["region"]["regions"]} == {"src/state.py", "lib/advisor.py"}, finding
    # The confirmed finding owns the pair: the bare mechanical candidate must
    # not also stay active for the same owners.
    assert [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")] == ["confirmed-unresolved"]
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    assert code == 0 and payload["ok"] is True, (code, payload["errors"])


@with_repo
def test_rename_only_repair_leaves_the_warning_unresolved(repo: Path) -> None:
    # Renaming the competitor deletes the superseded anchor without deleting
    # the competition: a surviving mechanical candidate still names the
    # survivor, so the same-responsibility record must not read as resolved.
    # One top directory keeps owner discovery complete, so only the surviving
    # candidate can block resolution here.
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "src" / "advisor.py", _RESOLVER_B)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two resolvers")
    base = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write(repo / "src" / "advisor.py", _RESOLVER_B.replace("advisor_state_dir", "advisor_root_dir"))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "rename the competitor")
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write_disposition(repo, [state_root_record(base=base, candidate=head)])
    code, payload, _ = run_gate(repo, "--base-ref", base,
                                "--gitnexus-context-json", str(graph_evidence(base, head, _RESOLVER_FILES)))
    assert payload["resolvedFindings"] == [], json.dumps(payload["resolvedFindings"], indent=2)
    states = sorted(item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION"))
    assert "confirmed-unresolved" in states, states
    assert code == 0, (code, payload["errors"])


@with_repo
def test_one_owner_repair_needs_a_parent_pinned_record_to_resolve(repo: Path) -> None:
    # One owner remains, the superseded surface is absent and unreferenced,
    # and scope is complete — the one-owner predicate holds and is published
    # as telemetry — but resolution silences a warning, so it additionally
    # requires a parent-pinned record (#54 decision, 2026-08-12); a
    # self-issued record leaves the finding active with rule incompleteness.
    # The pinned G and P2 replays prove the resolving polarity.
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "src" / "advisor.py", _RESOLVER_B)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two resolvers")
    base = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write(repo / "src" / "advisor.py", "from src.state import resolve_state_root\n\n\ndef advisor_dir():\n    return resolve_state_root() + '/advisor'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "consolidate onto the surviving owner")
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write_disposition(repo, [state_root_record(base=base, candidate=head)])
    code, payload, _ = run_gate(repo, "--base-ref", base,
                                "--gitnexus-context-json", str(graph_evidence(base, head, _RESOLVER_FILES)))
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    confirmed = [item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
                 if item["state"] == "confirmed-unresolved"]
    assert len(confirmed) == 1 and confirmed[0]["evidence"]["oneOwnerPredicate"] is True, confirmed
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("parent-pinned" in note for note in notes), notes
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "incomplete"})
    assert code == 0 and payload["ok"] is True, (code, payload["errors"])


@with_repo
def test_an_unpinned_distinct_authority_record_never_resolves(repo: Path) -> None:
    # Resolution trust is the shipped identifier+digest table: a
    # distinct-authority record whose digest is not parent-pinned is rejected
    # with a named note and the mechanical candidate stays active.
    write(repo / "src" / "reader.py", _RESOLVER_A)
    write(repo / "src" / "pruner.py", _RESOLVER_B)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "distinct authorities over shared state")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write_disposition(repo, [{
        "ruleId": "QG54-OWNER-COMPETITION-PRODUCTION",
        "responsibilityKey": "app-state-consumers",
        "disposition": "distinct-authority",
        "base": base,
        "candidate": head,
        "owners": [
            {"path": "src/reader.py", "symbol": "resolve_state_root"},
            {"path": "src/pruner.py", "symbol": "advisor_state_dir"},
        ],
        "parentRecord": "future3OOO/claude-skills#54 comment 5251048442",
    }])
    code, payload, _ = run_gate(repo, "--base-ref", base, "--gitnexus-context-json",
                                str(graph_evidence(base, head, ("src/reader.py", "src/pruner.py"))))
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    states = [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert states == ["candidate"], states
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("parent-pinned" in note for note in notes), notes
    assert code == 0 and payload["ok"] is True, (code, payload["errors"])


@with_repo
def test_stale_or_inapplicable_records_never_clear_a_candidate(repo: Path) -> None:
    # A record naming commits the evaluation did not evaluate, and a record
    # whose repair is meaningless for its disposition, are reported and
    # applied to nothing; the mechanical candidate stays active.
    base, head = two_resolvers(repo)
    write_disposition(repo, [
        state_root_record(base=base, candidate=base),
        state_root_record(disposition="distinct-authority", base=base, candidate=head),
    ])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    candidates = owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert [item["state"] for item in candidates] == ["candidate"], candidates
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("stale" in note for note in notes), notes
    assert any("meaningless for distinct-authority" in note for note in notes), notes
    assert code == 0, (code, payload["errors"])


@with_repo
def test_candidate_tree_disposition_files_are_never_read(repo: Path) -> None:
    # Candidate-authored provenance is not trust: the gate reads records only
    # from the fixed out-of-tree carrier, so a records-shaped file inside the
    # evaluated tree — even one named like the carrier — has no effect.
    base, head = two_resolvers(repo)
    smuggled = {"records": stamp_records([state_root_record(
        disposition="distinct-authority", repair=None, base=base, candidate=head)])}
    smuggled["records"][0].pop("repair")
    write(repo / "qg54-dispositions.json", json.dumps(smuggled))
    code, payload, _ = run_gate(repo, "--base-ref", base)
    states = [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert states == ["candidate"], states
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    assert owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"] == []
    assert code == 0, (code, payload["errors"])


@with_repo
def test_truncated_owner_discovery_keeps_the_finding_unresolved(repo: Path) -> None:
    # Interface-level negative on the real skipped-scope path: discovery
    # observes one owner and stops before the second, so the rule is
    # incomplete and the record cannot resolve, however clean the diff looks.
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "src" / "huge.py", _OVERSIZED)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "one readable owner, one behind the bound")
    base = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write(repo / "src" / "state.py", _RESOLVER_A + "\n# widened\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "touch the readable owner")
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write_disposition(repo, [state_root_record(
        base=base, candidate=head,
        owners=[{"path": "src/state.py", "symbol": "resolve_state_root"},
                {"path": "src/huge.py", "symbol": "normalize_user_identifier"}],
    )])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "incomplete"})
    rule = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert any("huge.py" in gap for gap in rule["completeness"]["gaps"]), rule
    states = [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert "confirmed-unresolved" in states, states
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    assert code == 0, (code, payload["errors"])


@with_repo
def test_lifecycle_signature_discriminates_the_near_misses(repo: Path) -> None:
    # The five pinned-shape scaffolds group; an extra assert operation, a
    # prelude callee, callees nested in the payload slot, and extra lifecycle
    # callees each break the ordered signature and stay outside the group.
    near_misses = "\n".join((
        "def test_extra_assert_verdict():",
        "    def body(repo):",
        "        write(repo / 'src/extra.py', 'value = 9\\n')",
        "        code, payload = run_gate(repo)",
        "        assert code == 0",
        "        assert payload['ok'] is True",
        "        assert payload['errors'] == []",
        "    with_repo(body)",
        "",
        "",
        "def test_prelude_callee_verdict():",
        "    block = '\\n'.join(['value = 1', 'other = 2'])",
        "    def body(repo):",
        "        write(repo / 'src/prelude.py', block)",
        "        code, payload = run_gate(repo)",
        "        assert code == 0",
        "        assert payload['ok'] is True",
        "    with_repo(body)",
        "",
        "",
        "def test_payload_callee_verdict():",
        "    def body(repo):",
        "        write(repo / 'src/joined.py', '\\n'.join('v = %d' % i for i in range(3)))",
        "        code, payload = run_gate(repo)",
        "        assert code == 0",
        "        assert payload['ok'] is True",
        "    with_repo(body)",
        "",
        "",
        "def test_extra_lifecycle_callee_verdict():",
        "    def body(repo):",
        "        write(repo / 'src/extra2.py', 'value = 2\\n')",
        "        snapshot_paths(repo)",
        "        code, payload = run_gate(repo)",
        "        assert code == 0",
        "        assert payload['ok'] is True",
        "    with_repo(body)",
        "",
    ))
    write(repo / "tests" / "test_lifecycle.py", _LIFECYCLE_SCAFFOLDS + "\n\n" + near_misses)
    code, payload, _ = run_gate(repo)
    candidates = owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")
    assert len(candidates) == 1, json.dumps(payload["findings"], indent=2)
    owners = [region["owner"] for region in candidates[0]["region"]["regions"]]
    assert owners == [
        "test_alpha_verdict", "test_beta_verdict", "test_gamma_verdict",
        "test_delta_verdict", "test_epsilon_verdict",
    ], owners
    assert code == 0, (code, payload["errors"])


@with_repo
def test_parameterized_single_lifecycle_owner_is_negative(repo: Path) -> None:
    # Centralized setup with one parameterized owner is the resolved shape:
    # one lifecycle owner, one loop over rows, no candidate.
    consolidated = "\n".join((
        "_ROWS = (('a.py', 'value = 1', 0), ('b.py', 'value = 2', 2))",
        "",
        "",
        "def _scenario_row(repo, path, payload, expected):",
        "    write(repo / path, payload)",
        "    code, verdict = run_gate(repo)",
        "    assert code == expected",
        "    assert verdict['ok'] is (expected == 0)",
        "",
        "",
        "def test_every_scenario_row():",
        "    for path, payload, expected in _ROWS:",
        "        with_repo(lambda repo: _scenario_row(repo, path, payload, expected))",
        "",
    ))
    write(repo / "tests" / "test_rows.py", consolidated)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "one parameterized owner")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    head = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    code, payload, _ = run_gate(repo, "--base-ref", base,
                                "--gitnexus-context-json", str(graph_evidence(base, head, ("tests/test_rows.py",))))
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-TEST": "passed"})
    assert owner_findings(payload, "QG54-OWNER-COMPETITION-TEST") == [], payload["findings"]
    assert code == 0, (code, payload["errors"])


@with_repo
def test_two_validators_deciding_one_invariant_are_a_candidate(repo: Path) -> None:
    # Two predicate-shaped functions deciding the same comparison in two
    # files compete to own the invariant; same names with no shared decision,
    # callee, or boundary evidence never form a candidate.
    validator = (
        "def ensure_ready(record):\n"
        "    if record.status != 'ready':\n"
        "        raise ValueError(record.status)\n"
        "    return record\n"
    )
    write(repo / "src" / "intake.py", validator)
    write(repo / "src" / "dispatch.py", validator.replace("ensure_ready", "require_ready").replace("return record", "return True"))
    write(repo / "src" / "alpha.py", "def handler(value):\n    return value.strip()\n")
    write(repo / "src" / "beta.py", "def handler(value):\n    return [item for item in value]\n")
    code, payload, _ = run_gate(repo)
    candidates = owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    validators = [item for item in candidates if item["region"]["evidenceClass"] == "invariant-validators"]
    assert len(validators) == 1, json.dumps(payload["findings"], indent=2)
    assert {region["owner"] for region in validators[0]["region"]["regions"]} == {"ensure_ready", "require_ready"}, validators
    assert [item for item in candidates if item["region"]["evidenceClass"] == "interface-overlap"] == [], candidates
    assert code == 0, (code, payload["errors"])


@with_repo
def test_temporary_coexistence_is_v2_territory_and_stays_active(repo: Path) -> None:
    # Parent amendment (#54 issuecomment-5259793024): v1 is exactly the
    # pinned field set and the tracked follow-up/expiry slice is v2
    # territory, so a temporary-coexistence claim leaves the candidate
    # active -- bare, or carrying the wider fields.
    base, head = two_resolvers(repo)
    record = state_root_record(disposition="temporary-coexistence", base=base, candidate=head)
    write_disposition(repo, [record])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("not expressible in schema v1" in note for note in notes), notes
    assert [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")] == ["candidate"]

    write_disposition(repo, [{**record, "followUp": "future3OOO/claude-skills#88", "expiry": "one slice"}])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("outside the pinned schema-v1 set: expiry, followUp" in note for note in notes), notes
    assert [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")] == ["candidate"]
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    assert code == 0, (code, payload["errors"])


@with_repo
def test_deletion_without_rewiring_stays_unresolved(repo: Path) -> None:
    # Deleting the competitor and its call sites without rewiring the
    # affected surface to the survivor merely deletes behavior; resolution
    # requires every affected path to reach the survivor.
    caller = "from src.advisor import advisor_state_dir\n\n\ndef locate():\n    return advisor_state_dir()\n"
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "src" / "advisor.py", _RESOLVER_B)
    write(repo / "src" / "caller.py", caller)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two resolvers and a caller")
    base = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    record = state_root_record(base=base)

    write(repo / "src" / "advisor.py", "ADVISOR_SUFFIX = '/advisor'\n")
    write(repo / "src" / "caller.py", "def locate():\n    return '/var/state/advisor'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "delete without rewiring")
    dropped = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write_disposition(repo, [{**record, "candidate": dropped}])
    code, payload, _ = run_gate(repo, "--base-ref", base,
                                "--gitnexus-context-json", str(graph_evidence(base, dropped, ("src/state.py", "src/advisor.py", "src/caller.py"))))
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    dropped_confirmed = [item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
                         if item["state"] == "confirmed-unresolved"]
    assert dropped_confirmed and dropped_confirmed[0]["evidence"]["oneOwnerPredicate"] is False, dropped_confirmed

    write(repo / "src" / "caller.py", "from src.state import resolve_state_root\n\n\ndef locate():\n    return resolve_state_root() + '/advisor'\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "rewire to the survivor")
    rewired = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    write_disposition(repo, [{**record, "candidate": rewired}])
    code, payload, _ = run_gate(repo, "--base-ref", base,
                                "--gitnexus-context-json", str(graph_evidence(base, rewired, ("src/state.py", "src/advisor.py", "src/caller.py"))))
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    confirmed = [item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
                 if item["state"] == "confirmed-unresolved"]
    assert confirmed and confirmed[0]["evidence"]["oneOwnerPredicate"] is True, confirmed
    assert code == 0, (code, payload["errors"])


@with_repo
def test_signature_preserves_command_discriminators(repo: Path) -> None:
    # Git subcommands, CLI-mode flags, and nested control-flow shape are
    # operation discriminators: scaffolds differing only there never share a
    # lifecycle signature, while payload strings stay normalized slots.
    scaffolds = "\n".join((
        "def run_gate(repo, *args):",
        "    return 0, {}",
        "",
        "",
        "def git(repo, *args):",
        "    return repo",
        "",
        "",
        "def test_adds():",
        "    git('r', 'add')",
        "    code, payload = run_gate('r')",
        "    assert code == 0",
        "",
        "",
        "def test_commits():",
        "    git('r', 'commit')",
        "    code, payload = run_gate('r')",
        "    assert code == 0",
        "",
        "",
        "def test_worktree_mode():",
        "    git('r', 'add')",
        "    code, payload = run_gate('r', '--staged-only')",
        "    assert code == 0",
        "",
        "",
        "def test_looped():",
        "    for attempt in range(2):",
        "        git('r', 'add')",
        "        code, payload = run_gate('r')",
        "    assert code == 0",
        "",
        "",
        "def test_ready_state():",
        "    write_marker('r', 'ready')",
        "    code, payload = run_gate('r')",
        "    assert code == 0",
        "    assert payload == {}",
        "",
        "",
        "def test_failed_state():",
        "    write_marker('r', 'failed')",
        "    code, payload = run_gate('r')",
        "    assert code == 0",
        "    assert payload == {}",
        "",
        "",
        "def test_guarded_cleanup():",
        "    write_marker('r', 'armed')",
        "    try:",
        "        code, payload = run_gate('r')",
        "    except OSError:",
        "        rollback('r')",
        "    assert code == 0",
        "",
        "",
        "def test_guarded_report():",
        "    write_marker('r', 'armed')",
        "    try:",
        "        code, payload = run_gate('r')",
        "    except OSError:",
        "        report('r')",
        "    assert code == 0",
        "",
        "",
        "def test_waits_ready():",
        "    write_marker('r', 'armed')",
        "    while probe('r'):",
        "        code, payload = run_gate('r')",
        "    assert code == 0",
        "",
        "",
        "def test_waits_settled():",
        "    write_marker('r', 'armed')",
        "    while settled('r'):",
        "        code, payload = run_gate('r')",
        "    assert code == 0",
        "",
    ))
    write(repo / "tests" / "test_modes.py", scaffolds)
    code, payload, _ = run_gate(repo)
    lifecycle = [
        item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")
        if item["region"]["evidenceClass"] == "fixture-lifecycle"
    ]
    # Bare payload words under an ordinary callee stay normalized value
    # slots: the ready/failed pair is the one group the discriminators leave.
    assert len(lifecycle) == 1, json.dumps([item["evidence"]["owners"] for item in lifecycle], indent=2)
    owners = [region["owner"] for region in lifecycle[0]["region"]["regions"]]
    assert owners == ["test_ready_state", "test_failed_state"], owners
    assert code == 0, (code, payload["errors"])


@with_repo
def test_disposition_trust_negatives_leave_the_rule_incomplete(repo: Path) -> None:
    # Unknown schema versions, broken digests, and path-only wildcard owner
    # references cannot bind; each is reported and the rule reads incomplete,
    # never clean, with the candidate untouched.
    base, head = two_resolvers(repo)
    record = state_root_record(base=base, candidate=head)
    write_disposition(repo, [
        {**record, "schemaVersion": 2},
        {**record, "validationRoot": {"identifier": "future3OOO/claude-skills#54", "digest": "0" * 64}},
        {**record, "owners": [{"path": "src/state.py"}, {"path": "src/advisor.py"}]},
        {**record, "owners": [{"path": "src/state.py", "symbol": "resolve_state_root"},
                              {"path": "src/advisor.py", "symbol": "vanished_resolver"}]},
    ])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    for expected in ("unknown disposition schema version", "does not match the validation root digest",
                     "symbol or exact content anchor", "resolve nowhere"):
        assert any(expected in note for note in notes), (expected, notes)
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "incomplete"})
    states = [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert states == ["candidate"], states
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_non_string_rule_id_is_rejected_not_a_crash(repo: Path) -> None:
    # A record whose ruleId is not a string is malformed input the gate must
    # survive: the record reads rejected, the candidate stays untouched, and
    # the gate still emits its verdict.
    base, head = two_resolvers(repo)
    record = state_root_record(base=base, candidate=head)
    write_disposition(repo, [{**record, "ruleId": ["QG54-OWNER-COMPETITION-PRODUCTION"]}])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("unknown ruleId" in note for note in notes), notes
    states = [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert states == ["candidate"], states
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_shapeless_survivor_is_rejected_not_a_crash(repo: Path) -> None:
    # A survivor without the owner-reference shape is malformed input the
    # gate must survive: the record reads rejected, the candidate stays
    # untouched, and the gate still emits its verdict.
    base, head = two_resolvers(repo)
    write_disposition(repo, [state_root_record(base=base, candidate=head, survivor={})])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("survivor" in note for note in notes), notes
    states = [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert states == ["candidate"], states
    assert code == 0, (code, payload["errors"])


@with_repo
def test_an_undecodable_carrier_is_reported_not_a_crash(repo: Path) -> None:
    # A carrier that exists but does not decode is damaged input, not an
    # absent one: the gate reports the ignored carrier, the rule reads
    # incomplete, and the verdict is still emitted.
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "src" / "advisor.py", _RESOLVER_B)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "two resolvers")
    base = run(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    located = run(["git", "rev-parse", "--git-path", "qg54-dispositions.json"], repo).stdout.strip()
    carrier = Path(located) if os.path.isabs(located) else repo / located
    carrier.write_bytes(bytes([255, 254]) + b'{"records": []}')
    code, payload, _ = run_gate(repo, "--base-ref", base)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("dispositions carrier ignored" in note for note in notes), notes
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "incomplete"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_an_unreadable_carrier_is_reported_not_absent(repo: Path) -> None:
    # Only a missing carrier is absent: a carrier that exists but cannot be
    # read is a capture failure the owner rules must surface, never a silent
    # no-records run that drops real dispositions.
    base, head = two_resolvers(repo)
    write_disposition(repo, [state_root_record(base=base, candidate=head)])
    located = run(["git", "rev-parse", "--git-path", "qg54-dispositions.json"], repo).stdout.strip()
    carrier = Path(located) if os.path.isabs(located) else repo / located
    carrier.chmod(0o000)
    try:
        code, payload, _ = run_gate(repo, "--base-ref", base)
    finally:
        carrier.chmod(0o644)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("dispositions carrier ignored" in note for note in notes), notes
    assert_exact_rules(payload, {"QG54-OWNER-COMPETITION-PRODUCTION": "incomplete"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_duplicate_record_reference_binds_nothing(repo: Path) -> None:
    # The v1 contract rejects duplicate references: the same stamped record
    # twice on the carrier yields exactly one confirmed transition and a
    # duplicate-reference note for the second copy.
    base, head = two_resolvers(repo)
    record = state_root_record(base=base, candidate=head)
    write_disposition(repo, [record, record])
    code, payload, _ = run_gate(repo, "--base-ref", base)
    states = [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")]
    assert states == ["confirmed-unresolved"], states
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("duplicate record reference rejected" in note for note in notes), notes
    assert code == 0, (code, payload["errors"])


@with_repo
def test_an_unknown_disposition_value_is_rejected(repo: Path) -> None:
    write(repo / "src" / "state.py", _RESOLVER_A)
    write(repo / "src" / "advisor.py", _RESOLVER_B)
    write_disposition(repo, [state_root_record(disposition="waived", parentRecord="prose")])
    code, payload, _ = run_gate(repo)
    notes = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["evidence"]["records"]
    assert any("semantic disposition" in note for note in notes), notes
    assert [item["state"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")] == ["candidate"]
    assert code == 0, (code, payload["errors"])


@with_repo
def test_duplicate_identity_survives_insertion_and_rename(repo: Path) -> None:
    # Content anchors, not positions: an unrelated line above a region and a
    # path rename must not orphan a disposition against the same debt.
    write(repo / "tests" / "left_helpers.py", DUPLICATE_HELPER + "\n")
    write(repo / "tests" / "right_helpers.py", DUPLICATE_HELPER + "\n")
    payload_before = run_gate(repo, "--base-ref", "HEAD")[1]
    assert_exact_rules(payload_before, {"QG54-DUPLICATE-ADDED-SYMBOL": "finding"})
    before = duplicate_findings(payload_before, "QG54-DUPLICATE-ADDED-SYMBOL")[0]
    write(repo / "tests" / "left_helpers.py", "UNRELATED = 1\n\n\n" + DUPLICATE_HELPER + "\n")
    (repo / "tests" / "right_helpers.py").rename(repo / "tests" / "renamed_helpers.py")
    payload_after = run_gate(repo, "--base-ref", "HEAD")[1]
    after = duplicate_findings(payload_after, "QG54-DUPLICATE-ADDED-SYMBOL")[0]
    assert before["findingId"] == after["findingId"], (before["findingId"], after["findingId"])
    assert before["region"]["contentAnchor"] == after["region"]["contentAnchor"]
    assert {region["path"] for region in after["region"]["regions"]} == {
        "tests/left_helpers.py", "tests/renamed_helpers.py",
    }, after["region"]["regions"]
    assert_exact_rules(payload_after, {"QG54-DUPLICATE-ADDED-SYMBOL": "finding"})


@with_repo
def test_owner_outside_the_changed_tree_reports_incomplete_not_clean(repo: Path) -> None:
    # Owner capture is bounded to the changed top directories. An owner the
    # bound never read is unread scope, not proof the copy is unique, so the
    # baseline rule reports what it did not read instead of passing.
    write(repo / "lib" / "owner.py", DUPLICATE_HELPER + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner outside the changed tree")
    write(repo / "src" / "copy.py", DUPLICATE_HELPER + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    baseline_rule = check_named(payload, "QG54-DUPLICATE-BASELINE")
    assert baseline_rule["passed"] is None and baseline_rule["status"] == "incomplete", baseline_rule
    assert any("reuse baseline scope" in gap for gap in baseline_rule["gaps"]), baseline_rule
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_retained_owner_claims_a_repeated_block_from_the_added_rule(repo: Path) -> None:
    # One occurrence, one defect: when a repeated added block is also an exact
    # copy of an owner the base tree still holds, the baseline rule owns it and
    # the added-block rule must not report the same copies again.
    write(repo / "tests" / "owner_helpers.py", f"def owner(config, attempt):\n{RETRY_BLOCK}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    for name in ("first", "second"):
        write(repo / "tests" / f"{name}_helpers.py", f"def {name}(config, attempt):\n{RETRY_BLOCK}\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert not duplicate_findings(payload, "QG54-DUPLICATE-ADDED-BLOCK"), json.dumps(duplicate_findings(payload), indent=2)
    assert_exact_rules(payload, {
        "QG54-DUPLICATE-BASELINE": "finding", "QG54-DUPLICATE-ADDED-BLOCK": "passed",
    })
    baseline = duplicate_findings(payload, "QG54-DUPLICATE-BASELINE")
    assert len(baseline) == 1, json.dumps(duplicate_findings(payload), indent=2)
    # Exact multiplicity, not a set: one physical occurrence, one region.
    assert [
        (region["path"], region["displayLine"], region["evidenceRole"])
        for region in baseline[0]["region"]["regions"]
    ] == [
        ("tests/first_helpers.py", 2, "duplicate"),
        ("tests/owner_helpers.py", 2, "retained-baseline"),
        ("tests/second_helpers.py", 2, "duplicate"),
    ], baseline[0]["region"]["regions"]
    assert code == 0, (code, payload["errors"])


@with_repo
def test_decorated_definitions_are_not_exact_duplicates(repo: Path) -> None:
    # A decorator changes how the definition below it behaves, so it belongs to
    # the implementation. Identical bodies under different decorators are not
    # one symbol; under the same decorator they are.
    body = "\n".join((
        "        ceiling = int(config.get('max_attempts', 3))",
        "        remaining = max(0, ceiling - attempt)",
        "        if not remaining:",
        "            return 0.0",
        "        scaled = round(0.5 * (2 ** attempt), 3)",
        "        return scaled",
    ))
    write(repo / "src" / "left.py", f"class L:\n    @property\n    def budget(self, config, attempt):\n{body}\n")
    write(repo / "src" / "right.py", f"class R:\n    @staticmethod\n    def budget(self, config, attempt):\n{body}\n")
    _, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert not duplicate_findings(payload, "QG54-DUPLICATE-ADDED-SYMBOL"), json.dumps(duplicate_findings(payload), indent=2)
    assert_exact_rules(payload, {"QG54-DUPLICATE-ADDED-SYMBOL": "passed"})

    # A decorator that spans lines is still one decorator: only the real
    # parser knows where its list begins.
    write(repo / "src" / "left.py", f"class L:\n    @retry(\n        attempts=3,\n    )\n    def budget(self, config, attempt):\n{body}\n")
    write(repo / "src" / "right.py", f"class R:\n    @retry(\n        attempts=9,\n    )\n    def budget(self, config, attempt):\n{body}\n")
    _, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert not duplicate_findings(payload, "QG54-DUPLICATE-ADDED-SYMBOL"), json.dumps(duplicate_findings(payload), indent=2)
    assert_exact_rules(payload, {"QG54-DUPLICATE-ADDED-SYMBOL": "passed"})

    write(repo / "src" / "left.py", f"class L:\n    @property\n    def budget(self, config, attempt):\n{body}\n")
    write(repo / "src" / "right.py", f"class R:\n    @property\n    def budget(self, config, attempt):\n{body}\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    findings = duplicate_findings(payload, "QG54-DUPLICATE-ADDED-SYMBOL")
    assert len(findings) == 1, json.dumps(duplicate_findings(payload), indent=2)
    # The named region starts at the decorator, not at the def below it.
    assert {(region["path"], region["displayLine"]) for region in findings[0]["region"]["regions"]} == {
        ("src/left.py", 2), ("src/right.py", 2),
    }, findings[0]
    assert_exact_rules(payload, {"QG54-DUPLICATE-ADDED-SYMBOL": "finding"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_block_never_spans_a_symbol_boundary(repo: Path) -> None:
    # Three body lines and three module-level lines are identical in both
    # files, but each side of the symbol's closing edge is under the reported
    # minimum. Only a block that ran past that edge would reach six lines.
    body = "\n".join((
        "    resolved = normalize_identifier(value)",
        "    combined = resolved.strip().lower()",
        "    return combined or 'id_unknown'",
    ))
    tail = "\n".join((
        "REGISTERED_PREFIXES = ('id_', 'ref_', 'key_')",
        "DEFAULT_PREFIX = REGISTERED_PREFIXES[0]",
        "FALLBACK_IDENTIFIER = DEFAULT_PREFIX + 'unknown'",
    ))
    for name in ("first", "second"):
        write(repo / "src" / f"{name}.py", f"def build_{name}(value):\n{body}\n\n\n{tail}\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert not duplicate_findings(payload), json.dumps(duplicate_findings(payload), indent=2)
    assert_exact_rules(payload, dict.fromkeys(EXACT_RULES, "passed"))
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_renamed_copy_of_a_retained_owner_is_still_a_copy(repo: Path) -> None:
    # The signature line alone cannot hide a copy: the implementation under it
    # is the owner's, and the owner is still retained. The body must itself
    # reach the reported minimum, which is the same threshold as any region.
    write(repo / "tests" / "owner_helpers.py", f"def resolve_retry_budget(config, attempt):\n{RETRY_BLOCK}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    write(repo / "tests" / "copy_helpers.py", f"def compute_retry_budget(config, attempt):\n{RETRY_BLOCK}\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    findings = duplicate_findings(payload, "QG54-DUPLICATE-BASELINE")
    assert len(findings) == 1, json.dumps(duplicate_findings(payload), indent=2)
    assert duplicate_regions(findings[0]) == {
        ("tests/copy_helpers.py", "duplicate"),
        ("tests/owner_helpers.py", "retained-baseline"),
    }, findings[0]
    assert_exact_rules(payload, {"QG54-DUPLICATE-BASELINE": "finding"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_a_reformatted_signature_does_not_hide_a_copied_body(repo: Path) -> None:
    # The copy renames the helper AND splits its signature across lines. Only
    # the implementation under the signature is the owner's, so the body-only
    # candidate must start at the suite, not one line below the `def`.
    write(repo / "tests" / "owner_helpers.py", f"def resolve_retry_budget(config, attempt):\n{RETRY_BLOCK}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    write(
        repo / "tests" / "copy_helpers.py",
        f"def compute_retry_budget(\n    config,\n    attempt,\n):\n{RETRY_BLOCK}\n",
    )
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    findings = duplicate_findings(payload, "QG54-DUPLICATE-BASELINE")
    assert len(findings) == 1, json.dumps(duplicate_findings(payload), indent=2)
    # The copy's region points at the first line of its suite, not into the
    # signature that spans lines 1 to 4.
    assert [
        (region["path"], region["displayLine"]) for region in findings[0]["region"]["regions"]
    ] == [("tests/copy_helpers.py", 5), ("tests/owner_helpers.py", 2)], findings[0]["region"]["regions"]
    assert_exact_rules(payload, {"QG54-DUPLICATE-BASELINE": "finding"})
    assert code == 0, (code, payload["errors"])


def _nested_decorator_helper(decorator: str) -> str:
    return (
        "def build_pipeline(config, attempt):\n"
        f"    @{decorator}\n"
        "    def inner(value):\n"
        "        return value.strip().lower()\n"
        "    resolved = inner(config['name'])\n"
        "    scaled = round(0.5 * (2 ** attempt), 3)\n"
        "    combined = resolved + str(scaled)\n"
        "    return combined\n"
    )


@with_repo
def test_a_nested_decorator_belongs_to_the_body_that_contains_it(repo: Path) -> None:
    # The suite of an outer definition begins at its first statement INCLUDING
    # that statement's own decorators. Two bodies differing only in a nested
    # decorator are not the same implementation.
    write(repo / "tests" / "owner_helpers.py", _nested_decorator_helper("staticmethod"))
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    write(
        repo / "tests" / "copy_helpers.py",
        _nested_decorator_helper("functools.cache").replace("build_pipeline", "assemble_pipeline"),
    )
    _, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert not duplicate_findings(payload), json.dumps(duplicate_findings(payload), indent=2)
    assert_exact_rules(payload, dict.fromkeys(EXACT_RULES, "passed"))

    # The same nested decorator is the same implementation, and is reported.
    write(
        repo / "tests" / "copy_helpers.py",
        _nested_decorator_helper("staticmethod").replace("build_pipeline", "assemble_pipeline"),
    )
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    findings = duplicate_findings(payload, "QG54-DUPLICATE-BASELINE")
    assert len(findings) == 1, json.dumps(duplicate_findings(payload), indent=2)
    assert [region["displayLine"] for region in findings[0]["region"]["regions"]] == [2, 2], findings[0]
    assert_exact_rules(payload, {"QG54-DUPLICATE-BASELINE": "finding"})
    assert code == 0, (code, payload["errors"])


@with_repo
def test_test_baseline_files_cannot_spend_the_production_read_budget(repo: Path) -> None:
    # Owner capture covers three roles, so one shared read budget would let
    # whichever role sorts first exhaust it. Here 4,000 eligible test files sort
    # before src/, and the production owner must still be read and scored.
    owner = f"def normalize_user_identifier(config, attempt):\n{RETRY_BLOCK}\n"
    bulk = repo / "apitests"
    bulk.mkdir(parents=True, exist_ok=True)
    # Exactly the enforced cap, read from the shipping constant so the fixture
    # tracks the contract it exists to exercise rather than restating it.
    snapshot = (SCRIPT_DIR / "_quality_gate" / "snapshot.py").read_text(encoding="utf-8")
    budget = int(next(l for l in snapshot.splitlines() if l.startswith("MAX_INDEX_FILES")).split("=")[1])
    for index in range(budget):
        (bulk / f"test_{index:05d}.py").write_text(f"VALUE_{index} = {index}\n", encoding="utf-8")
    write(repo / "src" / "owner.py", owner)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "baseline")
    write(repo / "src" / "copy.py", owner)
    write(repo / "apitests" / "test_new.py", "def helper():\n    return 1\n")
    _, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    baseline_copies = duplicate_findings(payload, "QG54-DUPLICATE-BASELINE")
    assert len(baseline_copies) == 1, json.dumps(payload["findings"], indent=2)
    assert ("src/owner.py", "retained-baseline") in duplicate_regions(baseline_copies[0]), baseline_copies


@with_repo
def test_a_test_owner_gap_does_not_dirty_the_production_owner_rule(repo: Path) -> None:
    # Owner discovery is role-scoped: a test owner the capture bound never
    # read must not make the production owner rule's verdict unknown, while
    # the rules that do read that scope keep the gap visible.
    owner = f"def normalize_user_identifier(config, attempt):\n{RETRY_BLOCK}\n"
    write(repo / "tests" / "huge_helpers.py", _OVERSIZED)
    write(repo / "src" / "owner.py", owner)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "baseline")
    write(repo / "src" / "copy.py", owner)
    write(repo / "tests" / "new_helpers.py", "def helper():\n    return 1\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert_exact_rules(payload, {
        "QG54-DUPLICATE-BASELINE": "incomplete",
        "QG54-OWNER-COMPETITION-TEST": "incomplete",
        "QG54-OWNER-COMPETITION-PRODUCTION": "incomplete",
    })
    baseline_rule = check_named(payload, "QG54-DUPLICATE-BASELINE")
    assert any("huge_helpers.py" in gap for gap in baseline_rule["gaps"]), baseline_rule
    # Role separation shows in the gap sets: the unread test owner dirties the
    # TEST rule while the production rule carries only the universal graph gap.
    assert any("huge_helpers.py" in gap for gap in owner_rule_finding(payload, "QG54-OWNER-COMPETITION-TEST")["completeness"]["gaps"])
    production_gaps = owner_rule_finding(payload, "QG54-OWNER-COMPETITION-PRODUCTION")["completeness"]["gaps"]
    assert not any("huge_helpers.py" in gap for gap in production_gaps), production_gaps
    # The exact retained copy is owner-competition evidence with no duplicate
    # prerequisite, and every rule here stays warning-only.
    assert owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION"), payload["findings"]
    assert code == 0, (code, payload["errors"])


@with_repo
def test_unreadable_owner_file_cannot_report_a_clean_exact_verdict(repo: Path) -> None:
    # The copied helper's only owner sits in a base file no tokenizer can
    # read. Skipping it quietly would report the copy as unique.
    (repo / "tests").mkdir()
    (repo / "tests" / "owner_helpers.py").write_bytes(
        (DUPLICATE_HELPER + "\n").encode("utf-8") + b"\x00\x00trailing\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "unreadable owner")
    write(repo / "tests" / "copy_helpers.py", DUPLICATE_HELPER + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    baseline_rule = check_named(payload, "QG54-DUPLICATE-BASELINE")
    assert baseline_rule["passed"] is None and baseline_rule["status"] == "incomplete", baseline_rule
    assert any("tests/owner_helpers.py" in gap for gap in baseline_rule["gaps"]), baseline_rule
    assert code == 0, (code, payload["errors"])


@with_repo
def test_truncated_baseline_capture_cannot_report_a_clean_exact_verdict(repo: Path) -> None:
    # The second owner sits behind a capture bound the run never reads. A rule
    # that never read it has not proven the copy is unique.
    write(repo / "tests" / "huge_helpers.py", _OVERSIZED)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "oversized owner")
    write(repo / "tests" / "copy_helpers.py", DUPLICATE_HELPER + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    baseline_rule = check_named(payload, "QG54-DUPLICATE-BASELINE")
    assert baseline_rule["passed"] is None and baseline_rule["status"] == "incomplete", baseline_rule
    assert any("huge_helpers.py" in gap for gap in baseline_rule["gaps"]), baseline_rule
    assert code == 0, (code, payload["errors"])


SPLIT_LINES = (
    "    resolved = normalize_identifier(candidate_value) + normalize_identifier(fallback_value)",
    "    combined = resolved.strip().lower().replace('-', '_').replace(' ', '_')",
    "    trimmed = combined[:64] if len(combined) > 64 else combined",
    "    prefixed = trimmed if trimmed.startswith('id_') else f'id_{trimmed}'",
    "    checked = prefixed.replace('__', '_').rstrip('_')",
    "    return checked or 'id_unknown'",
)


@with_repo
def test_separate_hunks_do_not_form_one_duplicate(repo: Path) -> None:
    # The six lines exist intact in one file and split across two distant
    # hunks in another. Only joining those hunks makes them look duplicated.
    # Both files stay parseable, or the rules would report unread scope instead.
    filler = "\n".join(f"    keep_{i} = {i}" for i in range(8))
    write(repo / "src" / "split.py", f"def spread(candidate_value, fallback_value):\n    x = 1\n{filler}\n    return x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "split baseline")
    write(
        repo / "src" / "split.py",
        "def spread(candidate_value, fallback_value):\n    x = 1\n"
        + "\n".join(SPLIT_LINES[:3]) + f"\n{filler}\n"
        + "\n".join(SPLIT_LINES[3:]) + "\n    return x\n",
    )
    write(repo / "src" / "intact.py", "def build_identifier(candidate_value, fallback_value):\n" + "\n".join(SPLIT_LINES) + "\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert_exact_rules(payload, dict.fromkeys(EXACT_RULES, "passed"))
    assert code == 0, json.dumps(payload["errors"], indent=2)


# Every way scope can go missing: the affected rule reports incomplete and
# names the gap, its projections drop to unknown, and error-class capture
# failures fail the run outright. "*" sweeps all checks and every hard rule
# except the two not_evaluated policy keys, which evaluate caller input only
# and are legitimately untouched by capture gaps.
#
# name, git config, baseline files, candidate files (bytes stay unmeasured
# binary), staged, gate args, expectations.
_BINARY = b"def ok() -> int:\n    return 1\n\x00\x00binary\n"
_UNREADABLE_OWNER = "def normalize_user_identifier(value):\n    return value.strip().lower()\n"
_OVERSIZED = _UNREADABLE_OWNER + "\n".join(f"# pad {i}" * 6 for i in range(9000))
_SCOPE_ROWS = (
    ("unknown-numstat", None, {}, {"src/base.py": _BINARY}, False, (),
     {"code": 0, "growth": "src/base.py"}),
    ("skipped-oversized-baseline", None, {"src/huge.py": _OVERSIZED},
     {"src/dup.py": _UNREADABLE_OWNER}, False, (),
     {"code": 0, "owner": "huge.py",
      "warning": "QG54-ANALYSIS-INCOMPLETE for QG54-OWNER-COMPETITION-PRODUCTION",
      "checks": ("QG54-OWNER-COMPETITION-PRODUCTION",)}),
    ("unmeasured-production-file", None, {}, {"src/base.py": _BINARY}, False, ("--base-ref", "HEAD"),
     {"code": 0, "growth": "src/base.py", "owner": "src/base.py",
      "checks": ("QG54-OWNER-COMPETITION-PRODUCTION", "no-quality-escapes",
                 "QG54-DUPLICATE-ADDED-SYMBOL", "QG54-DUPLICATE-ADDED-BLOCK")}),
    ("unbased-run", None, {}, {"src/app.py": "VALUE = 1\n"}, False, (),
     {"code": 0, "growth": "no caller-supplied base", "warning": "QG54-GROWTH-CUMULATIVE",
      "evalGap": "no caller-supplied base"}),
    ("missing-base-ref", None, {}, {"src/app.py": "VALUE = 1\n"}, False, ("--base-ref", "deadbeef"),
     {"code": 2, "error": "base-ref not found", "growth": "", "checks": "*", "hardRules": "*"}),
    # A clean filter that emits different bytes on every read stages different
    # content per capture pass: the gate must report drift, never evaluate a
    # state that never existed.
    ("capture-drift", ("filter.drift.clean", "sh -c 'cat >/dev/null; date +%s%N'"), {},
     {".gitattributes": "drifty.txt filter=drift\n", "drifty.txt": "content the filter rewrites every read\n"},
     False, ("--base-ref", "HEAD"),
     {"code": 2, "error": "capture drift", "evalGap": "capture drift", "checksNotTrue": True}),
    # A rejected repo-level diff config fails every diff read with empty
    # output while base resolution and capture stay healthy; read that failure
    # as "" and every rule passes over a change nobody looked at.
    ("failed-diff-read", ("diff.algorithm", "bogus"), {},
     {"src/app.py": "def one():\n    return 1\n"}, True, ("--base-ref", "HEAD"),
     {"code": 2, "error": "read failed", "evalGap": "", "checksNotTrue": True}),
)


def test_missing_scope_never_reads_as_a_clean_pass() -> None:
    for name, config, baseline, candidate, staged, args, expect in _SCOPE_ROWS:
        in_repo(lambda repo, cf=config, b=baseline, c=candidate, s=staged, a=args, e=expect, label=name:
                _scope_row(repo, cf, b, c, s, a, e, label))


def _scope_row(repo, config, baseline, candidate, staged, args, expect, name: str) -> None:
    if config:
        git(repo, "config", *config)
    for path, text in baseline.items():
        write(repo / path, text)
    if baseline:
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "baseline")
    for path, content in candidate.items():
        if isinstance(content, bytes):
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_bytes(content)
        else:
            write(repo / path, content)
    if staged:
        git(repo, "add", ".")
    code, payload, _ = run_gate(repo, *args)
    assert code == expect["code"], (name, code, payload["errors"])
    assert payload["ok"] is (code == 0), (name, payload["errors"])
    if "error" in expect:
        assert any(expect["error"] in item for item in payload["errors"]), (name, payload["errors"])
    if "warning" in expect:
        assert any(expect["warning"] in item for item in payload["warnings"]), (name, payload["warnings"])
    for rule, finding_of in (
        ("growth", growth_finding),
        ("owner", lambda p: owner_rule_finding(p, "QG54-OWNER-COMPETITION-PRODUCTION")),
    ):
        if rule in expect:
            finding = finding_of(payload)
            assert finding["status"] == "incomplete", (name, rule, finding)
            assert finding["completeness"]["complete"] is False, (name, rule, finding)
            assert any(expect[rule] in gap for gap in finding["completeness"]["gaps"]), (name, rule, finding)
    checks = expect.get("checks", ())
    if checks == "*":
        checks = [item["name"] for item in payload["checks"]]
    for check_name in checks:
        item = check_named(payload, check_name)
        assert item["passed"] is None and item["status"] == "incomplete", (name, item)
    hard_rules = expect.get("hardRules", ())
    if hard_rules == "*":
        hard_rules = [key for key in payload["hardRules"] if key not in ("consequenceCoverage", "noDuplication")]
    for rule_name in hard_rules:
        rule = payload["hardRules"][rule_name]
        assert rule["status"] == "incomplete" and rule["passed"] is None, (name, rule_name, rule)
    if expect.get("checksNotTrue"):
        for item in payload["checks"]:
            assert item["passed"] is not True, (name, item)
    if "evalGap" in expect:
        assert payload["evaluation"]["complete"] is False, (name, payload["evaluation"])
        assert any(expect["evalGap"] in gap for gap in payload["evaluation"]["gaps"]), (name, payload["evaluation"]["gaps"])


# Each row is one decoder or transport branch Git can put in front of the
# gate: a path the decoder mishandles must never silently drop out of the
# measured change.
#
# name, git config, baseline files, candidate files, expected production
# growth, expected error substring, expected sample path.
_DECODER_ROWS = (
    ("c-quoted-tab", None, {"src/we\tird.py": "A = 1\n"}, {"src/we\tird.py": "A = 1\nB = 2\n"},
     {"added": 1, "deleted": 0, "net": 1}, None, None),
    ("literal-leading-quote", None, {}, {'src/"weird.py': "A = 1\nB = 2\n"},
     {"added": 2, "deleted": 0, "net": 2}, None, None),
    ("non-utf8-with-quotepath-off", ("core.quotePath", "false"), {},
     {b"src/we\tir\xe9.py": b"def f():\n    return 1\n"},
     {"added": 2, "deleted": 0, "net": 2}, None, b"src/we\tir\xe9.py"),
    ("leading-whitespace-dir", None, {" pad/app.py": "A = 1\n"},
     {" pad/app.py": "A = 1\n<<<<<<< theirs\nB = 2\n=======\nC = 3\n>>>>>>> ours\n"},
     None, "merge conflict markers", b" pad/app.py"),
    ("control-char-payload", None, {},
     {"src/ctl.py": "Z = 'a\x0bb'  # " + "TO" + "DO: hidden after a control character\n"},
     None, "quality escapes", None),
    # A lossy decode would make the blob unaddressable; the unread file must
    # never read as a clean pass, and its five escape lines stay measured.
    ("non-utf8-escape-payload", None, {},
     {b"src/caf\xe9.py": b"def f():\n    try:\n        g()\n    except Exception:\n        pass\n"},
     {"added": 5, "deleted": 0, "net": 5}, "quality escapes", None),
)


def test_every_decoder_branch_stays_fully_measured() -> None:
    for name, config, baseline, candidate, growth, error, sample in _DECODER_ROWS:
        in_repo(lambda scratch, c=config, b=baseline, n=candidate, g=growth, e=error, s=sample, label=name:
                _decoder_row(scratch, c, b, n, g, e, s, label))


def _decoder_row(repo: Path, config, baseline: dict, candidate: dict, growth, error, sample, name: str) -> None:
    if config:
        git(repo, "config", *config)
    for path, content in baseline.items():
        write(repo / path, content)
    if baseline:
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "baseline")
    for path, content in candidate.items():
        target = repo / (os.fsdecode(path) if isinstance(path, bytes) else path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    git(repo, "add", "-A")
    code, payload, stderr = run_gate(repo, "--base-ref", "HEAD")

    if error:
        assert any(error in item for item in payload["errors"]), (name, payload["errors"])
        assert code == 2, (name, code, stderr)
    else:
        assert payload["errors"] == [], (name, payload["errors"])
        assert code == 0, (name, code, stderr)
    if growth is not None:
        assert growth_totals(payload)["production"] == growth, (name, growth_totals(payload))
        # The odd name is unusual, not unmeasurable: it contributes no gap of
        # its own — only the universal graph-evidence gap may remain.
        assert all("graph evidence" in gap for gap in payload["evaluation"]["gaps"]), (name, payload["evaluation"]["gaps"])
    if sample is not None:
        encoded = [item.encode("utf-8", "surrogateescape") for item in payload["changedFilesSample"]]
        assert sample in encoded, (name, payload["changedFilesSample"])


# Growth accounting per row: which bucket counts each change, deletions and
# staged-deletion-plus-recreation measured rather than vanishing, and
# intermediate-only content neither leaking into escape rules nor
# double-counting.
# name, baseline files, ops after the baseline commit, candidate files,
# base-bound, expected buckets, clean check that must stay green.
_GROWTH_ROWS = (
    ("deleting-a-production-file-counts-as-deletions",
     {"src/legacy.py": "\n".join(f"OLD_{i} = {i}" for i in range(40)) + "\n"},
     (("unlink", "src/legacy.py"),), {}, True,
     {"production": {"added": 0, "deleted": 40, "net": -40}}, None),
    ("staged-deletion-with-unstaged-recreation-measures-the-candidate",
     {"src/thing.py": "OLD_A = 1\nOLD_B = 2\nOLD_C = 3\n"},
     (("rm", "src/thing.py"),),
     {"src/thing.py": "NEW_A = 1\nNEW_B = 2\nNEW_C = 3\nNEW_D = 4\n"}, True,
     {"production": {"added": 4, "deleted": 3, "net": 1}}, None),
    ("each-role-counts-separately", {}, (),
     {"src/app.py": "\n".join(f"VALUE_{i} = {i}" for i in range(10)) + "\n",
      "tests/test_app.py": "\n".join(f"def test_{i}():\n    assert {i} == {i}" for i in range(4)) + "\n",
      "tests/fixtures/sample.py": "SAMPLE = {'a': 1}\n"}, True,
     {"production": {"added": 10, "deleted": 0, "net": 10}, "test": {"added": 8, "deleted": 0, "net": 8},
      "testSupport": {"added": 1, "deleted": 0, "net": 1},
      "humanAuthored": {"added": 19, "deleted": 0, "net": 19}}, None),
    ("generated-and-non-source-stay-out-of-human-authored", {}, (),
     {"src/real.py": "REAL = 1\nREAL_TWO = 2\n",
      "src/generated/client.py": "\n".join(f"GEN_{i} = {i}" for i in range(30)) + "\n",
      "src/payload.schema.json": '{"type": "object"}\n',
      "docs/notes.md": "# notes\n\nprose\n"}, False,
     {"production": {"added": 2, "deleted": 0, "net": 2}, "generated": {"added": 30, "deleted": 0, "net": 30},
      "humanAuthored": {"added": 2, "deleted": 0, "net": 2}}, None),
    ("intermediate-commits-do-not-leak", {},
     (("commit", {"src/base.py": "def ok() -> int:  # TO" + "DO: temporary\n    return 1\n"}),),
     {"src/base.py": "def ok() -> int:\n    return 2\n"}, True,
     {"production": {"added": 1, "deleted": 1, "net": 0}}, "no-quality-escapes"),
)


def test_growth_accounting_holds_for_every_bucket() -> None:
    for name, baseline, ops, candidate, based, buckets, clean_check in _GROWTH_ROWS:
        in_repo(lambda repo, b=baseline, o=ops, c=candidate, bb=based, x=buckets, k=clean_check, label=name:
                _growth_row(repo, b, o, c, bb, x, k, label))


def _growth_row(repo, baseline, ops, candidate, based, buckets, clean_check, name: str) -> None:
    for path, text in baseline.items():
        write(repo / path, text)
    if baseline:
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "baseline")
    base = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    for op, arg in ops:
        if op == "unlink":
            (repo / arg).unlink()
        elif op == "rm":
            git(repo, "rm", "-q", arg)
        else:
            for path, text in arg.items():
                write(repo / path, text)
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "intermediate")
    for path, text in candidate.items():
        write(repo / path, text)
    code, payload, _ = run_gate(repo, *(("--base-ref", base) if based else ()))
    growth = growth_totals(payload)
    for bucket, expected in buckets.items():
        assert growth[bucket] == expected, (name, bucket, growth)
    if clean_check:
        assert check_named(payload, clean_check)["passed"] is True, (name, check_named(payload, clean_check))
    assert code == 0, (name, code, payload["errors"])


def replay_pinned_range(base: str, candidate: str, *args: str,
                        records: list[dict[str, object]] | None = None) -> dict[str, object]:
    """Run the gate over one pinned historical range in a throwaway detached
    worktree. Every corpus replay owns this one add/run/remove lifecycle.

    Cleanup runs only after a successful worktree add: removing a worktree
    that never registered raises its own error and would mask the setup
    failure it is cleaning up after. Registered-worktree leaks into the
    shared source repository must still surface, so that removal is unguarded.
    """
    repo = source_repo()
    replay = Path(tempfile.mkdtemp(prefix="pinned-corpus-")) / "candidate"
    added = False
    try:
        git(repo, "worktree", "add", "-q", "--detach", str(replay), candidate)
        added = True
        if records is not None:
            write_disposition(replay, records)
        return run_gate(replay, "--base-ref", base, *args)[1]
    finally:
        if added:
            git(repo, "worktree", "remove", "--force", str(replay))
        shutil.rmtree(replay.parent, ignore_errors=True)


def canonical_diff_sha256(repo: Path, base: str, candidate: str) -> str:
    """The pinned-fixture identity hash: the canonical diff options are part
    of each corpus identity, and changing one requires a parent re-pin."""
    diff = run(
        [
            "git",
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
            "-c", "core.quotePath=true",
            "-c", "diff.indentHeuristic=true",
            "-c", "diff.suppressBlankEmpty=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--diff-algorithm=myers",
            "--no-renames",
            "--unified=3",
            "--inter-hunk-context=0",
            "--abbrev=7",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--line-prefix=",
            "--submodule=short",
            "--ignore-submodules=none",
            "-O/dev/null",
            base,
            candidate,
        ],
        repo,
    )
    assert diff.returncode == 0, diff.stderr
    return hashlib.sha256(diff.stdout.encode("utf-8")).hexdigest()


def test_captured_round_six_corpus_reports_pinned_totals() -> None:
    base, candidate = CORPUS_BASE, CORPUS_CANDIDATE
    repo = source_repo()
    for sha in (base, candidate):
        present = run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo)
        assert present.returncode == 0, f"corpus commit {sha} missing from local history"
    digest = canonical_diff_sha256(repo, base, candidate)
    assert digest == CORPUS_DIFF_SHA256, digest

    payload = replay_pinned_range(
        CORPUS_BASE, CORPUS_CANDIDATE,
        "--gitnexus-context-json", str(range_graph_evidence(repo, CORPUS_BASE, CORPUS_CANDIDATE)))
    growth = growth_totals(payload)
    assert growth["production"] == {"added": 481, "deleted": 8, "net": 473}, growth
    assert growth["test"] == {"added": 648, "deleted": 0, "net": 648}, growth
    assert growth["testSupport"] == {"added": 0, "deleted": 0, "net": 0}, growth
    assert growth["humanAuthored"] == {"added": 1129, "deleted": 8, "net": 1121}, growth
    assert growth_finding(payload)["completeness"] == {"complete": True, "gaps": []}
    # The corpus adds new committed files; their absent baselines are not
    # discovery failures, so every rule must still read complete.
    for rule in ("QG54-OWNER-COMPETITION-PRODUCTION", "QG54-OWNER-COMPETITION-TEST"):
        owner_rule = owner_rule_finding(payload, rule)
        assert owner_rule["completeness"] == {"complete": True, "gaps": []}, owner_rule
    # The exact rules carry their own corpus verdict in the calibration
    # replay below, including the shell scope no tokenizer can read.
    assert all(
        item["status"] != "incomplete"
        for item in payload["checks"]
        if not item["name"].startswith("QG54-DUPLICATE-")
    ), payload["checks"]


_MANIFEST_R = ("02ebe4c3a9163497f81d05364f2d1b5624477bd6", "29e355ea3d73e5631914a1376c7ba68a64e5711e",
               "40b6f27617e593a8b89e5b722982c834f348ed9e3eaf876ff9e47876814db830")
_MANIFEST_G = ("65f14318cb94d995dcfe961a09eb1e4dbe374dd1", "08074c7e727d26ce62b0a3f80899de76e34818ef",
               "854db8efcb9c9ffaf8efc26bb42475cf7bfde155567a3cbfca5a0e23919c5c0b")
_MANIFEST_TESTFILE = "skills/production-code/scripts/test_code_quality_gate.py"
_MANIFEST_KEY = "quality-gate:cleanup-verdict-scenario-lifecycle"
_MANIFEST_FIVE = (
    "test_bare_noqa_is_still_a_quality_escape", "test_js_ts_escapes_fail", "test_python_escapes_fail",
    "test_test_any_annotations_do_not_fail_cleanup", "test_test_fake_green_escapes_still_fail",
)
_MANIFEST_PARENT = "future3OOO/claude-skills#54 comment 5251048442"
# The parent decision of 2026-08-12 binds every state-changing disposition
# record by its canonical content digest; a record cannot mint its own root.
_PINNED_RECORD_DIGESTS = {
    "R": "08f61bed0d5df8b9435a38b1fb1712530bebb063d7c9b457dbe85770f97a016e",
    "P1": "d7bda52e9bff988face173e92467cc2db78d159c1564f2817075b4cd1c195de8",
    "P2": "3e96fd97af71111fc5e724f457ca5b3f32ef79fdd4d0a7a25e635ce600a0b39c",
    "G": "6c2fdd01db924618efc9df048884b2ef64082d5d254657e6fae4d47c92d15575",
}


def assert_pinned_digest(stamped: list[dict[str, object]], case: str, index: int = 0) -> None:
    """The replayed record must be byte-identical to the parent-pinned one."""
    root = stamped[index]["validationRoot"]
    assert root["digest"] == _PINNED_RECORD_DIGESTS[case], (case, root)
_P1_SHELL_ANCHOR = 'state_dir="${DEVIN_WORKFLOW_STATE_ROOT:-${DEVIN_ESTATE_HOME:-$HOME/.config/devin}/state}/_advisor-sessions"'


def range_graph_evidence(repo: Path, base: str, candidate: str) -> Path:
    changed = run(["git", "diff", "--name-only", base, candidate], repo).stdout.splitlines()
    return graph_evidence(base, candidate, tuple(
        item for item in changed if item.endswith((".py", ".sh", ".js", ".ts"))
    ))


def _lifecycle_record(base: str, candidate: str, survivor: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "ruleId": "QG54-OWNER-COMPETITION-TEST", "responsibilityKey": _MANIFEST_KEY,
        "disposition": "same-responsibility", "repair": "consolidate",
        "base": base, "candidate": candidate,
        "owners": [{"path": _MANIFEST_TESTFILE, "symbol": name} for name in _MANIFEST_FIVE],
        **({"survivor": survivor} if survivor else {}),
        "parentRecord": _MANIFEST_PARENT,
    }


def _active_states(payload: dict[str, object], rule: str) -> list[str]:
    return [item["state"] for item in owner_findings(payload, rule)]


def test_owner_manifest_calibration_is_reproducible() -> None:
    # The parent-pinned owner manifest (#54 comment 5251048442), verbatim:
    # cases R, P1, P2, and G over three canonical historical diffs. Every
    # pinned anchor is adjudicated here and every additional mechanical
    # candidate is counted as outside-pinned-scope for parent #54 — never
    # silently adjudicated, added to the corpus, or read as a failure.
    published = (SCRIPT_DIR.parent / "references" / "owner-calibration.md").read_text(encoding="utf-8")
    repo = source_repo()
    for base, candidate, digest in (_MANIFEST_R, (CORPUS_BASE, CORPUS_CANDIDATE, CORPUS_DIFF_SHA256), _MANIFEST_G):
        for sha in (base, candidate):
            assert run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], repo).returncode == 0, sha
        assert canonical_diff_sha256(repo, base, candidate) == digest, (base, candidate)
        for pinned in (base, candidate, digest):
            assert pinned in published, pinned
    assert "unexaminedCount = 0" in published

    # Case R without records: candidate generation is independent of both
    # duplicate detection and dispositions, and the pinned five-group appears
    # exactly, in region order, among the counted candidates.
    payload = replay_pinned_range(*_MANIFEST_R[:2])
    test_candidates = owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")
    five_groups = [
        item for item in test_candidates
        if [region["owner"] for region in item["region"]["regions"]] == list(_MANIFEST_FIVE)
    ]
    assert len(five_groups) == 1, [item["evidence"]["owners"] for item in test_candidates]
    assert five_groups[0]["region"]["evidenceClass"] == "fixture-lifecycle", five_groups[0]
    assert len(test_candidates) == 4 and not owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION"), (
        [item["evidence"]["owners"] for item in test_candidates])

    # Case R with the record: repeated-scaffolding RED confirms and stays
    # active; the bare five-candidate retires into the confirmed finding.
    r_records = [_lifecycle_record(*_MANIFEST_R[:2])]
    assert_pinned_digest(stamp_records(r_records), "R")
    payload = replay_pinned_range(*_MANIFEST_R[:2], records=r_records)
    states = _active_states(payload, "QG54-OWNER-COMPETITION-TEST")
    assert sorted(states) == ["candidate"] * 3 + ["confirmed-unresolved"], states
    confirmed = [item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")
                 if item["state"] == "confirmed-unresolved"]
    assert [region["owner"] for region in confirmed[0]["region"]["regions"]] == list(_MANIFEST_FIVE), confirmed
    assert confirmed[0]["evidence"]["responsibilityKey"] == _MANIFEST_KEY, confirmed
    assert payload["resolvedFindings"] == [], payload["resolvedFindings"]

    # Cases P1 and P2 over the captured round-six corpus: partial
    # consolidation stays confirmed-unresolved while distinct authority over
    # the same marker data transitions directly to resolved telemetry.
    corpus_records = [
        {"ruleId": "QG54-OWNER-COMPETITION-PRODUCTION",
         "responsibilityKey": "workflow-state-root-location",
         "disposition": "same-responsibility", "repair": "consolidate",
         "base": CORPUS_BASE, "candidate": CORPUS_CANDIDATE,
         "owners": [{"path": "hooks/lib/state_store.py", "symbol": "state_root"},
                    {"path": "skills/codex-advisor/scripts/ask-codex-advisor.sh", "content": _P1_SHELL_ANCHOR}],
         "parentRecord": _MANIFEST_PARENT},
        {"ruleId": "QG54-OWNER-COMPETITION-PRODUCTION",
         "responsibilityKey": "session-association-marker-consumption",
         "disposition": "distinct-authority",
         "base": CORPUS_BASE, "candidate": CORPUS_CANDIDATE,
         "owners": [{"path": "hooks/lib/state_store.py", "symbol": "session_associations"},
                    {"path": "hooks/lib/state_prune.py", "symbol": "_retire_associations"}],
         "parentRecord": _MANIFEST_PARENT},
    ]
    assert_pinned_digest(stamp_records(corpus_records), "P1", 0)
    assert_pinned_digest(stamp_records(corpus_records), "P2", 1)
    payload = replay_pinned_range(
        CORPUS_BASE, CORPUS_CANDIDATE,
        "--gitnexus-context-json", str(range_graph_evidence(source_repo(), CORPUS_BASE, CORPUS_CANDIDATE)),
        records=corpus_records)
    for rule in ("QG54-OWNER-COMPETITION-PRODUCTION", "QG54-OWNER-COMPETITION-TEST"):
        assert owner_rule_finding(payload, rule)["completeness"] == {"complete": True, "gaps": []}, rule
    production = _active_states(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
    assert sorted(production) == ["candidate"] * 5 + ["confirmed-unresolved"], production
    confirmed = [item for item in owner_findings(payload, "QG54-OWNER-COMPETITION-PRODUCTION")
                 if item["state"] == "confirmed-unresolved"]
    assert confirmed[0]["evidence"]["responsibilityKey"] == "workflow-state-root-location", confirmed
    assert {region["path"] for region in confirmed[0]["region"]["regions"]} == {
        "hooks/lib/state_store.py", "skills/codex-advisor/scripts/ask-codex-advisor.sh"}, confirmed
    assert [(item["evidence"]["responsibilityKey"], item["state"]) for item in payload["resolvedFindings"]] == [
        ("session-association-marker-consumption", "resolved")], payload["resolvedFindings"]
    assert _active_states(payload, "QG54-OWNER-COMPETITION-TEST") == ["candidate"] * 8, (
        [item["evidence"]["owners"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")])

    # Case G: consolidate-and-delete resolves. The repo-context packet names
    # the whole base tree so owner discovery is complete — the widening
    # direction the incompleteness action prescribes, through an existing
    # input, never an exclusion knob.
    listing = run(["git", "ls-tree", "-r", "--name-only", _MANIFEST_G[0]], repo).stdout
    packet = Path(tempfile.mkdtemp(prefix="owner-packet-")) / "packet.txt"
    packet.write_text(listing, encoding="utf-8")
    g_records = [
        _lifecycle_record(*_MANIFEST_G[:2], survivor={"path": _MANIFEST_TESTFILE, "symbol": "_escape_row"}),
    ]
    assert_pinned_digest(stamp_records(g_records), "G")
    payload = replay_pinned_range(*_MANIFEST_G[:2],
                                  "--repo-context-packet", str(packet),
                                  "--gitnexus-context-json", str(range_graph_evidence(source_repo(), *_MANIFEST_G[:2])),
                                  records=g_records)
    assert [(item["evidence"]["responsibilityKey"], item["state"]) for item in payload["resolvedFindings"]] == [
        (_MANIFEST_KEY, "resolved")], payload["resolvedFindings"]
    assert _active_states(payload, "QG54-OWNER-COMPETITION-TEST") == ["candidate"] * 4, (
        [item["evidence"]["owners"] for item in owner_findings(payload, "QG54-OWNER-COMPETITION-TEST")])
    assert not any(_MANIFEST_KEY in warning for warning in payload["warnings"]), payload["warnings"]
    # The published outside-pinned-scope counts are these measured volumes.
    for count in ("| R | 3 |", "| P1/P2 | 13 |", "| G | 4 |"):
        assert count in published, count


def test_captured_corpus_duplicate_calibration_is_reproducible() -> None:
    # The checked-in calibration is evidence only if it is re-derived. The
    # fires, their regions, the unreadable scopes and the warning projection
    # come from replaying the pinned corpus through the real CLI; the pinned
    # identities and the threshold are bound by assertion below. The bound-two
    # adjudication is a recorded measurement and is deliberately not replayed.
    published = (SCRIPT_DIR.parent / "references" / "duplicate-calibration.md").read_text(encoding="utf-8")
    for pinned in (CORPUS_BASE, CORPUS_CANDIDATE, CORPUS_DIFF_SHA256, "unexaminedCount = 0"):
        assert pinned in published, pinned
    # The published threshold and the shipped constant cannot drift apart.
    shipped = (SCRIPT_DIR / "_quality_gate" / "redundancy.py").read_text(encoding="utf-8")
    threshold = next(line for line in shipped.splitlines() if line.startswith("MIN_REGION_LINES"))
    assert threshold.replace(" ", "") == "MIN_REGION_LINES=6", threshold
    assert "`MIN_REGION_LINES = 6`" in published, "the document must name the shipped threshold"

    payload = replay_pinned_range(CORPUS_BASE, CORPUS_CANDIDATE)

    fires: dict[str, list[list[tuple[str, int]]]] = {
        rule: [] for rule in
        ("QG54-DUPLICATE-ADDED-SYMBOL", "QG54-DUPLICATE-ADDED-BLOCK", "QG54-DUPLICATE-BASELINE")
    }
    for finding in duplicate_findings(payload):
        fires[finding["ruleId"]].append(
            [(region["path"], region["displayLine"]) for region in finding["region"]["regions"]]
        )
    assert fires == {
        "QG54-DUPLICATE-ADDED-SYMBOL": [],
        "QG54-DUPLICATE-ADDED-BLOCK": [[
            ("hooks/tests/test_state_prune.py", 50),
            ("hooks/tests/test_state_prune.py", 395),
        ]],
        "QG54-DUPLICATE-BASELINE": [],
    }, json.dumps(fires, indent=2)
    # Every fire and every unreadable scope is named in the published
    # adjudication; an unexamined one would be a silent regression.
    unreadable = [
        "hooks/tests/run.sh",
        "skills/codex-advisor/scripts/ask-codex-advisor.sh",
        "skills/codex-advisor/tests/test-ask-codex-advisor.sh",
    ]
    for rule in fires:
        state = check_named(payload, rule)
        assert state["status"] == "incomplete" and state["passed"] is None, state
        assert [gap.split(":")[0] for gap in state["gaps"]] == unreadable, state["gaps"]
        for path in unreadable:
            assert path in published, path
    for finding in duplicate_findings(payload):
        assert finding["severity"] == "warning" and finding["passed"] is True, finding
        for region in finding["region"]["regions"]:
            assert f"{region['path']}:{region['displayLine']}" in published, region
    assert not any("QG54-DUPLICATE-" in error for error in payload["errors"]), payload["errors"]


@with_repo
def test_explicit_base_is_evaluated_as_the_commit_the_caller_supplied(repo: Path) -> None:
    # Base selection belongs to the caller; this Module captures the base it is
    # given. When the supplied base is not an ancestor of HEAD, resolving it to
    # anything else drops the very difference the caller asked about.
    write(repo / "src" / "shared.py", "SHARED = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "shared")
    fork = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    # The caller's chosen base drops shared.py and carries a file of its own;
    # a merge-base reading sees neither, and can never report a deletion.
    git(repo, "checkout", "-q", "-b", "side")
    git(repo, "rm", "-q", "src/shared.py")
    write(repo / "src" / "sideonly.py", "SIDE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "side drops shared and adds its own")
    side = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    git(repo, "checkout", "-q", "-b", "feature", fork)
    write(repo / "src" / "feature.py", "FEATURE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "feature")

    for extra in ((), ("--staged-only",)):
        _, payload, _ = run_gate(repo, "--base-ref", side, *extra)
        base = payload["evaluation"]["base"]
        assert base["commit"] == side, (extra, base, fork)
        assert base["source"] == "caller", (extra, base)
        assert "src/shared.py" in payload["changedFilesSample"], (extra, payload["changedFilesSample"])
        assert "src/sideonly.py" in payload["changedFilesSample"], (extra, payload["changedFilesSample"])
        assert growth_totals(payload)["production"] == {"added": 2, "deleted": 1, "net": 1}, (extra, growth_totals(payload))


@with_repo
def test_skipped_baseline_scope_is_reported_not_silent(repo: Path) -> None:
    # An owner discovery never read cannot say "no reimplementation". A
    # baseline file over the size cap is skipped before its blob is read, and
    # the verdict names the unread scope instead of passing silently.
    write(repo / "src" / "huge.py", _OVERSIZED)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "oversized baseline")
    write(repo / "src" / "dup.py", _UNREADABLE_OWNER)
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert code == 0 and payload["ok"] is True, (code, payload["errors"])
    assert any("huge.py" in warning and "baseline" in warning for warning in payload["warnings"]), payload["warnings"]


@with_repo
def test_unmeasured_binary_source_change_is_never_silently_clean(repo: Path) -> None:
    # Git reports "-" counts for a file it treats as binary. The stored
    # measurement gap must reach the verdict as a visible warning — never a
    # silent clean pass — while the run stays warning-only with exit zero.
    (repo / "src" / "unmeasured.py").write_bytes(b"def ok() -> int:\n    return 1\n\x00\x00binary\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert code == 0 and payload["ok"] is True, (code, payload["errors"])
    assert any("no line counts" in warning for warning in payload["warnings"]), payload["warnings"]


@with_repo
def test_rename_only_change_keeps_preexisting_content_clean(repo: Path) -> None:
    # A pure rename must evaluate as it did before the captured-tree rewrite:
    # no added lines, so content that predates the change — even an escape
    # marker — is not newly introduced. An EDIT riding on the rename is still
    # evaluated at the new path.
    marker = "# TO" + "DO: predates the rename"
    write(repo / "src" / "old_name.py", f"{marker}\ndef f() -> int:\n    return 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "baseline with marker")
    git(repo, "mv", "src/old_name.py", "src/new_name.py")
    code, payload, stderr = run_gate(repo, "--base-ref", "HEAD")
    assert payload["errors"] == [], payload["errors"]
    assert code == 0, (code, stderr)

    text = (repo / "src" / "new_name.py").read_text(encoding="utf-8")
    write(repo / "src" / "new_name.py", text + "X = 1  # " + "FIX" + "ME later\n")
    code, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert code == 2, (code, payload["errors"])
    escapes = check_named(payload, "no-quality-escapes")
    assert any("src/new_name.py" in sample for sample in escapes["sample"]), escapes


@with_repo
def test_rename_detection_ignores_repository_rename_limits(repo: Path) -> None:
    # diff.renameLimit=1 makes Git skip exhaustive detection for two inexact
    # renames. The gate pins its own rename budget, so repository config can
    # never turn a rename into new content that resurrects old markers.
    marker = "# TO" + "DO: predates the rename"
    write(repo / "src" / "a.py", f"{marker} A\nA = 1\nAA = 2\n")
    write(repo / "src" / "b.py", f"{marker} B\nB = 1\nBB = 2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "baseline")
    git(repo, "config", "diff.renameLimit", "1")
    git(repo, "mv", "src/a.py", "src/a2.py")
    git(repo, "mv", "src/b.py", "src/b2.py")
    with (repo / "src" / "a2.py").open("a", encoding="utf-8") as handle:
        handle.write("A = 9\n")
    with (repo / "src" / "b2.py").open("a", encoding="utf-8") as handle:
        handle.write("B = 9\n")
    code, payload, stderr = run_gate(repo, "--base-ref", "HEAD")
    assert payload["errors"] == [], payload["errors"]
    assert code == 0, (code, stderr)


@with_repo
def test_staged_quoted_path_escape_is_evaluated(repo: Path) -> None:
    # A staged filename holding a tab arrives C-quoted on Git's line-based
    # name transports; the gate must decode it back to the literal path and
    # evaluate the staged content, or an escape inside it silently passes.
    marker = "# TO" + "DO: staged escape behind a quoted path"
    write(repo / "src" / "we\tird.py", f"{marker}\ndef f() -> int:\n    return 1\n")
    git(repo, "add", "-A")
    code, payload, stderr = run_gate(repo, "--base-ref", "HEAD", "--staged-only")
    assert code == 2, (code, payload["errors"], stderr)
    escapes = check_named(payload, "no-quality-escapes")
    assert any("src/we\tird.py" in sample for sample in escapes["sample"]), escapes


@with_repo
def test_snapshot_reads_the_captured_tree_not_the_moving_worktree(repo: Path) -> None:
    # Concurrent mutation between capture and evaluation cannot produce a mixed
    # snapshot: every byte comes from the captured candidate tree object.
    #
    # DELIBERATE PROOF-CLASS EXCEPTION, operator-approved: this drives the
    # internal capture/freeze Seam directly and is not claimed as public-CLI
    # RED/GREEN. The CLI captures and evaluates in one process, so the public
    # Interface offers no window in which to mutate between the two, and none
    # was added solely for testing.
    import sys

    sys.path.insert(0, str(SCRIPT_DIR))
    from _quality_gate.git_scope import collect_scope
    from _quality_gate.snapshot import EvaluationSnapshot

    write(repo / "src" / "base.py", "def ok() -> int:\n    return 99\n")
    scope = collect_scope(repo, "HEAD")
    write(repo / "src" / "base.py", "def mutated() -> int:\n    return -1\n")
    snapshot = EvaluationSnapshot.from_scope(repo, scope)
    entry = snapshot.entry("src/base.py")
    assert entry is not None
    assert entry.current_text == "def ok() -> int:\n    return 99\n", entry.current_text
    assert [text for _, text in entry.added_lines()] == ["    return 99"], entry.hunks
    assert entry.added == 1 and entry.deleted == 1, (entry.added, entry.deleted)


def test_detectors_cannot_read_git_or_the_filesystem_after_the_freeze() -> None:
    # DELIBERATE PROOF-CLASS EXCEPTION, operator-approved. This is structural
    # enforcement, not public-CLI RED/GREEN, and is not claimed as the latter:
    # detector reads run after the snapshot freezes, and the CLI captures and
    # evaluates in one process, so the public Interface offers no window in
    # which to observe such a read, and none was added solely for testing.
    detectors = ("checks.py", "redundancy.py", "symbols.py", "findings.py")
    banned_calls = {"run_git", "git_text", "git_read", "read_git_file", "open", "read_text", "read_bytes"}
    for name in detectors:
        tree = ast.parse((SCRIPT_DIR / "_quality_gate" / name).read_text(encoding="utf-8"))
        imported = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        } | {
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.Import) for alias in node.names
        }
        assert not {"git_scope", ".git_scope", "subprocess", "os"} & imported, (name, sorted(imported))
        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        assert not banned_calls & called, (name, sorted(banned_calls & called))

    snapshot_source = (SCRIPT_DIR / "_quality_gate" / "snapshot.py").read_text(encoding="utf-8")
    assert "def read_baseline" not in snapshot_source, "read_baseline is a detector-time Git read"
    class_def = next(
        node for node in ast.parse(snapshot_source).body
        if isinstance(node, ast.ClassDef) and node.name == "EvaluationSnapshot"
    )
    fields = {
        node.target.id
        for node in class_def.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "repo" not in fields, "EvaluationSnapshot must hold no repository handle after the freeze"


def test_dependency_manifests_classify_as_production() -> None:
    # Dependency pins are configuration, not prose, whatever their spelling: the
    # gate and the CI lane read one answer, and this is where it lives.
    marker = "GATE_CALLED_A_MANIFEST_DOCUMENTATION"
    module = _load_path_policy(SCRIPT_DIR / "_quality_gate" / "path_policy.py")
    for path in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt",
                 "requirements.dev.txt", "requirements/dev.txt", "constraints/base.txt"):
        found = module.classify_path(path)
        assert found.role == module.ROLE_PRODUCTION, f"{marker}: {path} is {found.role}"
        assert found.human_authored is True, f"{marker}: {path} is not human-authored"
    for path in ("docs/notes.md", "skills/guide.txt", "README.md"):
        assert module.classify_path(path).role == module.ROLE_DOCS, f"{marker}: {path}"
    # A name is a manifest by its literal bytes, not by what stripping it would
    # produce; Git persists and enumerates the whitespace, so the gate sees it.
    for path in (" requirements.txt", "requirements.txt ", "requirements.txt\t",
                 "requirements /dev.txt"):
        found = module.classify_path(path)
        assert found.role != module.ROLE_PRODUCTION, (
            f"WHITESPACE_NAME_READ_AS_MANIFEST: {path!r} is {found.role}"
        )


@with_repo
def test_a_manifest_change_counts_as_production(repo: Path) -> None:
    marker = "MANIFEST_CHANGE_NOT_COUNTED_AS_PRODUCTION"
    write(repo / "requirements.txt", "ruff==0.16.2\n")
    _, payload, _ = run_gate(repo)
    growth = payload["evaluation"]["growth"]
    assert growth["production"]["added"] >= 1, f"{marker}: {growth}"
    assert growth["humanAuthored"]["added"] >= 1, f"{marker}: {growth}"
    # The same question at the Seam Git actually presents: a committed name
    # carrying trailing whitespace, read back from the index, is not a manifest.
    module = _load_path_policy(SCRIPT_DIR / "_quality_gate" / "path_policy.py")
    write(repo / "requirements.txt ", "ruff==0.16.2\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    listed = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"],
                            check=True, capture_output=True).stdout
    tracked = [os.fsdecode(name) for name in listed.split(b"\0") if name]
    spaced = [name for name in tracked if name != name.strip()]
    assert spaced, f"{marker}: git did not keep the whitespace name"
    for name in spaced:
        assert module.classify_path(name).role != module.ROLE_PRODUCTION, (
            f"WHITESPACE_NAME_READ_AS_MANIFEST: {name!r} counted as a manifest"
        )


def test_full_history_test_like_classification_is_unchanged() -> None:
    # The standalone predicate workflow state loads must keep the exact
    # pre-snapshot truth table over every path that ever existed here. The
    # oracle is the real predicate shipped at the pinned pre-#75 commit, never
    # a copy of its regexes: a copied oracle can be wrong in exactly the way
    # the implementation is wrong and still agree with it.
    module = _load_path_policy(SCRIPT_DIR / "_quality_gate" / "path_policy.py")
    repo = source_repo()
    pinned = run(["git", "show", f"{PINNED_PRE_75}:{POLICY_PATH}"], repo)
    assert pinned.returncode == 0, f"pinned {PINNED_PRE_75} {POLICY_PATH} unreachable: {pinned.stderr}"
    pinned_dir = Path(tempfile.mkdtemp(prefix="pinned-path-policy-"))
    try:
        write(pinned_dir / "path_policy.py", pinned.stdout)
        reference = _load_path_policy(pinned_dir / "path_policy.py").is_test_like_path
    finally:
        shutil.rmtree(pinned_dir, ignore_errors=True)

    listed = run(["git", "log", "--all", "--name-only", "--format="], repo)
    paths = {line for line in listed.stdout.splitlines() if line.strip()}
    paths |= set(run(["git", "ls-files"], repo).stdout.splitlines())
    assert len(paths) > 100, "full-history path enumeration failed"
    for path in sorted(paths):
        expected = reference(path)
        assert module.is_test_like_path(path) is expected, path
        assert module.classify_path(path).test_like_compat is expected, path
    assert module.is_test_like_path("src/generated/client.py") is True
    assert module.is_test_like_path("api/payload.schema.json") is True
    # The stored language stays inside the classification enum: real parser
    # names for source entries, "other" for everything else.
    assert module.classify_path("docs/notes.md").language == "other"
    assert module.classify_path("api/data.json").language == "other"
    assert module.classify_path("src/app.py").language == "python"


def test_gate_completes_on_an_unborn_repo_with_open_stdin() -> None:
    # git mktree reads stdin by design; the gate must not let any git child
    # inherit an open stdin, or the first run in a freshly initialized repo
    # hangs at a terminal until EOF.
    repo = Path(tempfile.mkdtemp(prefix="production-code-gate-unborn-"))
    read_fd, write_fd = os.pipe()
    try:
        git(repo, "init", "-q")
        git(repo, "config", "user.email", "test@example.com")
        git(repo, "config", "user.name", "Test User")
        write(repo / "app.py", "VALUE = 1\n")
        proc = subprocess.Popen(
            ["python3", str(SCRIPT), "check", "--repo", str(repo), "--json"],
            stdin=read_fd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8",
        )
        try:
            stdout, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise AssertionError("gate hung on an unborn repository while stdin stayed open")
        payload = json.loads(stdout)
        assert proc.returncode == 0, stdout
        assert payload["ok"] is True, payload["errors"]
    finally:
        os.close(write_fd)
        os.close(read_fd)
        shutil.rmtree(repo, ignore_errors=True)


@with_repo
def test_growth_warning_survives_base_binding_incompleteness(repo: Path) -> None:
    # Without --base-ref the cumulative claim is incomplete, and that must be
    # reported - but it is not a reason to stop reporting the growth that WAS
    # measured. Incompleteness qualifies the warning; it never suppresses it.
    write(repo / "src" / "big.py", "".join(f"VALUE_{i} = {i}\n" for i in range(600)))
    code, payload, _ = run_gate(repo)
    assert payload["evaluation"]["growth"]["humanAuthored"]["net"] > 500, payload["evaluation"]["growth"]
    incomplete = [w for w in payload["warnings"] if "QG54-ANALYSIS-INCOMPLETE" in w and "no caller-supplied base" in w]
    growth = [w for w in payload["warnings"] if w.startswith("QG54-GROWTH-CUMULATIVE:")]
    assert incomplete, payload["warnings"]
    assert growth, payload["warnings"]
    # Warning-only: the hook contract keeps exit zero.
    assert code == 0 and payload["ok"] is True, (code, payload["errors"])


@with_repo
def test_growth_finding_carries_stable_identity_and_evidence(repo: Path) -> None:
    write(repo / "src" / "app.py", "VALUE = 1\n")
    first = growth_finding(run_gate(repo)[1])
    repeated = growth_finding(run_gate(repo)[1])
    assert first["ruleId"] == "QG54-GROWTH-CUMULATIVE"
    assert first["findingId"] == repeated["findingId"]
    assert first["severity"] == "warning"
    assert first["state"] is None and "passed" in first
    assert first["base"] and first["candidate"]
    assert first["region"]["scope"] == "evaluation"
    assert first["evidence"]["humanAuthored"] == {"added": 1, "deleted": 0, "net": 1}
    assert first["action"] and first["passCondition"]["kind"] == "growth-below"
    assert set(first["passCondition"]) == {"kind", "requires", "statement"}, first["passCondition"]
    write(repo / "src" / "app.py", "VALUE = 1\nOTHER = 2\n")
    assert growth_finding(run_gate(repo)[1])["findingId"] != first["findingId"]


def test_promotion_follows_exact_rule_id_metadata_only() -> None:
    # Every QG54 rule starts promotion-ineligible: no warning can fail the
    # gate even under --fail-on-warnings until #54 approves an exact ID.
    def growth_body(repo: Path) -> None:
        write(repo / "src" / "huge.py", "\n".join(f"VALUE_{i} = {i}" for i in range(801)) + "\n")
        code, payload, _ = run_gate(repo, "--base-ref", "HEAD", "--fail-on-warnings")
        assert growth_finding(payload)["status"] == "finding", growth_finding(payload)
        assert payload["errors"] == [], payload["errors"]
        assert payload["ok"] is True
        assert code == 0

    in_repo(growth_body)

    # Malformed graph input is unread evidence for the rules that consume
    # graph boosts, not a rule of its own: each affected owner rule reports
    # QG54-ANALYSIS-INCOMPLETE, and nothing promotes.
    def graph_input_body(repo: Path) -> None:
        context = repo / "broken-context.json"
        context.write_text("not json", encoding="utf-8")
        write(repo / "src" / "app.py", "def resolver(value):\n    return value\n")
        code, payload, _ = run_gate(
            repo, "--base-ref", "HEAD", "--fail-on-warnings", "--gitnexus-context-json", str(context)
        )
        assert_exact_rules(payload, {
            "QG54-OWNER-COMPETITION-PRODUCTION": "incomplete",
            "QG54-OWNER-COMPETITION-TEST": "incomplete",
        })
        assert any(
            "QG54-ANALYSIS-INCOMPLETE for QG54-OWNER-COMPETITION-PRODUCTION" in warning
            and "gitnexus context JSON ignored" in warning
            for warning in payload["warnings"]
        ), payload["warnings"]
        assert payload["errors"] == [], payload["errors"]
        assert payload["ok"] is True and code == 0, (code, payload["errors"])

    in_repo(graph_input_body)


@with_repo
def test_every_emitted_region_has_a_content_anchor(repo: Path) -> None:
    # Schema v2 has no rule-specific escape from content-anchored regions.
    write(repo / "src" / "ids.py", _OWNER)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    write(repo / "src" / "copycat.py", _OWNER)
    _, payload, _ = run_gate(repo, "--base-ref", "HEAD")

    with_regions = {finding["ruleId"] for finding in payload["findings"] if finding["region"].get("regions")}
    assert with_regions, payload["findings"]
    for finding in payload["findings"]:
        for region in finding["region"].get("regions", []):
            assert region["contentAnchor"], (finding["ruleId"], region)


@with_repo
def test_incompleteness_finding_identity_survives_a_path_rename(repo: Path) -> None:
    # Identity is the affected rule plus scope kind; the path-bearing gap text
    # is evidence only, so renaming the unreadable owner cannot move the ID.
    write(repo / "src" / "huge.py", _OVERSIZED)
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "oversized baseline")
    write(repo / "src" / "dup.py", _UNREADABLE_OWNER)

    def incompleteness_id(payload: dict[str, object]) -> str:
        found = [
            item for item in payload["findings"]
            if item["ruleId"] == "QG54-ANALYSIS-INCOMPLETE"
            and item["evidence"]["affectedRuleId"] == "QG54-OWNER-COMPETITION-PRODUCTION"
            and item["evidence"]["scopeKind"] == "baseline-discovery"
        ]
        assert len(found) == 1, payload["findings"]
        return found[0]["findingId"]

    first = incompleteness_id(run_gate(repo, "--base-ref", "HEAD")[1])
    git(repo, "mv", "src/huge.py", "src/huge_renamed.py")
    git(repo, "commit", "-q", "-m", "rename oversized baseline")
    second = incompleteness_id(run_gate(repo, "--base-ref", "HEAD")[1])
    assert first == second, (first, second)


@with_repo
def test_a_witnessed_violation_outranks_missing_scope(repo: Path) -> None:
    # Unseen scope cannot un-see a violation that was already found. The rule
    # that caught it must say so, while a rule that found nothing and could not
    # see everything still reports incomplete rather than a pass.
    (repo / "src" / "unmeasured.py").write_bytes(_BINARY)
    write(repo / "src" / "escape.py", "def f():\n    try:\n        return 2\n    except Exception:\n        pass\n")
    _, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    escapes = check_named(payload, "no-quality-escapes")
    assert escapes["passed"] is False and escapes["status"] == "finding", escapes
    assert payload["hardRules"]["cleanup"]["passed"] is False, payload["hardRules"]["cleanup"]
    # The other hunk-reading rule found nothing and still cannot see it all.
    duplicates = check_named(payload, "QG54-DUPLICATE-ADDED-SYMBOL")
    assert duplicates["passed"] is None and duplicates["status"] == "incomplete", duplicates


@with_repo
def test_a_failed_hard_rule_child_outranks_an_unknown_sibling(repo: Path) -> None:
    # Aggregation follows the same lattice as a single check: an established
    # failure dominates an unknown sibling, and unknown still beats a pass.
    (repo / "src" / "unmeasured.py").write_bytes(_BINARY)
    write(repo / "tmp" / "leftover.py", "VALUE = 1\n")
    _, payload, _ = run_gate(repo, "--base-ref", "HEAD")
    assert check_named(payload, "no-temp-artifacts")["passed"] is False, payload["checks"]
    assert check_named(payload, "no-quality-escapes")["passed"] is None, payload["checks"]
    assert payload["hardRules"]["cleanup"] == {
        "status": "evaluated",
        "passed": False,
        "checks": ["no-quality-escapes", "no-temp-artifacts"],
    }, payload["hardRules"]["cleanup"]
    # Nothing failed under noDuplication, so its unreadable scope still wins.
    assert payload["hardRules"]["noDuplication"]["passed"] is None, payload["hardRules"]["noDuplication"]


@with_repo
def test_a_non_utf8_path_reaches_a_stable_finding(repo: Path) -> None:
    # A path whose bytes are not valid UTF-8 survives the whole pipeline: it
    # is matched, its real bytes are serialized back out, and the finding
    # hashes to the same ID on a repeat run instead of raising.
    write(repo / "src" / "ids.py", DUPLICATE_HELPER + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "owner")
    (repo / "src" / os.fsdecode(b"caf\xe9.py")).write_bytes((DUPLICATE_HELPER + "\n").encode("utf-8"))
    code, payload, stderr = run_gate(repo, "--base-ref", "HEAD")
    assert code == 0, stderr
    copies = duplicate_findings(payload, "QG54-DUPLICATE-BASELINE")
    # There must actually be a finding, or this proves nothing: an empty
    # region list hashes to a stable id too.
    assert len(copies) == 1, payload["findings"]
    paths = {region["path"].encode("utf-8", "surrogateescape") for region in copies[0]["region"]["regions"]}
    assert b"src/caf\xe9.py" in paths, copies
    assert len(copies[0]["findingId"]) == 16, copies
    repeated = duplicate_findings(run_gate(repo, "--base-ref", "HEAD")[1], "QG54-DUPLICATE-BASELINE")
    assert repeated[0]["findingId"] == copies[0]["findingId"], (repeated, copies)


def test_gate_implementation_budget() -> None:
    limits = {
        "wrapper_lines": 150,
        "module_lines": 1200,
        "function_lines": 180,
        # Every raise requires a recorded operator approval against a measured
        # total; a comment here is never an approval. Recorded: 1800 -> 1950
        # (PR #90, 2026-08-08), -> 2300 -> 2350 (PR #101 for #76, 2026-08-10),
        # -> 2825 for the complete #77 slice (#54 issuecomment-5265202971,
        # 2026-08-12 — the only recorded approval for the 2350..2825 interval;
        # intermediate figures during PR #102's review rounds were working
        # measurements, not approvals).
        "total_lines": 2825,
    }
    review_triggers = {
        "module_lines": 700,
        "function_lines": 90,
        "total_lines": 1200,
    }
    justified: dict[str, str] = {
        "TOTAL": "complete #75 canonical evaluation, #76 exact duplication, and #77 responsibility ownership: captured base-to-candidate snapshot, typed schema-v2 findings, warning-only cumulative growth, three QG54-DUPLICATE-* rules and two QG54-OWNER-COMPETITION-* rules over one redundancy owner with disposition records, with the lexical reuse scorer deleted; 2825 ceiling operator-approved 2026-08-12",
        "_quality_gate/redundancy.py": "the one redundancy owner the architecture mandates: exact-duplicate phases plus the responsibility phases (eight evidence classes, three finding states, disposition validation, one-owner resolution) behind runner.check",
        "_quality_gate/runner.py:check": "the one evaluation walk the architecture mandates: every check, warning, error, and hard rule derives from a single typed outcome column",
    }
    production_files = [SCRIPT, *sorted((SCRIPT_DIR / "_quality_gate").glob("*.py"))]
    line_counts = {str(path.relative_to(SCRIPT_DIR)): len(path.read_text(encoding="utf-8").splitlines()) for path in production_files}

    assert line_counts["code_quality_gate.py"] <= limits["wrapper_lines"]
    assert sum(line_counts.values()) <= limits["total_lines"]
    if sum(line_counts.values()) > review_triggers["total_lines"]:
        assert "TOTAL" in justified

    for rel_path, count in line_counts.items():
        if rel_path == "code_quality_gate.py":
            continue
        assert count <= limits["module_lines"], rel_path
        if count > review_triggers["module_lines"]:
            assert rel_path in justified

    for path in production_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not hasattr(node, "end_lineno"):
                continue
            size = node.end_lineno - node.lineno + 1
            key = f"{path.relative_to(SCRIPT_DIR)}:{node.name}"
            assert size <= limits["function_lines"], key
            if size > review_triggers["function_lines"]:
                assert key in justified


def main() -> int:
    # Discovered in definition order: a hand-maintained registry silently
    # drops any test that is never added to it.
    tests = [value for name, value in list(globals().items()) if name.startswith("test_")]
    passed = skipped = 0
    for test in tests:
        try:
            test()
        except _SourceRepositoryUnavailable as reason:
            print(f"SKIP {test.__name__} ({reason})")
            skipped += 1
            continue
        # Counted only once the test has returned, so a failure escapes here
        # and aborts before it can be summarised as a pass.
        passed += 1
        print(f"PASS {test.__name__}")
    print(f"{passed} passed, {skipped} skipped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
