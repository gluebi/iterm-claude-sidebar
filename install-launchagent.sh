#!/bin/sh
# Installs and starts the LaunchAgent for this checkout (re-run after moving the repo).
set -e
repo=$(cd "$(dirname "$0")" && pwd)
label=io.github.gluebi.iterm-claude-sidebar
dest="$HOME/Library/LaunchAgents/$label.plist"
sed -e "s|__REPO__|$repo|g" -e "s|__HOME__|$HOME|g" "$repo/$label.plist.template" > "$dest"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$dest"
