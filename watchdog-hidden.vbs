' Launches watchdog.ps1 with no console window at all.
' wscript runs the PowerShell host with window mode 0 (hidden) from the
' start, so there is no conhost flash like -WindowStyle Hidden leaves behind
' when Task Scheduler spawns powershell.exe directly in an interactive session.
CreateObject("WScript.Shell").Run _
  "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""D:\Tools\claude-quota-tray\watchdog.ps1""", _
  0, False
