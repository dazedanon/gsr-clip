"""gsr-clip command-line interface.

Most subcommands are thin clients that talk to the running daemon over its Unix
socket. ``trim`` works offline using the highlight sidecar + ffmpeg.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from .config import load_config
from .highlights import load_sidecar, sidecar_path_for
from .trim import trim_clip


def _send(cmd: dict, timeout: float = 5.0) -> dict:
    cfg = load_config()
    sock_path = cfg.socket_path
    if not sock_path.exists():
        return {"ok": False, "error": f"daemon not running (no socket at {sock_path})"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock_path))
            s.sendall((json.dumps(cmd) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.decode()) if buf else {"ok": False, "error": "no response"}
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def _print_result(resp: dict) -> int:
    if resp.get("ok"):
        return 0
    print(f"error: {resp.get('error', 'unknown')}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------- trim
def _ffmpeg_trim(src: Path, start: float, end: float, out: Path) -> int:
    ok, msg = trim_clip(src, start, end, out)
    if ok:
        print(f"exported: {msg}")
        return 0
    print(f"error: {msg}", file=sys.stderr)
    return 1


def cmd_trim(args: argparse.Namespace) -> int:
    cfg = load_config()
    src = Path(args.file).expanduser()
    if not src.exists():
        # try sessions dir
        cand = cfg.paths.sessions_path / args.file
        if cand.exists():
            src = cand
        else:
            print(f"error: file not found: {args.file}", file=sys.stderr)
            return 1

    if args.highlight is not None:
        sc = sidecar_path_for(src)
        if not sc.exists():
            print(f"error: no sidecar {sc}", file=sys.stderr)
            return 1
        data = load_sidecar(sc)
        hs = data.get("highlights", [])
        idx = args.highlight - 1
        if idx < 0 or idx >= len(hs):
            print(f"error: highlight {args.highlight} out of range (have {len(hs)})", file=sys.stderr)
            return 1
        t = float(hs[idx]["time"])
        # A highlight is the reaction moment; lean the window backward.
        if args.padding is not None:
            start, end = max(0.0, t - args.padding), t + args.padding
        else:
            start, end = max(0.0, t - cfg.trim.highlight_pre), t + cfg.trim.highlight_post
        out = args.output or src.with_name(f"{src.stem}_h{args.highlight}.mp4")
    elif args.from_ is not None and args.to is not None:
        start, end = float(args.from_), float(args.to)
        out = args.output or src.with_name(f"{src.stem}_{int(start)}-{int(end)}.mp4")
    else:
        print("error: specify --highlight N or --from S --to S", file=sys.stderr)
        return 1

    return _ffmpeg_trim(src, start, end, Path(out))


def cmd_status(_: argparse.Namespace) -> int:
    resp = _send({"cmd": "status"})
    if not resp.get("ok"):
        return _print_result(resp)
    st = resp["status"]
    print(json.dumps(st, indent=2))
    return 0


def cmd_start(_: argparse.Namespace) -> int:
    from . import daemon

    daemon.main()
    return 0


def cmd_viewer(args: argparse.Namespace) -> int:
    from . import viewer

    viewer.serve(port=args.port, open_browser=not args.no_open)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    from . import storage

    cfg = load_config()
    if args.max_gb is not None:
        cfg.storage.max_size_gb = args.max_gb
    used = storage.total_bytes(cfg)
    gb = lambda b: b / (1024 ** 3)  # noqa: E731
    cap = cfg.storage.max_size_gb
    print(f"current usage: {gb(used):.2f} GB across {len(storage.list_recordings(cfg))} file(s)")
    if cap <= 0:
        print("no storage cap set (storage.max_size_gb = 0); nothing to prune")
        return 0
    print(f"cap: {cap:.2f} GB")
    if args.dry_run:
        over = max(0, used - cfg.storage.max_size_bytes)
        print(f"over by: {gb(over):.2f} GB" if over else "under cap; nothing to delete")
        return 0
    freed, deleted = storage.enforce_limit(cfg)
    for p in deleted:
        print(f"  deleted {p}")
    print(f"freed {gb(freed):.2f} GB; now at {gb(storage.total_bytes(cfg)):.2f} GB")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gsr-clip", description="GSR clip + session recorder")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("start", help="run the daemon (foreground)")
    sub.add_parser("stop", help="stop the running daemon")
    sub.add_parser("status", help="show daemon status")
    sub.add_parser("clip", help="save a replay clip from the buffer")
    sub.add_parser("highlight", help="add a highlight (or clip if no session)")
    sub.add_parser("session", help="manual override: toggle session start/stop")

    t = sub.add_parser("trim", help="trim a session using highlights or a time range")
    t.add_argument("file", help="session .mp4 (name or path)")
    t.add_argument("--highlight", type=int, help="highlight number (1-based)")
    t.add_argument("--from", dest="from_", type=float, help="start seconds")
    t.add_argument("--to", type=float, help="end seconds")
    t.add_argument("--padding", type=float, help="seconds around a highlight")
    t.add_argument("--output", help="output file path")

    v = sub.add_parser("viewer", help="open the local highlight viewer/trimmer")
    v.add_argument("--port", type=int, default=8723)
    v.add_argument("--no-open", action="store_true", help="don't open a browser")

    pr = sub.add_parser("prune", help="delete oldest recordings to stay under the size cap")
    pr.add_argument("--max-gb", type=float, help="override the configured cap (GiB)")
    pr.add_argument("--dry-run", action="store_true", help="show usage without deleting")

    osv = sub.add_parser("on-save", help="(internal) GSR save-hook callback")
    osv.add_argument("a")
    osv.add_argument("b", nargs="?", default="")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        return cmd_start(args)
    if args.command == "stop":
        return _print_result(_send({"cmd": "stop_daemon"}))
    if args.command == "status":
        return cmd_status(args)
    if args.command == "clip":
        return _print_result(_send({"cmd": "clip"}))
    if args.command == "highlight":
        return _print_result(_send({"cmd": "highlight"}))
    if args.command == "session":
        return _print_result(_send({"cmd": "session"}))
    if args.command == "trim":
        return cmd_trim(args)
    if args.command == "viewer":
        return cmd_viewer(args)
    if args.command == "prune":
        return cmd_prune(args)
    if args.command == "on-save":
        return _print_result(_send({"cmd": "on_save", "a": args.a, "b": args.b}))
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
