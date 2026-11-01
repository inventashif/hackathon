@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

for /f "delims=" %%I in ('powershell -NoProfile -Command "$ip = Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp,Manual ^| Where-Object { $_.IPAddress -notlike ''127.*'' } ^| Select-Object -First 1 -ExpandProperty IPAddress; Write-Output $ip"') do set "LAN_IP=%%I"
echo.
echo NexusSOP is available at:
echo   On this computer:  http://127.0.0.1:8000
if defined LAN_IP (
    echo   On same Wi-Fi:     http://%LAN_IP%:8000
) else (
    echo   On same Wi-Fi:     Could not detect LAN IP.
)
echo.
".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000