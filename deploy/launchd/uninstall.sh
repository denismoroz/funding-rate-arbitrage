#!/usr/bin/env bash
# Uninstall frab engine + web LaunchAgents.

set -euo pipefail

LAUNCH_DIR="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"

for name in engine web; do
    label="com.frab.$name"
    launchctl bootout "$GUI/$label" 2>/dev/null || echo "  $label: not loaded"
    rm -f "$LAUNCH_DIR/$label.plist"
    echo "  removed: $label"
done
