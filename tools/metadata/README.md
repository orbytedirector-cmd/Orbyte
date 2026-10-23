# Scripts de metadata de Orbyte

Las versiones vigentes de los 5 scripts, en el orden en que los corre Orbyte Ingesta (etapa 3) para cada álbum nuevo:

| # | Script | Para un álbum |
|---|---|---|
| 1 | `index_libraryV3.py` (v2.2: tags con mutagen) | `--incremental --album RUTA --db music.db --root /mnt/musica --quiet` |
| 2 | `RichMetaDSD3.py` | `RUTA --mode pending` |
| 3 | `import_richmetapro.py` | `--album RUTA --db music.db` |
| 4 | `RichMetaRelations3.py` | `--album ID --all --db music.db` |
| 5 | `lastfm_fetch2.py` | `--scope album:ID --db music.db` |

Siguen funcionando a mano (sin argumentos abren su menú).

**Claves:** ya no van en el código. Se leen del entorno o del `.env` de la raíz del repo:

```
LASTFM_API_KEY=...
ACOUSTID_API_KEY=...
```

Dependencias: `requests tqdm rapidfuzz python-dotenv mutagen numpy langdetect` (opcional: `pyacoustid`).
