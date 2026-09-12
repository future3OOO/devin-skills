#!/usr/bin/env python3
"""Bidirectional content transform between the claude-skills and devin-skills
estates. Usage: estate_xform.py to-devin|to-claude < upstream_file > dest
Paths transform via the filename map; content via the token map. Prints any
remaining harness-specific hits to stderr for human review — never silently.
"""
import re, sys

FILENAME = {  # upstream -> devin
    "CLAUDE.md": "AGENTS.md",
    "settings.json": "config.json",
    "settings.local.json": "config.local.json",
}
FILENAME_REV = {v: k for k, v in FILENAME.items()}

TOKENS = [  # (claude_form, devin_form) — applied in order
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
]
# Either/or phrasing that must survive the CLAUDE.md rename.
REPAIR = [
    (re.compile(r"`AGENTS\.md` or `AGENTS\.md`"), "`AGENTS.md` or `CLAUDE.md`"),
    (re.compile(r"`AGENTS\.md` \(or `AGENTS\.md`"), "`AGENTS.md` (or `CLAUDE.md`"),
    (re.compile(r"`AGENTS\.md`/`AGENTS\.md`"), "`AGENTS.md`/`CLAUDE.md`"),
    (re.compile(r"either `AGENTS\.md` or `AGENTS\.md`"), "either `AGENTS.md` or `CLAUDE.md`"),
    (re.compile(r'"AGENTS\.md", "AGENTS\.md"'), '"AGENTS.md", "CLAUDE.md"'),
    (re.compile(r"'AGENTS\.md', 'AGENTS\.md'"), "'AGENTS.md', 'CLAUDE.md'"),
]
RESIDUAL = re.compile(r"claude|CLAUDE", re.IGNORECASE)


def xform_text(text: str, pairs: list) -> str:
    for old, new in pairs:
        text = text.replace(old, new)
    for rx, rep in REPAIR:
        text = rx.sub(rep, text)
    return text


def xform_path(path: str, fmap: dict) -> str:
    parts = path.split("/")
    parts[-1] = fmap.get(parts[-1], parts[-1])
    return "/".join(parts)


def main() -> None:
    direction, path = sys.argv[1], sys.argv[2]
    text = sys.stdin.read()
    if direction == "to-devin":
        pairs = TOKENS
        out_path = xform_path(path, FILENAME)
    else:
        pairs = [(d, c) for c, d in TOKENS]
        out_path = xform_path(path, FILENAME_REV)
    out = xform_text(text, pairs)
    sys.stdout.write(out)
    hits = [ln for i, ln in enumerate(out.splitlines(), 1) if RESIDUAL.search(ln)]
    if hits:
        print(f"--- {path} -> {out_path}: {len(hits)} residual claude/devin hits ---",
              file=sys.stderr)
        for ln in hits[:10]:
            print(f"    {ln.strip()[:110]}", file=sys.stderr)
    else:
        print(f"--- {path} -> {out_path}: clean ---", file=sys.stderr)


if __name__ == "__main__":
    main()
