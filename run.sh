#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Prefer a virtualenv that matches this platform.
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
elif [ -x "/home/inventashif/.hermes/hermes-agent/venv/bin/python3" ]; then
    PYTHON="/home/inventashif/.hermes/hermes-agent/venv/bin/python3"
elif command -v python3 > /dev/null 2>&1; then
    PYTHON="python3"
else
    PYTHON="python"
fi

"$PYTHON" -m pip install -r requirements.txt

# Run multi-interface server (Plain HTTP on 8000, HTTPS on 8443, and Cloudflare Quick Tunnel)
exec "$PYTHON" -m backend.server "$@"
