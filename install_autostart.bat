@echo off
rem Creates a Start Menu > Startup shortcut so ClipBridge starts (tray icon) when you log in.
set "TARGET=%~dp0ClipBridge.bat"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\ClipBridge.lnk'); $s.TargetPath='%TARGET%'; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Description='ClipBridge clipboard sync'; $s.Save()"
echo Added ClipBridge to Startup. Remove it with uninstall_autostart.bat
pause
