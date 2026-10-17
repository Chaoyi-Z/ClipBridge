@echo off
rem Start ClipBridge in a console window: shows the log and an ASCII QR code.
cd /d "%~dp0"
python.exe "%~dp0clipbridge.py" --console %*
pause
