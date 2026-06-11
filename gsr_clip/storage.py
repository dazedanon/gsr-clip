"""Disk-usage enforcement for gsr-clip.

Keeps the total size of all recordings (replays + sessions + exported clips)
under a configurable cap by deleting the oldest files first. Sidecars are
removed alongside their video. Files modified very recently are never touched,
which protects the clip currently being recorded and anything just exported.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .highlights import sidecar_path_for

log = logging.getLogger("gsr-clip.storage")

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".ts"}


@dataclass
class Rec:
    path: Path
    size: int
    mtime: float


def recording_dirs(cfg: Config) -> list[Path]:
    seen: list[Path] = []
    for d in (cfg.paths.replays_path, cfg.paths.sessions_path, cfg.clips_path):
        if d not in seen:
            seen.append(d)
    return seen


def list_recordings(cfg: Config) -> list[Rec]:
    recs: list[Rec] = []
    for d in recording_dirs(cfg):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            recs.append(Rec(p, st.st_size, st.st_mtime))
    return recs


def total_bytes(cfg: Config) -> int:
    return sum(r.size for r in list_recordings(cfg))


def enforce_limit(
    cfg: Config,
    protect: set[Path] | frozenset[Path] = frozenset(),
) -> tuple[int, list[Path]]:
    """Delete oldest recordings until total size is under the configured cap.

    Returns ``(bytes_freed, deleted_paths)``. No-op when the cap is unset.
    """
    limit = cfg.storage.max_size_bytes
    if limit <= 0:
        return 0, []

    recs = list_recordings(cfg)
    total = sum(r.size for r in recs)
    if total <= limit:
        return 0, []

    now = time.time()
    grace = cfg.storage.keep_recent_seconds
    protected = {Path(p).resolve() for p in protect}
    recs.sort(key=lambda r: r.mtime)  # oldest first

    freed = 0
    deleted: list[Path] = []
    for r in recs:
        if total - freed <= limit:
            break
        if now - r.mtime < grace:
            continue
        if r.path.resolve() in protected:
            continue
        try:
            r.path.unlink()
        except OSError:
            log.exception("could not delete %s", r.path)
            continue
        freed += r.size
        deleted.append(r.path)
        sc = sidecar_path_for(r.path)
        if sc.exists():
            try:
                sc.unlink()
            except OSError:
                log.debug("could not delete sidecar %s", sc)

    if deleted:
        log.info(
            "pruned %d recording(s), freed %.1f MB (cap %.1f GB)",
            len(deleted),
            freed / (1024 * 1024),
            cfg.storage.max_size_gb,
        )
    elif total > limit:
        log.warning(
            "over storage cap (%.1f GB) but nothing eligible to delete "
            "(all files within keep-recent window)",
            cfg.storage.max_size_gb,
        )
    return freed, deleted
