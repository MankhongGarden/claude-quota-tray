@echo off
REM Register the Claude Quota Tray watchdog as a Scheduled Task that fires
REM every 5 minutes. Restarts any dead tray instance silently.

set "TASK_NAME=ClaudeQuotaTrayWatchdog"
set "SCRIPT=%~dp0watchdog.ps1"

if not exist "%SCRIPT%" (
    echo ERROR: watchdog.ps1 not found at %SCRIPT%
    exit /b 1
)

REM Quote the script path (handles spaces in the parent directory).
schtasks /create /tn "%TASK_NAME%" ^
    /tr "powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%SCRIPT%\"" ^
    /sc minute /mo 5 ^
    /st 00:00 ^
    /rl LIMITED ^
    /f

if errorlevel 1 (
    echo Failed to register %TASK_NAME%.
    exit /b 1
)

echo Watchdog registered. Task: %TASK_NAME%
echo Will check both trays every 5 minutes and restart any that died.
echo Uninstall via: uninstall-watchdog.bat
