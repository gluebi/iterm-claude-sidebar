#!/usr/bin/env python3
"""iTerm2 daemon: toolbelt web view listing Claude Code sessions; click focuses the iTerm session."""
import asyncio
import json
import re
import shlex
import sys
import time
from pathlib import Path

import iterm2
from aiohttp import web

import state

PORT = 47831
TOOL_ID = "io.github.gluebi.iterm-claude-sidebar"
STATIC = Path(__file__).parent / "static"
DATA = Path.home() / "Library" / "Application Support" / "iterm-claude-sidebar"
CLOSED_FILE = DATA / "closed.json"
UUID = re.compile(r"^[0-9a-f-]{36}$")

live: list[dict] = []
closed: list[dict] = json.loads(CLOSED_FILE.read_text()) if CLOSED_FILE.exists() else []
unseen: set[str] = set()
prs: dict[str, dict | None] = {}
pr_checked: dict[str, float] = {}
iterm_active = True


def summary() -> str:
    n = lambda s: sum(x["state"] == s for x in live)
    parts = [f"◐ {n('waiting')} waiting", f"✖ {n('failed')} failed", f"● {n('busy')} busy", f"✓ {len(unseen)} new"]
    return "  ".join(p for p, c in zip(parts, (n("waiting"), n("failed"), n("busy"), len(unseen))) if c) or "Claude: idle"


async def run(*cmd, cwd=None):
    p = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    return (await p.communicate())[0].decode().strip()


async def notify(title, text):
    esc = lambda t: json.dumps(t, ensure_ascii=False)
    await run("osascript", "-e", f"display notification {esc(text)} with title {esc(title)}")


async def find_session(app, pid):
    tty = await asyncio.to_thread(state.tty_of, pid)
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if tty and await session.async_get_variable("tty") == tty:
                    return session
    return None


async def focused_pid(app):
    window = app.current_terminal_window
    session = window and window.current_tab and window.current_tab.current_session
    tty = session and await session.async_get_variable("tty")
    ttys = await asyncio.to_thread(lambda: {s["id"]: state.tty_of(s["pid"]) for s in live})
    return next((sid for sid, t in ttys.items() if t == tty), None) if tty else None


async def refresh_pr(s):
    key = f"{s['cwd']}@{s['branch']}"
    if not s["branch"] or s["branch"] in ("master", "main") or time.time() - pr_checked.get(key, 0) < 120:
        return
    pr_checked[key] = time.time()
    out = await run("gh", "pr", "view", "--json", "number,url,state,isDraft", cwd=s["cwd"])
    try:
        prs[key] = json.loads(out)
    except ValueError:
        prs[key] = None


def save_closed():
    DATA.mkdir(parents=True, exist_ok=True)
    CLOSED_FILE.write_text(json.dumps(closed))


async def monitor(app):
    global live
    prev: dict[str, dict] = {}
    while True:
        live = await asyncio.to_thread(state.load)
        now = {s["id"]: s for s in live}
        focus = await focused_pid(app) if iterm_active else None
        for sid, s in now.items():
            old = prev.get(sid)
            if old and old["state"] != s["state"]:
                if s["state"] in ("waiting", "failed") and focus != sid:
                    await notify(f"Claude {s['label']}: {s['project']}", s["name"])
                if s["state"] == "idle" and focus != sid:
                    unseen.add(sid)
            asyncio.ensure_future(refresh_pr(s))
        if focus:
            unseen.discard(focus)
        for sid, old in prev.items():
            if sid not in now:
                unseen.discard(sid)
                closed.insert(0, {k: old[k] for k in ("id", "name", "cwd", "project")} | {"closedAt": int(time.time() * 1000)})
                del closed[15:]
                save_closed()
        closed[:] = [c for c in closed if c["id"] not in now]
        prev = now
        await asyncio.sleep(1)


async def focus_monitor(connection):
    global iterm_active
    async with iterm2.FocusMonitor(connection) as monitor_:
        while True:
            update = await monitor_.async_get_next_update()
            if update.application_active is not None:
                iterm_active = update.application_active


def view(s):
    pr = prs.get(f"{s['cwd']}@{s['branch']}")
    return s | {"unseen": s["id"] in unseen, "pr": pr}


async def open_tab(connection, app, command):
    window = app.current_terminal_window or await iterm2.Window.async_create(connection)
    tab = await window.async_create_tab()
    await tab.current_session.async_send_text(command + "\n")


async def main(connection):
    app = await iterm2.async_get_app(connection)

    async def sessions(_):
        groups = state.grouped([view(s) for s in live])
        for g in groups:
            g["root"] = g["sessions"][0]["root"]
        return web.json_response({"groups": groups, "closed": closed, "summary": summary()})

    def guarded(handler):
        async def inner(request):
            if request.headers.get("X-Sidebar") != "1":  # blocks cross-site form posts
                raise web.HTTPForbidden()
            return await handler(request, await request.json())
        return inner

    async def activate(session):
        await session.async_activate(select_tab=True, order_window_front=True)

    async def focus(_, body):
        match = next((s for s in live if s["id"] == body.get("id")), None)
        session = match and await find_session(app, match["pid"])
        if not session:
            raise web.HTTPNotFound()
        unseen.discard(match["id"])
        await activate(session)
        return web.Response(status=204)

    async def new(_, body):
        if body.get("root") not in {s["root"] for s in live}:
            raise web.HTTPBadRequest()
        await open_tab(connection, app, f"cd {shlex.quote(body['root'])} && claude")
        return web.Response(status=204)

    async def resume(_, body):
        item = next((c for c in closed if c["id"] == body.get("id")), None)
        if not item or not UUID.match(item["id"]):
            raise web.HTTPNotFound()
        await open_tab(connection, app, f"cd {shlex.quote(item['cwd'])} && claude --resume {item['id']}")
        return web.Response(status=204)

    async def open_pr(_, body):
        if body.get("url") not in {p["url"] for p in prs.values() if p}:
            raise web.HTTPBadRequest()
        await run("open", body["url"])
        return web.Response(status=204)

    server = web.Application()
    server.add_routes([
        web.get("/", lambda _: web.FileResponse(STATIC / "index.html")),
        web.get("/api/sessions", sessions),
        web.post("/api/focus", guarded(focus)),
        web.post("/api/new", guarded(new)),
        web.post("/api/resume", guarded(resume)),
        web.post("/api/open-pr", guarded(open_pr)),
    ])
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()

    url = f"http://127.0.0.1:{PORT}/"
    reveal = lambda: iterm2.tool.async_register_web_view_tool(connection, "Claude Sessions", TOOL_ID, True, url)
    await reveal()

    @iterm2.RPC
    async def claude_next_waiting():
        waiting = sorted((s for s in live if s["state"] in ("waiting", "failed")), key=lambda s: s["since"] or 0)
        for s in waiting:
            session = await find_session(app, s["pid"])
            if session:
                await activate(session)
                return

    await claude_next_waiting.async_register(connection)

    component = iterm2.StatusBarComponent(
        short_description="Claude Sessions", detailed_description="Counts of Claude sessions needing attention",
        knobs=[], exemplar="◐ 1 waiting  ● 2 busy", update_cadence=2, identifier="io.github.gluebi.iterm-claude-sidebar.status",
    )

    @iterm2.StatusBarRPC
    async def status_coro(knobs):
        return summary()

    async def onclick(_session_id):
        await reveal()

    await component.async_register(connection, status_coro, onclick=onclick)
    await asyncio.gather(monitor(app), focus_monitor(connection))


if "--selfcheck" not in sys.argv:
    iterm2.run_forever(main)
