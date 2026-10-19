@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PORT=8877"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (
  echo 未找到 Python，请先安装 Python 3.9 或更高版本：https://www.python.org/downloads/
  pause
  exit /b 1
)

rem 仪表盘已在运行则直接打开浏览器，不重复启动
%PY% -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%PORT%/api/samples',timeout=2)" >nul 2>&1
if not errorlevel 1 (
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)

rem 缺少 paramiko 则自动安装
%PY% -c "import paramiko" >nul 2>&1
if errorlevel 1 (
  echo 首次运行，正在安装依赖 paramiko ...
  %PY% -m pip install paramiko
  if errorlevel 1 (
    echo 依赖安装失败，请手动执行：%PY% -m pip install paramiko
    pause
    exit /b 1
  )
)

%PY% app.py --open
pause
