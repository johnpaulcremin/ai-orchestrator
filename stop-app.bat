@echo off
REM Stops whatever is listening on the backend (8000) and frontend (5173)
REM dev-server ports, however it was started.
setlocal enabledelayedexpansion

echo Stopping backend (port 8000)...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

echo Stopping frontend (port 5173)...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

echo Done.
