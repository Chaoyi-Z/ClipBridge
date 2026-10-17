@echo off
powershell -NoProfile -Command "Remove-Item -LiteralPath ([Environment]::GetFolderPath('Startup')+'\ClipBridge.lnk') -ErrorAction SilentlyContinue"
echo Removed ClipBridge from Startup.
pause
