@echo off
title PinYin - bat tu khoi dong
echo.
echo  File nay se:
echo   1. Tao loi tat trong thu muc Startup: dang nhap Windows la tu mo MT5 + may chu PinYin + link tu xa
echo   2. Tat che do tu ngu (sleep / ngu dong) KHI CAM SAC. Man hinh van tu tat binh thuong.
echo.
echo  Muon go bo: chay file GO_TU_DONG.bat
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'PinYin - truy cap tu xa.lnk';" ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk);" ^
  "$s.TargetPath = '%~dp0CHAY_TU_XA.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Save();" ^
  "if (Test-Path $lnk) { Write-Host ' [OK] Da tao loi tat tu khoi dong.' -ForegroundColor Green }"
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
echo  [OK] Da tat tu ngu khi cam sac.
echo.
echo  Xong. Lan sau bat may va dang nhap Windows, PinYin se tu chay.
pause
