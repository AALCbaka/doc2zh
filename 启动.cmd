@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"
set "VENV=%~dp0.venv\Scripts\python.exe"
set "PY=%VENV%"
set "PORT=8848"

echo ==========================================================
echo   PDF 中英对照翻译  -  DeepSeek + BabelDOC
echo ==========================================================
echo.

if not exist "%VENV%" (
    echo [1/4] 未找到运行环境，开始创建 Python 虚拟环境...
    where python >nul 2>nul
    if errorlevel 1 (
        echo       未检测到 Python，请先安装 Python 3.10-3.13 ^(https://www.python.org/downloads/^)
        echo       安装时务必勾选 "Add python.exe to PATH"。
        pause
        exit /b 1
    )
    python -m venv "%~dp0.venv"
    if errorlevel 1 ( echo       虚拟环境创建失败 & pause & exit /b 1 )
    echo       安装依赖（约 5-10 分钟，首次较慢）...
    "%VENV%" -m pip install --upgrade pip --quiet
    "%VENV%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 ( echo       依赖安装失败，请检查网络 & pause & exit /b 1 )
) else (
    echo [1/4] 运行环境已就绪
)

echo [2/4] 检查版面模型与字体资源（首次约 340MB）...
"%PY%" "%~dp0webapp\prepare_assets.py"
if errorlevel 1 (
    echo       资源下载中断，可重新运行本脚本自动续传。
    pause
    exit /b 1
)

echo [3/4] 启动服务...
if "%DEEPSEEK_API_KEY%"=="" (
    echo       提示：未设置 DEEPSEEK_API_KEY 环境变量，请在网页里填写 Key。
) else (
    echo       已从环境变量读取 DEEPSEEK_API_KEY。
)
start "" http://127.0.0.1:%PORT%
echo.
echo [4/4] 浏览器打开 http://127.0.0.1:%PORT%    ^(按 Ctrl+C 停止服务^)
echo.
"%PY%" -m uvicorn webapp.server:app --host 127.0.0.1 --port %PORT% --log-level warning

echo.
echo 服务已停止。
pause
