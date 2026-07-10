@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ========================================
echo   自动合并报表 - 打包（单文件 exe）
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.9+
    pause
    exit /b 1
)

echo [1/4] 安装打包依赖...
python -m pip install -r requirements-build.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo [2/4] 运行 PyInstaller（单文件）...
python -m PyInstaller "自动合并_onefile.spec" --noconfirm --clean
if errorlevel 1 (
    echo [错误] 打包失败
    pause
    exit /b 1
)

set RELEASE=release\自动合并报表

echo [3/4] 整理发布目录...
if not exist release mkdir release
if exist "%RELEASE%" rmdir /s /q "%RELEASE%"
mkdir "%RELEASE%"
copy /y "dist\自动合并报表.exe" "%RELEASE%\" >nul
if not exist "%RELEASE%\输出" mkdir "%RELEASE%\输出"
copy /y "api-key.json.example" "%RELEASE%\api-key.json.example" >nul
copy /y "使用说明.txt" "%RELEASE%\使用说明.txt" >nul

echo [4/4] 生成 ZIP...
powershell -NoProfile -Command "Compress-Archive -Path 'release\自动合并报表' -DestinationPath 'release\自动合并报表_Win64.zip' -Force"

echo.
echo ========================================
echo   打包完成
echo   文件夹: %RELEASE%
echo   压缩包: release\自动合并报表_Win64.zip
echo ========================================
echo.
echo 发给同事：解压后只需「自动合并报表.exe」+ api-key.json
echo 注意：单文件首次启动会稍慢（解压到临时目录），属正常现象。
echo.
pause
