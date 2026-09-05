@echo off
REM One-click updater: pulls the latest code and rebuilds the frontend, so the
REM browser and phone see the new UI.
REM
REM Why this exists: `git pull` only works from inside the project folder, and
REM a PowerShell window opened from the Start menu starts in C:\Windows\system32
REM instead -- where it fails with "not a git repository". This script cds to
REM its OWN folder first (%~dp0), so it always runs in the right place no
REM matter where it is launched from. It also avoids `&&`, which Windows
REM PowerShell 5.1 does not support.
setlocal
cd /d "%~dp0"

echo Pulling the latest code...
echo.
git pull
if errorlevel 1 (
    echo.
    echo git pull failed -- see the message above.
    goto :end
)

echo.
echo Rebuilding the frontend. This takes a moment...
echo.
cd frontend
REM `call` is required: npm is a .cmd, and without it this script would exit
REM here rather than carrying on to the message below.
call npm run build
if errorlevel 1 (
    echo.
    echo The frontend build failed -- see the message above.
    goto :end
)

echo.
echo ============================================================
echo   Done. Reload the app in your browser or on your phone.
echo ============================================================
echo.
echo The running backend picks up the new files on its next
echo request, so there is no need to restart it.

:end
echo.
pause
