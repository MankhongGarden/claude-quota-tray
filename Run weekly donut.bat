@echo off
REM Second instance of Claude Quota Tray — donut style · weekly (7d) metric · Personal creds.
REM Uses a separate data dir via CQT_DATA_DIR env var so it doesn't collide with the main instance.

set "CQT_DATA_DIR=%USERPROFILE%\.claude-quota-tray-weekly"
set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

start "" "%PROJECT_DIR%\.venv\Scripts\pythonw.exe" "%PROJECT_DIR%\src\main.py"
