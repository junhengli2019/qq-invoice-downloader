@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title QQ 邮箱发票下载工具

py -3 -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info.minor not in (0,1,2,3,4,5,6) else 1)" >nul 2>nul
if not errorlevel 1 goto run_with_py

python -c "import sys; raise SystemExit(0 if sys.version_info.major == 3 and sys.version_info.minor not in (0,1,2,3,4,5,6) else 1)" >nul 2>nul
if not errorlevel 1 goto run_with_python

echo.
echo 未检测到可用的 Python 3（需要 3.7 或以上版本），暂时无法启动本工具。
echo.
echo 请访问 https://www.python.org/downloads/windows/ 下载并安装 Python 3。
echo 安装时请务必勾选：Add python.exe to PATH
echo 安装完成后，重新双击本文件即可。
echo.
pause
exit /b 1

:run_with_py
py -3 "代码\invoice_web_app.py"
goto service_ended

:run_with_python
python "代码\invoice_web_app.py"

:service_ended
if errorlevel 1 (
    echo.
    echo 工具未能正常启动，请阅读上方错误提示。
    echo.
    pause
)
endlocal
