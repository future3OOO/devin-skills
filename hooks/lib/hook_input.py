"""Parse Claude hook input at one boundary."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .workflow_state import safe_slug


def read_hook_payload() -> dict[str, object]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def edited_path(payload: dict[str, object]) -> Path | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    value = tool_input.get("file_path") or tool_input.get("notebook_path")
    return Path(value).expanduser().resolve(strict=False) if isinstance(value, str) and value else None


def working_directory(payload: dict[str, object]) -> str:
    """Working directory across hook transports.

    Payloads may carry `cwd` or `working_directory`; Devin always sets the
    `DEVIN_PROJECT_DIR` environment variable for hook commands. Falls back to
    the hook's own process cwd.
    """
    for key in ("cwd", "working_directory"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    env = os.environ.get("DEVIN_PROJECT_DIR")
    return env if env else os.getcwd()


def session_key(payload: dict[str, object]) -> str | None:
    """The session identifier as one state path segment, or None when absent.

    Derived here so the hook that records an association and the hook that reads
    it cannot drift, and so a hostile `session_id` is bounded to a single safe
    segment before it ever reaches the filesystem.

    Absence is returned rather than defaulted. A session key names a per-session
    set, so defaulting a missing id to any shared literal would file every
    anonymous payload under one identity and let one repository's pass reach
    another's Stop. Callers that want a display name for repository-scoped
    storage supply their own fallback.
    """
    value = payload.get("session_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return safe_slug(value)[:40]
