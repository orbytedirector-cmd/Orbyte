#!/bin/bash
# Orbyte Ingesta — arranque (lo usa orbyte-ingesta.service).
# Usa el mismo venv de Orbyte: flask, mutagen y Pillow ya están instalados ahí.
cd "$(dirname "$0")/.."
if [ -f .env ]; then
  set -a; source .env; set +a
fi
source venv/bin/activate
exec python -m ingesta
