#!/usr/bin/env bash
# Install the devin-skills estate to ~/.config/devin/.
# Never writes to ~/.claude or any other harness directory.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd -P)"
DEST="$HOME/.config/devin"
BACKUP="$HOME/.config/devin-backups/$(date +%Y%m%d-%H%M%S)"
EXCLUDES=(--exclude='/hooks/tests' --exclude='/skills/codex-advisor/tests' \
  --exclude='/skills/production-code/scripts/test_code_quality_gate.py' \
  --exclude='__pycache__' --exclude='*.pyc')

mkdir -p "$DEST" "$BACKUP"
for path in AGENTS.md config.json mcp_config.json hooks skills; do
  [[ -e "$DEST/$path" ]] && cp -a "$DEST/$path" "$BACKUP/"
done

rsync -a "${EXCLUDES[@]}" "$SRC/hooks" "$SRC/skills" "$DEST/"
cp "$SRC/AGENTS.md" "$DEST/AGENTS.md"
cp "$SRC/mcp_config.json" "$DEST/mcp_config.json"
chmod +x "$DEST"/hooks/*.py

# Merge managed keys (permissions, hooks, read_config_from) into the live
# config.json; keys the installer does not own (org_id, agent, shell, theme)
# survive. $HOME placeholders in hook commands are expanded to absolute paths.
python3 - "$SRC/config.json" "$DEST/config.json" <<'PY'
import json, os, sys
from pathlib import Path

managed = json.loads(Path(sys.argv[1]).read_text())
live_path = Path(sys.argv[2])
live = json.loads(live_path.read_text()) if live_path.exists() else {}
home = os.environ["HOME"]

def expand(value):
    if isinstance(value, str):
        return value.replace("$HOME", home)
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value

for key in ("permissions", "hooks", "read_config_from"):
    if key in managed:
        live[key] = expand(managed[key])
live_path.write_text(json.dumps(live, indent=2) + "\n")
PY

echo "installed -> $DEST (backup: $BACKUP)"
echo "verify: devin session -> /hooks lists 4 entries; skills load from $DEST/skills"
