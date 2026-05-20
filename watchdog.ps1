# Claude Quota Tray watchdog
# -------------------------------------------------------------------
# Periodic checker that restarts a tray instance whose pythonw.exe has
# died (silent Tcl_Panic, OOM, manual taskkill, etc.).
#
# Each tray instance touches <data-dir>/tray.heartbeat every ~30 seconds
# from a daemon thread (see src/main.py::_start_heartbeat_thread). If
# the heartbeat is stale, the tray is dead and we relaunch.
#
# PID-based detection was abandoned because Microsoft-Store Python runs
# inside an App Container — os.getpid() returns a container-internal
# PID that the host's Get-Process never sees, so the two namespaces
# never line up.
#
# Designed to be run by a Scheduled Task every 5 minutes.
# Install via: install-watchdog.bat
# Uninstall via: uninstall-watchdog.bat
# -------------------------------------------------------------------

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Stale = heartbeat older than this many seconds. The tray touches its
# heartbeat every 30s, so 5 minutes is a generous safety margin that
# avoids racing the next watchdog tick.
$StaleSeconds = 300

$Trays = @(
    @{
        Name     = "personal-5h"
        DataDir  = Join-Path $env:USERPROFILE ".claude-quota-tray"
        Launcher = Join-Path $ScriptDir "Run claude quota tray.bat"
    },
    @{
        Name     = "weekly-donut"
        DataDir  = Join-Path $env:USERPROFILE ".claude-quota-tray-weekly"
        Launcher = Join-Path $ScriptDir "Run weekly donut.bat"
    }
)

function Write-WatchdogLog {
    param([string]$DataDir, [string]$Message)
    try {
        if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir -Force | Out-Null }
        $logFile = Join-Path $DataDir "watchdog.log"
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $logFile -Value "[$ts] $Message" -Encoding utf8
    } catch { }
}

function Test-TrayAlive {
    param([string]$DataDir, [int]$StaleSeconds)
    $hb = Join-Path $DataDir "tray.heartbeat"
    if (-not (Test-Path $hb)) { return $false }
    try {
        $mtime = (Get-Item $hb -ErrorAction Stop).LastWriteTime
        $age = (Get-Date) - $mtime
        return ($age.TotalSeconds -lt $StaleSeconds)
    } catch {
        return $false
    }
}

foreach ($tray in $Trays) {
    if (-not (Test-Path $tray.Launcher)) {
        Write-WatchdogLog $tray.DataDir "[$($tray.Name)] launcher missing: $($tray.Launcher) -- skipping"
        continue
    }
    if (Test-TrayAlive -DataDir $tray.DataDir -StaleSeconds $StaleSeconds) { continue }

    Write-WatchdogLog $tray.DataDir "[$($tray.Name)] heartbeat stale -- restarting via $($tray.Launcher)"
    try {
        Start-Process -FilePath $tray.Launcher -WindowStyle Hidden
    } catch {
        Write-WatchdogLog $tray.DataDir "[$($tray.Name)] restart failed: $($_.Exception.Message)"
    }
}
