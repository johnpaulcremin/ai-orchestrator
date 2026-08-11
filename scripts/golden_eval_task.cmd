@echo off
rem Weekly golden answer-quality eval, run by Windows Task Scheduler (see
rem evals/README.md "Schedule it" for the Register-ScheduledTask command and
rem how to remove it). Everything below appends to evals\results\scheduled.log
rem -- gitignored alongside the run results themselves.
rem
rem cd first: --database is repo-relative, and the app loads .env from the
rem working directory.
cd /d "%~dp0.."
if not exist evals\results mkdir evals\results
echo ================ %date% %time% ================>> evals\results\scheduled.log
venv\Scripts\python.exe -m evals.golden_run --database ai_orchestrator.db --fail-on-regression >> evals\results\scheduled.log 2>&1
echo exit code %errorlevel% (non-zero = a previously-passing item regressed)>> evals\results\scheduled.log
