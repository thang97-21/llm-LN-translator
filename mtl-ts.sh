#!/usr/bin/env bash
# ============================================
# LLM Translator - macOS/Linux TypeScript TUI Launcher
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MENU_DIR="$SCRIPT_DIR/ts"

if [ ! -d "$MENU_DIR/node_modules" ]; then
    echo "Installing TypeScript menu dependencies..."
    (cd "$MENU_DIR" && npm install)
fi

cd "$MENU_DIR"
exec npm start
