"""A browser terminal into a short-lived, locked-down Chalk sandbox.

Each WebSocket on `/ws` gets its own sandbox: gVisor kernel, no network egress, half a CPU,
a hard lifetime, and no credentials. The server proxies a PTY session between the sandbox
and the page at `/`, which renders it with ghostty-web.

Wire protocol on `/ws?cols=<n>&rows=<n>`:
  client -> server  text {"type": "input", "data": "<keystrokes>"}
  client -> server  text {"type": "resize", "cols": <n>, "rows": <n>}
  server -> client  binary frames: raw PTY output
  server -> client  text {"type": "status", "message"}: progress while the sandbox starts
  server -> client  text {"type": "ready", "expires_in": <seconds>}
  server -> client  text {"type": "busy" | "limited", "message"}, then close
  server -> client  text {"type": "ended", "reason"}, then close
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from chalkcompute import ComputeClass, ContainerSecurityPolicy, ContainerSession, KernelPolicy, Sandbox

# Interactive PTY sessions are exposed through the session client, which is not yet part of
# the public chalkcompute namespace.
from chalkcompute._connect import ConnectClient
from chalkcompute._container import ContainerSessionClient
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

PORT = int(os.environ.get("PORT", "8080"))
HERE = Path(__file__).resolve().parent
STATIC_DIR = Path(os.environ.get("TERMINAL_STATIC_DIR", str(HERE / "static")))

MAX_SESSIONS = int(os.environ.get("TERMINAL_MAX_SESSIONS", "6"))
SESSION_SECONDS = int(os.environ.get("TERMINAL_SESSION_SECONDS", "240"))
IDLE_SECONDS = int(os.environ.get("TERMINAL_IDLE_SECONDS", "90"))
PER_IP_PER_HOUR = int(os.environ.get("TERMINAL_PER_IP_PER_HOUR", "10"))
STARTS_PER_MINUTE = int(os.environ.get("TERMINAL_STARTS_PER_MINUTE", "12"))
WARM_POOL = int(os.environ.get("TERMINAL_WARM_POOL", "2"))
# A pooled sandbox waits at most this long for a visitor, so its lifetime covers the wait
# plus a full session.
POOL_MAX_AGE_SECONDS = int(os.environ.get("TERMINAL_POOL_MAX_AGE_SECONDS", "600"))
SANDBOX_CPU = os.environ.get("TERMINAL_SANDBOX_CPU", "500m")
SANDBOX_MEMORY = os.environ.get("TERMINAL_SANDBOX_MEMORY", "1Gi")
# Active sandbox names are unique per environment, so each gets a random suffix.
SANDBOX_NAME_PREFIX = "web-terminal-"
SANDBOX_TAG = {"app": "web-terminal-demo"}
MAX_INPUT_BYTES = 16 * 1024

# Printed before the shell starts. The visitor's sandbox has no network, so the "try"
# list sticks to things that work offline, plus one that demonstrates the isolation.
MOTD = """
\x1b[38;5;150m  chalk sandbox\x1b[0m  ·  gVisor kernel · no network · {minutes} minutes · yours alone

  Try:  \x1b[38;5;114mpython3\x1b[0m                      a REPL, Python 3.14
        \x1b[38;5;114muname -a\x1b[0m                     the sandboxed kernel
        \x1b[38;5;114mls /\x1b[0m                         look around
        \x1b[38;5;114mpython3 -c "import urllib.request as u; u.urlopen('https://example.com')"\x1b[0m
                                     egress is blocked, so this one fails

"""
# For terminals too narrow for MOTD, mostly phones.
MOTD_COMPACT = """
\x1b[38;5;150m  chalk sandbox\x1b[0m
  gVisor kernel · no network
  {minutes} minutes · yours alone

  Try \x1b[38;5;114mpython3\x1b[0m, \x1b[38;5;114muname -a\x1b[0m, \x1b[38;5;114mls /\x1b[0m

"""
MOTD_MIN_COLS = 76
PS1 = r"\[\e[38;5;150m\]sandbox\[\e[0m\]:\[\e[38;5;114m\]\w\[\e[0m\]$ "

log = logging.getLogger("terminal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def is_public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


class RateLimiter:
    """Caps sandbox starts globally, and per client IP when the real client IP is visible.

    Some gateways forward only their own private address, which every visitor shares; limiting
    by it would lock everyone out at once, so private addresses get only the global caps.
    """

    def __init__(self, per_hour: int, global_per_minute: int) -> None:
        super().__init__()
        self.per_hour = per_hour
        self.global_per_minute = global_per_minute
        self.starts: dict[str, collections.deque[float]] = collections.defaultdict(collections.deque)
        self.global_starts: collections.deque[float] = collections.deque()
        self.live: set[str] = set()

    @staticmethod
    def _trim(window: collections.deque[float], now: float, horizon: float) -> None:
        while window and now - window[0] > horizon:
            window.popleft()

    def acquire(self, ip: str) -> str | None:
        """Returns a refusal message, or None after recording a start for `ip`."""
        now = time.monotonic()
        self._trim(self.global_starts, now, 60)
        if len(self.global_starts) >= self.global_per_minute:
            return "Lots of people are launching sandboxes right now. Try again in a minute."
        if is_public(ip):
            if ip in self.live:
                return "You already have a terminal open in another tab."
            window = self.starts[ip]
            self._trim(window, now, 3600)
            if len(window) >= self.per_hour:
                return "That's a lot of sandboxes for one hour. Try again later."
            window.append(now)
            self.live.add(ip)
        self.global_starts.append(now)
        return None

    def release(self, ip: str) -> None:
        self.live.discard(ip)


state: dict[str, Any] = {}


def make_sandbox() -> Sandbox:
    # No network_policy means no egress at all (not even DNS). No chalk_identity and no
    # secrets: nothing inside the sandbox can call back into Chalk.
    sandbox = Sandbox(
        name=f"{SANDBOX_NAME_PREFIX}{uuid.uuid4().hex[:10]}",
        cpu=SANDBOX_CPU,
        memory=SANDBOX_MEMORY,
        lifetime=f"{POOL_MAX_AGE_SECONDS + SESSION_SECONDS + 60}s",
        compute_class=ComputeClass.HOST,
        security_policy=ContainerSecurityPolicy(kernel_policy=KernelPolicy.RESTRICTED),
        tags=SANDBOX_TAG,
    )
    sandbox.run()
    return sandbox


def terminate_quietly(sandbox: Sandbox) -> None:
    try:
        sandbox.terminate()
    except Exception:
        log.exception("terminate failed for %s", sandbox.id)


def reap_orphans() -> None:
    """Terminates sandboxes a previous replica left behind; their lifetime would also end them."""
    try:
        for info in Sandbox.list_all():
            if (info.name or "").startswith(SANDBOX_NAME_PREFIX) and info.status in ("pending", "running"):
                terminate_quietly(Sandbox.from_id(info.id))
    except Exception:
        log.exception("orphan reap failed")


class WarmPool:
    """Keeps `size` started sandboxes ready, because a cold start takes anywhere from 2 to 60 s.

    Every sandbox handed out is fresh: a visitor never gets one another visitor has used.
    """

    def __init__(self, size: int) -> None:
        super().__init__()
        self.size = size
        self.ready: collections.deque[tuple[float, Sandbox]] = collections.deque()
        self.filling = 0

    def fill(self) -> None:
        while len(self.ready) + self.filling < self.size:
            self.filling += 1
            asyncio.create_task(self._create())

    async def prime(self) -> None:
        """Starts one sandbox and waits for it, then fills the rest in the background."""
        self.filling += 1
        await self._create()
        self.fill()

    async def _create(self) -> None:
        try:
            t0 = time.monotonic()
            sandbox = await asyncio.to_thread(make_sandbox)
            self.ready.append((time.monotonic(), sandbox))
            log.info("pooled sandbox %s started in %.1fs", sandbox.id, time.monotonic() - t0)
        except Exception:
            log.exception("pooled sandbox start failed")
            await asyncio.sleep(10)
        finally:
            self.filling -= 1

    async def take(self) -> Sandbox:
        while self.ready:
            created, sandbox = self.ready.popleft()
            if time.monotonic() - created < POOL_MAX_AGE_SECONDS:
                self.fill()
                return sandbox
            asyncio.create_task(asyncio.to_thread(terminate_quietly, sandbox))
        self.fill()
        return await asyncio.to_thread(make_sandbox)

    async def recycle_forever(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            while self.ready and now - self.ready[0][0] >= POOL_MAX_AGE_SECONDS:
                _, stale = self.ready.popleft()
                await asyncio.to_thread(terminate_quietly, stale)
            self.fill()

    async def drain(self) -> None:
        while self.ready:
            _, sandbox = self.ready.popleft()
            await asyncio.to_thread(terminate_quietly, sandbox)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await asyncio.to_thread(reap_orphans)
    pool = WarmPool(WARM_POOL)
    # The first sandbox also builds the default image; wait for it so the replica only
    # reports ready once a visitor can get a terminal immediately.
    await pool.prime()
    recycler = asyncio.create_task(pool.recycle_forever())
    state["pool"] = pool
    state["slots"] = asyncio.Semaphore(MAX_SESSIONS)
    state["limiter"] = RateLimiter(PER_IP_PER_HOUR, STARTS_PER_MINUTE)
    state["active"] = 0
    state["sessions"] = ContainerSessionClient.from_connect(ConnectClient())
    yield
    recycler.cancel()
    await pool.drain()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse((HERE / "index.html").read_text())


# ghostty-web's bundle (its WASM is inlined) plus the empty chunk it imports.
STATIC_FILES = {"ghostty-web.js", "__vite-browser-external-2447137e.js"}


@app.get("/static/{name}")
async def static_file(name: str) -> FileResponse:
    if name not in STATIC_FILES:
        raise HTTPException(status_code=404)
    return FileResponse(
        STATIC_DIR / name,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/status")
async def status() -> JSONResponse:
    return JSONResponse({"ok": True, "active": state["active"], "capacity": MAX_SESSIONS})


def client_ip(websocket: WebSocket) -> str:
    forwarded = websocket.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return websocket.client.host if websocket.client else "unknown"


def clamp_dim(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    ip = client_ip(websocket)
    limiter: RateLimiter = state["limiter"]
    slots: asyncio.Semaphore = state["slots"]

    async def send(msg: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(msg))

    refusal = limiter.acquire(ip)
    if refusal is not None:
        await send({"type": "limited", "message": refusal})
        await websocket.close()
        return
    if slots.locked():
        limiter.release(ip)
        await send({"type": "busy", "message": "Every demo sandbox is in use. Try again in a minute."})
        await websocket.close()
        return

    async with slots:
        state["active"] += 1
        sandbox: Sandbox | None = None
        session: ContainerSession | None = None
        try:
            await send({"type": "status", "message": "Starting a fresh sandbox…"})
            t0 = time.monotonic()
            pool: WarmPool = state["pool"]
            sandbox = await pool.take()
            cols = clamp_dim(websocket.query_params.get("cols"), 20, 300, 100)
            rows = clamp_dim(websocket.query_params.get("rows"), 5, 120, 30)
            # The banner is display-only, so it goes straight to the browser rather than
            # through the shell, where its quotes would need escaping.
            banner = MOTD if cols >= MOTD_MIN_COLS else MOTD_COMPACT
            await websocket.send_bytes(banner.format(minutes=SESSION_SECONDS // 60).replace("\n", "\r\n").encode())
            session = await asyncio.to_thread(
                state["sessions"].new_session,
                sandbox.id,
                "/bin/bash",
                "--norc",
                "--noprofile",
                "-i",
                workdir="/workspace",
                env={"PS1": PS1, "TERM": "xterm-256color", "HOME": "/workspace"},
                pty=(cols, rows),
            )
            log.info("session for %s ready in %.1fs (sandbox %s)", ip, time.monotonic() - t0, sandbox.id)
            await send({"type": "ready", "expires_in": SESSION_SECONDS})
            reason = await proxy(websocket, session)
            await send({"type": "ended", "reason": reason})
            await websocket.close()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("session for %s failed", ip)
            try:
                await send({"type": "ended", "reason": "error"})
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            state["active"] -= 1
            limiter.release(ip)
            if sandbox is not None:
                await asyncio.to_thread(terminate_quietly, sandbox)


async def proxy(websocket: WebSocket, session: ContainerSession) -> str:
    """Pumps PTY output to the browser and keystrokes to the PTY; returns why it stopped."""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[Any] = asyncio.Queue()
    deadline = time.monotonic() + SESSION_SECONDS
    last_input = time.monotonic()

    # The SDK's event stream is a blocking iterator, so it gets its own thread.
    def read_events() -> None:
        try:
            for event in session.events():
                loop.call_soon_threadsafe(events.put_nowait, event)
        except Exception as e:
            loop.call_soon_threadsafe(events.put_nowait, e)
        loop.call_soon_threadsafe(events.put_nowait, None)

    threading.Thread(target=read_events, daemon=True).start()

    async def pump_output() -> str:
        while True:
            event = await events.get()
            if event is None or isinstance(event, Exception):
                return "exited"
            if isinstance(event, ContainerSession.OutputEvent):
                await websocket.send_bytes(event.data)
            elif isinstance(
                event,
                (
                    ContainerSession.ExitedEvent,
                    ContainerSession.FailedEvent,
                    ContainerSession.TimedOutEvent,
                    ContainerSession.ErrorEvent,
                ),
            ):
                return "exited"

    async def pump_input() -> str:
        nonlocal last_input
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                raise WebSocketDisconnect
            text = msg.get("text")
            if not text or len(text) > MAX_INPUT_BYTES:
                continue
            ctrl = json.loads(text)
            if ctrl.get("type") == "input" and isinstance(ctrl.get("data"), str):
                last_input = time.monotonic()
                await asyncio.to_thread(session.write, ctrl["data"].encode())
            elif ctrl.get("type") == "resize":
                cols = clamp_dim(ctrl.get("cols"), 20, 300, 100)
                rows = clamp_dim(ctrl.get("rows"), 5, 120, 30)
                await asyncio.to_thread(session.resize, cols, rows)

    async def watchdog() -> str:
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now >= deadline:
                return "time_limit"
            if now - last_input >= IDLE_SECONDS:
                return "idle"

    tasks = [asyncio.create_task(c) for c in (pump_output(), pump_input(), watchdog())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        return next(iter(done)).result()
    finally:
        for t in tasks:
            t.cancel()


if __name__ == "__main__":
    import uvicorn

    # Pings well under the gateway's 60 s idle timeout keep a quiet terminal connected.
    uvicorn.run(app, host="0.0.0.0", port=PORT, ws_ping_interval=20, ws_ping_timeout=20)
