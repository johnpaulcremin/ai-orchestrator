#!/usr/bin/env bash
# Stops whatever is listening on the backend (8000) and frontend (5173)
# dev-server ports, however it was started -- the shell twin of stop-app.bat.
set -u

stop_port() {
    local port="$1" label="$2" pids=""
    echo "Stopping $label (port $port)..."
    if command -v lsof >/dev/null 2>&1; then
        pids=$(lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null)
    elif command -v fuser >/dev/null 2>&1; then
        pids=$(fuser "$port"/tcp 2>/dev/null)
    fi
    if [ -n "$pids" ]; then
        # shellcheck disable=SC2086
        kill $pids 2>/dev/null
    fi
}

stop_port 8000 backend
stop_port 5173 frontend

echo "Done."
