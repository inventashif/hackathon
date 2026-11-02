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
if not defined SOP_SSL_CERTFILE set "SOP_SSL_CERTFILE=data\devcert\mechtech-ai.crt"
if not defined SOP_SSL_KEYFILE set "SOP_SSL_KEYFILE=data\devcert\mechtech-ai.key"
if not exist "data\devcert" mkdir "data\devcert"
if not exist "%SOP_SSL_CERTFILE%" powershell -NoProfile -Command "if (Get-Command mkcert -ErrorAction SilentlyContinue) { mkcert -cert-file '%SOP_SSL_CERTFILE%' -key-file '%SOP_SSL_KEYFILE%' localhost 127.0.0.1 '%LAN_IP%' } else { openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 -keyout '%SOP_SSL_KEYFILE%' -out '%SOP_SSL_CERTFILE%' -subj '/CN=MechTech AI' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' }"
echo.
echo MechTech AI is available at:
if exist "%SOP_SSL_CERTFILE%" if exist "%SOP_SSL_KEYFILE%" (
    echo   On this computer:  https://localhost:8000
) else (
    echo   On this computer:  http://127.0.0.1:8000
)
if defined LAN_IP if exist "%SOP_SSL_CERTFILE%" if exist "%SOP_SSL_KEYFILE%" (
    echo   On same Wi-Fi:     https://%LAN_IP%:8000
) else if defined LAN_IP (
    echo   On same Wi-Fi:     http://%LAN_IP%:8000 ^(text only; microphone requires HTTPS^)
) else (
    echo   On same Wi-Fi:     Could not detect LAN IP.
)
echo.
if exist "%SOP_SSL_CERTFILE%" if exist "%SOP_SSL_KEYFILE%" (
    echo HTTPS enabled with configured certificate.
    set "SOP_COOKIE_SECURE=1"
    ".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --ssl-certfile "%SOP_SSL_CERTFILE%" --ssl-keyfile "%SOP_SSL_KEYFILE%"
) else (
    echo HTTPS certificate unavailable; microphone works only on localhost.
    ".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
)
