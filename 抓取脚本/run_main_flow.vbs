Set shell = CreateObject("WScript.Shell")
PW = "E:\AI程序员\03_实用程序\航小Q\航小麦\MaiBot-main\.venv\Scripts\pythonw.exe"
MF = "E:\AI程序员\03_实用程序\航小Q\航小麦\抓取脚本\main_flow.py"
shell.Run """" & PW & """ """ & MF & """", 0, False