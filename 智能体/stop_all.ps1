# 一键停止本工具相关的 Streamlit / 拉取任务（Windows PowerShell）
# 用法：在 PowerShell 中执行  .\stop_all.ps1
# 或在资源管理器中右键「使用 PowerShell 运行」

$ErrorActionPreference = "SilentlyContinue"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDirName = Split-Path -Leaf $AppDir

Write-Host "正在停止 [$AppDirName] 相关进程…" -ForegroundColor Cyan

$patterns = @(
    "streamlit_app\.py",
    "streamlit run",
    "fetch_deal_daily\.py",
    "daily_job\.py",
    "聊天质检工具\.py"
)

$killed = @()
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | ForEach-Object {
    $cmd = $_.CommandLine
    if (-not $cmd) { return }
    if ($cmd -notmatch [regex]::Escape($AppDir)) { return }
    foreach ($pat in $patterns) {
        if ($cmd -match $pat) {
            Stop-Process -Id $_.ProcessId -Force
            $killed += "PID $($_.ProcessId): $($cmd.Substring(0, [Math]::Min(80, $cmd.Length)))"
            break
        }
    }
}

# 释放默认 Streamlit 端口 8501（防止僵尸进程占端口）
$portLines = netstat -ano | Select-String ":8501\s+.*LISTENING"
foreach ($line in $portLines) {
    $parts = ($line -split "\s+") | Where-Object { $_ -ne "" }
    $pid = $parts[-1]
    if ($pid -match '^\d+$') {
        $proc = Get-Process -Id $pid -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -match '^python') {
            Stop-Process -Id $pid -Force
            $killed += "PID $pid (占用 8501)"
        }
    }
}

if ($killed.Count -eq 0) {
    Write-Host "未发现运行中的相关进程。" -ForegroundColor Yellow
} else {
    Write-Host "已停止 $($killed.Count) 个进程：" -ForegroundColor Green
    $killed | ForEach-Object { Write-Host "  - $_" }
}

Write-Host "完成。" -ForegroundColor Cyan
