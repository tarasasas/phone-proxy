@echo off
title Phone Proxy (USB)
rem Set PORT to the port shown in the iPhone app.
set PORT=8180

rem Starting/stopping ProxiFyre needs admin rights: re-launch elevated if needed.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

python "%~dp0pc\usb_forward.py" --port %PORT% --system-proxy --proxifyre
if errorlevel 1 pause
