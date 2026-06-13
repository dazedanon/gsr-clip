"""Small desktop GUI for gsr-clip.

Gives a one-window control panel: daemon status, start/stop/restart of the
systemd --user service, a button to open the highlight trimmer, and a tabbed
editor for the config options that writes ``~/.config/gsr-clip/config.toml``.

Requires PySide6 (install the ``gui`` extra: ``pip install gsr-clip[gui]``).
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDoubleSpinBox,
        QFormLayout,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QPushButton,
        QSpinBox,
        QSystemTrayIcon,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError:  # pragma: no cover
    sys.stderr.write(
        "gsr-clip GUI needs PySide6.\n"
        "Install it with:  pip install 'gsr-clip[gui]'   (or: pip install PySide6)\n"
    )
    raise SystemExit(1)

from . import storage
from .audio_devices import default_selection, join_audio_sources, list_devices, parse_audio_string
from .cli import _send
from .config import CONFIG_PATH, Config, load_config, save_config

SERVICE = "gsr-clip.service"
VIEWER_PORT = 8723
ASSETS = Path(__file__).parent / "assets"


def app_icon() -> QIcon:
    svg = ASSETS / "icon.svg"
    if svg.exists():
        return QIcon(str(svg))
    return QIcon.fromTheme("media-record")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args, SERVICE],
        capture_output=True,
        text=True,
        check=False,
    )


def _service_active() -> bool:
    return _systemctl("is-active").stdout.strip() == "active"


class AudioSourcePicker(QWidget):
    """Checklist of gpu-screen-recorder audio sources (-a), auto-populated."""

    def __init__(self, audio: str, capture_audio: bool, capture_microphone: bool, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._list = QListWidget()
        self._list.setMinimumHeight(140)
        layout.addWidget(self._list)

        row = QHBoxLayout()
        self._hint = QLabel()
        self._hint.setStyleSheet("color: #888; font-size: 11px;")
        row.addWidget(self._hint, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(lambda: self.reload(audio=self.audio_string()))
        row.addWidget(refresh)
        layout.addLayout(row)

        saved = parse_audio_string(audio) or default_selection(capture_audio, capture_microphone)
        self.reload(saved)

    def reload(self, saved: list[str] | None = None, audio: str | None = None) -> None:
        if saved is None:
            saved = parse_audio_string(audio or "")
        saved_set = set(saved)
        self._list.clear()
        devices = list_devices()
        if not devices:
            self._hint.setText("gpu-screen-recorder not found or no devices listed")
            return
        matched: set[str] = set()
        for source_id, label in devices:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, source_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if source_id in saved_set else Qt.Unchecked)
            self._list.addItem(item)
            if source_id in saved_set:
                matched.add(source_id)
        extra = saved_set - matched
        for source_id in sorted(extra):
            item = QListWidgetItem(f"{source_id} (not connected)")
            item.setData(Qt.UserRole, source_id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self._list.addItem(item)
        n = sum(1 for i in range(self._list.count()) if self._list.item(i).checkState() == Qt.Checked)
        self._hint.setText(f"{n} selected — mixed together in the recording")

    def audio_string(self) -> str:
        sources: list[str] = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item.checkState() == Qt.Checked:
                sources.append(item.data(Qt.UserRole))
        return join_audio_sources(sources)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("gsr-clip")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(520)
        self._viewer: subprocess.Popen | None = None
        self.tray: QSystemTrayIcon | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh_status)
        self._timer.start(3000)
        self._build()
        self._build_tray()

    def _build(self) -> None:
        self.cfg = load_config()
        self._fields: dict[str, object] = {}

        root = QWidget()
        self.setCentralWidget(root)  # replaces & deletes any previous central widget
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        layout.addLayout(self._build_header())
        layout.addWidget(self._divider())
        layout.addWidget(self._build_tabs(), 1)
        layout.addLayout(self._build_footer())
        self.refresh_status()

    # ----------------------------------------------------------- header
    def _build_header(self) -> QVBoxLayout:
        box = QVBoxLayout()
        self.status_label = QLabel("…")
        self.status_label.setTextFormat(Qt.RichText)
        box.addWidget(self.status_label)

        row = QHBoxLayout()
        self.btn_trimmer = QPushButton("Open Trimmer")
        self.btn_trimmer.clicked.connect(self.open_trimmer)
        self.btn_start = QPushButton("Start")
        self.btn_start.clicked.connect(lambda: self._service("start"))
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(lambda: self._service("stop"))
        self.btn_restart = QPushButton("Restart")
        self.btn_restart.clicked.connect(lambda: self._service("restart"))
        for b in (self.btn_trimmer, self.btn_start, self.btn_stop, self.btn_restart):
            row.addWidget(b)
        row.addStretch(1)
        box.addLayout(row)
        return box

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    # ------------------------------------------------------------ tabs
    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.addTab(self._tab_recording(), "Recording")
        tabs.addTab(self._tab_sessions(), "Sessions")
        tabs.addTab(self._tab_storage(), "Storage")
        tabs.addTab(self._tab_trim(), "Trim")
        tabs.addTab(self._tab_hotkeys(), "Input")
        tabs.addTab(self._tab_notifications(), "Notifications")
        return tabs

    def _form(self) -> tuple[QWidget, QFormLayout]:
        w = QWidget()
        f = QFormLayout(w)
        f.setLabelAlignment(Qt.AlignRight)
        return w, f

    def _combo(self, key: str, options: list[tuple[str, object]], value: object) -> QComboBox:
        c = QComboBox()
        for label, val in options:
            c.addItem(label, val)
        idx = next((i for i, (_, v) in enumerate(options) if v == value), 0)
        c.setCurrentIndex(idx)
        self._fields[key] = c
        return c

    def _check(self, key: str, value: bool) -> QCheckBox:
        c = QCheckBox()
        c.setChecked(bool(value))
        self._fields[key] = c
        return c

    def _int(self, key: str, value: int, lo: int, hi: int) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(value))
        self._fields[key] = s
        return s

    def _float(self, key: str, value: float, lo: float, hi: float, step: float = 0.5) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(1)
        s.setValue(float(value))
        self._fields[key] = s
        return s

    def _text(self, key: str, value: str) -> QLineEdit:
        e = QLineEdit(str(value))
        self._fields[key] = e
        return e

    def _tab_recording(self) -> QWidget:
        w, f = self._form()
        r = self.cfg.recording
        f.addRow("Mode", self._combo(
            "recording.always_on",
            [("Sessions-only (record only in-game)", False), ("Always-on (rolling buffer)", True)],
            r.always_on,
        ))
        f.addRow("FPS", self._int("recording.fps", r.fps, 10, 480))
        f.addRow("Resolution", self._text("recording.resolution", r.resolution))
        f.addRow("Frame rate mode", self._combo(
            "recording.frame_rate_mode",
            [("Variable (vfr)", "vfr"), ("Constant 60fps (cfr)", "cfr"), ("Content", "content")],
            r.frame_rate_mode,
        ))
        f.addRow("Replay buffer (s)", self._int("recording.buffer_seconds", r.buffer_seconds, 5, 3600))
        f.addRow("Audio codec", self._combo(
            "recording.audio_codec", [("aac", "aac"), ("opus", "opus")], r.audio_codec,
        ))
        picker = AudioSourcePicker(r.audio, r.capture_audio, r.capture_microphone)
        self._fields["recording.audio"] = picker
        f.addRow("Audio to capture", picker)
        return w

    def _tab_sessions(self) -> QWidget:
        w, f = self._form()
        s = self.cfg.session
        f.addRow("Auto-start on game focus", self._check("session.auto_start", s.auto_start))
        f.addRow("Require Steam game", self._check("session.require_steam_game", s.require_steam_game))
        f.addRow("Auto-stop when game exits", self._check("session.auto_stop_on_exit", s.auto_stop_on_exit))
        f.addRow("Stop on focus loss (alt-tab)", self._check("session.stop_on_focus_loss", s.stop_on_focus_loss))
        f.addRow("Focus poll (s)", self._float("session.focus_poll_seconds", s.focus_poll_seconds, 0.2, 10))
        f.addRow("PID poll (s)", self._float("session.pid_poll_seconds", s.pid_poll_seconds, 0.2, 10))
        f.addRow("Debounce (s)", self._float("session.debounce_seconds", s.debounce_seconds, 0, 30))
        return w

    def _tab_storage(self) -> QWidget:
        w, f = self._form()
        st = self.cfg.storage
        f.addRow("Max total size (GB, 0=∞)", self._float("storage.max_size_gb", st.max_size_gb, 0, 100000, step=5))
        f.addRow("Keep files newer than (s)", self._float("storage.keep_recent_seconds", st.keep_recent_seconds, 0, 600))
        self.usage_label = QLabel("…")
        f.addRow("Current usage", self.usage_label)
        prune = QPushButton("Prune now")
        prune.clicked.connect(self.prune_now)
        f.addRow("", prune)
        self._refresh_usage()
        return w

    def _tab_trim(self) -> QWidget:
        w, f = self._form()
        t = self.cfg.trim
        f.addRow("Highlight: seconds before press", self._float("trim.highlight_pre", t.highlight_pre, 0, 120))
        f.addRow("Highlight: seconds after press", self._float("trim.highlight_post", t.highlight_post, 0, 120))
        f.addRow("CLI padding (s)", self._float("trim.padding_seconds", t.padding_seconds, 0, 120))
        return w

    def _tab_hotkeys(self) -> QWidget:
        w, f = self._form()
        h, g = self.cfg.hotkeys, self.cfg.gamepad
        f.addRow("Keyboard hotkeys enabled", self._check("hotkeys.enabled", h.enabled))
        f.addRow("Clip/highlight key", self._text("hotkeys.clip", h.clip))
        f.addRow("Double-tap window (ms)", self._int("hotkeys.double_tap_ms", h.double_tap_ms, 100, 1000))
        f.addRow("Gamepad enabled", self._check("gamepad.enabled", g.enabled))
        f.addRow("Gamepad button", self._text("gamepad.button", g.button))
        f.addRow("Gamepad axis", self._text("gamepad.axis", g.axis))
        f.addRow("Gamepad threshold", self._int("gamepad.threshold", g.threshold, 1, 255))
        return w

    def _tab_notifications(self) -> QWidget:
        w, f = self._form()
        n = self.cfg.notifications
        f.addRow("Notify: session start", self._check("notifications.session_start", n.session_start))
        f.addRow("Notify: session stop", self._check("notifications.session_stop", n.session_stop))
        f.addRow("Notify: clips", self._check("notifications.clips", n.clips))
        f.addRow("Notify: highlights", self._check("notifications.highlights", n.highlights))
        f.addRow("Play sounds (audible in-game)", self._check("notifications.sound", n.sound))
        f.addRow("Sound: session start", self._text("notifications.sound_session_start", n.sound_session_start))
        f.addRow("Sound: session stop", self._text("notifications.sound_session_stop", n.sound_session_stop))
        f.addRow("Sound: highlight", self._text("notifications.sound_highlight", n.sound_highlight))
        return w

    # ---------------------------------------------------------- footer
    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        path_lbl = QLabel(f"<span style='color:#888'>{CONFIG_PATH}</span>")
        path_lbl.setTextFormat(Qt.RichText)
        row.addWidget(path_lbl)
        row.addStretch(1)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self.reload_config)
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.save)
        row.addWidget(reload_btn)
        row.addWidget(save_btn)
        return row

    # ------------------------------------------------------------ tray
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("gsr-clip")
        menu = QMenu()
        menu.addAction("Show / Hide window").triggered.connect(self._toggle_window)
        menu.addSeparator()
        menu.addAction("Open Trimmer").triggered.connect(self.open_trimmer)
        menu.addSeparator()
        self.tray_start = menu.addAction("Start daemon")
        self.tray_start.triggered.connect(lambda: self._service("start"))
        self.tray_stop = menu.addAction("Stop daemon")
        self.tray_stop.triggered.connect(lambda: self._service("stop"))
        menu.addAction("Restart daemon").triggered.connect(lambda: self._service("restart"))
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.Trigger:  # left click
            self._toggle_window()

    def _toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        # Closing the window quits the app (the tray only offers quick actions
        # while it's open; it does not keep the app alive in the background).
        self._cleanup()
        event.accept()
        QApplication.quit()

    def _quit(self) -> None:
        self._cleanup()
        QApplication.quit()

    def _cleanup(self) -> None:
        if self._viewer is not None and self._viewer.poll() is None:
            self._viewer.terminate()

    # --------------------------------------------------------- actions
    def _apply_fields(self) -> None:
        for key, widget in self._fields.items():
            section, attr = key.split(".", 1)
            obj = getattr(self.cfg, section)
            if isinstance(widget, AudioSourcePicker):
                if attr == "audio":
                    sources = parse_audio_string(widget.audio_string())
                    setattr(obj, "audio", join_audio_sources(sources))
                    obj.capture_audio = bool(sources)
                    obj.capture_microphone = "default_input" in sources
                continue
            if isinstance(widget, QComboBox):
                value = widget.currentData()
            elif isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            else:  # QLineEdit
                value = widget.text()
            setattr(obj, attr, value)

    def save(self) -> None:
        self._apply_fields()
        try:
            save_config(self.cfg)
        except OSError as exc:
            QMessageBox.critical(self, "gsr-clip", f"Could not write config:\n{exc}")
            return
        self._refresh_usage()
        if _service_active():
            ans = QMessageBox.question(
                self,
                "gsr-clip",
                "Settings saved.\n\nRestart the daemon now to apply them?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans == QMessageBox.Yes:
                self._service("restart")
        else:
            QMessageBox.information(self, "gsr-clip", "Settings saved.")

    def reload_config(self) -> None:
        self._build()

    def _service(self, action: str) -> None:
        proc = _systemctl(action)
        if proc.returncode != 0:
            QMessageBox.warning(self, "gsr-clip", proc.stderr.strip() or f"{action} failed")
        QTimer.singleShot(600, self.refresh_status)

    def open_trimmer(self) -> None:
        url = f"http://127.0.0.1:{VIEWER_PORT}/"
        if self._viewer is None or self._viewer.poll() is not None:
            self._viewer = subprocess.Popen(
                [sys.executable, "-m", "gsr_clip.cli", "viewer", "--no-open", "--port", str(VIEWER_PORT)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            QTimer.singleShot(800, lambda: webbrowser.open(url))
        else:
            webbrowser.open(url)

    def prune_now(self) -> None:
        self._apply_fields()
        try:
            save_config(self.cfg)
        except OSError:
            pass
        freed, deleted = storage.enforce_limit(self.cfg)
        self._refresh_usage()
        QMessageBox.information(
            self,
            "gsr-clip",
            f"Freed {freed / (1024 ** 3):.2f} GB by removing {len(deleted)} file(s)."
            if deleted else "Nothing to prune — under the cap.",
        )

    def _refresh_usage(self) -> None:
        if not hasattr(self, "usage_label"):
            return
        used = storage.total_bytes(self.cfg)
        n = len(storage.list_recordings(self.cfg))
        self.usage_label.setText(f"{used / (1024 ** 3):.2f} GB across {n} file(s)")

    def refresh_status(self) -> None:
        active = _service_active()
        parts = []
        if active:
            parts.append("<b style='color:#3ecf8e'>daemon running</b>")
            resp = _send({"cmd": "status"})
            st = resp.get("status") if isinstance(resp, dict) else None
            if st:
                parts.append(f"mode: {st.get('mode', '?')}")
                if st.get("session_active"):
                    parts.append(f"recording <b>{st.get('game')}</b> ({st.get('highlights', 0)} ★)")
                else:
                    parts.append("idle")
                if not st.get("kdotool"):
                    parts.append("<span style='color:#ff6b6b'>kdotool missing</span>")
        else:
            parts.append("<b style='color:#ff6b6b'>daemon stopped</b>")
        self.status_label.setText(" &nbsp;·&nbsp; ".join(parts))
        self.btn_start.setEnabled(not active)
        self.btn_stop.setEnabled(active)
        if self.tray is not None:
            self.tray.setToolTip("gsr-clip — " + ("running" if active else "stopped"))
            self.tray_start.setEnabled(not active)
            self.tray_stop.setEnabled(active)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("gsr-clip")
    app.setApplicationDisplayName("gsr-clip")
    app.setWindowIcon(app_icon())
    win = MainWindow()
    app.setQuitOnLastWindowClosed(True)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
