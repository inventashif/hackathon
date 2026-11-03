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
# Listen on every interface and include every detected IPv4 address in the
# certificate so any device on a connected LAN can use HTTPS.
LAN_IPS="$(hostname -I 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/')"
if [ -z "$LAN_IPS" ] && command -v ip >/dev/null 2>&1; then
    LAN_IPS="$(ip -4 addr show scope global 2>/dev/null | awk '/inet / {sub(/\/.*/, "", $2); print $2}')"
fi
LAN_IP="$(printf '%s\n' "$LAN_IPS" | awk 'NF {print; exit}')"
read -r -a LAN_IP_ARRAY <<< "$LAN_IPS"
CERT_DIR="${SOP_CERT_DIR:-data/devcert}"
CERT_FILE="${SOP_SSL_CERTFILE:-$CERT_DIR/mechtech-ai.crt}"
KEY_FILE="${SOP_SSL_KEYFILE:-$CERT_DIR/mechtech-ai.key}"
if [ -z "${SOP_SSL_CERTFILE:-}" ] && [ -z "${SOP_SSL_KEYFILE:-}" ] && command -v openssl >/dev/null 2>&1; then
    mkdir -p "$CERT_DIR"
    CERT_NEEDS_RENEWAL=0
    if [ ! -s "$CERT_FILE" ] || [ ! -s "$KEY_FILE" ]; then
        CERT_NEEDS_RENEWAL=1
    else
        for IP in "${LAN_IP_ARRAY[@]}"; do
            if ! openssl x509 -in "$CERT_FILE" -noout -ext subjectAltName 2>/dev/null | grep -q "IP Address:$IP"; then
                CERT_NEEDS_RENEWAL=1
                break
            fi
        done
    fi
    if [ "$CERT_NEEDS_RENEWAL" -eq 1 ]; then
        if command -v mkcert >/dev/null 2>&1; then
            mkcert -cert-file "$CERT_FILE" -key-file "$KEY_FILE" \
                localhost 127.0.0.1 "${LAN_IP_ARRAY[@]}" >/dev/null 2>&1
        else
            SAN="DNS:localhost,IP:127.0.0.1"
            for IP in "${LAN_IP_ARRAY[@]}"; do
                SAN="$SAN,IP:$IP"
            done
            openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 \
                -keyout "$KEY_FILE" -out "$CERT_FILE" -subj "/CN=MechTech AI" \
                -addext "subjectAltName=$SAN" >/dev/null 2>&1
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
if [ -n "$LAN_IP" ] && [ -s "$CERT_FILE" ] && [ -s "$KEY_FILE" ]; then
    for IP in "${LAN_IP_ARRAY[@]}"; do
        echo "  On same network:   https://$IP:8000"
    done
elif [ -n "$LAN_IP" ]; then
    echo "  On same network:   http://$LAN_IP:8000 (microphone requires HTTPS)"
else
    echo "  On same network:   Could not detect LAN IP; run 'hostname -I'"
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
