# gsr-clip — Implementation Plan

Standalone clip + session recorder using **only `gpu-screen-recorder`** as the capture backend. No Vice dependency.

---

## Decisions (locked in)

| Topic | Choice |
|---|---|
| **Project name** | `gsr-clip` (daemon: `gsr-clipd`, CLI: `gsr-clip`) |
| **Output directory** | `~/Videos/GSRClip/` (`replays/` + `sessions/` subdirs) |
| **Session start** | **Automatic** — daemon auto-starts a full session when a Steam game gains focus (no hotkey). Double-tap remains a manual override. |
| **Session stop** | Driven by **watched game PID exit**, *not* focus loss — alt-tabbing out of the game must NOT stop recording |
| **Steam gate** | Primary signal `/proc/<pid>/environ` `SteamAppId`; PPID ancestry walk as fallback |
| **Focus detection** | `kdotool` is a **required** dependency (KDE Wayland); no interactive `queryWindowInfo` fallback |
| **Single encoder** | Daemon refuses to start if another `gpu-screen-recorder` is already running (Vice guard) |
| **Vice migration** | Disable `vice.service` **manually** when switching — install script does **not** touch Vice |
| **Capture mode** | `always_on` toggle: rolling buffer always-on (instant clips) **or** sessions-only (GSR runs only during a game; recording icon only in-game) |
| **Notifications** | `notify-send` on session start/stop, plus a short **sound** cue on start/stop/highlight (audible in fullscreen where popups are suppressed) |
| **Session filenames** | Rename in `-sc` hook: `{GameName}_{YYYY-MM-DD}_{HH-MM-SS}.mp4` (game name + full date/time) |

---

## Goals

Build a small, self-owned daemon that provides:

| Feature | Behavior |
|---|---|
| **Always-on replay buffer** | Rolling buffer in background (ShadowPlay / Medal style) |
| **Clip hotkey** | Save the replay buffer on demand (full buffer, or a fixed GSR bucket) |
| **Auto session start** | When a Steam game gains focus, auto-start a full session (records all future footage, not just the buffer) |
| **Steam-only gate** | Only auto-start when the focused app is a real Steam game (`SteamAppId` in environ) |
| **Auto session stop** | When the watched game process exits, stop and finalize the session file |
| **Manual override** | Double-tap can force-start or force-stop a session if auto-detection misses |
| **Highlight bookmarks** | Hotkey during session drops timestamp markers on a timeline |
| **Session trim** | Cut highlights out of the full session afterward |

## Non-goals (v1)

- No Vice dependency or code reuse from Vice packages
- No game whitelist (any Steam game with a valid `SteamAppId` auto-records)
- No auto-start for *unfocused* game launches — start trigger is **focus**, so you must bring the game to the foreground once
- No web UI / cloud sharing / Discord RPC (can add later)
- No `gpu-screen-recorder-ui` overlay dependency
- No second GSR process (single encoder always)

---

## Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│  gsr-clipd (Python daemon, ~400–600 lines)                  │
│                                                             │
│  ┌──────────────┐  ┌─────────────┐  ┌────────────────────┐  │
│  │ Hotkey layer │  │ Session mgr │  │ Highlight store    │  │
│  │ evdev kbd +  │  │ start/stop  │  │ JSON sidecar per   │  │
│  │ gamepad combo│  │ SIGRTMIN    │  │ session file       │  │
│  └──────┬───────┘  └──────┬──────┘  └─────────┬──────────┘  │
│         │                 │                    │            │
│  ┌──────┴─────────────────┴────────────────────┴────────┐  │
│  │ Focus watcher (auto-start)   +   PID watcher (auto-stop)│  │
│  │ poll active window → SteamAppId? → start session        │  │
│  │ then poll watched game PID → exit → stop session         │  │
│  └──────────────────────────┬─────────────────────────────┘  │
│                             │ signals                        │
└─────────────────────────────┼────────────────────────────────┘
                              ▼
                   gpu-screen-recorder (ONE process)
                   replay buffer + session via -ro / SIGRTMIN
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
        ~/Videos/GSRClip/replays      ~/Videos/GSRClip/sessions/
        (SIGUSR1 clips)                 (SIGRTMIN session files)
```

**Key design decision:** one GSR process handles replay **and** full session via `-ro` + `SIGRTMIN`. This avoids the dual-portal / dual-encoder OOM issue when spawning a second GSR for sessions.

---

## GSR command (the core)

Single long-running process:

```bash
gpu-screen-recorder \
  -w portal \
  -restore-portal-session yes \
  -portal-session-token-filepath "$HOME/.local/state/gsr-clip/portal.token" \
  -f 60 \
  -s 1920x1080 \
  -r 300 \
  -c mp4 \
  -a "default_output|default_input" \
  -o "$HOME/Videos/GSRClip/replays" \
  -ro "$HOME/Videos/GSRClip/sessions" \
  -sc "$HOME/.local/share/gsr-clip/on-save.sh"
```

### Signal map

| Signal | When daemon sends it | Effect |
|---|---|---|
| `SIGUSR1` | Clip hotkey (outside session) | Save **entire** replay buffer from RAM |
| `SIGRTMIN` | Session start / session stop | Toggle full session recording to `-ro` dir |
| `SIGRTMIN+2` | Optional quick-save | Save last 30s from buffer without ending session |

**Clip length is not freely configurable.** GSR's `SIGUSR1` saves the *whole* buffer; partial saves only exist as fixed buckets: `SIGRTMIN+1`=10s, `+2`=30s, `+3`=60s, `+4`=5min, `+5`=10min, `+6`=30min. The clip hotkey defaults to `SIGUSR1` (full buffer); `clip_length` in config maps to the nearest bucket signal if set to one of those values.

**Single-encoder guard:** at startup the daemon runs `pgrep -x gpu-screen-recorder` (and checks for a running `vice.service`). If another encoder is live it **refuses to start** with a clear message rather than spawning a second capture (which would fight over the portal/GPU). Disable Vice first.

### Portal capture (HDR)

- `-w portal` tonemaps HDR→SDR on KDE Wayland (fixes dark clips)
- One-time KDE screen-share prompt; token file makes restarts silent
- Token path: `~/.local/state/gsr-clip/portal.token`

---

## User workflows

### A) Instant clip (always available)

1. Daemon running, GSR buffering last 300s
2. Press **guide+LT** or **F9** (single tap)
3. Daemon sends `SIGUSR1` (full buffer) → clip saved to `replays/`
4. No notification (silent)

### B) Full session with highlights (automatic)

1. Launch Steam game and bring its window to the foreground
2. **Focus watcher** notices the active window changed:
   - Resolves focused window PID (kdotool)
   - Reads `/proc/<pid>/environ` → finds `SteamAppId` (PPID ancestry as fallback)
   - No session active → auto-start:
     - Sends `SIGRTMIN` → GSR begins session file in `sessions/`
     - `notify-send`: "Recording started — {GameName}"
     - Records session start time + game name to state file; starts **PID watcher**
3. During session: **single F9 / guide+LT** → append highlight `{time, label}` to sidecar JSON (silent)
4. **Alt-tab to Discord / browser / Cursor → session keeps recording** (stop is PID-driven, not focus-driven)
5. Quit game → watched game PID dies → PID watcher fires → `SIGRTMIN` again → session finalized
   - `notify-send`: "Recording stopped — saved to {path}"
6. Open session + sidecar in trim tool (v1: CLI; v2: simple local UI)

**Once a session is active, the focus watcher stops auto-starting** until the current session ends (no double-recording when switching to a second game; single encoder records one session at a time).

### C) Manual override (start / stop)

- **Double-tap F9** while a Steam game is focused but no session started (auto-detection missed) → force-start (still runs the Steam gate)
- **Double-tap F9** while a session is active → force-stop even if the game is still running (early stop, or if PID detection fails)

---

## Steam gate (auto-start trigger)

### Why

Only auto-start a session for a **real Steam game**, never for Steam UI, Cursor, Discord, the desktop, etc. On this system games run inside systemd app scopes (`app-steam@…`) and Proton's pressure-vessel (`bwrap`, new PID namespaces), so a plain PPID-walk is unreliable. The robust signal is the game's environment.

### Primary signal: `SteamAppId` in environ

```
is_steam_game(pid) -> (bool, appid|None):
    env = read_proc_environ(pid)          # /proc/<pid>/environ, NUL-separated
    appid = env.get("SteamAppId") or env.get("STEAM_APPID")
    if appid and appid != "0":
        return True, appid
    # fallback: also accept STEAM_COMPAT_* markers (Proton)
    if any(k.startswith("STEAM_COMPAT") for k in env):
        return True, None
    return False, None
```

`environ` is readable for our own UID. `SteamAppId` also doubles as a stable game identity (for naming / future whitelist).

### Fallback signal: PPID ancestry

If `environ` is empty/unreadable, walk `/proc/<pid>/stat` PPIDs upward looking for a process whose `comm == "steam"` or whose `/proc/<ppid>/exe` resolves under `~/.local/share/Steam/`. Treat the cgroup (`/proc/<pid>/cgroup` containing `app-steam`) as an additional hint. This is best-effort only.

### Reject list (never treat as "the game")

- `steam`, `steamwebhelper`, `steam-runtime-launcher-service`
- `srt-bwrap`, `pv-adverb`, `steam.sh`, `reaper`

### Auto-start decision (focus watcher)

```
on_focus_change(pid):
    if session_active:          return   # one session at a time
    if pid is None:             return
    if comm(pid) in REJECT_LIST: return
    ok, appid = is_steam_game(pid)
    if not ok:                  return
    if debounced_recently():    return   # ignore focus flicker
    watched_pid  = pid
    watched_comm = read_comm(pid)        # backup
    game_name    = resolve_game_name(pid, appid)
    start_session_recording()
    start_pid_watcher(watched_pid)
```

### PID watcher loop (auto-stop)

```
every 2 seconds:
    if pid_exists(watched_pid):
        continue
    if watched_comm and any_pgrep(watched_comm) is_steam_game:
        watched_pid = that_pid      # Proton respawn edge case: re-acquire
        continue
    else:
        stop_session_recording()
        finalize_highlights_sidecar()
        notify_send("Recording stopped — saved to {path}")
```

**Important:** stop is driven by the **game PID exiting**, never by focus loss. Alt-tabbing to Discord/browser keeps recording. Steam itself stays running after you quit the game, so we watch the game PID, not Steam.

---

## Focused PID detection (KDE Wayland)

### `kdotool` (required dependency)

```bash
paru -S kdotool
kdotool getactivewindow getwindowpid
```

The focus watcher polls this every ~1–2s to get the active window's PID. `kdotool` works by loading a transient KWin script over D-Bus — it is **non-interactive** (unlike `org.kde.KWin queryWindowInfo`, which forces the user to click a window and is therefore unusable for automation; we do **not** use it).

### If `kdotool` is missing or returns nothing

- The daemon logs a clear error at startup and **disables auto-start** (the buffer/clip path still works).
- Manual override double-tap can still start a session, but with no reliable focused-PID there is **no auto-stop** — the user must double-tap to stop.
- `install.sh` checks for `kdotool` and prompts to install it.

### Game name resolution

`resolve_game_name(pid, appid)` tries, in order:

1. Steam `appinfo`/`libraryfolders` lookup by `appid` (best: real title)
2. KWin `caption` (window title) for the active window
3. `kdotool` window class / process `comm`

Sanitize to alnum + hyphens, max ~48 chars.

---

## Hotkey layer

### Keyboard (evdev)

| Action | Default | Notes |
|---|---|---|
| Clip (replay) | `KEY_F9` single tap | `SIGUSR1` (full buffer) when no session active |
| Highlight | `KEY_F9` single tap | During active session only |
| Manual override | `KEY_F9` double tap | Force-start (Steam gate still applies) / force-stop |

Double-tap window: ~350ms. Note: detecting a *single* tap means waiting out the double-tap window, so single-tap actions are delayed ~350ms (harmless for "save last 300s"). Sessions normally start **automatically** via the focus watcher — the double-tap is only a fallback.

### Gamepad (evdev, gamepad device — not keyboard emulation)

| Combo | Action |
|---|---|
| `BTN_MODE` + `ABS_Z >= 100` | Same as single F9 (clip or highlight) |

Listens on gamepad endpoint (e.g. `event3`), not the keyboard interface (`event4`).

**Steam Input caveat:** when Steam Input is enabled for a controller, Steam often grabs the physical device and/or consumes `BTN_MODE` (guide), so `guide+LT` may never reach our evdev listener while in-game. Test this; it may only work with Steam Input disabled for the controller, or require reading the device before Steam claims it. The keyboard F9 path is the reliable fallback.

### Hotkey routing state machine

```
single F9 / guide+LT:
    if session_active → add_highlight()
    else                → save_clip()          # SIGUSR1, full buffer

double F9:
    if session_active → stop_session()          # manual override (early stop)
    else              → start_session()          # manual override; runs steam gate
                                                 # (normally auto-started by focus watcher)
```

---

## Highlight storage

Per session file, a sidecar JSON:

```
~/Videos/GSRClip/sessions/Vice_2026-06-10_20-30-45.mp4
~/Videos/GSRClip/sessions/Vice_2026-06-10_20-30-45.highlights.json
```

```json
{
  "session_file": "Vice_2026-06-10_20-30-45.mp4",
  "game": "Vice",
  "watched_pid": 31234,
  "started_at": "2026-06-10T20:30:00",
  "highlights": [
    { "time": 142.5, "label": "Highlight 1" },
    { "time": 387.2, "label": "Highlight 2" }
  ]
}
```

`time` = seconds elapsed since session start (monotonic clock at bookmark moment).

---

## Session file naming

GSR writes session files to `-ro` with its own default names. The `-sc` hook **renames on save** to:

```
{GameName}_{YYYY-MM-DD}_{HH-MM-SS}.mp4
```

Examples:

- `Vice_2026-06-10_20-30-45.mp4`
- `Rocket-League_2026-06-11_14-05-22.mp4`

Rules:

- `GameName` from Steam appmanifest (by `SteamAppId`), else KWin `caption`/class, else process `comm` at session start (sanitized: alnum + hyphens, max ~48 chars)
- Date/time is the **full** local timestamp captured at session **start** (stable stem so incremental highlight sidecars match the final file)
- Daemon computes the final name at start, writes it to a state file before sending the stop `SIGRTMIN`; the `-sc` hook forwards the saved path to the daemon, which renames the `regular` save to that name

Sidecar follows the same stem:

```
Vice_2026-06-10_20-30-45.highlights.json
```

**Build-time verify:** confirm GSR `-sc` callback args for `regular` saves in `-ro` mode (path + type).

---

## Trim (v1 vs v2)

### v1 — CLI trim helper

```bash
gsr-clip trim session.mp4 --highlight 1   # cuts highlight 1 ± padding
gsr-clip trim session.mp4 --from 140 --to 160
```

Implementation: `ffmpeg -ss -to -c copy` (fast, keyframe-aligned).

Uses highlight JSON for `--highlight N` with configurable padding (default ±5s).

### v1 — local web viewer (implemented)

`gsr-clip viewer` starts a stdlib localhost HTTP server (range-capable video
serving) + a single-page UI: session list, `<video>` player with highlight
markers on the timeline, click-to-seek, per-highlight **Export ±padding**, and
custom In/Out range export. All exports are lossless `ffmpeg -c copy` to
`~/Videos/GSRClip/clips/`. No extra dependencies.

---

## Project layout

```
~/Projects/gsr-clip/
├── pyproject.toml
├── README.md
├── PLAN.md                    # this file
├── gsr_clip/
│   ├── __init__.py
│   ├── config.py              # TOML load/save
│   ├── daemon.py              # main asyncio loop, state machine
│   ├── gsr_process.py         # spawn GSR, signal control, health check, single-encoder guard
│   ├── steam_gate.py          # SteamAppId/environ check, PPID fallback, reject list
│   ├── focus.py               # kdotool active-window PID + game-name resolution
│   ├── watcher.py             # focus watcher (auto-start) + PID watcher (auto-stop)
│   ├── hotkeys.py             # evdev keyboard + double-tap override
│   ├── gamepad.py             # guide+LT combo listener
│   ├── highlights.py          # sidecar read/write
│   └── cli.py                 # gsr-clip start|stop|status|trim|clip
├── scripts/
│   └── on-save.sh             # -sc callback: rename session files only
└── packaging/
    ├── gsr-clip.service       # systemd user unit
    └── install.sh             # venv, service, config defaults, kdotool check (does NOT disable Vice)
```

### Config: `~/.config/gsr-clip/config.toml`

```toml
[recording]
buffer_seconds = 300
clip_length = "full"        # "full" (SIGUSR1) or one of: 10, 30, 60, 300, 600, 1800 (nearest GSR bucket)
fps = 60
resolution = "1920x1080"
audio_codec = "aac"         # aac for mp4 sharing (Discord/browsers); opus is GSR default but less compatible
capture_audio = true
capture_microphone = true
portal_token = "/home/dazed/.local/state/gsr-clip/portal.token"

[paths]
replays = "/home/dazed/Videos/GSRClip/replays"
sessions = "/home/dazed/Videos/GSRClip/sessions"

[session]
auto_start = true               # auto-start a session when a Steam game gains focus
start_trigger = "focus"         # focus watcher drives start
require_steam_game = true       # only auto-start when SteamAppId is present
auto_stop_on_exit = true        # stop when watched game PID exits
stop_on_focus_loss = false      # alt-tab must NOT stop the session
focus_poll_seconds = 1          # active-window poll interval
pid_poll_seconds = 2            # watched-PID poll interval
debounce_seconds = 3            # ignore focus flicker before (re)starting

[hotkeys]
clip = "KEY_F9"             # single tap
session_override = "KEY_F9" # double-tap force start/stop

[gamepad]
enabled = true
button = "BTN_MODE"
axis = "ABS_Z"
threshold = 100

[trim]
padding_seconds = 5

[notifications]
session_start = true   # notify-send when session recording begins
session_stop = true    # notify-send when session recording ends
clips = false          # replay clips: silent
highlights = false     # bookmark dropped: silent
```

---

## Dependencies

| Package | Purpose | Install |
|---|---|---|
| `gpu-screen-recorder` | Capture backend | Already installed |
| `python-evdev` | Global hotkeys + gamepad | `pip install evdev` or pacman |
| `tomli` / `tomli-w` | Config (Py3.11+ has tomllib) | stdlib on 3.14 |
| `kdotool` | Focused PID on KDE Wayland (required for auto-start/stop) | `paru -S kdotool` (AUR, **required**) |
| `ffmpeg` | Trim helper | Likely already installed |
| `libnotify` / `notify-send` | Session start/stop notifications | `pacman -S libnotify` |

**Python only** for v1 — no Rust, Qt, or web stack.

---

## systemd integration

`~/.config/systemd/user/gsr-clip.service`:

```ini
[Unit]
Description=GSR clip + session recorder
After=graphical-session.target

[Service]
Type=simple
ExecStart=%h/Projects/gsr-clip/.venv/bin/gsr-clip start
Restart=on-failure
RestartSec=3
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=graphical-session.target
```

No `PYTHONPATH` overlays — installed via venv or `pip install -e .` in project dir.

**Environment caveat:** portal capture, `kdotool`, and `notify-send` all need `WAYLAND_DISPLAY` / `XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS`. `PassEnvironment=` only forwards vars that already exist in the systemd *user manager* — which isn't guaranteed. The working `vice.service` already solves this (Plasma imports the graphical env into the user manager via `graphical-session.target`); copy whatever Vice does. If unset, `install.sh` should run `systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS` (or drop an env file) before enabling.

The daemon also requires read access to `/dev/input/event*` — i.e. membership in the `input` group. `install.sh` verifies this and warns (a re-login is needed after adding the group).

---

## Coexistence with Vice

| Option | Recommendation |
|---|---|
| Run both simultaneously | **No** — two GSR processes fight for capture. Daemon enforces this with a single-encoder guard at startup. |
| Disable Vice when switching | **Manual step** (not done by `install.sh`): `systemctl --user disable --now vice.service` |
| Output directories | Separate: `~/Videos/GSRClip/` vs `~/Videos/Vice/` |
| Portal token | Use a dedicated token: `~/.local/state/gsr-clip/portal.token` (do not share with Vice while both could run) |

### Switching from Vice to gsr-clip (manual)

```bash
systemctl --user disable --now vice.service
systemctl --user enable --now gsr-clip.service
```

---

## Edge cases & mitigations

| Case | Mitigation |
|---|---|
| Focus desktop / non-game | No `SteamAppId` → no auto-start |
| Focus Steam library/UI | Reject list + no `SteamAppId` → no auto-start |
| Alt-tab out of game mid-session | `stop_on_focus_loss=false` → keeps recording (stop is PID-driven) |
| Focus flicker / fast alt-tab | `debounce_seconds` before (re)starting |
| Switch to a 2nd Steam game mid-session | Ignored — one session at a time until current game exits (single encoder) |
| HDR dark clips | Portal capture + saved token |
| Portal permission lost | Log error, notify user to re-approve |
| kdotool missing | Auto-start disabled + warn; buffer/clips still work; manual double-tap start = no auto-stop |
| Proton forks new PID | Watch original PID; on exit, re-acquire by `comm` if a matching Steam game PID still exists |
| Game crash | PID dies → auto-stop (good) |
| GSR dies | Daemon restarts GSR; session state lost (log warning) |
| Another GSR already running (Vice) | Daemon refuses to start (single-encoder guard) |
| Highlight during non-session | Treated as replay clip (`SIGUSR1`) |
| Controller combo during session | Same as single F9 → highlight |

---

## Build phases

### Phase 1 — Core daemon (MVP)

- [ ] GSR single-process launcher with portal + `-ro`
- [ ] Single-encoder guard (refuse if another GSR / Vice running)
- [ ] `SIGUSR1` clip, `SIGRTMIN` session toggle
- [ ] evdev F9 single/double tap
- [ ] Gamepad guide+LT
- [ ] systemd service (+ env import / input-group check)
- [ ] `gsr-clip status` CLI

### Phase 2 — Steam session automation (auto-start)

- [ ] Active-window PID via kdotool
- [ ] Steam gate via `SteamAppId` (environ), PPID fallback
- [ ] Focus watcher → auto-start session
- [ ] PID watcher → auto-stop on game exit (not focus loss)
- [ ] Debounce + one-session-at-a-time guard
- [ ] Highlight sidecar JSON
- [ ] `on-save.sh` session rename (`{GameName}_{date}_{time}.mp4`)
- [ ] `notify-send` on session start/stop only
- [ ] Manual double-tap override (force start/stop)

### Phase 3 — Trim

- [ ] `gsr-clip trim` CLI using highlights JSON
- [ ] Batch export all highlights from one session

### Phase 4 — Polish (optional)

- [ ] Simple local file browser (TUI or minimal web)
- [ ] Storage prune script (50GB cap)
- [ ] `SIGRTMIN+2` quick 30s clip during session

---

## Testing checklist

1. Portal capture starts without error; token persists across restart
2. Single F9 saves replay clip with correct brightness (not HDR-dark)
3. Focus a Steam game → session **auto-starts** within ~1–2s; notify fires
4. Focus Cursor / desktop / Steam UI → **no** auto-start
5. Alt-tab from game to Discord/browser → session **keeps recording** (no stop)
6. `SteamAppId` correctly read from `/proc/<pid>/environ` for a Proton game
7. Single F9 during session → highlight appears in JSON at sane timestamp
8. Guide+LT does same as single F9 in both modes (verify it isn't eaten by Steam Input)
9. Quit game → session auto-stops within ~2–4s; notify fires
10. Double-tap F9 force-stop works as override; double-tap force-start works if auto missed
11. Daemon refuses to start while another GSR (Vice) is running
12. Only one GSR process in `pgrep` during simultaneous replay + session

---

## Summary

A ~500-line Python daemon wrapping **one** `gpu-screen-recorder` process provides replay clips, **auto-started** Steam-gated sessions (start on game focus, stop on game-PID exit — not on alt-tab), highlight bookmarks, and a single-encoder guard so it never fights Vice. Trim is a small ffmpeg CLI on top. The gate uses `SteamAppId` from `/proc/<pid>/environ`; the only external runtime dependency beyond GSR is **`kdotool`** for reliable active-window PID on KDE Wayland.

Start with Phase 1 and validate GSR `-ro` / `SIGRTMIN` (one process, replay buffer + regular recording at once) on your machine before wiring the focus watcher and Steam detection.
