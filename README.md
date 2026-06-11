# gsr-clip

A small, self-owned clip + session recorder built on **`gpu-screen-recorder`** (GSR).
One GSR process provides a ShadowPlay-style rolling replay buffer **and** full game
sessions, with auto-start for Steam games, highlight bookmarks, and a trim helper.

See [`PLAN.md`](PLAN.md) for the full design.

## Features

- **Always-on replay buffer** — press a hotkey to save the last N seconds.
- **Auto-started sessions** — when a Steam game gains focus, a full session starts
  automatically and records *all* future footage; it stops when the game exits
  (not when you alt-tab away).
- **Highlight bookmarks** — tap during a session to drop timestamps into a JSON
  sidecar next to the video.
- **Single encoder** — refuses to start if another GSR (e.g. Vice) is running.
- **Trim** — `gsr-clip trim` cuts highlights out with ffmpeg (`-c copy`).
- **Desktop GUI** — `gsr-clip-gui` (PySide6): status, service controls, one-click
  trimmer, and a settings editor for `config.toml`.

## Requirements

- `gpu-screen-recorder`
- `kdotool` (AUR) — required for active-window detection on KDE Wayland
- `ffmpeg` (trim), `libnotify`/`notify-send` (notifications)
- Python 3.11+, membership in the `input` group (for evdev hotkeys)

## Install

### Option A — Arch package (recommended)

Builds a real pacman package, so `gpu-screen-recorder`, `ffmpeg`, `kdotool`,
etc. are tracked as proper dependencies (no more "orphan" surprises):

```bash
cd ~/Projects/gsr-clip
makepkg -si            # builds from committed HEAD, installs with pacman
# GUI is optional; install it explicitly (NOT --asdeps, or it becomes an orphan):
sudo pacman -S pyside6 libnotify
systemctl --user daemon-reload
systemctl --user enable --now gsr-clip.service
gsr-clip status
```

Console scripts land at `/usr/bin/{gsr-clip,gsr-clipd,gsr-clip-gui}`, the
service at `/usr/lib/systemd/user/gsr-clip.service`, and the launcher/icon
system-wide. Copy the example config once:
`mkdir -p ~/.config/gsr-clip && cp /usr/share/doc/gsr-clip/config.example.toml ~/.config/gsr-clip/config.toml`.

> Builds use the committed git state, so `git commit` your changes before
> rebuilding.

### Option B — venv (development)

```bash
cd ~/Projects/gsr-clip
./packaging/install.sh        # GSR_CLIP_GUI=0 to skip the GUI deps
systemctl --user enable --now gsr-clip.service
gsr-clip status
```

### Migrating from the venv install to the package

The venv install drops user-level overrides that shadow the packaged files.
Remove them so the package's copies take effect:

```bash
systemctl --user disable --now gsr-clip.service
rm -f ~/.config/systemd/user/gsr-clip.service        # use the packaged unit
rm -f ~/.local/share/applications/gsr-clip.desktop   # use the packaged launcher
rm -f ~/.local/share/icons/hicolor/scalable/apps/gsr-clip.svg
rm -rf ~/Projects/gsr-clip/.venv                     # optional
systemctl --user daemon-reload
systemctl --user enable --now gsr-clip.service
```

## Usage

| Action | Default |
|---|---|
| Save a clip (no session) | tap **F9** / guide+LT |
| Drop a highlight (in session) | tap **F9** / guide+LT |
| Manual session start/stop | **double-tap F9** |

```bash
gsr-clip status                       # daemon + session state
gsr-clip clip                         # save replay buffer now (always-on mode)
gsr-clip session                      # manual session toggle
gsr-clip viewer                       # open the local highlight viewer/trimmer
gsr-clip trim Game_2026-..._.mp4 --highlight 1
gsr-clip trim Game_2026-..._.mp4 --from 140 --to 160
gsr-clip prune --dry-run              # show disk usage vs the size cap
gsr-clip prune                        # delete oldest recordings to fit the cap
```

### Desktop app

`gsr-clip-gui` opens a small control window (installed when you build with the
`gui` extra, and added to your app launcher as **gsr-clip**):

- live daemon status + **Start / Stop / Restart** the service
- **Open Trimmer** — launches the highlight viewer and opens it in your browser
- tabbed editor for every config option (Recording / Sessions / Storage / Trim /
  Input / Notifications); **Save** writes `~/.config/gsr-clip/config.toml` and
  offers to restart the daemon to apply changes
- **Prune now** button on the Storage tab
- **System tray** icon — left-click toggles the window; right-click for Open
  Trimmer / Start / Stop / Restart / Quit. Closing the window keeps it running
  in the tray.

Install the GUI deps with `pip install 'gsr-clip[gui]'` (the installer does this
by default; set `GSR_CLIP_GUI=0` to skip).

### Highlight viewer

`gsr-clip viewer` launches a small localhost web app (no extra deps) that lists
your sessions, plays each video with highlight markers on the timeline, and
exports lossless clips (ffmpeg `-c copy`) to `~/Videos/GSRClip/clips/`:

- click a marker to snap the selection to the **10s before** the press (the reaction window)
- drag the **In/Out handles** on the timeline — the video scrubs to that frame live so you can see exactly where to cut
- set In/Out from the playhead with keys `i` / `o`, export with `e`
- choose a **share preset** (Lossless, or re-encode to 10/25/50 MB for Discord) before **Export clip**

### Storage cap

Set `storage.max_size_gb` (GiB) to cap the total size of all recordings
(replays + sessions + exported clips). When exceeded, the **oldest** recordings
and their sidecars are auto-deleted until back under the limit — enforced after
every save/export and at daemon startup. The file being recorded right now (and
anything newer than `keep_recent_seconds`) is never deleted. `0` = unlimited.
Run `gsr-clip prune` to enforce it manually.

### Modes

- `recording.always_on = true` — rolling buffer always running (instant clips, persistent recording icon).
- `recording.always_on = false` — **sessions-only**: GSR runs only during a Steam game, so the recording icon only appears in-game.

### Controller button (Steam Input on KDE Wayland)

Steam Input grabs the physical pad and, on Wayland, injects any button→key
binding through the RemoteDesktop portal — so the synthetic key never reaches
`evdev`. The reliable bridge is a KDE **global shortcut** that runs a gsr-clip
action (KDE sees compositor-level keys, including Steam's injected ones):

```bash
gsr-clip install-shortcut            # binds F10 -> `gsr-clip highlight`
```

Then in Steam → the game's **Controller Layout**, bind the button (e.g. **Guide**,
or a **Guide + LT** chord) to keyboard **F10**. Pressing it now drops a highlight
(or saves a clip outside a session), with the usual sound cue.

Use a key *other* than the keyboard hotkey (F9) to avoid a double-trigger; pass
`--key`/`--action` to change them (e.g. `gsr-clip install-shortcut --key F10 --action clip`).
Prefer the raw pad instead? Disable Steam Input for the controller and the
daemon reads **Guide + LT** directly via evdev.

### Notifications

Session start/stop and highlights also play a short **sound** (configurable),
which is audible in fullscreen games where popups are suppressed.

## Layout

```
gsr_clip/
  config.py       config load + defaults
  gsr_process.py  spawn GSR, signals, single-encoder guard
  steam_gate.py   SteamAppId/environ gate (+ PPID fallback)
  focus.py        kdotool active window + game-name resolution
  watcher.py      focus watcher (auto-start) + PID watcher (auto-stop)
  hotkeys.py      evdev keyboard single/double tap
  gamepad.py      guide+LT combo
  highlights.py   sidecar JSON
  trim.py         lossless stream-copy + target-size re-encode (share presets)
  storage.py      size-cap enforcement (oldest-first auto-delete)
  viewer.py       localhost web viewer/trimmer (range-capable video server)
  web/            viewer UI (index.html, app.js, style.css)
  gui.py          PySide6 desktop app (status, controls, settings editor)
  daemon.py       asyncio loop, state machine, IPC socket
  cli.py          gsr-clip start|stop|status|clip|highlight|session|trim|viewer|prune|install-shortcut
```
