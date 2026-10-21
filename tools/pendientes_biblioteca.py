#!/usr/bin/env python3
"""Reporte de lo que falta en Orbyte: qué indexar y qué procesar.

Recorre la carpeta de música (por defecto /mnt/musica), mira cada archivo de
audio y lo compara con music.db. Solo lee: no modifica la BD ni los archivos.

Grupos del reporte (cada álbum como Artista — Álbum, con su ruta):
  1. Álbumes sin indexar          carpeta con audio que no está en `albums`.
  2. Canciones sin indexar        el álbum está, pero hay archivos que no
                                  están en `tracks` (agregados después).
  3. Falta procesar (metadata)    canciones en la BD sin `track_meta` cuyo
                                  archivo sí pesa: falta correr el proceso.
  4. Canciones de 0 bytes         archivo vacío en disco (indexado o no):
                                  hay que volver a copiarlo, no procesarlo.

Uso (desde la carpeta del servidor):
  python3 tools/pendientes_biblioteca.py              # reporte completo
  python3 tools/pendientes_biblioteca.py --dias 7     # solo álbumes tocados en 7 días
  python3 tools/pendientes_biblioteca.py --rutas      # carpetas a procesar (grupos 1-3), una por línea
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

G_ALB = "1. Álbumes sin indexar"
G_CAN = "2. Canciones sin indexar"
G_META = "3. Falta procesar (metadata)"
G_CERO = "4. Canciones de 0 bytes"


def nfc(s):
    # Algunas copias (Mac, SMB) guardan los acentos descompuestos: se comparan normalizados.
    return unicodedata.normalize("NFC", s)


def leer_disco(raiz):
    """{carpeta: {"mt": fecha más nueva, "archivos": {nombre: tamaño}}} de cada carpeta con audio."""
    out = {}
    for d, subdirs, _ in os.walk(raiz):
        subdirs[:] = [s for s in subdirs if not s.startswith(".")]
        archivos, mt = {}, 0.0
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith(".") or os.path.splitext(e.name)[1].lower() not in AUDIO:
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    if not e.is_file():
                        continue
                    archivos[e.name] = st.st_size
                    mt = max(mt, st.st_mtime)
            if archivos:
                mt = max(mt, os.path.getmtime(d))
        except OSError:
            continue
        if archivos:
            out[d.rstrip("/")] = {"mt": mt, "archivos": archivos}
    return out


def cuenta(n, uno, varios):
    return f"{n} {uno if n == 1 else varios}"


def artista_album(ruta, raiz):
    """/mnt/musica/C/Cradle of Filth/Midian [LP]/CD 01 -> ('Cradle of Filth', 'Midian [LP]/CD 01')."""
    partes = os.path.relpath(ruta, raiz).split(os.sep)
    if len(partes) >= 3:          # Letra / Artista / Álbum[/CD xx]
        return partes[1], "/".join(partes[2:])
    if len(partes) == 2:          # Letra / Artista (audio suelto en la carpeta del artista)
        return partes[1], "(suelto en la carpeta del artista)"
    return partes[0], "(suelto)"


def main():
    ap = argparse.ArgumentParser(description="Qué falta indexar y procesar en Orbyte")
    ap.add_argument("--db", default=DB, help=f"ruta a music.db (por defecto {DB})")
    ap.add_argument("--raiz", default=RAIZ, help=f"carpeta de música (por defecto {RAIZ})")
    ap.add_argument("--dias", type=float, help="solo álbumes modificados en los últimos N días")
    ap.add_argument("--rutas", action="store_true",
                    help="imprimir solo las carpetas a procesar (grupos 1 a 3), una por línea")
    ap.add_argument("--csv", help="guardar también en un CSV (Grupo, Artista, Álbum, Ruta)")
    a = ap.parse_args()

    raiz = a.raiz.rstrip("/")
    if not os.path.isdir(raiz):
        sys.exit(f"No encuentro la carpeta de música: {raiz}")
    if not os.path.isfile(a.db):
        sys.exit(f"No encuentro la BD: {a.db} (usa --db)")

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    albums = {nfc(p.rstrip("/")) for (p,) in con.execute("SELECT path FROM albums WHERE path IS NOT NULL")}
    # Canciones indexadas: ruta -> ¿tiene track_meta?
    tracks = {nfc(p): bool(m) for p, m in con.execute("""
        SELECT t.file_path, tm.track_id IS NOT NULL
        FROM tracks t LEFT JOIN track_meta tm ON tm.track_id = t.id
        WHERE t.file_path IS NOT NULL""")}
    con.close()

    disco = leer_disco(raiz)
    limite = time.time() - a.dias * 86400 if a.dias else None

    # grupo -> {carpeta: [archivos]}   (lista vacía = el álbum completo)
    grupos = {G_ALB: {}, G_CAN: {}, G_META: {}, G_CERO: {}}
    for ruta, info in disco.items():
        if limite and info["mt"] < limite:
            continue
        indexado = nfc(ruta) in albums
        for nombre, tam in sorted(info["archivos"].items()):
            clave = nfc(os.path.join(ruta, nombre))
            if tam == 0:
                grupos[G_CERO].setdefault(ruta, []).append(nombre)
            elif not indexado:
                grupos[G_ALB].setdefault(ruta, [])
            elif clave not in tracks:
                grupos[G_CAN].setdefault(ruta, []).append(nombre)
            elif not tracks[clave]:
                grupos[G_META].setdefault(ruta, []).append(nombre)

    def ordenados(carpetas):                       # más nuevos primero
        return sorted(carpetas, key=lambda r: (-disco[r]["mt"], r))

    if a.rutas:
        vistas = set()
        for g in (G_ALB, G_CAN, G_META):
            for ruta in ordenados(grupos[g]):
                if ruta not in vistas:
                    vistas.add(ruta)
                    print(ruta)
        return

    filas = []
    for g, carpetas in grupos.items():
        n_can = sum(len(v) for v in carpetas.values())
        detalle = cuenta(len(carpetas), "álbum", "álbumes") + (
            ", " + cuenta(n_can, "canción", "canciones") if g != G_ALB else "")
        print(f"\n== {g} ({detalle}) ==")
        for ruta in ordenados(carpetas):
            art, alb = artista_album(ruta, raiz)
            print(f"{art} — {alb}\n    {ruta}")
            if carpetas[ruta]:
                for nombre in carpetas[ruta]:
                    print(f"      · {nombre}")
                    filas.append((g, art, alb, os.path.join(ruta, nombre)))
            else:
                filas.append((g, art, alb, ruta))

    filtro = f" (últimos {a.dias:g} días)" if a.dias else ""
    print(f"\nResumen{filtro}: " + " · ".join(f"{g.split('. ', 1)[1]}: {len(c)}" for g, c in grupos.items())
          + f"   [carpetas con audio revisadas: {len(disco)}]")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Grupo", "Artista", "Álbum", "Ruta"])
            w.writerows(filas)
        print(f"CSV guardado en {a.csv}")


if __name__ == "__main__":
    main()
