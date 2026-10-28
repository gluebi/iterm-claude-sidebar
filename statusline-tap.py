#!/usr/bin/env python3
"""Status line tap: records the 5h/7d limits Claude Code hands to its status line command.

Usage (settings.json -> statusLine.command): /path/to/statusline-tap.py [real status line command ...]
Runs the real command with the same stdin/stdout/exit code; with no command it prints nothing.
Never fails: a problem here must not blank the status bar.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RATE_FILE = Path(os.environ.get("CLAUDE_RATE_LIMITS_FILE", Path.home() / ".claude" / "rate-limits.json"))


def publish(payload):
    limits = payload.get("rate_limits") or {}
    new = {k: {"pct": limits[k]["used_percentage"], "resets_at": limits[k].get("resets_at")} for k in ("five_hour", "seven_day") if k in limits}
    if not new:
        return
    try:
        old = json.loads(RATE_FILE.read_text())["windows"]
    except (OSError, ValueError, KeyError):
        old = {}
    # a session idle since before the last reset must not overwrite a newer reading
    if any((old.get(k, {}).get("resets_at") or 0, old.get(k, {}).get("pct") or 0) > (v["resets_at"] or 0, v["pct"]) for k, v in new.items()):
        return
    tmp = RATE_FILE.with_name(f"{RATE_FILE.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"windows": {**old, **new}, "updated": time.time()}))
    os.replace(tmp, RATE_FILE)


raw = sys.stdin.buffer.read()
try:
    publish(json.loads(raw))
except Exception:
    pass
sys.exit(subprocess.run(sys.argv[1:], input=raw).returncode if sys.argv[1:] else 0)
