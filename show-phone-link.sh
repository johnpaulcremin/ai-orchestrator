#!/usr/bin/env bash
# Prints the address to open this app from your phone -- the shell twin of
# show-phone-link.bat. `tailscale serve status` already knows it; this
# wrapper finds the binary when it is not on PATH (the macOS app bundles it
# under /Applications), explains what to do when nothing is being served
# yet, and prints this machine's tailnet name, which the address is built
# from. See docs/remote-access.md.
set -u

TS=""
if command -v tailscale >/dev/null 2>&1; then
    TS=tailscale
elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
    TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
elif [ -x /usr/local/bin/tailscale ]; then
    TS=/usr/local/bin/tailscale
fi

if [ -z "$TS" ]; then
    echo "Tailscale does not appear to be installed on this machine."
    echo
    echo "Install it from https://tailscale.com/ , sign in, then run this again."
    echo "See docs/remote-access.md for the full setup."
    exit 1
fi

echo "============================================================"
echo "  Open this address on your phone"
echo "============================================================"
echo
"$TS" serve status
echo
echo "------------------------------------------------------------"
echo "If an https://...ts.net address is listed above, that is the"
echo "one - open it in Safari/Chrome on your phone."
echo
echo "If it said there is no serve config, the app is not being"
echo "served yet. Run these two on this machine, then try again:"
echo
echo "    (cd frontend && npm run build)"
echo "    tailscale serve --bg 8000"
echo
echo "Your phone also needs Tailscale installed and signed in to"
echo "the SAME account - a new phone is a new device, and a data"
echo "restore does not carry that over."
echo "------------------------------------------------------------"
echo
echo "This machine on your tailnet (the address is built from its name):"
echo
# --peers=false keeps this to just this machine; fall back to the full
# listing if this Tailscale build does not know that flag.
"$TS" status --peers=false 2>/dev/null || "$TS" status
