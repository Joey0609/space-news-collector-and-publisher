' HangXiaoMai - main flow launcher (no console window, for Task Scheduler / autostart)
' Paths are derived from this file's location: <project root>\run_main_flow.vbs
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
PW = root & "\MaiBot-main\.venv\Scripts\pythonw.exe"
MF = root & "\main_flow.py"
If Not fso.FolderExists(root & "\logs") Then fso.CreateFolder(root & "\logs")
Set shell = CreateObject("WScript.Shell")
shell.Run """" & PW & """ """ & MF & """", 0, False
