#!/usr/bin/env bash
# Installation de l'atelier de cours dans WSL (Ubuntu).
# Usage : bash install.sh
set -euo pipefail

cd "$(dirname "$0")"
VENV="${ATELIER_VENV:-$HOME/.venv-transcriptor}"
echo
echo "  Atelier de cours — installation"
echo "  ================================"
echo

# --- 1. Paquets système -----------------------------------------------
echo "→ Paquets système"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip ffmpeg curl

# --- 2. Environnement Python ------------------------------------------
echo "→ Environnement Python"
python3 -m venv "$VENV"
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet \
  "fastapi>=0.110" "uvicorn[standard]>=0.29" "python-multipart>=0.0.9" \
  "faster-whisper>=1.1" "markdown>=3.5" "playwright>=1.44"

# --- 3. Chromium pour la mise en page PDF -----------------------------
echo "→ Chromium (rendu PDF)"
playwright install --with-deps chromium >/dev/null 2>&1 || {
  echo "  ⚠ Chromium n'a pas pu s'installer. Les .md et .txt fonctionneront,"
  echo "    l'étape PDF sera simplement ignorée."
}

# --- 4. Claude Code ----------------------------------------------------
echo "→ Claude Code"
if ! command -v claude >/dev/null 2>&1; then
  if ! command -v npm >/dev/null 2>&1; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
    sudo apt-get install -y -qq nodejs
  fi
  sudo npm install -g @anthropic-ai/claude-code
fi

# --- 5. Vérification GPU ----------------------------------------------
echo
if python3 -c "import ctypes,sys; ctypes.CDLL('libcudart.so.12')" 2>/dev/null; then
  echo "  ✓ CUDA détecté"
else
  echo "  ⚠ CUDA non détecté dans WSL."
  echo "    Vérifie le pilote NVIDIA côté Windows (nvidia-smi doit répondre ici)."
  echo "    Sans CUDA l'atelier bascule automatiquement sur le CPU — très lent."
fi

echo
echo "  Installation terminée."
echo
echo "  Une seule chose à faire maintenant : connecter Claude Code à ton"
echo "  abonnement Max. Lance 'claude' une fois et suis la procédure."
echo
echo "  Ensuite : double-clic sur Transcriptor.bat côté Windows,"
echo "  ou ./start.sh depuis WSL."
echo
