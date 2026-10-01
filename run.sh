#!/usr/bin/env bash
set -e

# ChatGPT Integration POC Launcher (R5)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "=== Lancement de ChatGPT Integration POC ==="

# Check virtual environment
if [ ! -d "venv" ]; then
    echo "Création de l'environnement virtuel..."
    python3 -m venv venv
    ./venv/bin/pip install --upgrade pip
    ./venv/bin/pip install -r requirements.txt
fi

# Run database migrations and storage isolation checks
./venv/bin/python3 -c "from app.config import settings; settings.ensure_isolated_storage(); from app.persistence.db import db_manager; db_manager.initialize_schema(); print('Base de données initialisée.')"

# Ensure French AZERTY keyboard layout if running in X11 / Wayland session
if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    setxkbmap fr 2>/dev/null || true
fi

# Start FastAPI server
echo "Serveur disponible sur http://127.0.0.1:8000"
exec ./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
