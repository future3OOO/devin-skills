# devin-skills

Version-controlled source for the governed Devin agent estate. The
tracked files on `main` are authoritative; `~/.config/devin/` is the installed copy.
Machine-managed files and config keys that are not tracked here must survive
an install — the installer merges rather than overwrites.

| Repo path | Live path |
| --- | --- |
| `AGENTS.md` | `~/.config/devin/AGENTS.md` — global rules |
| `skills/` | `~/.config/devin/skills/` |
| `config.json` | merged into `~/.config/devin/config.json` — permissions, hooks, imports |
| `mcp_config.json` | `~/.config/devin/mcp_config.json` — MCP servers |
| `hooks/` | `~/.config/devin/hooks/` — the gates config.json wires up |

Development tests stay in GitHub and the complete mirror. All installs exclude
`hooks/tests/`, `skills/codex-advisor/tests/`, and
`skills/production-code/scripts/test_code_quality_gate.py`; keep runtime scripts
and skill references. Adding development tests elsewhere must update the shared
`excluded_tests` list below in the same change.

## Workflow boundary

The estate records one repository-scoped production workflow:

```
context -> preflight advice -> production preflight -> TDD -> production-code
        -> implementation -> verification -> code-review delegate review
        -> final Codex Advisor review -> complete -> delivery
```

The state is continuity for the agent, not Git authorization. No shipped hook
parses Bash or intercepts commits. Edit hooks admit governed work and invalidate
stale downstream review state; compaction/resume hooks preserve the next action;
there is no Stop hook. `skills/repo-production-workflow/WORKFLOW-MAP.md` owns the hook roles.

## Code-review delegate

Edit `model` (currently `opus`) in
[`skills/code-review/SKILL.md`](skills/code-review/SKILL.md); keep the other
frontmatter (`agent: subagent_general` runs the skill as its own subagent
context). Publish, install to `~/.config/devin/skills/code-review/SKILL.md`, and
restart existing sessions.

Devin resolves `model:` through its own model router; confirm the executed model
in harness receipts. There is no per-skill effort pin under Devin — thinking
level is session-level (`Alt+T`). The Codex Advisor is configured separately.

## Install or update

Review live differences first; the installer backs up and merges, it does not
clobber machine-owned keys (`org_id`, `agent`, `shell`, `theme_mode`).

```bash
cd ~/projects/devin-skills
./install.sh
```

`install.sh` backs up every managed live path to `~/.config/devin-backups/<ts>/`,
rsyncs `hooks/` and `skills/` (minus the development-test exclusions), copies
`AGENTS.md` and `mcp_config.json`, merges `permissions`, `hooks`, and
`read_config_from` into the live `~/.config/devin/config.json`, expands `$HOME`
in hook command paths, and marks `hooks/*.py` executable.

Verify the installed estate itself, not only the checkout:

```bash
python3 ~/.config/devin/skills/repo-production-workflow/scripts/workflow.py --help
diff -u AGENTS.md ~/.config/devin/AGENTS.md
python3 -c "import json; print(sorted(json.load(open('$HOME/.config/devin/config.json'))['hooks']))"
find ~/.config/devin/hooks -maxdepth 1 -name '*.py' ! -perm -u+x
```

`find` must print nothing. Then verify inside a Devin session: `/hooks` lists
the four estate entries (PreToolUse, PostToolUse, SessionStart, PostCompaction)
and skills resolve from `~/.config/devin/skills/`.

## Scoped install from a non-`main` branch

**Install, motherfucker.**

The procedure above reconciles the whole estate from pinned remote `main`.
For a verified but unmerged slice, pin its published head and install only
the branch's changed-path set —
`git diff --name-status origin/main...HEAD` — and within it only paths with a
live target in the mapping above, applying the same test exclusions; a scoped
install must not restore them. Repository-only paths such as `README.md` have
none. Update a live path when it matches current `main`, the candidate,
or what this PR last installed there: copy an added or modified candidate,
retire a deleted one with the procedure above, and treat a rename as that
retirement plus a copy. Anything else means another slice may own it — stop.
Every installed change is carried by the installing branch's PR; when
another slice's installed contract refuses scratch input, mechanically
re-encode existing evidence only and keep the adaptation out of the PR. When
another slice merges, rebase onto the new `main` and repeat verification and
review on the new head.

## Syncing with upstream (claude-skills)

`upstream` tracks `future3OOO/claude-skills`. Port forward with:

```bash
./scripts/sync-from-upstream.sh
```

It diffs upstream since `.upstream-sync`, runs each changed file through
`scripts/estate_xform.py` (filename map + token map), stages the result, and
prints diverged files — `AGENTS.md`, `config.json`, `README.md`, the devin
hook variants, `code-review` frontmatter — for manual merge instead of
overwriting them. Residual `claude` hits are printed to stderr; review the
staged diff, then commit.

Back-port a devin-side improvement to the Claude estate without touching
the local `~/projects/claude-skills` checkout:

```bash
./scripts/sync-to-upstream.sh skills/<name>/SKILL.md hooks/lib/<file>.py
```

It clones upstream to a scratch dir, applies the inverse transform, and
pushes a `devin-sync/<ts>` branch for PR.

## Workflow state root

`DEVIN_WORKFLOW_STATE_ROOT` selects where workflow state is stored; otherwise it
lands in `$DEVIN_ESTATE_HOME/state`, or `~/.config/devin/state`. Everything the workflow
writes follows that root — repository state, producer evidence, locks, Stop and
session records, advisor pointers. Nothing else moves: skills, hooks,
`AGENTS.md`, and Devin's own sessions are unaffected, and state already written
elsewhere stays there.

Each process reads the variable from its own environment, so export it before
launching or resuming and reuse the same root for the whole pass.

```bash
export DEVIN_WORKFLOW_STATE_ROOT="$HOME/.config/devin-state-roots/agent-a"
mkdir -p "$DEVIN_WORKFLOW_STATE_ROOT" && chmod 700 "$DEVIN_WORKFLOW_STATE_ROOT"
devin
# later, in a new shell: export the same root again, then
devin -r <session-id>
```

`prune --apply` deletes from whichever root is selected, so check the variable
before running it by hand; the prune tests always point it at a temporary root.

A per-agent root is optional: it isolates concurrent agents from each other's
workflow state, at the cost of splitting audit history across roots.

## External dependencies

This estate is **not self-contained**. `AGENTS.md` mandates these tools and the
hooks refuse work without them, but none of them live here. Install it onto a
machine without them and the estate bricks itself: `rcf-intake-gate.py`
blocks every code edit until a Repo Context Forge intake and a fresh GitNexus
index exist, and neither tool would be present to produce one.

Other tools may drop files into `~/.config/devin/` outside this repo's
management; the installer's backup-and-merge keeps them. Reconcile unexpected
live files rather than deleting them.

SHAs are what this estate was last verified against, not minimums.

| Tool | Source | Branch @ SHA |
| --- | --- | --- |
| GitNexus | [future3OOO/GitNexus](https://github.com/future3OOO/GitNexus) | `codex/add-global-codex-hooks` @ `6a305e05` |
| Repo Context Forge | [future3OOO/repo-context-forge](https://github.com/future3OOO/repo-context-forge) — **private** | `fix/gitnexus-singleflight-crash-dump` @ `63be8751` |
| SoulForge | [future3OOO/soulforge](https://github.com/future3OOO/soulforge) | `main` @ `a8b416cf` |
| fff | [future3OOO/fff.nvim](https://github.com/future3OOO/fff.nvim), prebuilt binary, no local checkout | `0.7.1` (`e8dd50ce`) |

Two entries need more than a public clone:

- **Repo Context Forge is private** — the only closed repo of the four.
- **fff publishes no `0.7.1` GitHub Release.** The fork is public, but neither it
  nor upstream `dmtrKovalenko/fff` publishes that release, and this mirror
  records no downloadable artifact and no verified build recipe. The installed
  binary self-reports `fff-mcp 0.7.1 (e8dd50ce…)`, and that commit is shared
  upstream history, so it fixes the version but not which remote it was built
  from.

Expected paths, all hardcoded somewhere in the estate:

```
/home/prop_/projects/GitNexus-pr1-review      GitNexus checkout (built to gitnexus/dist)
/home/prop_/.local/share/repo-context-forge/current   RCF runtime; SOURCE_ROOT resolves through this snapshot pointer
/home/prop_/soulforge                         via ~/.local/bin/soulforge
~/.local/bin/gitnexus  -> /home/prop_/projects/GitNexus-pr1-review/gitnexus/dist/cli/index.js
~/.local/bin/fff-mcp                          static binary
```

**GitNexus is wired twice and both must point at the same build.** The MCP
server answers `context`/`impact`/`query`; the `~/.local/bin/gitnexus` symlink
runs `analyze`, which writes the index the MCP reads. The two are configured
independently, so verify they agree:

```bash
readlink -f "$(command -v gitnexus)"     # must equal the MCP's args[0]
```

**MCP config is tracked** in [`mcp_config.json`](mcp_config.json) and installs
to `~/.config/devin/mcp_config.json`. `gitnexus` and `fff` are declared there;
project-local servers belong in each repo's `.devin/mcp_config.json`.

Neither GitNexus nor fff has a skill — GitNexus is used directly as
`mcp__gitnexus__*` tools under the `AGENTS.md` §9 workflow, and fff as
`mcp__fff__*`. Only Repo Context Forge has a skill, and that skill is a shim
that shells out to its separate repo.

## Notes

- `config.json` hardcodes absolute paths under `/home/prop_`, so it is the
  tracked configuration for this machine rather than a portable default.
- `~/.config/devin/config.local.json` is deliberately **not** mirrored: it is the
  machine-local override and may hold credentials.
- The advisor delegate still invokes `claude -p` headless; Claude Code remains
  installed for that purpose only. Nothing else in this estate reads `~/.claude`.
- A sibling `~/projects/codex-skills` mirrors the Codex estate the same way
  (`~/.codex/skills/` plus `AGENTS.md`); sync both after cross-estate changes.
