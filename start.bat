@echo off
setlocal

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch_app.ps1"
if errorlevel 1 (
  echo Could not start Autonomous Shorts Agent.
  pause
  exit /b 1
)
exit /b 0
