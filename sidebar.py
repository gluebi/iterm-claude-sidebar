#!/usr/bin/env python3
"""iTerm2 daemon: toolbelt web view listing Claude Code sessions; click focuses the iTerm session."""
import asyncio
import json
import re
import secrets
import shlex
import time
import webbrowser
from pathlib import Path

import iterm2
from aiohttp import web
from iterm2.notifications import async_subscribe_to_screen_update_notification, async_unsubscribe

import access
import state
import term

PORT = 47831
TOOL_ID = "io.github.gluebi.iterm-claude-sidebar"
STATIC = Path(__file__).parent / "static"
DATA = Path.home() / "Library" / "Application Support" / "iterm-claude-sidebar"
CLOSED_FILE = DATA / "closed.json"
ALIASES_FILE = DATA / "aliases.json"
ORDER_FILE = DATA / "order.json"
CONFIG_FILE = DATA / "config.json"  # optional: {"bind", "allow_from", "allowed_hosts"}, see README
UUID = re.compile(r"^[0-9a-f-]{36}$")


def read_json(file, default):
    try:
        return json.loads(file.read_text())
    except (OSError, ValueError):
        return default


def save_json(file, value):
    DATA.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value))


BIND, NETWORKS, HOSTS = access.load(read_json(CONFIG_FILE, {}), PORT)
LOCAL_HOST = f"127.0.0.1:{PORT}"  # the toolbelt's own address; any other Host means the page came through a proxy

live: list[dict] = []
closed: list[dict] = read_json(CLOSED_FILE, [])
aliases: dict[str, str] = read_json(ALIASES_FILE, {})  # session id -> name chosen in the sidebar
order: list[str] = read_json(ORDER_FILE, [])  # session ids in the sequence the user dragged them into
unseen: set[str] = set()
prs: dict[str, tuple[float, dict | None]] = {}  # cwd@branch -> (checked_at, PR)
iterm_active = True
active: str | None = None  # the session selected in iTerm's front window


def summary() -> str:
    n = lambda s: sum(x["state"] == s for x in live)
    items = [("◐", "waiting", n("waiting")), ("✖", "failed", n("failed")), ("●", "busy", n("busy")), ("✓", "new", len(unseen))]
    return "  ".join(f"{icon} {count} {label}" for icon, label, count in items if count) or "Claude: idle"


async def run(*cmd, cwd=None):
    p = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        return (await asyncio.wait_for(p.communicate(), 10))[0].decode().strip()
    except asyncio.TimeoutError:
        p.kill()
        return ""


background: set[asyncio.Task] = set()


def spawn(coro):
    task = asyncio.create_task(coro)  # keep a reference so it isn't collected mid-run
    background.add(task)
    task.add_done_callback(background.discard)


async def notify(title, text):
    esc = lambda t: json.dumps(t, ensure_ascii=False)
    await run("osascript", "-e", f"display notification {esc(text)} with title {esc(title)}")


async def find_session(app, pid):
    tty = await asyncio.to_thread(state.tty_of, pid)
    if not tty:
        return None
    sessions = [s for window in app.terminal_windows for tab in window.tabs for s in tab.sessions]
    ttys = await asyncio.gather(*(s.async_get_variable("tty") for s in sessions))
    return next((s for s, t in zip(sessions, ttys) if t == tty), None)


async def read_screen(session, full=True):
    """(visible lines unless full=False, text on the cursor's row before the cursor)."""
    contents = await session.async_get_screen_contents()
    row = lambda i: contents.line(i).string.replace("\x00", " ")  # NUL marks empty cells
    lines = [row(i).rstrip() for i in range(contents.number_of_lines)] if full else []
    while lines and not lines[-1]:
        lines.pop()
    cursor = contents.cursor_coord
    return lines, row(cursor.y)[:cursor.x]


def draft_of(typed):
    """Text already typed at Claude's prompt; the placeholder shown after the cursor is not a draft."""
    typed = typed.strip()
    return typed[1:].strip() if typed.startswith("❯") else ""


@web.middleware
async def protect(request, handler):
    if not access.peer_allowed(request.remote, NETWORKS) or request.host not in HOSTS:  # who may connect; DNS rebinding
        raise web.HTTPForbidden()
    if request.path.startswith("/ws/") and not access.origin_allowed(request.headers.get("Origin", ""), HOSTS):  # browsers always send Origin on a websocket handshake
        raise web.HTTPForbidden()
    if request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if request.headers.get("X-Sidebar") != "1" or (origin and not access.origin_allowed(origin, HOSTS)):  # header forces a CORS preflight cross-site
            raise web.HTTPForbidden()
    return await handler(request)


async def term_stream(session, ws):
    """Mirror the session's screen to the browser terminal, rewriting only changed rows."""
    previous, shape, cursor = [], None, None
    changed = asyncio.Event()  # persistent: ScreenStreamer drops updates that arrive between two waits

    async def on_update(_connection, _message):
        changed.set()

    token = await async_subscribe_to_screen_update_notification(session.connection, on_update, session.session_id)
    try:
        while True:
            changed.clear()  # before reading, so an update during the read triggers another pass
            contents = await session.async_get_screen_contents()
            columns = int(await session.async_get_variable("columns"))
            rows = [term.render_row(contents.line(i), columns) for i in range(contents.number_of_lines)]
            at = (contents.cursor_coord.x, contents.cursor_coord.y)
            if (columns, len(rows)) != shape:  # first frame or a resize: repaint everything
                previous, shape = [], (columns, len(rows))
            if rows != previous or at != cursor:
                await ws.send_json({"cols": columns, "rows": len(rows), "data": term.paint(rows, previous, at)})
                previous, cursor = rows, at
            try:  # the timeout is a safety net, not the update path
                await asyncio.wait_for(changed.wait(), 2.0)
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.02)  # coalesce bursts
    finally:
        try:
            await async_unsubscribe(session.connection, token)
        except Exception:  # the session may already be gone
            pass


async def term_input(session, ws):
    async for msg in ws:
        try:
            m = json.loads(msg.data) if msg.type == web.WSMsgType.TEXT else None
        except ValueError:
            continue
        text = m.get("d") if isinstance(m, dict) else None
        if isinstance(text, str) and len(text) <= 65536:
            await session.async_send_text(f"\x1b[200~{text}\x1b[201~" if m.get("t") == "paste" else text)


async def focused_id(app):
    window = app.current_terminal_window
    session = window and window.current_tab and window.current_tab.current_session
    tty = session and await session.async_get_variable("tty")
    if not tty:
        return None
    ttys = await asyncio.to_thread(lambda: {s["id"]: state.tty_of(s["pid"]) for s in live})
    return next((sid for sid, t in ttys.items() if t == tty), None)


def pr_key(s):
    return f"{s['cwd']}@{s['branch']}"


async def refresh_pr(s):
    out = await run("gh", "pr", "view", "--json", "number,url,state,isDraft", cwd=s["cwd"])
    try:
        pr = json.loads(out)
    except ValueError:
        pr = None
    prs[pr_key(s)] = (time.time(), pr)


def display_name(s):
    return aliases.get(s["id"], s["name"])


async def monitor(app):
    global live, active
    prev: dict[str, dict] = {}
    while True:
        live = await asyncio.to_thread(state.load)
        now = {s["id"]: s for s in live}
        active = await focused_id(app)
        focus = active if iterm_active else None
        for sid, s in now.items():
            old = prev.get(sid)
            if old and old["state"] != s["state"] and focus != sid:
                if s["state"] in state.NEEDS_ATTENTION:
                    spawn(notify(f"Claude {s['label']}: {s['project']}", display_name(s)))
                elif s["state"] == "idle":
                    unseen.add(sid)
            checked, pr = prs.get(pr_key(s), (0, None))
            if s["branch"] not in ("", "master", "main") and time.time() - checked > 120:
                prs[pr_key(s)] = (time.time(), pr)  # claim the slot before the slow gh call
                spawn(refresh_pr(s))
        for key in prs.keys() - {pr_key(s) for s in live}:
            del prs[key]
        if focus:
            unseen.discard(focus)
        for sid, old in prev.items():
            if sid not in now:
                unseen.discard(sid)
                closed.insert(0, {k: old[k] for k in ("id", "cwd", "project")} | {"name": old["name"], "closedAt": int(time.time() * 1000)})
                del closed[15:]
                save_json(CLOSED_FILE, closed)
        closed[:] = [c for c in closed if c["id"] not in now]
        known = now.keys() | {c["id"] for c in closed}  # live or resumable
        gone = aliases.keys() - known
        for sid in gone:
            del aliases[sid]
        if gone:
            save_json(ALIASES_FILE, aliases)
        kept = [sid for sid in order if sid in known]
        if len(kept) != len(order):
            order[:] = kept
            save_json(ORDER_FILE, order)
        prev = now
        await asyncio.sleep(1)


async def focus_monitor(connection, app):
    global iterm_active, active
    async with iterm2.FocusMonitor(connection) as monitor_:
        while True:
            update = await monitor_.async_get_next_update()
            if update.application_active is not None:
                iterm_active = update.application_active
            active = await focused_id(app)  # tab and window switches land here at once instead of on the next monitor tick


def view(s):
    return s | {"name": display_name(s), "auto": s["name"], "unseen": s["id"] in unseen, "active": s["id"] == active, "pr": prs.get(pr_key(s), (0, None))[1]}


async def open_tab(connection, app, command):
    window = app.current_terminal_window or await iterm2.Window.async_create(connection)
    tab = await window.async_create_tab()
    await tab.current_session.async_send_text(command + "\n")


async def main(connection):
    app = await iterm2.async_get_app(connection)

    async def sessions(request):
        return web.json_response({"remote": request.host != LOCAL_HOST, "groups": state.grouped([view(s) for s in live], order), "closed": [c | {"name": aliases.get(c["id"], c["name"])} for c in closed], "summary": summary(), "limits": state.limits(), "version": (STATIC / "index.html").stat().st_mtime_ns})

    def guarded(handler):
        async def inner(request):
            return await handler(request, await request.json())
        return inner

    async def activate(session):
        await session.async_activate(select_tab=True, order_window_front=True)

    def live_match(sid):
        match = next((s for s in live if s["id"] == sid), None)
        if not match:
            raise web.HTTPNotFound()
        return match

    async def live_session(sid):
        match = live_match(sid)
        session = await find_session(app, match["pid"])
        if not session:
            raise web.HTTPNotFound()
        return match, session

    async def focus(_, body):
        match, session = await live_session(body.get("id"))
        unseen.discard(match["id"])
        await activate(session)
        return web.Response(status=204)

    async def rename(_, body):
        sid = body.get("id")
        if sid not in {s["id"] for s in live} | {c["id"] for c in closed}:
            raise web.HTTPNotFound()
        name = "".join(ch for ch in str(body.get("name", "")) if ch.isprintable()).strip()[:80]
        if name:
            aliases[sid] = name
        else:
            aliases.pop(sid, None)  # empty resets to Claude's own name
        save_json(ALIASES_FILE, aliases)
        return web.Response(status=204)

    async def reorder(_, body):
        ids = body.get("ids")
        if not isinstance(ids, list) or not ids or not set(ids) <= {s["id"] for s in live}:
            raise web.HTTPBadRequest()
        order[:] = [sid for sid in order if sid not in ids] + ids  # only the relative order inside one project is ever compared
        save_json(ORDER_FILE, order)
        return web.Response(status=204)

    async def peek(request):
        sid = request.query.get("id")
        if request.query.get("mode") == "screen":
            _, session = await live_session(sid)
            return web.json_response({"lines": (await read_screen(session))[0]})
        return web.json_response({"items": await asyncio.to_thread(state.conversation, live_match(sid)["id"])})

    async def send(_, body):
        match, session = await live_session(body.get("id"))
        if body.get("key") == "esc":
            await session.async_send_text("\x1b")
            return web.Response(status=204)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000 or not text.isprintable():
            raise web.HTTPBadRequest()  # single line only: a newline would submit early
        if not body.get("force"):
            if match["state"] == "waiting":
                return web.json_response({"detail": "The session is waiting for input; text would go into that prompt."}, status=409)
            draft = draft_of((await read_screen(session, full=False))[1])
            if draft:
                return web.json_response({"detail": f"Unsent text at the prompt: {draft[:80]}"}, status=409)
        await session.async_send_text(text)
        await asyncio.sleep(0.15)  # Claude reads a text+Enter chunk as a paste, so Enter has to arrive on its own
        await session.async_send_text("\r")
        return web.Response(status=204)

    tickets: dict[str, tuple[str, float]] = {}  # one-time websocket tickets: a browser websocket can't send the X-Sidebar header

    async def term_ticket(_, body):
        match, _session = await live_session(body.get("id"))
        now = time.time()
        for stale in [t for t, (_, expires) in tickets.items() if expires < now]:
            del tickets[stale]
        token = secrets.token_urlsafe(24)
        tickets[token] = (match["id"], now + 30)
        return web.json_response({"ticket": token})

    async def term_ws(request):
        sid, expires = tickets.pop(request.query.get("ticket", ""), (None, 0))
        if not sid or expires < time.time():
            raise web.HTTPForbidden()
        _, session = await live_session(sid)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 17)
        await ws.prepare(request)
        tasks = [asyncio.create_task(term_stream(session, ws)), asyncio.create_task(term_input(session, ws))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            task.cancel()
        await ws.close()
        return ws

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
        if body.get("url") not in {pr["url"] for _, pr in prs.values() if pr}:
            raise web.HTTPBadRequest()
        await asyncio.to_thread(webbrowser.open, body["url"])
        return web.Response(status=204)

    server = web.Application(middlewares=[protect])
    server.add_routes([
        web.get("/", lambda _: web.FileResponse(STATIC / "index.html")),
        web.get("/api/sessions", sessions),
        web.post("/api/focus", guarded(focus)),
        web.get("/api/peek", peek),
        web.post("/api/send", guarded(send)),
        web.post("/api/term-ticket", guarded(term_ticket)),
        web.get("/ws/term", term_ws),
        web.static("/vendor", STATIC / "vendor"),
        web.post("/api/rename", guarded(rename)),
        web.post("/api/reorder", guarded(reorder)),
        web.post("/api/new", guarded(new)),
        web.post("/api/resume", guarded(resume)),
        web.post("/api/open-pr", guarded(open_pr)),
    ])
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, BIND, PORT).start()

    url = f"http://{'127.0.0.1' if BIND in ('0.0.0.0', '::') else BIND}:{PORT}/"
    reveal = lambda: iterm2.tool.async_register_web_view_tool(connection, "Claude Sessions", TOOL_ID, True, url)
    await reveal()

    @iterm2.RPC
    async def claude_next_waiting():
        waiting = sorted((s for s in live if s["state"] in state.NEEDS_ATTENTION), key=lambda s: s["since"] or 0)
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
    await asyncio.gather(monitor(app), focus_monitor(connection, app))


iterm2.run_forever(main)
