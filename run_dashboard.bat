@echo off
rem HangXiaoMai dashboard launcher: starts dashboard\server.py and opens the page.
rem Paths are derived from this file's location: <project root>\run_dashboard.bat
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run_dashboard.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo [错误] 启动失败，退出码 %CODE%
    pause
)
endlocal
