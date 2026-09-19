@echo off
rem HangXiaoMai - main flow launcher (for Task Scheduler)
rem Paths are derived from this file's location: <project root>\run_main_flow.bat
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
if not exist "logs" mkdir "logs"
"MaiBot-main\.venv\Scripts\python.exe" "main_flow.py" >> "logs\main_flow_task.log" 2>&1
endlocal
