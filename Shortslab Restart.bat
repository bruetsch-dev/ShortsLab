@echo off
rem Stream Deck target: point a second "System > Open" button at this file.
rem
rem Stops every running Shortslab server and starts a fresh one, then opens the browser. Needed
rem after a code change: the server runs with a hidden window, so closing the browser leaves it
rem running and the ordinary button just reopens the SAME old process.
rem
rem It asks first - a render in flight would be lost.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0shortslab.ps1" -Restart
exit /b 0
