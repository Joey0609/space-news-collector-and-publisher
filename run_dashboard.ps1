# 航小麦 · 看板启动器
#   用法：双击 run_dashboard.bat，或  powershell -ExecutionPolicy Bypass -File run_dashboard.ps1
#   做的事：启动 dashboard\server.py → 等端口起来 → 自动打开 http://127.0.0.1:<端口>
#   参数：-Port 8787（默认）、-NoBrowser（只启动不开浏览器）
#
# ⚠️ 本文件必须存成「UTF-8 带 BOM」：Windows PowerShell 5.1 对无 BOM 的 .ps1 按 GBK 解码，
#    里面的中文会变乱码并报语法错误。改完这个文件请确认开头还是 EF BB BF（自检 check_dashboard 会盯着）。
param(
    [int]$Port = 8787,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
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
} else {
    # 优先用项目自带的虚拟环境，找不到就退回 PATH 里的 python
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $fallback = Get-Command python -ErrorAction SilentlyContinue
        if (-not $fallback) {
            Write-Host "[错误] 找不到 $python，也没在 PATH 里找到 python。请先创建 .venv 并安装依赖。"
            exit 1
        }
        $python = $fallback.Source
        Write-Host "[注意] 没找到 .venv\Scripts\python.exe，改用 $python"
    }

    Write-Host "[看板] 启动：$python dashboard\server.py --port $Port"
    # 单独开一个窗口跑服务：既能看实时日志和启动时打印的 token，也不挡住本脚本去开浏览器
    Start-Process -FilePath $python `
        -ArgumentList @("dashboard\server.py", "--port", "$Port") `
        -WorkingDirectory $root | Out-Null

    # 等它起来（最多 20 秒）
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-DashboardUp $url) { break }
    }

    if (-not (Test-DashboardUp $url)) {
        Write-Host "[错误] 等了 20 秒端口还没通，启动失败；请检查上面那个新窗口里的报错（常见原因：.venv 缺依赖、端口被占）。"
        exit 1
    }
    Write-Host "[看板] 已就绪：$url"
}

if (-not $NoBrowser) {
    Write-Host "[看板] 打开浏览器：$url"
    Start-Process $url
}
