@echo off
REM One-click launcher: starts the backend and frontend dev servers (each in
REM its own visible console window, so logs are visible and closing the
REM window stops that server) and opens the UI in the default browser.
setlocal
cd /d "%~dp0"

netstat -ano | findstr ":8000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo Backend already running on port 8000 - leaving it alone.
) else (
    echo Starting backend on port 8000...
    start "AI Orchestrator - Backend" cmd /k "venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"
)

netstat -ano | findstr ":5173" | findstr "LISTENING" >nul
if %errorlevel%==0 (
    echo Frontend already running on port 5173 - leaving it alone.
) else (
    echo Starting frontend on port 5173...
    start "AI Orchestrator - Frontend" cmd /k "cd frontend && npm run dev"
)

echo Waiting for the frontend to come up...
timeout /t 4 /nobreak >nul
start "" "http://localhost:5173"
