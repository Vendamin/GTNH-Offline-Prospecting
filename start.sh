#!/bin/bash
set -e

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] Python3 could not be found. Please install Python 3.8+"
    exit 1
fi

if [ ! -x "venv/bin/python" ]; then
    echo "Creating an isolated Virtual Environment (venv)..."
    python3 -m venv venv
fi

echo "Activating environment..."
source "venv/bin/activate"

echo "Installing necessary dependencies..."
python -m pip install -r requirements.txt

echo "Starting server..."
echo "-----------------------------------------------------"
echo "Go to http://127.0.0.1:5050/ in your web browser!"
echo "-----------------------------------------------------"
exec python app.py
