#!/bin/bash

# Music Browser Application
# Flask-based HiRes music browser

cd "$(dirname "$0")"

# Cargar variables de entorno desde .env si existe (no se sube a git)
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
# qrcode[pil]: genera el QR de invitación de la Playlist Colaborativa
# (/admin/colaborativa) — es la única dependencia nueva que trae esa feature.
pip install -q flask flask-cors qrcode[pil]

# Start the application
echo "Starting HiRes Browser..."
python app.py
