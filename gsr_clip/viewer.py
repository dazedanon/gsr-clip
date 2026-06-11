"""Local web viewer/trimmer for session recordings + highlight sidecars.

Starts a tiny localhost HTTP server (stdlib only) that serves the session videos
(with HTTP Range support so seeking works) and a single-page UI. Trims are
lossless ffmpeg stream-copies exported to ``GSRClip/clips/``.
"""

from __future__ import annotations

import json
import logging
import re
import socketserver
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import storage
from .config import Config, load_config
from .highlights import load_sidecar, sidecar_path_for
from .trim import transcode_clip, trim_clip

log = logging.getLogger("gsr-clip.viewer")

# Share/export presets. "copy" = lossless; others are target sizes in MB.
PRESETS = [
    {"id": "copy", "label": "Lossless (copy)", "target_mb": None},
    {"id": "10", "label": "10 MB (Discord)", "target_mb": 10},
    {"id": "25", "label": "25 MB (Discord)", "target_mb": 25},
    {"id": "50", "label": "50 MB (Discord Nitro)", "target_mb": 50},
]
_PRESET_MB = {p["id"]: p["target_mb"] for p in PRESETS}

WEB_DIR = Path(__file__).parent / "web"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".mp4": "video/mp4",
}


def _safe_session_file(cfg: Config, name: str) -> Path | None:
    """Resolve a session filename to a path inside the sessions dir (no traversal)."""
    base = cfg.paths.sessions_path.resolve()
    candidate = (base / Path(name).name).resolve()
    if candidate.parent == base and candidate.exists():
        return candidate
    return None


class Handler(BaseHTTPRequestHandler):
    cfg: Config  # set on the server class

    def log_message(self, fmt: str, *args) -> None:  # quieter
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ----- helpers -----
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_video(self, name: str) -> None:
        path = _safe_session_file(self.cfg, name)
        if path is None:
            self.send_error(404)
            return
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = RANGE_RE.search(rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break
                remaining -= len(chunk)

    # ----- routing -----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route == "/" or route == "/index.html":
            self._send_static(WEB_DIR / "index.html")
        elif route in ("/app.js", "/style.css"):
            self._send_static(WEB_DIR / route.lstrip("/"))
        elif route == "/api/config":
            self._send_json(
                {
                    "highlight_pre": self.cfg.trim.highlight_pre,
                    "highlight_post": self.cfg.trim.highlight_post,
                    "presets": PRESETS,
                }
            )
        elif route == "/api/sessions":
            self._send_json(self._list_sessions())
        elif route.startswith("/video/"):
            self._serve_video(route[len("/video/"):])
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/trim":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "bad json"}, 400)
            return
        self._handle_trim(payload)

    # ----- api impl -----
    def _list_sessions(self) -> list[dict]:
        out = []
        sessions = self.cfg.paths.sessions_path
        if not sessions.exists():
            return out
        for mp4 in sorted(sessions.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
            highlights = []
            sc = sidecar_path_for(mp4)
            game = mp4.stem
            if sc.exists():
                try:
                    data = load_sidecar(sc)
                    highlights = data.get("highlights", [])
                    game = data.get("game", game)
                except (OSError, json.JSONDecodeError):
                    pass
            out.append(
                {
                    "name": mp4.name,
                    "game": game,
                    "size": mp4.stat().st_size,
                    "mtime": mp4.stat().st_mtime,
                    "highlights": highlights,
                }
            )
        return out

    def _handle_trim(self, payload: dict) -> None:
        name = payload.get("file", "")
        src = _safe_session_file(self.cfg, name)
        if src is None:
            self._send_json({"ok": False, "error": "session not found"}, 404)
            return
        try:
            start = float(payload["start"])
            end = float(payload["end"])
        except (KeyError, TypeError, ValueError):
            self._send_json({"ok": False, "error": "invalid start/end"}, 400)
            return
        preset = str(payload.get("preset", "copy"))
        target_mb = _PRESET_MB.get(preset)
        clips_dir = self.cfg.paths.sessions_path.parent / "clips"
        label = payload.get("label") or f"{int(start)}-{int(end)}"
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(label)).strip("-") or "clip"
        suffix = "" if target_mb is None else f"_{int(target_mb)}mb"
        out = clips_dir / f"{src.stem}_{safe_label}{suffix}.mp4"
        n = 1
        while out.exists():
            out = clips_dir / f"{src.stem}_{safe_label}{suffix}-{n}.mp4"
            n += 1
        if target_mb is None:
            ok, msg = trim_clip(src, start, end, out)
        else:
            ok, msg = transcode_clip(src, start, end, out, float(target_mb))
        if ok:
            log.info("exported clip %s (preset=%s)", out, preset)
            # Keep total usage under the cap; the just-made clip is protected by
            # the keep-recent window in storage.enforce_limit.
            try:
                storage.enforce_limit(self.cfg)
            except Exception:  # noqa: BLE001
                log.exception("storage enforcement after export failed")
            self._send_json({"ok": True, "output": str(out), "name": out.name})
        else:
            self._send_json({"ok": False, "error": msg}, 500)


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(cfg: Config | None = None, port: int = 8723, open_browser: bool = True) -> None:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    handler = type("BoundHandler", (Handler,), {"cfg": cfg})
    server = _Server(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"gsr-clip viewer running at {url}")
    print("  sessions:", cfg.paths.sessions_path)
    print("  exports :", cfg.paths.sessions_path.parent / "clips")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
