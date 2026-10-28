# iterm-claude-sidebar

An iTerm2 toolbelt panel that lists your open [Claude Code](https://claude.com/claude-code) sessions grouped by project, shows their state (busy / waiting / done / failed) and focuses the session when you click it.

> Reads Claude Code's undocumented `~/.claude/sessions/*.json` files, which may change between releases.

## Features
- State per session; "new" badge when one finished while you were elsewhere; macOS notification on waiting/failed
- Branch, Jira-style key, PR (via `gh`, cached 2 min), context usage %, time in state
- Filter, collapsible and pinnable projects, hide old done sessions, `+` starts `claude` in the project root
- Rename sessions in the sidebar (✎ or F2; empty resets): an alias stored by the sidebar, Claude and the tab titles keep the original name
- Recently closed sessions (last 15) with `claude --resume`
- Status bar component and a `claude_next_waiting()` function you can bind to a key

## Install
Requires iTerm2 with *Settings > General > Magic > Enable Python API*, Python 3 and (optionally) `gh`.

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python sidebar.py        # then View > Show Toolbelt > Claude Sessions
    ./install-launchagent.sh           # optional: start at login, restart if it exits

The log is `~/Library/Logs/iterm-claude-sidebar.log`. Remove with
`launchctl bootout gui/$(id -u)/io.github.gluebi.iterm-claude-sidebar`.

### Failed state (optional hook)
Add to `~/.claude/settings.json` (use the absolute path of `hooks/claude-failed.sh`): `StopFailure` and `PostToolUseFailure` run it with `set`; `Stop`, `UserPromptSubmit` and `SessionStart` run it with `clear`.

### 5h / 7d usage footer (status line tap)
Claude Code only hands the plan limits to its status line command, so `statusline-tap.py` records them and then runs your real status line unchanged. In `~/.claude/settings.json` set
`statusLine.command` to `/absolute/path/statusline-tap.py <your existing status line command>` (or just the tap if you have none). The tap writes `~/.claude/rate-limits.json`; the sidebar never depends on what your status line prints. Subscription plans only; values appear after a session's first response.

### Manual iTerm setup
- Status bar: Profiles > Session > Status bar enabled > Configure > drag in "Claude Sessions"
- Hotkey: Settings > Keys > Key Bindings > Action "Invoke Script Function" > `claude_next_waiting()`

## Notes
- Context % assumes a 1M-token window (`CONTEXT_WINDOW` in `state.py`)
- Notifications use `osascript`, so clicking one doesn't focus the session
- `python state.py` runs the self-check

## License
MIT
