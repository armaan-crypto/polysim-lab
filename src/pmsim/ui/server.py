"""Tiny local HTTP server for the dashboard (standard library only).

Listens on 127.0.0.1 (and ::1 when available, so `localhost` works).
Requests must come from the dashboard itself: the Host header must be a
loopback name, and writes need a JSON body and a same-origin Origin header,
so other websites open in your browser cannot drive the app.
Backtests are serialized with a lock; they are CPU-bound.
"""

from __future__ import annotations

import json
import socket
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from pmsim.config import Config
from pmsim.live.worker import run_forever
from pmsim.ui import api, desk_api
from pmsim.ui.desk_api import DeskContext, make_context

STATIC = resources.files("pmsim.ui") / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
MAX_BODY = 64_000
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")

Query = dict[str, str]
Body = dict[str, Any]
Result = dict[str, Any]


def _int(query: Query, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(query.get(key, default)), lo), hi)
    except ValueError:
        return default


def get_routes(cfg: Config, ctx: DeskContext) -> dict[str, Callable[[str, Query], Result]]:
    """Prefix -> handler(rest of path, query)."""
    return {
        "/api/meta": lambda _, q: api.meta(cfg),
        "/api/live/events": lambda _, q: desk_api.live_events(
            ctx, q.get("q", ""), q.get("tag", ""), _int(q, "limit", 60, 1, 200)
        ),
        "/api/live/event/": lambda rest, q: desk_api.live_event(ctx, rest),
        "/api/live/book": lambda _, q: desk_api.live_book(ctx, q.get("token", "")),
        "/api/live/history": lambda _, q: desk_api.live_history(
            ctx, q.get("token", ""), q.get("interval", "1d")
        ),
        "/api/portfolio": lambda _, q: desk_api.portfolio(ctx),
        "/api/bots": lambda _, q: desk_api.bots(ctx),
    }


def post_routes(cfg: Config, ctx: DeskContext) -> dict[str, Callable[[str, Body], Result]]:
    return {
        "/api/run": lambda _, b: api.run(cfg, b),
        "/api/compare": lambda _, b: api.compare(cfg, b),
        "/api/live/quote": lambda _, b: desk_api.quote(ctx, b),
        "/api/live/trade": lambda _, b: desk_api.trade(ctx, b),
        "/api/live/remove-limit": lambda _, b: desk_api.remove_limit(ctx, b),
        "/api/portfolio/reset": lambda _, b: desk_api.reset(ctx, b),
        "/api/bots/new": lambda _, b: desk_api.create_bot(ctx, b),
        "/api/bots/backtest": lambda _, b: desk_api.backtest_bot(ctx, b),
        "/api/bots/": lambda rest, b: desk_api.bot_action(ctx, *_split_action(rest)),
    }


def _split_action(rest: str) -> tuple[str, str]:
    bot_id, _, action = rest.partition("/")
    return bot_id, action


SLOW = ("/api/run", "/api/compare", "/api/bots/backtest")


def _match(routes: dict[str, Any], path: str) -> tuple[Any, str] | None:
    if path in routes:
        return routes[path], ""
    for prefix, fn in routes.items():
        if prefix.endswith("/") and path.startswith(prefix) and len(path) > len(prefix):
            return fn, path.removeprefix(prefix)
    return None


def make_handler(cfg: Config, ctx: DeskContext | None = None) -> type[BaseHTTPRequestHandler]:
    context = ctx or make_context(cfg)
    gets, posts = get_routes(cfg, context), post_routes(cfg, context)
    run_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "pmsim-ui"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # keep the terminal quiet

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in LOOPBACK

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._send(403, {"error": "dashboard only answers on localhost"})
                return
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            found = _match(gets, url.path)
            if found is None:
                self._static(url.path)
                return
            fn, rest = found
            self._json(lambda: fn(rest, query))

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or not self._same_origin():
                self._send(403, {"error": "requests must come from the dashboard page"})
                return
            path = urlparse(self.path).path
            found = _match(posts, path)
            if found is None:
                self._send(404, {"error": "not found"})
                return
            body = self._body()
            if body is None:
                return
            fn, rest = found
            if path in SLOW:
                self._json(lambda: self._locked(lambda: fn(rest, body)))
            else:
                self._json(lambda: fn(rest, body))

        def _locked(self, fn: Callable[[], Result]) -> Result:
            with run_lock:
                return fn()

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # non-browser clients (curl, tests) send no Origin
            return urlparse(origin).netloc == self.headers.get("Host")

        def _body(self) -> Body | None:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._send(415, {"error": "body must be application/json"})
                return None
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, {"error": "request too large"})
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "body must be JSON"})
                return None
            if not isinstance(body, dict):
                self._send(400, {"error": "body must be a JSON object"})
                return None
            return body

        def _json(self, fn: Callable[[], Result]) -> None:
            try:
                self._send(200, fn())
            except api.ApiError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # surfaced in the UI rather than a dead socket
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _send(self, code: int, payload: Result) -> None:
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _static(self, path: str) -> None:
            name = "index.html" if path in ("", "/") else path.lstrip("/")
            target = STATIC / name
            suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
            if "/" in name or ".." in name or suffix not in CONTENT_TYPES or not target.is_file():
                self._send(404, {"error": "not found"})
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES[suffix])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def make_server(
    cfg: Config, host: str = "127.0.0.1", port: int = 8765, ctx: DeskContext | None = None
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(cfg, ctx))


def serve(cfg: Config, host: str, port: int, open_browser: bool) -> None:
    ctx = make_context(cfg)
    server = make_server(cfg, host, port, ctx)
    servers = [server]
    if host == "127.0.0.1":  # also answer on ::1 so http://localhost works everywhere
        try:
            v6 = _V6Server(("::1", server.server_address[1]), make_handler(cfg, ctx))
            servers.append(v6)
            threading.Thread(target=v6.serve_forever, daemon=True).start()
        except OSError:
            pass
    stop = threading.Event()
    run_forever(ctx.desk, stop)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"pmsim dashboard at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for s in servers:
            s.server_close()
