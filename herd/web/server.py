"""Zero-dependency HTTP server for the Project Herd Story-First WebUI."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .inspector import RunIndex

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _send_json(handler: BaseHTTPRequestHandler, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_static(handler: BaseHTTPRequestHandler, rel_path: str) -> None:
    rel = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
    target = (STATIC_DIR / rel).resolve()
    if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
        _send_json(handler, {"error": "not found"}, HTTPStatus.NOT_FOUND)
        return
    data = target.read_bytes()
    ctype, _ = mimetypes.guess_type(target.name)
    if target.suffix == ".js":
        ctype = "application/javascript; charset=utf-8"
    elif target.suffix == ".css":
        ctype = "text/css; charset=utf-8"
    elif target.suffix == ".html":
        ctype = "text/html; charset=utf-8"
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", ctype or "application/octet-stream")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(index: RunIndex):
    class HerdWebHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path == "/api/runs":
                _send_json(self, {"runs": index.list_runs()})
                return
            if path.startswith("/api/run/"):
                parts = [p for p in path[len("/api/run/") :].split("/") if p]
                try:
                    if len(parts) == 2 and parts[1] == "meta":
                        _send_json(self, index.get(parts[0]).meta())
                        return
                    if len(parts) == 3 and parts[1] == "tick":
                        tick_idx = int(parts[2])
                        _send_json(self, index.get(parts[0]).tick_data(tick_idx))
                        return
                    if len(parts) == 2 and parts[1] == "all_ticks":
                        cache = index.get(parts[0])
                        cache.refresh()
                        ticks = cache.ticks if cache.ticks else [cache.opening_tick]
                        _send_json(self, {"ticks": ticks})
                        return
                except (KeyError, IndexError, ValueError) as exc:
                    _send_json(self, {"error": str(exc)}, HTTPStatus.NOT_FOUND)
                    return
                except Exception as exc:
                    _send_json(self, {"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    return
                _send_json(self, {"error": "unknown api endpoint"}, HTTPStatus.NOT_FOUND)
                return
            _send_static(self, path)

    return HerdWebHandler


def create_server(
    *,
    runs_dir: Path | None = None,
    db_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    index = RunIndex(runs_dir=runs_dir, db_path=db_path)
    return ThreadingHTTPServer((host, port), make_handler(index))


def serve(
    *,
    runs_dir: Path | None = None,
    db_path: Path | None = None,
    host: str = "0.0.0.0",
    port: int = 8765,
) -> None:
    server = create_server(runs_dir=runs_dir, db_path=db_path, host=host, port=port)
    print(f"Project Herd WebUI listening on http://{host}:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
