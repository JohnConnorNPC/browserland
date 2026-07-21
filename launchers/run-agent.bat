@echo off
rem Run a webterm agent on Windows. Set BROWSERLAND_BROKER_URL and/or
rem WEB_TERMINAL_TOKEN in the environment first if needed.
rem WEB_TERMINAL_TOKEN is REQUIRED on every broker since #142, including
rem loopback ones. Print it with: python -m webterm.broker --print-token
rem (run-agent.ps1 falls back to webterm_token.json automatically; this
rem plain-cmd wrapper does not.)
setlocal
set "REPO_DIR=%~dp0.."
if defined PYTHONPATH (
    set "PYTHONPATH=%REPO_DIR%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%REPO_DIR%"
)
python -m webterm.agent %*
endlocal & exit /b %ERRORLEVEL%
