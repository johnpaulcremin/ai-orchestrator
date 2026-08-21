@echo off
REM Withdraws the tailnet tunnel that start-remote.bat published and stops the
REM backend behind it. Counterpart to stop-app.bat, which only stops the local
REM dev-server ports and knows nothing about Tailscale.
setlocal
cd /d "%~dp0"

set "TAILSCALE=tailscale"
where tailscale >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Tailscale\tailscale.exe" (
        set "TAILSCALE=%ProgramFiles%\Tailscale\tailscale.exe"
    ) else (
        echo Tailscale CLI not found - skipping the tunnel teardown.
        set "TAILSCALE="
    )
)

REM Withdraw the tunnel BEFORE stopping the backend, so there is no window where
REM the tailnet address is still published but answering nothing.
if defined TAILSCALE (
    echo Withdrawing the tailnet tunnel...
    "%TAILSCALE%" serve --https=8000 off
)

echo Stopping backend (port 8000)...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

echo Done.
