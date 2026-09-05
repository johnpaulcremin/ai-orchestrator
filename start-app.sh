#!/usr/bin/env bash
# One-click launcher for macOS/Linux -- the shell twin of start-app.bat.
# Starts the backend and frontend dev servers (each logging to its own file
# under .run/, since there is no separate console window to give it) and
# opens the UI in the default browser. A server already listening on its
# port is left alone rather than started twice, so running this again is
# safe.
set -u
cd "$(dirname "$0")" || exit 1

mkdir -p .run

port_in_use() {
    # lsof is on every Mac and most Linux desktops; ss is the Linux fallback.
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1
    elif command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":$1\$"
    else
        return 1
    fi
}

if [ -x venv/bin/python ]; then
    PYTHON=venv/bin/python
else
    PYTHON=python3
    echo "No venv/ found -- using $(command -v python3) (see README.md, Backend)."
fi

if port_in_use 8000; then
    echo "Backend already running on port 8000 - leaving it alone."
else
    echo "Starting backend on port 8000 (log: .run/backend.log)..."
    nohup "$PYTHON" -m uvicorn app.main:app --reload --port 8000 \
        >.run/backend.log 2>&1 &
fi

if port_in_use 5173; then
    echo "Frontend already running on port 5173 - leaving it alone."
else
    echo "Starting frontend on port 5173 (log: .run/frontend.log)..."
    (cd frontend && nohup npm run dev >../.run/frontend.log 2>&1 &)
fi

echo "Waiting for the frontend to come up..."
sleep 4

URL="http://localhost:5173"
if command -v open >/dev/null 2>&1; then
    open "$URL"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1
else
    echo "Open $URL in your browser."
fi

echo "Logs: .run/backend.log and .run/frontend.log. Run ./stop-app.sh to stop both."
