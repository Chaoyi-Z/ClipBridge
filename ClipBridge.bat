@echo off
rem Start ClipBridge in the background with a tray icon (no console window).
cd /d "%~dp0"
start "" pythonw.exe "%~dp0clipbridge.py" %*
