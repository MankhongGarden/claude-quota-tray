@echo off
set "TASK_NAME=ClaudeQuotaTrayWatchdog"
schtasks /delete /tn "%TASK_NAME%" /f
if errorlevel 1 (
    echo No task found (or removal failed) -- nothing to do.
    exit /b 0
)
echo %TASK_NAME% removed.
