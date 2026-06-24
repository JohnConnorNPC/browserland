@echo off
rem Run a webterm agent on Windows. Set BROWSERLAND_BROKER_URL and/or
rem WEB_TERMINAL_TOKEN in the environment first if needed.
setlocal
set "REPO_DIR=%~dp0.."
if defined PYTHONPATH (
    set "PYTHONPATH=%REPO_DIR%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%REPO_DIR%"
)
python -m webterm.agent %*
endlocal & exit /b %ERRORLEVEL%
