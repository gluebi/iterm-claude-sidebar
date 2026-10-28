#!/bin/sh
# Claude Code hook: `claude-failed.sh set|clear`. Marks the session failed for the iTerm sidebar.
dir="$HOME/.claude/sessions-failed"
id=$(cat | python3 -c 'import json,sys;print(json.load(sys.stdin).get("session_id",""))')
[ -n "$id" ] || exit 0
mkdir -p "$dir"
case "$1" in
  set) : > "$dir/$id" ;;
  clear) rm -f "$dir/$id" ;;
esac
