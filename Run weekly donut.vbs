' Hidden launcher for the weekly-donut second instance of Claude Quota Tray.
' Sets CQT_DATA_DIR for THIS process tree only (process-scope env, not registry),
' then spawns pythonw.exe with style 0 (hidden) so no console flashes at boot.

Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
env("CQT_DATA_DIR") = sh.ExpandEnvironmentStrings("%USERPROFILE%\.claude-quota-tray-weekly")

projectDir = "D:\Tools\claude-quota-tray"
cmd = """" & projectDir & "\.venv\Scripts\pythonw.exe"" """ & projectDir & "\src\main.py"""
sh.Run cmd, 0, False
