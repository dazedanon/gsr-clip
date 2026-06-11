"""Configuration loading for gsr-clip.

Loads ``~/.config/gsr-clip/config.toml`` (if present) over a set of built-in
defaults. Everything is exposed as a single :class:`Config` dataclass so the
rest of the daemon never touches raw dicts.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.path.expanduser("~/.config/gsr-clip"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

# GSR can only save fixed replay-buffer durations on command. SIGUSR1 saves the
# whole buffer; these RT signals save fixed windows.
CLIP_BUCKET_SIGNALS = {
    10: "SIGRTMIN+1",
    30: "SIGRTMIN+2",
    60: "SIGRTMIN+3",
    300: "SIGRTMIN+4",
    600: "SIGRTMIN+5",
    1800: "SIGRTMIN+6",
}


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


@dataclass
class RecordingConfig:
    # always_on=True keeps a rolling replay buffer running at all times (instant
    # clips, persistent recording icon). always_on=False = sessions-only: GSR is
    # launched on session start and shut down on session end.
    always_on: bool = True
    buffer_seconds: int = 300
    clip_length: str = "full"  # "full" or one of CLIP_BUCKET_SIGNALS keys (as str/int)
    fps: int = 60
    resolution: str = "1920x1080"
    frame_rate_mode: str = "vfr"  # vfr|cfr|content — cfr = guaranteed constant 60fps (best for editors)
    audio_codec: str = "aac"  # aac is widely compatible for mp4 sharing
    capture_audio: bool = True
    capture_microphone: bool = True
    portal_token: str = "~/.local/state/gsr-clip/portal.token"

    @property
    def portal_token_path(self) -> Path:
        return Path(_expand(self.portal_token))


@dataclass
class PathsConfig:
    replays: str = "~/Videos/GSRClip/replays"
    sessions: str = "~/Videos/GSRClip/sessions"

    @property
    def replays_path(self) -> Path:
        return Path(_expand(self.replays))

    @property
    def sessions_path(self) -> Path:
        return Path(_expand(self.sessions))


@dataclass
class SessionConfig:
    auto_start: bool = True
    require_steam_game: bool = True
    auto_stop_on_exit: bool = True
    stop_on_focus_loss: bool = False
    focus_poll_seconds: float = 1.0
    pid_poll_seconds: float = 2.0
    debounce_seconds: float = 3.0


@dataclass
class HotkeysConfig:
    clip: str = "KEY_F9"
    session_override: str = "KEY_F9"
    double_tap_ms: int = 350
    enabled: bool = True


@dataclass
class GamepadConfig:
    enabled: bool = True
    button: str = "BTN_MODE"
    axis: str = "ABS_Z"
    threshold: int = 100


@dataclass
class TrimConfig:
    padding_seconds: float = 5.0
    # A highlight is the moment you *reacted* to — the action happened a few
    # seconds before the press. Default export window leans backward.
    highlight_pre: float = 10.0
    highlight_post: float = 2.0


@dataclass
class StorageConfig:
    # Hard cap on total bytes across replays + sessions + clips. When exceeded,
    # the oldest recordings are deleted (with their sidecars) until back under
    # the limit. 0 = unlimited. Interpreted as GiB (1024^3 bytes).
    max_size_gb: float = 0.0
    # Never delete files modified within this many seconds (protects the clip
    # being recorded right now and anything just exported).
    keep_recent_seconds: float = 30.0

    @property
    def max_size_bytes(self) -> int:
        return int(self.max_size_gb * 1024 ** 3) if self.max_size_gb > 0 else 0


@dataclass
class NotificationsConfig:
    session_start: bool = True
    session_stop: bool = True
    clips: bool = False
    highlights: bool = False
    # Sounds are audible even in fullscreen games where popups are suppressed.
    sound: bool = True
    sound_session_start: str = "/usr/share/sounds/freedesktop/stereo/service-login.oga"
    sound_session_stop: str = "/usr/share/sounds/freedesktop/stereo/complete.oga"
    sound_highlight: str = "/usr/share/sounds/freedesktop/stereo/bell.oga"


@dataclass
class Config:
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    hotkeys: HotkeysConfig = field(default_factory=HotkeysConfig)
    gamepad: GamepadConfig = field(default_factory=GamepadConfig)
    trim: TrimConfig = field(default_factory=TrimConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)

    @property
    def clips_path(self) -> Path:
        return self.paths.sessions_path.parent / "clips"

    # ---- runtime paths (not user-configurable) ----
    @property
    def runtime_dir(self) -> Path:
        base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        d = Path(base) / "gsr-clip"
        return d

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / "gsr-clipd.sock"

    @property
    def hook_path(self) -> Path:
        return self.runtime_dir / "on-save.sh"

    @property
    def session_name_path(self) -> Path:
        """File the on-save hook reads to learn the intended session filename."""
        return self.runtime_dir / "session-name"

    def ensure_dirs(self) -> None:
        for d in (
            self.paths.replays_path,
            self.paths.sessions_path,
            self.recording.portal_token_path.parent,
            self.runtime_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _merge_section(section_obj: Any, data: dict[str, Any]) -> None:
    for key, value in data.items():
        if hasattr(section_obj, key):
            setattr(section_obj, key, value)


def load_config(path: Path | None = None) -> Config:
    cfg = Config()
    path = path or CONFIG_PATH
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        for section in (
            "recording",
            "paths",
            "session",
            "hotkeys",
            "gamepad",
            "trim",
            "storage",
            "notifications",
        ):
            if section in raw and isinstance(raw[section], dict):
                _merge_section(getattr(cfg, section), raw[section])
    return cfg


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def dump_config(cfg: Config) -> str:
    """Serialize a Config back to TOML text (our schema is flat scalars)."""
    out = ["# gsr-clip configuration (managed by the GUI; safe to hand-edit).\n"]
    for section, vals in config_to_dict(cfg).items():
        out.append(f"[{section}]")
        for key, value in vals.items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    return "\n".join(out)


def save_config(cfg: Config, path: Path | None = None) -> Path:
    path = path or CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_config(cfg))
    return path


def config_to_dict(cfg: Config) -> dict[str, Any]:
    return {
        "recording": asdict(cfg.recording),
        "paths": asdict(cfg.paths),
        "session": asdict(cfg.session),
        "hotkeys": asdict(cfg.hotkeys),
        "gamepad": asdict(cfg.gamepad),
        "trim": asdict(cfg.trim),
        "storage": asdict(cfg.storage),
        "notifications": asdict(cfg.notifications),
    }
