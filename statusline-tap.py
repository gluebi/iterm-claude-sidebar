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


def rank(window):
    return (window.get("resets_at") or 0, window.get("pct") or 0)


def publish(payload):
    from state import RATE_FILE, read_windows  # inside the guarded path: an import error must not stop the real status line

    given = payload.get("rate_limits") or {}
    new = {k: {"pct": given[k]["used_percentage"], "resets_at": given[k].get("resets_at")} for k in ("five_hour", "seven_day") if k in given}
    old = read_windows()
    # skip no-ops, and never let a session idle since before the last reset overwrite a newer reading
    if not new or all(old.get(k) == v for k, v in new.items()) or any(rank(old.get(k, {})) > rank(v) for k, v in new.items()):
        return
    tmp = RATE_FILE.with_name(f"{RATE_FILE.name}.{os.getpid()}")
    try:
        tmp.write_text(json.dumps({"windows": {**old, **new}}))
        os.replace(tmp, RATE_FILE)
    finally:
        tmp.unlink(missing_ok=True)


raw = sys.stdin.buffer.read()
child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE) if sys.argv[1:] else None
if child:  # start the real status line first so publishing overlaps with it
    child.stdin.write(raw)
    child.stdin.close()
try:
    publish(json.loads(raw))
except Exception:
    pass
sys.exit(child.wait() if child else 0)
