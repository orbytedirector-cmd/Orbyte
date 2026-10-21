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

"Ingreso" = cuándo llegaron los archivos al disco (fecha de cambio del
archivo, ctime): para un álbum, la del primer archivo; para canciones
sueltas, la de la más reciente.

Uso (desde la carpeta del servidor):
  python3 tools/pendientes_biblioteca.py                       # resumen en consola
  python3 tools/pendientes_biblioteca.py --html pendientes.html  # reporte HTML con gráficos
  python3 tools/pendientes_biblioteca.py --dias 7              # solo lo que ingresó en 7 días
  python3 tools/pendientes_biblioteca.py --rutas               # carpetas a procesar (grupos 1-3)
  python3 tools/pendientes_biblioteca.py --csv pendientes.csv
Para abrir las carpetas desde otro equipo: --enlace-base smb://servidor/musica
"""
import argparse
import csv
import datetime as dt
import json
import os
import sqlite3
import sys
import time
import unicodedata
from urllib.parse import quote

AUDIO = {".flac", ".dsf", ".dff", ".mp3", ".wav", ".aif", ".aiff", ".m4a",
         ".alac", ".ape", ".wv", ".ogg", ".opus"}
RAIZ = "/mnt/musica"
AQUI = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(os.path.dirname(AQUI), "music.db")
PLANTILLA = os.path.join(AQUI, "pendientes_plantilla.html")

GRUPOS = [
    ("alb", "Álbumes sin indexar", "Carpetas con audio que todavía no están en la BD."),
    ("can", "Canciones sin indexar", "El álbum está en la BD, pero tiene archivos agregados después."),
    ("meta", "Falta procesar metadata", "Canciones en la BD sin metadata cuyo archivo sí pesa."),
    ("cero", "Canciones de 0 bytes", "Archivos vacíos en disco: hay que volver a copiarlos."),
]


def nfc(s):
    # Algunas copias (Mac, SMB) guardan los acentos descompuestos: se comparan normalizados.
    return unicodedata.normalize("NFC", s)


def leer_disco(raiz):
    """{carpeta: {nombre: (tamaño, ctime)}} de cada carpeta con audio directo."""
    out = {}
    for d, subdirs, _ in os.walk(raiz):
        subdirs[:] = [s for s in subdirs if not s.startswith(".")]
        archivos = {}
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith(".") or os.path.splitext(e.name)[1].lower() not in AUDIO:
                        continue
                    try:
                        if not e.is_file():
                            continue
                        st = e.stat()
                    except OSError:
                        continue
                    archivos[e.name] = (st.st_size, st.st_ctime)
        except OSError:
            continue
        if archivos:
            out[d.rstrip("/")] = archivos
    return out


def artista_album(ruta, raiz):
    """/mnt/musica/C/Cradle of Filth/Midian [LP]/CD 01 -> ('Cradle of Filth', 'Midian [LP]/CD 01')."""
    partes = os.path.relpath(ruta, raiz).split(os.sep)
    if len(partes) >= 3:          # Letra / Artista / Álbum[/CD xx]
        return partes[1], "/".join(partes[2:])
    if len(partes) == 2:          # Letra / Artista (audio suelto en la carpeta del artista)
        return partes[1], "(suelto en la carpeta del artista)"
    return partes[0], "(suelto)"


def enlace(ruta, raiz, base):
    rel = os.path.relpath(ruta, raiz)
    if base:
        return base.rstrip("/") + "/" + quote(rel)
    return "file://" + quote(ruta)


def cuenta(n, uno, varios):
    return f"{n} {uno if n == 1 else varios}"


def analizar(a):
    raiz = a.raiz.rstrip("/")
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    albums = {nfc(p.rstrip("/")): c for p, c in con.execute(
        "SELECT path, created_at FROM albums WHERE path IS NOT NULL")}
    # Canciones indexadas: ruta -> ¿tiene track_meta?
    tracks = {nfc(p): bool(m) for p, m in con.execute("""
        SELECT t.file_path, tm.track_id IS NOT NULL
        FROM tracks t LEFT JOIN track_meta tm ON tm.track_id = t.id
        WHERE t.file_path IS NOT NULL""")}
    con.close()

    disco = leer_disco(raiz)
    limite = time.time() - a.dias * 86400 if a.dias else None

    filas = []   # una por (grupo, carpeta)
    for ruta, archivos in disco.items():
        indexado = albums.get(nfc(ruta))
        por_grupo = {}
        for nombre, (tam, ct) in sorted(archivos.items()):
            clave = nfc(os.path.join(ruta, nombre))
            if tam == 0:
                g = "cero"
            elif indexado is None:
                g = "alb"
            elif clave not in tracks:
                g = "can"
            elif not tracks[clave]:
                g = "meta"
            else:
                continue
            por_grupo.setdefault(g, []).append((nombre, ct))
        for g, items in por_grupo.items():
            # Álbum completo: llegó con su primer archivo. Canciones sueltas: la más reciente.
            ingreso = min(ct for _, ct in items) if g == "alb" else max(ct for _, ct in items)
            if limite and ingreso < limite:
                continue
            art, alb = artista_album(ruta, raiz)
            filas.append({
                "g": g, "art": art, "alb": alb, "ruta": ruta,
                "href": enlace(ruta, raiz, a.enlace_base),
                "canciones": [] if g == "alb" else [n for n, _ in items],
                "n": len(items), "ingreso": int(ingreso), "indexado": indexado,
            })
    filas.sort(key=lambda f: (-f["ingreso"], f["ruta"]))
    return raiz, disco, filas


def reporte_texto(filas, disco, a):
    for g, nombre, _ in GRUPOS:
        fg = [f for f in filas if f["g"] == g]
        n_can = sum(f["n"] for f in fg)
        detalle = cuenta(len(fg), "álbum", "álbumes") + (
            ", " + cuenta(n_can, "canción", "canciones") if g != "alb" else "")
        print(f"\n== {nombre} ({detalle}) ==")
        for f in fg:
            fecha = dt.datetime.fromtimestamp(f["ingreso"]).strftime("%Y-%m-%d")
            print(f"{f['art']} — {f['alb']}   [{fecha}]\n    {f['ruta']}")
            for c in f["canciones"]:
                print(f"      · {c}")
    filtro = f" (últimos {a.dias:g} días)" if a.dias else ""
    print(f"\nResumen{filtro}: " + " · ".join(
        f"{n}: {sum(1 for f in filas if f['g'] == g)}" for g, n, _ in GRUPOS)
        + f"   [carpetas con audio revisadas: {len(disco)}]")


def reporte_html(filas, disco, raiz, a):
    with open(PLANTILLA, encoding="utf-8") as fh:
        plantilla = fh.read()
    datos = {
        "generado": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "raiz": raiz, "db": os.path.abspath(a.db), "carpetas": len(disco),
        "dias": a.dias, "grupos": [{"id": g, "nombre": n, "desc": d} for g, n, d in GRUPOS],
        "filas": filas,
    }
    js = json.dumps(datos, ensure_ascii=False).replace("</", "<\\/")
    with open(a.html, "w", encoding="utf-8") as fh:
        fh.write(plantilla.replace("/*__DATOS__*/null", js))


def main():
    ap = argparse.ArgumentParser(description="Qué falta indexar y procesar en Orbyte")
    ap.add_argument("--db", default=DB, help=f"ruta a music.db (por defecto {DB})")
    ap.add_argument("--raiz", default=RAIZ, help=f"carpeta de música (por defecto {RAIZ})")
    ap.add_argument("--dias", type=float, help="solo lo que ingresó en los últimos N días")
    ap.add_argument("--rutas", action="store_true",
                    help="imprimir solo las carpetas a procesar (grupos 1 a 3), una por línea")
    ap.add_argument("--csv", help="guardar también en un CSV")
    ap.add_argument("--html", help="guardar el reporte HTML (gráficos, filtros, orden, enlaces)")
    ap.add_argument("--enlace-base", help="prefijo para los enlaces en vez de file:// "
                                          "(ej. smb://servidor/musica)")
    a = ap.parse_args()

    if not os.path.isdir(a.raiz):
        sys.exit(f"No encuentro la carpeta de música: {a.raiz}")
    if not os.path.isfile(a.db):
        sys.exit(f"No encuentro la BD: {a.db} (usa --db)")

    raiz, disco, filas = analizar(a)

    if a.rutas:
        vistas = set()
        for f in filas:
            if f["g"] != "cero" and f["ruta"] not in vistas:
                vistas.add(f["ruta"])
                print(f["ruta"])
        return

    if a.csv:
        nombres = {g: n for g, n, _ in GRUPOS}
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Grupo", "Artista", "Álbum", "Canción", "Ingreso", "Indexado", "Ruta"])
            for f in filas:
                fecha = dt.datetime.fromtimestamp(f["ingreso"]).strftime("%Y-%m-%d %H:%M")
                for c in f["canciones"] or [""]:
                    w.writerow([nombres[f["g"]], f["art"], f["alb"], c, fecha,
                                f["indexado"] or "", f["ruta"]])

    if a.html:
        reporte_html(filas, disco, raiz, a)
        print("Resumen: " + " · ".join(
            f"{n}: {sum(1 for f in filas if f['g'] == g)}" for g, n, _ in GRUPOS))
        print(f"Reporte HTML guardado en {os.path.abspath(a.html)}")
    else:
        reporte_texto(filas, disco, a)
    if a.csv:
        print(f"CSV guardado en {a.csv}")


if __name__ == "__main__":
    main()
