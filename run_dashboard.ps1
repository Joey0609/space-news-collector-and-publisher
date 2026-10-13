# 航小麦 · 看板启动器
#   用法：双击 run_dashboard.bat，或 powershell -ExecutionPolicy Bypass -File run_dashboard.ps1
#   做的事：在当前终端前台运行 dashboard\server.py；端口绑定成功后由服务器自动打开浏览器
#   参数：-Port 8787（默认）、-NoBrowser（只启动不开浏览器）
#
# ⚠️ 本文件必须存成「UTF-8 带 BOM」：Windows PowerShell 5.1 对无 BOM 的 .ps1 按 GBK 解码，
#    里面的中文会变乱码并报语法错误。改完请确认开头还是 EF BB BF（check_dashboard.py 会盯着）。
param(
    [int]$Port = 8787,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
try {
    # cmd.exe 启动时可能还在 OEM 代码页，强制 PowerShell 输出 UTF-8，避免中文提示变乱码。
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [Console]::OutputEncoding
} catch {}
$root = $PSScriptRoot
Set-Location $root

$url = "http://127.0.0.1:$Port"

function Test-DashboardUp {
    param([string]$Target)
    try {
        # 只用 cmdlet，脚本在受限语言模式下也能跑
        Invoke-WebRequest -Uri "$Target/api/snapshot?limit=1" -TimeoutSec 1 -UseBasicParsing | Out-Null
        return $true
    } catch {
        # 端口通了但返回 401/其他状态也算"服务在跑"
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

if (Test-DashboardUp $url) {
    Write-Host "[看板] 已经在运行：$url"
    Write-Host "[提示] 现有实例无法附加到新终端。先关闭原服务器窗口，再双击 run_dashboard.bat 可重新启动。"
    if (-not $NoBrowser) { Start-Process $url }
    exit 0
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "[错误] 找不到项目虚拟环境 $python。请先创建 .venv 并安装依赖。"
    exit 1
}

$wsPort = $Port + 1
$serverArgs = @((Join-Path $root "dashboard\server.py"), "--port", "$Port", "--ws-port", "$wsPort")
if (-not $NoBrowser) { $serverArgs += "--open-browser" }
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
Write-Host "[看板] 此窗口就是服务器终端：$url"
Write-Host "[看板] 关闭此窗口即可停止服务。"
& $python @serverArgs
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 服务器退出，退出码 $LASTEXITCODE。"
    exit $LASTEXITCODE
}
