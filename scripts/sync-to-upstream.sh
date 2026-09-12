#!/usr/bin/env bash
# Port selected devin-side improvements BACK to claude-skills.
# Works on a scratch clone in /tmp — NEVER touches ~/projects/claude-skills.
# Usage: scripts/sync-to-upstream.sh skills/tdd/SKILL.md hooks/lib/foo.py ...
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $# -eq 0 ]] && { echo "usage: $0 <devin-side paths...>"; exit 1; }

work=$(mktemp -d)
git clone --quiet https://github.com/future3OOO/claude-skills.git "$work"
branch="devin-sync/$(date +%Y%m%d-%H%M%S)"
git -C "$work" switch -c "$branch" --quiet

for f in "$@"; do
  [[ -f "$f" ]] || { echo "skip (not a file): $f"; continue; }
  dest=$(python3 -c "import sys; sys.path.insert(0,'scripts'); from estate_xform import xform_path,FILENAME_REV; print(xform_path('$f',FILENAME_REV))")
  mkdir -p "$work/$(dirname "$dest")"
  python3 scripts/estate_xform.py to-claude "$f" < "$f" > "$work/$dest"
  echo "  back-ported $f -> $dest"
done

git -C "$work" add -A
git -C "$work" diff --cached --quiet && { echo "no changes to port"; rm -rf "$work"; exit 0; }
git -C "$work" commit --quiet -m "Port devin-skills improvements back to claude estate"
git -C "$work" push --quiet -u origin "$branch"
echo "pushed branch $branch on claude-skills — open a PR to merge"
rm -rf "$work"
