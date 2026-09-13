@echo off
rem Stream Deck target: point a "System > Open" button at this file.
rem
rem A .bat rather than the .ps1 directly, because Stream Deck's Open action hands the path to the
rem shell and PowerShell scripts do not run from a double-click on a default Windows install.
rem -WindowStyle Hidden so pressing the key does not flash a console window.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0shortslab.ps1"
exit /b 0
