@echo off
REM Prints the address to open this app from your phone, and KEEPS THE WINDOW
REM OPEN so you can read it.
REM
REM Why this exists: running `tailscale serve status` from the Run dialog or
REM by double-clicking spawns a console that closes the instant the command
REM finishes, so the answer flashes past unread. Double-click this file
REM instead -- the pause at the end holds the window open.
setlocal

REM Tailscale is not always on PATH, so fall back to its default install
REM location before giving up.
set "TS="
where tailscale >nul 2>&1 && set "TS=tailscale"
if not defined TS if exist "%ProgramFiles%\Tailscale\tailscale.exe" set "TS=%ProgramFiles%\Tailscale\tailscale.exe"
if not defined TS if exist "%ProgramFiles(x86)%\Tailscale\tailscale.exe" set "TS=%ProgramFiles(x86)%\Tailscale\tailscale.exe"

if not defined TS (
    echo Tailscale does not appear to be installed on this PC.
    echo.
    echo Install it from https://tailscale.com/ , sign in, then run this again.
    echo See docs\remote-access.md for the full setup.
    goto :end
)

echo ============================================================
echo   Open this address on your phone
echo ============================================================
echo.
"%TS%" serve status
echo.
echo ------------------------------------------------------------
echo If an https://...ts.net address is listed above, that is the
echo one - open it in Safari/Chrome on your phone.
echo.
echo If it said there is no serve config, the app is not being
echo served yet. Run these two on this PC, then try again:
echo.
echo     cd frontend ^&^& npm run build
echo     tailscale serve --bg 8000
echo.
echo Your phone also needs Tailscale installed and signed in to
echo the SAME account - a new phone is a new device, and a data
echo restore does not carry that over.
echo ------------------------------------------------------------
echo.
echo This machine on your tailnet (the address is built from its name):
echo.
REM --peers=false keeps this to just this machine; fall back to the full
REM listing if this Tailscale build does not know that flag, rather than
REM leaving a usage error as the last thing on screen.
"%TS%" status --peers=false 2>nul
if errorlevel 1 "%TS%" status

:end
echo.
pause
