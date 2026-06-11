#!/bin/sh
# Reference GSR -sc save hook. The daemon writes its own copy into
# $XDG_RUNTIME_DIR/gsr-clip/on-save.sh at startup (pointing at the venv python),
# so this file is only documentation / a manual fallback.
#
# GSR invokes this with: "$1" = saved filepath, "$2" = type (regular|replay|screenshot).
# We forward both to the daemon, which renames regular session files and writes
# the highlight sidecar.
exec gsr-clip on-save "$1" "$2"
