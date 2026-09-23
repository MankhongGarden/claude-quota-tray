' Launches watchdog.ps1 with no console window at all.
'
' wscript starts the PowerShell host with window mode 0 (hidden) from the
' outset, so there is no conhost flash. -WindowStyle Hidden alone cannot
' manage that: Task Scheduler spawns powershell.exe in an interactive
' session, the console appears, and only then is it hidden — visible as a
' flicker every five minutes.
'
' watchdog.ps1 is located relative to this file, so the pair can live
' anywhere as long as they stay in the same folder.

Dim fso, here, script
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(here, "watchdog.ps1")

' Quit silently on a missing script — never WScript.Echo here. Under wscript
' that is a modal dialog, and a dialog inside a Scheduled Task hangs the task
' forever with nobody there to click it. The non-zero code is what Task
' Scheduler records as the last result.
If Not fso.FileExists(script) Then WScript.Quit 1

CreateObject("WScript.Shell").Run _
    "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden " & _
    "-File """ & script & """", 0, False
