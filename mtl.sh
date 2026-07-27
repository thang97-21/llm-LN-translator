#!/usr/bin/env bash
# ============================================
# DeepSeek_MTLS - macOS/Linux CLI Launcher
# ============================================
# Usage:
#   ./mtl.sh extract <epub>
#   ./mtl.sh translate <volume_id>
#   ./mtl.sh build <volume_id>
#   ./mtl.sh run <epub>
#   ./mtl.sh list
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
PYTHON_CMD=""

if [ -x "$VENV_PYTHON" ]; then
    PYTHON_CMD="$VENV_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
else
    echo
    echo "ERROR: Python 3.10+ is required but not found."
    echo "Install it via your OS package manager or https://www.python.org/downloads/ and re-run."
    echo
    exit 1
fi

if ! "$PYTHON_CMD" -c "import anthropic, yaml, dotenv, lxml, bs4, PIL, tiktoken" >/dev/null 2>&1; then
    echo "Installing required dependencies..."
    "$PYTHON_CMD" -m pip install -r "$SCRIPT_DIR/requirements.txt" -q
    if ! "$PYTHON_CMD" -c "import anthropic, yaml, dotenv, lxml, bs4, PIL, tiktoken" >/dev/null 2>&1; then
        echo
        echo "ERROR: Failed to install dependencies."
        echo "Please run: \"$PYTHON_CMD\" -m pip install -r \"$SCRIPT_DIR/requirements.txt\""
        echo
        exit 1
    fi
    echo "Dependencies installed."
fi

exec "$PYTHON_CMD" "$SCRIPT_DIR/scripts/mtl.py" "$@"
