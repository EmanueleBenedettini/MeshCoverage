#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$ROOT_DIR/venv"
REQUIREMENTS_FILE="$ROOT_DIR/requirements.txt"

PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then
  PYTHON_CMD=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON_CMD=python
else
  echo "[ERROR] Python non trovato. Installa Python 3.11+ e riprova." >&2
  exit 1
fi

# Create virtual environment if missing
if [ ! -d "$VENV_DIR" ] || [ ! -f "$VENV_DIR/bin/activate" ]; then
  echo "Creazione virtualenv in $VENV_DIR..."
  "$PYTHON_CMD" -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck source=/dev/null
. "$VENV_DIR/bin/activate"

# Upgrade pip if needed
python -m pip install --upgrade pip setuptools wheel >/dev/null

# Install requirements
if [ -f "$REQUIREMENTS_FILE" ]; then
  echo "Installazione dipendenze da requirements.txt..."
  pip install -r "$REQUIREMENTS_FILE"
else
  echo "[WARNING] requirements.txt non trovato in $ROOT_DIR" >&2
fi

# Default command
if [ "$#" -gt 0 ]; then
  echo "Avvio servizio web con comando personalizzato: $*"
  exec "$@"
else
  echo "Avvio servizio web: uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload"
  exec uvicorn meshcoverage.api.app:app --host 0.0.0.0 --port 8000 --reload
fi
