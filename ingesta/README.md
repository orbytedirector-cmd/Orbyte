# Orbyte Ingesta (C-12)

Herramienta web aparte de Orbyte (puerto `5005`) para incorporar material nuevo a la biblioteca.

1. **Revisión** (solo lectura): detecta los álbumes de la carpeta de entrada, espera a que termine la copia y revisa
   formatos, cue, conversiones, decodificación completa, tags, carátula, MQA, duplicados en `music.db` y la
   **edición exacta en MusicBrainz** (año de la edición para el nombre de la carpeta).
2. **Decisiones** en pantalla: aceptar, elegir edición o año, carátula (de la carpeta, incrustada, Cover Art Archive o búsqueda en línea), editor de tags.
3. **Ubicar** (etapa 2): copia/convierte en una carpeta temporal dentro de la biblioteca, escribe tags, Cover.jpg y Extras,
   verifica cada archivo (audio idéntico cuando no hay conversión) y recién ahí lo deja en
   `<Letra>/<Artista>/<Álbum (Año) [Tag]>` y borra el original de la entrada.
4. **Metadata** (etapa 3): los 5 scripts de `tools/metadata/` para ese álbum y validación final.
5. **Modo desatendido** (Ajustes): lo que queda «Listo» pasa solo por 3 y 4.

- Solo responde a la red de la casa / Tailscale; entra una cuenta **admin** de Orbyte.
- Datos propios en `ingesta_datos/` (fuera de git). Claves en el `.env` del repo: `LASTFM_API_KEY`, `ACOUSTID_API_KEY`.
- AcoustID en la identificación de edición necesita `fpcalc` (`sudo apt install libchromaprint-tools`).
