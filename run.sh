#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Prefer a virtualenv that matches this platform. A Windows venv in .venv\Scripts
# cannot run on Linux, so it is only used on Windows.
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif command -v python3 > /dev/null 2>&1; then
    PYTHON="python3"
else
    PYTHON="python"
fi

"$PYTHON" -m pip install -r requirements.txt
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "$LAN_IP" ] && command -v ip >/dev/null 2>&1; then
    LAN_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
fi
CERT_DIR="${SOP_CERT_DIR:-data/devcert}"
CERT_FILE="${SOP_SSL_CERTFILE:-$CERT_DIR/mechtech-ai.crt}"
KEY_FILE="${SOP_SSL_KEYFILE:-$CERT_DIR/mechtech-ai.key}"
if [ -z "${SOP_SSL_CERTFILE:-}" ] && [ -z "${SOP_SSL_KEYFILE:-}" ] && command -v openssl >/dev/null 2>&1; then
    mkdir -p "$CERT_DIR"
    if [ ! -s "$CERT_FILE" ] || [ ! -s "$KEY_FILE" ]; then
        if command -v mkcert >/dev/null 2>&1; then
            mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" \
                localhost 127.0.0.1 ${LAN_IP:-127.0.0.1} >/dev/null 2>&1
        else
            openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 \
                -keyout "$KEY_FILE" -out "$CERT_FILE" -subj "/CN=MechTech AI" \
                -addext "subjectAltName=DNS:localhost,IP:127.0.0.1${LAN_IP:+,IP:$LAN_IP}" >/dev/null 2>&1
        fi
    fi
fi
echo
echo "MechTech AI is available at:"
if [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
    echo "  On this computer:  https://localhost:8000"
else
    echo "  On this computer:  http://127.0.0.1:8000"
fi
if [ -n "$LAN_IP" ]; then
    echo "  On same Wi-Fi:     http://$LAN_IP:8000 (text only; microphone requires HTTPS)"
else
    echo "  On same Wi-Fi:     Could not detect LAN IP; run 'hostname -I'"
fi
echo
if [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
    echo "  HTTPS enabled with configured certificate."
    export SOP_COOKIE_SECURE=1
    exec "$PYTHON" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 \
        --reload --ssl-certfile "$CERT_FILE" --ssl-keyfile "$KEY_FILE"
else
    echo "  HTTPS certificate unavailable; microphone works only on localhost."
    exec "$PYTHON" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
fi
