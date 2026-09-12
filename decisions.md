# Decisions — devin-skills

## 2026-09-12: Devin-native estate cloned from claude-skills

- `devin-skills` is a standalone clone; upstream = `future3OOO/claude-skills` (read-only sync source). The Claude estate (`~/projects/claude-skills`, `~/.claude/`) is never written by this repo or its installer.
- `CLAUDE.md` → `AGENTS.md`; `settings.json` → `config.json` (managed keys: `permissions`, `hooks`, `read_config_from`; merged into live config so machine keys survive).
- `read_config_from.claude = false` ships in managed config — the installed estate is the sole Devin source.
- State root chain renamed: `CLAUDE_WORKFLOW_STATE_ROOT` → `DEVIN_WORKFLOW_STATE_ROOT`, `CLAUDE_HOME` → `DEVIN_ESTATE_HOME`, default `~/.claude` → `~/.config/devin`. Fresh state namespace; nothing migrated from `~/.claude/state`.
- Hook matchers retargeted to Devin tool names (`edit|write|notebook_edit|apply_patch`); `skill-discipline-rearm.py` filters `SessionStart.source` in-script and emits `hookSpecificOutput.additionalContext` JSON; `PostCompaction` wired separately.
- HerdR session hook and moshi-hook entries are NOT ported — Claude-harness integrations, out of scope.
- Codex advisor still invokes `claude -p` headless — a deliberate external transport dependency, not a config dependency. Claude Code stays installed for that one purpose.
- `code-review` delegate frontmatter: `agent: subagent_general`, `model: opus`; Claude-only fields (`context: fork`, `effort`, `background`) dropped — no Devin equivalent.
- `install.sh` is the install contract: backup → rsync hooks+skills (minus test exclusions) → copy AGENTS.md/mcp_config.json → merge config keys → chmod. Never `--delete`, never touches `~/.claude`.
- Known pre-existing failure: `test_adapter_death_keeps_its_live_producer_locked` fails identically on the pristine claude-skills checkout (external RCF producer >15s startup on this machine) — environmental, not a port regression.
