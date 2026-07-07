# Orbyte — Guía para agentes

Orbyte es una app Flask (Python) para explorar y reproducir una biblioteca
musical local. Usa SQLite (music.db, no versionado), Jinja2 para templates,
y opcionalmente MPD para streaming.

## Reglas de trabajo

1. Solo modifica los archivos que el ticket indique como "en alcance".
   Si crees que necesitas tocar algo fuera de eso, dilo antes de hacerlo,
   no lo hagas por tu cuenta.
2. No inventes nuevas variables de entorno, columnas de base de datos ni
   convenciones de nombres — pregunta si no encuentras algo ya definido.
3. Entrega el archivo completo modificado (o un diff claro), nunca
   fragmentos sueltos sin contexto de dónde van.
4. No toques music.db, venv/, ni __pycache__ (están fuera de git a propósito).

## Estructura actual
- app.py — todas las rutas Flask (en proceso de dividir en módulos más chicos)
- templates/ — un .html por página (home, artist, album, track, search, browse, favorites)
- static/style.css, static/player.js — estilos y reproductor (compartidos por todas las páginas, cuidado al tocarlos)
