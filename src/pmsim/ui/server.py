"""Tiny local HTTP server for the dashboard (standard library only).

Binds to 127.0.0.1 by default. Runs are serialized with a lock: they are
CPU-bound, and one at a time keeps timings honest.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlparse

from pmsim.config import Config
from pmsim.ui import api

STATIC = resources.files("pmsim.ui") / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
MAX_BODY = 64_000


def make_handler(cfg: Config) -> type[BaseHTTPRequestHandler]:
    run_lock = threading.Lock()

    def locked(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with run_lock:
            return fn()

    class Handler(BaseHTTPRequestHandler):
        server_version = "pmsim-ui"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # keep the terminal quiet

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            path = url.path
            if path == "/api/meta":
                self._json(lambda: api.meta(cfg))
            elif path == "/api/paper":
                self._json(lambda: api.paper_sessions(cfg))
            elif path.startswith("/api/paper/"):
                name = path.removeprefix("/api/paper/")
                tail = int(query.get("tail", "100"))
                self._json(lambda: api.paper_session(cfg, name, min(max(tail, 1), 1000)))
            elif path == "/api/catalog":
                limit = int(query.get("limit", "100"))
                self._json(lambda: api.catalog(cfg, query.get("q", ""), limit))
            else:
                self._static(path)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            body = self._body()
            if body is None:
                return
            if path == "/api/run":
                self._json(lambda: locked(lambda: api.run(cfg, body)))
            elif path == "/api/compare":
                self._json(lambda: locked(lambda: api.compare(cfg, body)))
            else:
                self._send(404, {"error": "not found"})

        def _body(self) -> dict[str, Any] | None:
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

        def _json(self, fn: Callable[[], dict[str, Any]]) -> None:
            try:
                self._send(200, fn())
            except api.ApiError as exc:
                self._send(400, {"error": str(exc)})
            except Exception as exc:  # surfaced in the UI rather than a dead socket
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _send(self, code: int, payload: dict[str, Any]) -> None:
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


def make_server(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(cfg))


def serve(cfg: Config, host: str, port: int, open_browser: bool) -> None:
    server = make_server(cfg, host, port)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"pmsim dashboard at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
