@echo off
REM Single-process dual-icon launcher: 5h (frame) + Weekly (donut) in one process.
REM Replaces the pair of "Run claude quota tray.bat" + "Run weekly donut.bat".

setlocal

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

set "VENV_PYW=%PROJECT_DIR%\.venv\Scripts\pythonw.exe"
set "MAIN_SCRIPT=%PROJECT_DIR%\src\main.py"

if not exist "%VENV_PYW%" (
    echo The app has not been installed yet.
    echo Please run "Setup claude quota tray.bat" first.
    pause
    exit /b 1
)

set "CQT_DUAL_ICON=1"
start "" "%VENV_PYW%" "%MAIN_SCRIPT%"
endlocal
