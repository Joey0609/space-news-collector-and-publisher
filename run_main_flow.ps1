# 航小麦 · 主流程启动器（供计划任务调用）
# 位置：<项目根>\run_main_flow.ps1
# 路径由脚本自身位置推出，换盘/换目录不用改。
$root = $PSScriptRoot
Set-Location $root
if (-not (Test-Path "logs")) { New-Item -ItemType Directory -Path "logs" -Force | Out-Null }
& "MaiBot-main\.venv\Scripts\python.exe" "main_flow.py" *>> "logs\main_flow_task.log"
