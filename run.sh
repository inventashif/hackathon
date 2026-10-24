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
exec "$PYTHON" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
