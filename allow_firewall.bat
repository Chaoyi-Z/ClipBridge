@echo off
rem Adds a Windows Firewall rule so phones can reach ClipBridge (port 48765). Asks for admin rights once.
powershell -NoProfile -Command "Start-Process netsh -Verb RunAs -ArgumentList 'advfirewall','firewall','add','rule','name=ClipBridge','dir=in','action=allow','protocol=TCP','localport=48765','profile=any'"
echo If a UAC prompt appeared and you accepted it, the rule "ClipBridge" now allows TCP 48765 inbound.
pause
