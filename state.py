import json
import os
import re
import subprocess
import time
from pathlib import Path

SESSIONS = Path.home() / ".claude" / "sessions"
FAILED = Path.home() / ".claude" / "sessions-failed"
RATE_FILE = Path.home() / ".claude" / "rate-limits.json"  # written by the user's status line script
CONTEXT_WINDOW = 1_000_000  # matches the 1M window the status line shows; not read from Claude
LABELS = {"busy": "busy", "waiting": "waiting", "idle": "done", "failed": "failed"}
NEEDS_ATTENTION = ("waiting", "failed")


def state_of(status: str, failed: bool) -> str:
    if failed and status != "busy":
        return "failed"
    return status if status in ("busy", "waiting") else "idle"


def ttl_cache(seconds):
    def wrap(fn):
        store = {}

        def inner(*args):
            hit = store.get(args)
            if hit and time.monotonic() - hit[0] < seconds:
                return hit[1]
            now = time.monotonic()
            for stale in [k for k, (at, _) in store.items() if now - at >= seconds]:  # bound the growth
                del store[stale]
            store[args] = (now, fn(*args))
            return store[args][1]
        return inner
    return wrap


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def git(cwd: str, *args: str) -> str:
    return sh("git", "-C", cwd, *args)


@ttl_cache(30)
def root_of(cwd: str) -> Path:
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(common).parent if common.endswith("/.git") else Path(cwd)


def project_of(cwd: str) -> str:
    return re.sub(r"-\d+$", "", root_of(cwd).name)


@ttl_cache(5)
def branch_of(cwd: str) -> str:
    return git(cwd, "branch", "--show-current")


def jira_key(branch: str) -> str:
    m = re.search(r"(?<![a-z0-9])([a-z]{2,6}-\d+)(?!\d)", branch, re.I)
    return m.group(1).upper() if m else ""


@ttl_cache(5)
def transcript_tail(session_id: str, size: int = 262144) -> list[str]:
    """Last `size` bytes of the session transcript as lines (the first may be cut); empty if it doesn't exist yet."""
    path = next(Path.home().glob(f".claude/projects/*/{session_id}.jsonl"), None)
    if not path:
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - size))
        return f.read().decode(errors="ignore").splitlines()


@ttl_cache(5)
def context_tokens(session_id: str) -> int:
    """Tokens in the last assistant turn's prompt (input + cache), read from the transcript tail."""
    for line in reversed(transcript_tail(session_id)):
        if '"usage"' not in line:
            continue
        try:
            u = json.loads(line)["message"]["usage"]
        except (ValueError, KeyError, TypeError):
            continue
        return sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return 0


def tool_label(block: dict) -> str:
    args = block.get("input") or {}
    detail = next((str(args[k]) for k in ("command", "file_path", "pattern", "description", "url", "prompt") if k in args), "")
    return f"{block.get('name', 'tool')} {' '.join(detail.split())[:80]}".strip()


@ttl_cache(1)
def conversation(session_id: str, limit: int = 8) -> list[dict]:
    """The last few user/assistant turns as plain text with tool calls summarised; no thinking, tool output or injected notices."""
    items = []
    for line in reversed(transcript_tail(session_id, 524288)):
        if len(items) >= limit:
            break
        try:
            d = json.loads(line)
            content = d["message"]["content"]
        except (ValueError, KeyError, TypeError):
            continue
        if d.get("type") not in ("user", "assistant") or d.get("isSidechain") or d.get("isMeta"):
            continue
        parts = []
        for b in [{"type": "text", "text": content}] if isinstance(content, str) else content:
            if b.get("type") == "text" and b["text"].strip() and not b["text"].lstrip().startswith("<") and "<system-reminder>" not in b["text"]:
                parts.append(b["text"].strip())
            elif b.get("type") == "tool_use":
                parts.append("▸ " + tool_label(b))
        if parts:
            text = "\n".join(parts)
            items.append({"role": d["type"], "text": text if len(text) <= 600 else text[:600] + "…"})
    return items[::-1]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def load() -> list[dict]:
    out = []
    failed = {p.name for p in FAILED.glob("*")}
    for f in SESSIONS.glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("kind") != "interactive" or not alive(d["pid"]):
            continue
        state = state_of(d.get("status", "idle"), d["sessionId"] in failed)
        branch = branch_of(d["cwd"])
        root = root_of(d["cwd"])
        out.append({
            "id": d["sessionId"], "pid": d["pid"], "name": d.get("name") or Path(d["cwd"]).name,
            "cwd": d["cwd"], "root": str(root), "project": project_of(d["cwd"]),
            "branch": branch, "jira": jira_key(branch), "ctx": round(100 * context_tokens(d["sessionId"]) / CONTEXT_WINDOW),
            "state": state, "label": LABELS[state], "since": d.get("statusUpdatedAt") or d.get("updatedAt"),
        })
    for name in failed - {s["id"] for s in out}:  # drop markers of sessions that no longer exist
        (FAILED / name).unlink(missing_ok=True)
    return out


def read_windows() -> dict:
    """{"five_hour": {"pct", "resets_at"}, "seven_day": {...}} as published by statusline-tap.py; empty until then."""
    try:
        return json.loads(RATE_FILE.read_text())["windows"]
    except (OSError, ValueError, KeyError):
        return {}


def limits() -> dict:
    """The windows plus `stale`: the window has rolled over since the reading."""
    return {k: w | {"stale": (w.get("resets_at") or 0) < time.time()} for k, w in read_windows().items()}


def grouped(sessions: list[dict], order: list[str] = ()) -> list[dict]:
    """Sessions sit in the user's drag order; ones never placed follow by name."""
    rank = {sid: i for i, sid in enumerate(order)}
    groups: dict[str, list[dict]] = {}
    for s in sessions:
        groups.setdefault(s["project"], []).append(s)
    return [{"project": p, "root": v[0]["root"], "sessions": sorted(v, key=lambda s: (rank.get(s["id"], len(rank)), s["name"]))} for p, v in sorted(groups.items())]


@ttl_cache(60)
def tty_of(pid: int) -> str:
    t = sh("ps", "-o", "tty=", "-p", str(pid))
    return f"/dev/{t}" if t and t != "??" else ""


if __name__ == "__main__":
    assert state_of("idle", False) == "idle"
    assert state_of("waiting", False) == "waiting"
    assert state_of("busy", True) == "busy"  # new prompt supersedes a stale failure marker
    assert state_of("idle", True) == "failed"
    assert state_of("weird", False) == "idle"
    assert project_of("/nonexistent/my-repo-4") == "my-repo"
    assert jira_key("alice/abc-123-fix-thing") == "ABC-123" and jira_key("master") == ""
    assert jira_key("feature/abc123") == ""
    assert tool_label({"name": "Bash", "input": {"command": "ls  -la\n/tmp"}}) == "Bash ls -la /tmp"
    assert conversation("no-such-session") == []
    ids = lambda g: [x["id"] for x in g[0]["sessions"]]
    sess = [{"id": i, "name": i, "project": "p", "root": "/p"} for i in "abc"]
    assert ids(grouped(sess)) == ["a", "b", "c"] and ids(grouped(sess, ["c", "a"])) == ["c", "a", "b"]
    print("ok")
