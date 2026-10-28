# iterm-claude-sidebar

<p align="center">
  <img src="docs/sidebar.png" alt="Sidebar listing Claude Code sessions grouped by project, with busy, waiting, done and failed states, PR chips and 5h/7d usage meters" width="300">
  &nbsp;&nbsp;
  <img src="docs/peek.png" alt="Peek card floating over the list: transcript of a session, a message box, and Interrupt and /clear buttons" width="300">
</p>

An iTerm2 toolbelt panel that lists your open [Claude Code](https://claude.com/claude-code) sessions grouped by project, shows their state (busy / waiting / done / failed) and focuses the session when you click it.

> Reads Claude Code's undocumented `~/.claude/sessions/*.json` files, which may change between releases.

## Features
- State per session; "new" badge when one finished while you were elsewhere; macOS notification on waiting/failed
- Branch, Jira-style key, PR (via `gh`, cached 2 min), context usage %, time in state
- Filter, collapsible and pinnable projects, hide old done sessions, `+` starts `claude` in the project root
- Rename sessions in the sidebar (✎ or F2; empty resets): an alias stored by the sidebar, Claude and the tab titles keep the original name
- Peek (👁): a floating card with the session's transcript or live screen, plus a message box, Interrupt (Esc) and `/clear` (asks twice). Sending never focuses the session; it asks first if the session is waiting for input or you have unsent text at its prompt (single-line messages only)
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

## Contributing
Issues and pull requests are welcome. This is a small tool, so changes that keep it small are the easiest to merge.

**Setup**

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    launchctl bootout gui/$(id -u)/io.github.gluebi.iterm-claude-sidebar   # only if you installed the LaunchAgent: the daemon uses a fixed port (47831)
    .venv/bin/python sidebar.py

iTerm2 needs *Settings > General > Magic > Enable Python API*. There is no build step: `static/index.html` is served as is, and the page reloads itself when the file changes. Restart `sidebar.py` after editing the Python files.

**Checks**
- `python state.py` runs the assert-based self-check; it needs neither iTerm2 nor Claude Code.
- Try UI changes against your own sessions and in a narrow window (the toolbelt is about 300 px wide), in light and dark mode.

**Guidelines**
- Stay with the standard library, `iterm2` and `aiohttp`; avoid new dependencies and front-end tooling.
- Give new interactive elements a `data-testid` (`{component}-{element}`, kebab-case), keep them keyboard-accessible with proper ARIA, and respect `prefers-reduced-motion`.
- Anything under `/api/` goes through the `protect` middleware (localhost `Host` plus the `X-Sidebar` header). Anything that types into a terminal must keep asking first when the session is waiting for input or has unsent text.
- Comment only the *why*, in one short line.
- Never put real project, branch or ticket names, tokens or paths in code, tests or screenshots. The README screenshots come from invented data.
- Keep pull requests focused, say what you tested, and write commit messages in the imperative.

**Reporting a bug**
Include your Claude Code and iTerm2 versions, since the sidebar reads Claude Code's undocumented session files, plus the relevant lines of `~/Library/Logs/iterm-claude-sidebar.log` if you use the LaunchAgent.

## License
MIT
