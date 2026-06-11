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

## Requirements

- `gpu-screen-recorder`
- `kdotool` (AUR) — required for active-window detection on KDE Wayland
- `ffmpeg` (trim), `libnotify`/`notify-send` (notifications)
- Python 3.11+, membership in the `input` group (for evdev hotkeys)

## Install

```bash
git clone <repo> ~/Projects/gsr-clip
cd ~/Projects/gsr-clip
./packaging/install.sh
# switching from Vice? disable it first:
systemctl --user disable --now vice.service
systemctl --user enable --now gsr-clip.service
gsr-clip status
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
  trim.py         lossless ffmpeg stream-copy trims
  viewer.py       localhost web viewer/trimmer (range-capable video server)
  web/            viewer UI (index.html, app.js, style.css)
  daemon.py       asyncio loop, state machine, IPC socket
  cli.py          gsr-clip start|stop|status|clip|highlight|session|trim|viewer
```
