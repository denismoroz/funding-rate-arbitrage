#!/usr/bin/env bash
# Install frab engine + web as launchctl LaunchAgents.
#
# Usage:
#   deploy/launchd/install.sh           # install both
#   deploy/launchd/install.sh engine    # just the backend
#   deploy/launchd/install.sh web       # just the frontend
#
# Re-running is safe — bootout-then-bootstrap replaces an existing agent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
GUI="gui/$(id -u)"

mkdir -p "$LAUNCH_DIR" "$REPO_ROOT/logs"

UV_BIN="$(command -v uv || true)"
NPM_BIN="$(command -v npm || true)"
[[ -z "$UV_BIN" ]] && { echo "uv not found in PATH"; exit 1; }
[[ -z "$NPM_BIN" ]] && { echo "npm not found in PATH"; exit 1; }

# Construct a PATH the launchd job can use (login shell PATH is not inherited).
PLIST_PATH="$(dirname "$UV_BIN"):$(dirname "$NPM_BIN"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

render_and_load() {
    local name="$1"
    local label="com.frab.$name"
    local tpl="$REPO_ROOT/deploy/launchd/$label.plist.template"
    local out="$LAUNCH_DIR/$label.plist"

    sed \
        -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
        -e "s|__UV_BIN__|$UV_BIN|g" \
        -e "s|__NPM_BIN__|$NPM_BIN|g" \
        -e "s|__PATH__|$PLIST_PATH|g" \
        "$tpl" > "$out"

    # bootout if already loaded (ignore "service not loaded" error)
    launchctl bootout "$GUI/$label" 2>/dev/null || true
    launchctl bootstrap "$GUI" "$out"
    launchctl enable "$GUI/$label"

    echo "  loaded: $label ($out)"
}

case "${1:-both}" in
    engine) render_and_load engine ;;
    web)    render_and_load web ;;
    both)
        render_and_load engine
        render_and_load web
        ;;
    *) echo "unknown target: $1"; exit 1 ;;
esac

echo
echo "Status:"
launchctl print "$GUI/com.frab.engine" 2>/dev/null | grep -E "state =|last exit" || true
launchctl print "$GUI/com.frab.web"    2>/dev/null | grep -E "state =|last exit" || true
echo
echo "Logs: $REPO_ROOT/logs/{engine,web}.{out,err}.log"
echo "Backend: http://127.0.0.1:8765/healthz"
echo "Web UI:  http://127.0.0.1:5173/"
