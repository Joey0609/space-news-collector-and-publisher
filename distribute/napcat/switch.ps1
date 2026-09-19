# 航小麦 · 功能开关命令
# 用法：switch.ps1 <功能> <on/off>
#   功能：群聊回复 / 私聊回复 / 新闻推送 / 聊天总开关
# 作用：修改 配置\功能开关.toml，并同步修改麦麦 bot_config.toml 的发言参数（麦麦自动热重载生效）。
# 注意：所有写盘用无 BOM UTF-8，避免 bot_config.toml 被加 BOM 导致麦麦解析失败。
# 位置：本脚本在 <项目根>\脚本\ 下，项目根由脚本自身位置推出（换盘/换目录不用改脚本）。

param(
    [Parameter(Mandatory=$true)][string]$功能,
    [Parameter(Mandatory=$true)][ValidateSet("on","off")][string]$状态
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # distribute/napcat -> repository root
$switchFile = "$projectRoot\配置\功能开关.toml"
$botConfig = "$projectRoot\MaiBot-main\config\bot_config.toml"
$backupDir = "$projectRoot\配置\备份"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

if (-not (Test-Path $switchFile)) {
  Write-Output "找不到开关文件：$switchFile（确认项目根/麦麦目录是否正确）"
  exit 1
}

# 无 BOM 读 / 写的辅助
function Read-NoBom($path) { return [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8) }
function Write-NoBom($path, $text) { [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false))) }

$map = @{
  "群聊回复"   = @{ key="群聊回复"; param="talk_value"; off="0.0" }
  "私聊回复"   = @{ key="私聊回复"; param="private_talk_value"; off="0.0" }
  "新闻推送"   = @{ key="新闻推送"; param=""; off="" }
  "聊天总开关" = @{ key="聊天总开关"; param=""; off="" }
}

if (-not $map.ContainsKey($功能)) {
  Write-Output "未知功能：$功能。支持：群聊回复 / 私聊回复 / 新闻推送 / 聊天总开关"
  exit 1
}

$m = $map[$功能]
$on = ($状态 -eq "on")

# 1. 更新功能开关.toml
$sw = Read-NoBom $switchFile
if ($m.key) {
  $pattern = "($m.key)\s*=\s*(true|false)"
  $newVal = if ($on) { "$($m.key) = true" } else { "$($m.key) = false" }
  $sw = [regex]::Replace($sw, $pattern, $newVal)
  Write-NoBom $switchFile $sw
}
Write-Output "[开关] $功能 -> $状态"

# 2. 同步 bot_config.toml
$backup = "$backupDir\bot_config_$(Get-Date -format 'yyyyMMdd_HHmmss').bak"
function Set-BotParam($param, $val) {
  $botContent = Read-NoBom $botConfig
  Copy-Item $botConfig $backup -Force
  $botContent = [regex]::Replace($botContent, "($param\s*=\s*)\S+", "`${1}$val")
  Write-NoBom $botConfig $botContent
  Write-Output "[麦麦] $param -> $val （热重载生效）"
}

if ($功能 -eq "群聊回复") { Set-BotParam "talk_value" $(if ($on) { "0.2" } else { $m.off }) }
if ($功能 -eq "私聊回复") { Set-BotParam "private_talk_value" $(if ($on) { "0.7" } else { $m.off }) }
if ($功能 -eq "聊天总开关") {
  Set-BotParam "talk_value" $(if ($on) { "0.2" } else { "0.0" })
  Set-BotParam "private_talk_value" $(if ($on) { "0.7" } else { "0.0" })
}

Write-Output "完成。备份目录：$backupDir"