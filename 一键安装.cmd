@echo off
setlocal
echo HUST Connect setup
echo Right-click this file and choose Run as administrator before installing.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo Installation failed. Check administrator rights and Python 3.10+ with Tkinter.
  echo See README.md for detailed steps.
  pause
  exit /b 1
)
endlocal
