# Orbyte Ingesta (C-12)

Herramienta web aparte de Orbyte para revisar el material nuevo antes de pasarlo a la biblioteca.

**Etapa 1 (esta):** detecta los álbumes que llegan a la carpeta de entrada, espera a que termine la copia y los revisa:
formato y calidad, imagen + cue, conversiones necesarias, decodificación completa (archivos dañados) y pico,
tags (mutagen + ffprobe), carátula, MQA, duplicados contra `music.db` y nombre final propuesto
(`<Letra>/<Artista>/<Álbum (Año) [Tag]>`). **No mueve ni modifica ningún archivo.**

- Puerto `5005` (variable `INGESTA_PORT`). Solo responde a la red de la casa / Tailscale.
- Entra una cuenta **admin** de Orbyte (se lee `users` de `music.db`).
- Datos propios en `ingesta_datos/` (fuera de git): `ingesta.db`, clave de sesión y caché de carátulas.
- Corre con `Nice=10` e IO en prioridad baja para no afectar la reproducción.

Prueba manual sin servicio: `python -m ingesta.analisis /ruta/entrada /ruta/music.db`
