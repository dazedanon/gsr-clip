"""Highlight bookmark storage: a JSON sidecar per session file."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("gsr-clip.highlights")


def sidecar_path_for(session_file: Path) -> Path:
    """``Foo_2026-..._.mp4`` -> ``Foo_2026-..._.highlights.json``."""
    return session_file.with_suffix(".highlights.json")


@dataclass
class Highlight:
    time: float  # seconds since session start
    label: str

    def to_dict(self) -> dict:
        return {"time": round(self.time, 2), "label": self.label}


@dataclass
class SessionRecord:
    """In-memory state for an active session, serializable to a sidecar."""

    game: str
    appid: str | None
    watched_pid: int
    started_at: str  # ISO local time
    started_monotonic: float
    planned_name: str = ""  # stable target filename, set at session start
    session_file: str | None = None  # filled when finalized
    highlights: list[Highlight] = field(default_factory=list)

    def add_highlight(self, label: str | None = None) -> Highlight:
        t = max(0.0, time.monotonic() - self.started_monotonic)
        if label is None:
            label = f"Highlight {len(self.highlights) + 1}"
        h = Highlight(time=t, label=label)
        self.highlights.append(h)
        log.info("highlight @ %.1fs: %s", t, label)
        return h

    def to_dict(self) -> dict:
        return {
            "session_file": self.session_file,
            "game": self.game,
            "appid": self.appid,
            "watched_pid": self.watched_pid,
            "started_at": self.started_at,
            "highlights": [h.to_dict() for h in self.highlights],
        }

    def write_sidecar(self, session_file: Path) -> Path:
        self.session_file = session_file.name
        path = sidecar_path_for(session_file)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2))
        tmp.replace(path)
        log.info("wrote sidecar %s (%d highlights)", path, len(self.highlights))
        return path


def load_sidecar(path: Path) -> dict:
    with path.open() as fh:
        return json.load(fh)
