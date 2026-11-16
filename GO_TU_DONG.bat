@echo off
title PinYin - go tu khoi dong
echo.
echo  File nay se: xoa loi tat tu khoi dong PinYin, va dat lai tu ngu sau 15 phut khi cam sac (nhu cu).
echo.
pause
powershell -NoProfile -Command ^
  "$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'PinYin - truy cap tu xa.lnk';" ^
  "if (Test-Path $lnk) { Remove-Item $lnk; Write-Host ' [OK] Da xoa loi tat tu khoi dong.' -ForegroundColor Green } else { Write-Host ' Khong co loi tat nao.' }"
powercfg /change standby-timeout-ac 15
echo  [OK] Da dat lai tu ngu sau 15 phut khi cam sac.
pause
