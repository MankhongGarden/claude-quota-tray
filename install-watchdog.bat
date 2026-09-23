@echo off
REM Register the Claude Quota Tray watchdog as a Scheduled Task that fires
REM every 5 minutes. Restarts any dead tray instance silently.

set "TASK_NAME=ClaudeQuotaTrayWatchdog"
set "SCRIPT=%~dp0watchdog.ps1"
set "LAUNCHER=%~dp0watchdog-hidden.vbs"

if not exist "%SCRIPT%" (
    echo ERROR: watchdog.ps1 not found at %SCRIPT%
    exit /b 1
)
if not exist "%LAUNCHER%" (
    echo ERROR: watchdog-hidden.vbs not found at %LAUNCHER%
    exit /b 1
)

REM Launch through wscript rather than powershell.exe directly: the task runs
REM in an interactive session, so -WindowStyle Hidden still flashes a console
REM for an instant every five minutes. wscript starts the host hidden.
REM Paths are quoted (escaped for schtasks) to survive spaces in the folder.
schtasks /create /tn "%TASK_NAME%" ^
    /tr "wscript.exe \"%LAUNCHER%\"" ^
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
