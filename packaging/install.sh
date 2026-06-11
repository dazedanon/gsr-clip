#!/usr/bin/env bash
# gsr-clip installer. Sets up a venv, installs the package, configures the
# systemd user service, and checks dependencies.
#
# This script deliberately does NOT touch Vice. To switch from Vice, disable it
# yourself first:  systemctl --user disable --now vice.service
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
CONFIG_DIR="$HOME/.config/gsr-clip"
UNIT_DIR="$HOME/.config/systemd/user"

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

info "Project: $PROJECT_DIR"

# --- venv + package ---
if [ ! -d "$VENV" ]; then
    info "Creating venv at $VENV"
    python -m venv "$VENV"
fi
info "Installing gsr-clip into venv"
"$VENV/bin/pip" install --quiet --upgrade pip
WITH_GUI="${GSR_CLIP_GUI:-1}"
if [ "$WITH_GUI" = "1" ]; then
    info "Installing with GUI (PySide6). Set GSR_CLIP_GUI=0 to skip."
    "$VENV/bin/pip" install --quiet -e "$PROJECT_DIR[gui]"
else
    "$VENV/bin/pip" install --quiet -e "$PROJECT_DIR"
fi

# --- dependency checks ---
check_bin() {
    if command -v "$1" >/dev/null 2>&1; then
        info "found $1"
    else
        warn "MISSING: $1 — $2"
    fi
}
check_bin gpu-screen-recorder "required capture backend"
check_bin kdotool "REQUIRED for auto-start/stop on KDE Wayland: paru -S kdotool"
check_bin ffmpeg "needed for 'gsr-clip trim'"
check_bin notify-send "needed for session notifications (pacman -S libnotify)"

# --- input group (evdev hotkeys) ---
if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
    info "user is in 'input' group (evdev hotkeys OK)"
else
    warn "user NOT in 'input' group — hotkeys won't work."
    warn "  sudo usermod -aG input $USER   (then log out and back in)"
fi

# --- config ---
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    info "Installing default config to $CONFIG_DIR/config.toml"
    cp "$PROJECT_DIR/packaging/config.example.toml" "$CONFIG_DIR/config.toml"
else
    info "Config already exists; leaving it untouched"
fi

# --- desktop entry (launchable app) ---
if [ "$WITH_GUI" = "1" ]; then
    APP_DIR="$HOME/.local/share/applications"
    mkdir -p "$APP_DIR"
    info "Installing desktop launcher"
    sed "s#^Exec=gsr-clip-gui#Exec=$VENV/bin/gsr-clip-gui#" \
        "$PROJECT_DIR/packaging/gsr-clip.desktop" > "$APP_DIR/gsr-clip.desktop"
    update-desktop-database "$APP_DIR" 2>/dev/null || true
fi

# --- systemd unit ---
mkdir -p "$UNIT_DIR"
info "Installing systemd user unit"
sed "s#%h/Projects/gsr-clip#$PROJECT_DIR#g" \
    "$PROJECT_DIR/packaging/gsr-clip.service" > "$UNIT_DIR/gsr-clip.service"

# Make the graphical env available to the user manager (portal/kdotool/notify).
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS XDG_CURRENT_DESKTOP 2>/dev/null || true
systemctl --user daemon-reload

cat <<EOF

$(info "Done.")
Next steps:
  1. (If switching from Vice)  systemctl --user disable --now vice.service
  2. Enable + start:           systemctl --user enable --now gsr-clip.service
  3. Check it:                 gsr-clip status
  4. First run shows a one-time KDE screen-share prompt (token is then saved).
EOF
