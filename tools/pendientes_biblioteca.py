#!/usr/bin/env python3
"""Lista los álbumes de la biblioteca que falta procesar para Orbyte.

Recorre la carpeta de música (por defecto /mnt/musica), busca las carpetas
que tienen audio y las compara con music.db. Solo lee: no modifica la BD
ni los archivos.

Muestra tres grupos, cada álbum como Artista / Álbum / Ruta:
  1. Sin agregar a la BD     -> carpeta con audio que no está en `albums`.
  2. Con canciones nuevas    -> el álbum está, pero hay archivos que no
                                están en `tracks` (se agregaron después).
  3. Sin metadata            -> está en la BD, pero nunca se le generó
                                metadata (sin `album_meta` o ninguna canción
                                con `track_meta`).

Uso (desde la carpeta del servidor):
  python3 tools/pendientes_biblioteca.py              # todo, más nuevos primero
  python3 tools/pendientes_biblioteca.py --dias 7     # solo carpetas tocadas en 7 días
  python3 tools/pendientes_biblioteca.py --rutas      # solo rutas, una por línea
  python3 tools/pendientes_biblioteca.py --csv pendientes.csv
"""
import argparse
import csv
import os
import sqlite3
import sys
import time
import unicodedata

AUDIO = {".flac", ".dsf", ".dff", ".mp3", ".wav", ".aif", ".aiff", ".m4a",
         ".alac", ".ape", ".wv", ".ogg", ".opus"}
RAIZ = "/mnt/musica"
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "music.db")


def nfc(s):
    # Algunas copias (Mac, SMB) guardan los acentos descompuestos: se comparan normalizados.
    return unicodedata.normalize("NFC", s)


def carpetas_con_audio(raiz):
    """{ruta: (mtime, {archivos de audio})} para cada carpeta con audio directo."""
    out = {}
    for d, subdirs, archivos in os.walk(raiz):
        subdirs[:] = [s for s in subdirs if not s.startswith(".")]
        audio = {f for f in archivos
                 if not f.startswith(".") and os.path.splitext(f)[1].lower() in AUDIO}
        if not audio:
            continue
        try:
            # La fecha más nueva entre la carpeta y sus archivos de audio:
            # sirve para ordenar "lo recién agregado" primero.
            mt = max([os.path.getmtime(d)] + [os.path.getmtime(os.path.join(d, f)) for f in audio])
        except OSError:
            mt = 0
        out[d.rstrip("/")] = (mt, audio)
    return out


def artista_album(ruta, raiz):
    """/mnt/musica/C/Cradle of Filth/Midian [LP]/CD 01 -> ('Cradle of Filth', 'Midian [LP]/CD 01')."""
    partes = os.path.relpath(ruta, raiz).split(os.sep)
    if len(partes) >= 3:          # Letra / Artista / Álbum[/CD xx]
        return partes[1], "/".join(partes[2:])
    if len(partes) == 2:          # Letra / Artista (audio suelto en la carpeta del artista)
        return partes[1], "(suelto en la carpeta del artista)"
    return partes[0], "(suelto)"


def main():
    ap = argparse.ArgumentParser(description="Álbumes pendientes de procesar para Orbyte")
    ap.add_argument("--db", default=DB, help=f"ruta a music.db (por defecto {DB})")
    ap.add_argument("--raiz", default=RAIZ, help=f"carpeta de música (por defecto {RAIZ})")
    ap.add_argument("--dias", type=float, help="solo carpetas modificadas en los últimos N días")
    ap.add_argument("--rutas", action="store_true", help="imprimir solo las rutas (para pasarlas a otros scripts)")
    ap.add_argument("--csv", help="guardar también en un CSV (Grupo, Artista, Álbum, Ruta)")
    a = ap.parse_args()

    raiz = a.raiz.rstrip("/")
    if not os.path.isdir(raiz):
        sys.exit(f"No encuentro la carpeta de música: {raiz}")
    if not os.path.isfile(a.db):
        sys.exit(f"No encuentro la BD: {a.db} (usa --db)")

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    albums = {nfc(p.rstrip("/")): i for i, p in con.execute("SELECT id, path FROM albums WHERE path IS NOT NULL")}
    tracks = {nfc(p) for (p,) in con.execute("SELECT file_path FROM tracks WHERE file_path IS NOT NULL")}
    # Álbum nunca procesado: sin album_meta o ninguna canción con track_meta.
    # (Una canción suelta sin track_meta en un álbum ya procesado suele ser un
    # archivo dañado -duración 0- que el proceso salta, no algo pendiente.)
    sin_meta = {i for (i,) in con.execute("""
        SELECT a.id FROM albums a
        WHERE NOT EXISTS (SELECT 1 FROM album_meta m WHERE m.album_id = a.id)
           OR NOT EXISTS (SELECT 1 FROM tracks t JOIN track_meta tm ON tm.track_id = t.id
                          WHERE t.album_id = a.id)""")}
    con.close()

    disco = carpetas_con_audio(raiz)
    limite = time.time() - a.dias * 86400 if a.dias else None

    grupos = {"Sin agregar a la BD": [], "Con canciones nuevas": [], "Sin metadata": []}
    for ruta, (mt, audio) in disco.items():
        if limite and mt < limite:
            continue
        clave = nfc(ruta)
        aid = albums.get(clave)
        if aid is None:
            grupos["Sin agregar a la BD"].append((mt, ruta))
        elif any(nfc(os.path.join(ruta, f)) not in tracks for f in audio):
            grupos["Con canciones nuevas"].append((mt, ruta))
        elif aid in sin_meta:
            grupos["Sin metadata"].append((mt, ruta))

    filas = []
    for grupo, items in grupos.items():
        for mt, ruta in sorted(items, key=lambda x: (-x[0], x[1])):   # más nuevos primero
            art, alb = artista_album(ruta, raiz)
            filas.append((grupo, art, alb, ruta))

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Grupo", "Artista", "Álbum", "Ruta"])
            w.writerows(filas)

    if a.rutas:
        for *_, ruta in filas:
            print(ruta)
        return

    total = 0
    for grupo, items in grupos.items():
        print(f"\n== {grupo} ({len(items)}) ==")
        for g, art, alb, ruta in filas:
            if g == grupo:
                print(f"{art} — {alb}\n    {ruta}")
        total += len(items)
    filtro = f" en los últimos {a.dias:g} días" if a.dias else ""
    print(f"\nTotal pendientes{filtro}: {total}  (carpetas con audio revisadas: {len(disco)})")
    if a.csv:
        print(f"CSV guardado en {a.csv}")


if __name__ == "__main__":
    main()
