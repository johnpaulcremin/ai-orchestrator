@echo off
REM One-click remote launcher: builds the frontend, starts the backend on
REM 127.0.0.1:8000, and fronts it with Tailscale's HTTPS reverse proxy so the
REM whole app -- UI and API both -- is reachable from your phone at
REM https://DEVICE.TAILNET.ts.net -- your tailnet hostname, which this script
REM prints when it finishes. See docs/remote-access.md (Option A).
REM
REM No angle brackets, pipes or ampersands in these REM lines: cmd.exe parses
REM redirection during tokenization, before it works out the command is a
REM comment, so an angle bracket in a comment can still be read as a redirect.
REM
REM Counterpart to start-app.bat, which starts the two LOCAL dev servers and
REM exposes nothing. Use that one at the desk; use this one to test on a phone.
REM
REM Plain `setlocal`, not `enabledelayedexpansion`: nothing here needs it, and
REM delayed expansion would eat a `!` inside a token or password read from .env.
setlocal
cd /d "%~dp0"

REM --- 1. Config exists ------------------------------------------------------
if not exist ".env" (
    echo [X] No .env file found. Copy the example and add your key first:
    echo         copy .env.example .env
    pause
    exit /b 1
)

REM --- 2. Auth is configured -------------------------------------------------
REM The moment this address leaves localhost, "single trusted user" stops being
REM true: whoever reaches it can read every conversation, spend against your
REM budget, and change your settings. There is no read-only mode, so this is a
REM hard stop rather than a warning. findstr (rather than parsing .env in a for
REM loop) keeps quotes, exclamation marks, ampersands and equals signs inside a
REM secret from breaking anything, and the leading `^ *` means a commented-out
REM `#API_AUTH_TOKEN=` does not count.
REM The value must contain an alphanumeric rather than merely "something after
REM the `=`": on a CRLF .env (any edit in Notepad) an empty `API_AUTH_TOKEN=`
REM still has a carriage return after the `=`, and a looser pattern would read
REM that CR as a token and wave the exposure through -- failing open on exactly
REM the check that exists to prevent it.
findstr /r /c:"^ *API_AUTH_TOKEN *=.*[a-zA-Z0-9]" /c:"^ *JWT_SECRET *=.*[a-zA-Z0-9]" .env >nul 2>&1
if errorlevel 1 (
    echo [X] Refusing to expose this app: .env sets neither API_AUTH_TOKEN nor
    echo     JWT_SECRET to a value, so anything that can reach your tailnet
    echo     address could read your conversations and spend your budget.
    echo.
    echo     Set one in .env, then run this again:
    echo         API_AUTH_TOKEN=some-long-random-value
    pause
    exit /b 1
)

REM --- 3. Tailscale CLI ------------------------------------------------------
set "TAILSCALE=tailscale"
where tailscale >nul 2>&1
if errorlevel 1 (
    if exist "%ProgramFiles%\Tailscale\tailscale.exe" (
        set "TAILSCALE=%ProgramFiles%\Tailscale\tailscale.exe"
    ) else (
        echo [X] Tailscale CLI not found on PATH or in %ProgramFiles%\Tailscale.
        echo     Install Tailscale on this machine and sign it into the same
        echo     tailnet as your phone: https://tailscale.com/
        pause
        exit /b 1
    )
)

REM --- 4. Build the frontend -------------------------------------------------
REM Unconditional, not "only if dist is missing": frontend/dist never rebuilds
REM itself, and a stale dist fails silently -- the phone just shows old code and
REM you debug a bug you already fixed. The build also transpiles down to
REM es2020/safari15 (frontend/vite.config.ts), which is what makes older mobile
REM browsers work at all; they render the untranspiled dev server as a blank page.
if not exist "frontend\node_modules" (
    echo [X] frontend\node_modules is missing. Install dependencies first:
    echo         cd frontend ^&^& npm install
    pause
    exit /b 1
)
echo Building the frontend so your phone gets current code...
pushd frontend
call npm run build
set BUILD_ERR=%errorlevel%
popd
if not "%BUILD_ERR%"=="0" (
    echo.
    echo [X] Frontend build failed - see the errors above. Nothing was exposed.
    pause
    exit /b 1
)

REM --- 5. Backend ------------------------------------------------------------
REM Deliberately no --reload here, unlike start-app.bat: this is the serve-it-to-
REM another-device path, not the edit loop, and a reload restart mid-request
REM drops the phone's in-flight SSE stream. Bound to 127.0.0.1 on purpose --
REM `tailscale serve` is the trust boundary, and the app process itself never
REM leaves localhost (which is also why BIND_HOST stays unset: this setup is not
REM what the app's own "exposed without auth" startup check is meant to catch).
netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo Backend already running on port 8000 - leaving it alone.
) else (
    if not exist "venv\Scripts\python.exe" (
        echo [X] No venv found at venv\Scripts\python.exe. Set the backend up
        echo     first - see the README's Backend quickstart.
        pause
        exit /b 1
    )
    echo Starting backend on 127.0.0.1:8000...
    start "AI Orchestrator - Backend (remote)" cmd /k "venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
)

REM --- 6. Wait for it to actually answer -------------------------------------
REM Polling /health (public, no token needed) rather than sleeping a fixed few
REM seconds: publishing a tunnel to a backend that died on a config error would
REM hand you a URL that only 502s from the phone.
set "READY="
where curl >nul 2>&1
if errorlevel 1 (
    echo curl not available - waiting 8 seconds for the backend instead...
    timeout /t 8 /nobreak >nul
    set "READY=1"
) else (
    echo Waiting for the backend to answer /health...
    for /l %%i in (1,1,30) do (
        if not defined READY (
            curl -s -o nul -m 2 http://127.0.0.1:8000/health && set "READY=1"
            if not defined READY timeout /t 1 /nobreak >nul
        )
    )
)
if not defined READY (
    echo.
    echo [X] Backend never answered http://127.0.0.1:8000/health after 30s.
    echo     Check its console window for the error. Nothing was exposed.
    pause
    exit /b 1
)

REM --- 7. Publish to the tailnet --------------------------------------------
echo Publishing to your tailnet...
"%TAILSCALE%" serve --bg 8000
if errorlevel 1 (
    echo.
    echo [X] `tailscale serve` failed. The usual cause is HTTPS certificates
    echo     not being enabled for your tailnet - switch them on under DNS in
    echo     the Tailscale admin console, then run this again.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Open this on your phone:
echo ============================================================
"%TAILSCALE%" serve status
echo.
echo  - The phone must be signed into the SAME tailnet.
echo  - Paste your API_AUTH_TOKEN into the UI's token field.
echo  - /viewport-check.html on that host reports the phone's real
echo    viewport, handy on a screen size you have not tested before.
echo  - Share -^> Add to Home Screen installs it as an app icon.
echo.
echo  Re-run this script after frontend changes - the phone is served
echo  a build, and it does not refresh itself.
echo.
echo  stop-remote.bat withdraws the tunnel and stops the backend.
echo.
pause
