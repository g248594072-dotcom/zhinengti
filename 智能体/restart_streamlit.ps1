# 一键重启 Streamlit 网页版（先停后起）
# 用法：.\restart_streamlit.ps1

$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $AppDir

& "$AppDir\stop_all.ps1"

Start-Sleep -Seconds 1

Write-Host ""
Write-Host "正在启动 Streamlit…" -ForegroundColor Cyan
Write-Host "浏览器打开: http://localhost:8501" -ForegroundColor Green
Write-Host "按 Ctrl+C 可停止服务。" -ForegroundColor Gray
Write-Host ""

streamlit run streamlit_app.py
