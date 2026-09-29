@echo off
title Phone Proxy (USB)
rem Set PORT to the port shown in the iPhone app.
set PORT=8180

python "%~dp0pc\usb_forward.py" --port %PORT% --system-proxy
if errorlevel 1 pause
