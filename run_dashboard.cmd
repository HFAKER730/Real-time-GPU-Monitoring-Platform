@echo off
rem Start the GPU dashboard (hidden, non-interactive variant).
rem The SSH password is supplied through the GPU_MONITOR_PASSWORD environment
rem variable by the caller; it is never written to disk by this script.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PORT=8877"

if not defined GPU_MONITOR_PASSWORD (
  echo [ERROR] GPU_MONITOR_PASSWORD is not set.  Use start.cmd for the interactive prompt.
  exit /b 2
)

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (where py >nul 2>&1 && set "PY=py")
if not defined PY (
  echo [ERROR] Python not found on PATH.
  exit /b 1
)

rem Already running? Then just open the browser and stop.
%PY% -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%PORT%/api/samples',timeout=2)" >nul 2>&1
if not errorlevel 1 (
  start "" "http://127.0.0.1:%PORT%/"
  exit /b 0
)

%PY% -c "import paramiko" >nul 2>&1
if errorlevel 1 (
  echo Installing dependency paramiko ...
  %PY% -m pip install paramiko
  if errorlevel 1 exit /b 1
)

%PY% app.py --open
