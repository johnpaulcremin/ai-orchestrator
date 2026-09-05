#!/usr/bin/env bash
# One-click updater for macOS/Linux -- the shell twin of update-app.bat.
# Pulls the latest code and rebuilds the frontend, so the browser and phone
# see the new UI. Runs from its own folder no matter where it is launched
# from, and stops with the real error if either step fails rather than
# reporting success over a failed pull.
set -u
cd "$(dirname "$0")" || exit 1

echo "Pulling the latest code..."
echo
if ! git pull; then
    echo
    echo "git pull failed -- see the message above."
    exit 1
fi

echo
echo "Rebuilding the frontend. This takes a moment..."
echo
if ! (cd frontend && npm run build); then
    echo
    echo "The frontend build failed -- see the message above."
    exit 1
fi

echo
echo "============================================================"
echo "  Done. Reload the app in your browser or on your phone."
echo "============================================================"
echo
echo "The running backend picks up the new files on its next"
echo "request, so there is no need to restart it."
