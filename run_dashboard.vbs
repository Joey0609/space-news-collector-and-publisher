' HangXiaoMai dashboard launcher (no console window; for autostart / Task Scheduler)
' Paths are derived from this file's location: <project root>\run_dashboard.vbs
' 它只是静默调用 run_dashboard.ps1，脚本本身会把服务窗口和浏览器都拉起来。
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
ps1 = root & "\run_dashboard.ps1"
If Not fso.FileExists(ps1) Then
    MsgBox "找不到 " & ps1, 16, "航小麦看板"
    WScript.Quit 1
End If
Set shell = CreateObject("WScript.Shell")
' 0 = 隐藏本启动器窗口，False = 不等它结束（服务窗口和浏览器由 ps1 负责）
shell.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & ps1 & """", 0, False
