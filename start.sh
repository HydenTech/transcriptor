#!/usr/bin/env bash
# Démarre l'atelier et ouvre le navigateur Windows.
set -euo pipefail
cd "$(dirname "$0")"

VENV="${ATELIER_VENV:-$HOME/.venv-transcriptor}"
if [ ! -f "$VENV/bin/activate" ]; then
  echo "  Environnement absent. Lance d'abord Installer.bat (ou bash install.sh)."
  exit 1
fi
source "$VENV/bin/activate"

PORT="${ATELIER_PORT:-8765}"
( sleep 2; command -v explorer.exe >/dev/null && explorer.exe "http://localhost:${PORT}" ) &

exec python app.py
