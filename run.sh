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
echo
echo "NexusSOP is available at:"
echo "  On this computer:  http://127.0.0.1:8000"
if [ -n "$LAN_IP" ]; then
    echo "  On same Wi-Fi:     http://$LAN_IP:8000"
else
    echo "  On same Wi-Fi:     Could not detect LAN IP; run 'hostname -I'"
fi
echo
exec "$PYTHON" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
