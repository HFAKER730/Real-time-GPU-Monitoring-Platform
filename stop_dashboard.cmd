@echo off
chcp 65001 >nul
setlocal
set "PORT=8877"

set "PS=powershell"
where pwsh >nul 2>&1 && set "PS=pwsh"

%PS% -NoProfile -Command ^
  "$c = Get-NetTCPConnection -State Listen -LocalPort %PORT% -ErrorAction SilentlyContinue; if (-not $c) { Write-Host 'GPU dashboard is not running on port %PORT%.'; exit 0 }; $pid_ = $c[0].OwningProcess; $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue; Stop-Process -Id $pid_ -Force; Write-Host ('Stopped GPU dashboard (PID ' + $pid_ + ').')"

pause
