#!/usr/bin/env bash
# ============================================
# DeepSeek_MTLS - macOS/Linux TypeScript TUI Launcher
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MENU_DIR="$SCRIPT_DIR/mtls-menu-ts"

if [ ! -d "$MENU_DIR/node_modules" ]; then
    echo "Installing TypeScript menu dependencies..."
    (cd "$MENU_DIR" && npm install)
fi

cd "$MENU_DIR"
exec npm start
