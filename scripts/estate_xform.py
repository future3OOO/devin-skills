#!/usr/bin/env python3
"""Bidirectional content transform between the claude-skills, devin-skills and
codex-skills estates. Usage: estate_xform.py to-devin|to-claude|to-codex
<src_path> < input > output. Paths transform via the filename maps; content via
per-target token maps. Prints residual harness-specific hits to stderr for
human review — never silently.
"""
import re, sys

# Basename maps, claude-form -> harness-form.
BASENAME = {
    "devin": {"CLAUDE.md": "AGENTS.md", "settings.json": "config.json",
              "settings.local.json": "config.local.json"},
    "codex": {"CLAUDE.md": "AGENTS.md", "settings.json": "config.toml",
              "settings.local.json": "config.local.toml",
              "ask-codex-advisor.sh": "ask-claude-advisor.sh"},
}
# Whole-path-component maps (dir renames).
COMPONENT = {"codex": {"codex-advisor": "claude-advisor"}}

TOKENS = {
"devin": [
    ("CLAUDE_WORKFLOW_STATE_ROOT", "DEVIN_WORKFLOW_STATE_ROOT"),
    ("CLAUDE_HOME", "DEVIN_ESTATE_HOME"),
    ("claude_home", "estate_home"),
    ("~/.claude", "~/.config/devin"),
    ("$HOME/.claude", "$HOME/.config/devin"),
    (".claude/", ".config/devin/"),
    ('".claude")', '".config" / "devin")'),
    ("settings.local.json", "config.local.json"),
    ("settings.json", "config.json"),
    ("CLAUDE.md", "AGENTS.md"),
    ("`Agent` tool", "`run_subagent`"),
    ("Agent tool", "run_subagent"),
    ("`Skill` tool", "`skill`"),
    ("Skill tool", "skill tool"),
    ("TodoWrite", "todo_write"),
    ("AskUserQuestion", "ask_user_question"),
    ("ExitPlanMode", "exit_plan_mode"),
    ("NotebookEdit", "notebook_edit"),
    ("subagent_type", "profile"),
    ("general-purpose", "subagent_general"),
    ("`Explore`", "`subagent_explore`"),
],
"codex": [
    ("CLAUDE_WORKFLOW_STATE_ROOT", "CODEX_WORKFLOW_STATE_ROOT"),
    ("CLAUDE_HOME", "CODEX_HOME"),
    ("claude_home", "codex_home"),
    ("~/.claude", "~/.codex"),
    ("$HOME/.claude", "$HOME/.codex"),
    (".claude/", ".codex/"),
    ('".claude")', '".codex")'),
    ("settings.local.json", "config.local.toml"),
    ("settings.json", "config.toml"),
    ("CLAUDE.md", "AGENTS.md"),
    ("codex-advisor", "claude-advisor"),
    ("ask-codex-advisor", "ask-claude-advisor"),
    ("`Agent` tool", "`spawn_agent`"),
    ("Agent tool", "spawn_agent"),
    ("subagent_type", "agent_type"),
    ("general-purpose", "router_deepseek_deepseek_v4_flash"),
    ("`Explore`", "`router_deepseek_deepseek_v4_flash`"),
    ("TodoWrite", "update_plan"),
    ("AskUserQuestion", "request_user_input"),
    ("NotebookEdit", "apply_patch"),
    ("`Task`", "`spawn_agent`"),
],
}

REPAIR = [  # either/or phrasing that must survive the CLAUDE.md rename
    (re.compile(r"`AGENTS\.md` or `AGENTS\.md`"), "`AGENTS.md` or `CLAUDE.md`"),
    (re.compile(r"`AGENTS\.md` \(or `AGENTS\.md`"), "`AGENTS.md` (or `CLAUDE.md`"),
    (re.compile(r"`AGENTS\.md`/`AGENTS\.md`"), "`AGENTS.md`/`CLAUDE.md`"),
    (re.compile(r"either `AGENTS\.md` or `AGENTS\.md`"), "either `AGENTS.md` or `CLAUDE.md`"),
    (re.compile(r'"AGENTS\.md", "AGENTS\.md"'), '"AGENTS.md", "CLAUDE.md"'),
    (re.compile(r"'AGENTS\.md', 'AGENTS\.md'"), "'AGENTS.md', 'CLAUDE.md'"),
]
RESIDUAL = {
    "to-devin": re.compile(r"claude", re.IGNORECASE),
    "to-codex": re.compile(r"claude|devin", re.IGNORECASE),
    "to-claude": re.compile(r"devin|codex", re.IGNORECASE),
}


def xform_text(text: str, pairs: list) -> str:
    for old, new in pairs:
        text = text.replace(old, new)
    for rx, rep in REPAIR:
        text = rx.sub(rep, text)
    return text


def xform_path(path: str, target: str) -> str:
    comp = COMPONENT.get(target, {})
    parts = [comp.get(p, p) for p in path.split("/")]
    parts[-1] = BASENAME.get(target, {}).get(parts[-1], parts[-1])
    return "/".join(parts)


def inverse_pairs(target: str) -> list:
    return [(new, old) for old, new in TOKENS[target]]


def main() -> None:
    direction, path = sys.argv[1], sys.argv[2]
    text = sys.stdin.read()
    if direction == "to-claude":  # inverse of whichever harness the file came from
        pairs = inverse_pairs("devin") + inverse_pairs("codex")
        out_path = path
        for t in ("devin", "codex"):
            out_path = _inverse_path(out_path, t)
    else:
        target = direction.removeprefix("to-")
        pairs = TOKENS[target]
        out_path = xform_path(path, target)
    out = xform_text(text, pairs)
    sys.stdout.write(out)
    hits = [ln for ln in out.splitlines() if RESIDUAL[direction].search(ln)]
    if hits:
        print(f"--- {path} -> {out_path}: {len(hits)} residual hits ---", file=sys.stderr)
        for ln in hits[:10]:
            print(f"    {ln.strip()[:110]}", file=sys.stderr)
    else:
        print(f"--- {path} -> {out_path}: clean ---", file=sys.stderr)


def _inverse_path(path: str, target: str) -> str:
    comp = {v: k for k, v in COMPONENT.get(target, {}).items()}
    parts = [comp.get(p, p) for p in path.split("/")]
    parts[-1] = {v: k for k, v in BASENAME.get(target, {}).items()}.get(parts[-1], parts[-1])
    return "/".join(parts)


if __name__ == "__main__":
    main()
