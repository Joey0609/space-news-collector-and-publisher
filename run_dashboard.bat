@echo off
setlocal
chcp 65001 >nul
set "ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run_dashboard.ps1" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo [ERROR] Dashboard startup failed with exit code %CODE%.
    pause
)
endlocal & exit /b %CODE%
