"""
Orbyte Ingesta — revisión de títulos de pista de toda la biblioteca (lee solo music.db, no abre archivos).

Reglas (un álbum aparece por una regla si la cumple la mayoría de sus pistas; «genérico» basta con una):
  prefijo     «A1. Unbroken», «01 - Título», «1-03. Título»
  generico    «Track 01», «Pista 3», «Unknown», vacío, o el nombre del archivo («01 - Song.flac»)
  artista     «Battle Beast - Unbroken»
  mayusculas  todo en MAYÚSCULAS o todo en minúsculas
"""
import os
import re
import sqlite3
import threading

from . import analisis
from .autocorregir import PREFIJO_RE, estilo, mayusculas, quitar_artista, sin_prefijo

REGLAS = {
    'prefijo': 'Prefijo en el título (A1., 01 -)',
    'generico': 'Título genérico o de archivo',
    'artista': 'Artista dentro del título',
    'mayusculas': 'Todo en mayúsculas o minúsculas',
}
EXT_RE = re.compile(r'\.(?:' + '|'.join(e.lstrip('.') for e in sorted(analisis.AUDIO_EXT)) + r')\s*$', re.I)
# «Untitled» y «Title 5» no: hay canciones que se llaman así (Kid A, Exile on Main St.)
GENERICO_RE = re.compile(r'^\s*(?:(?:audio\s*)?track|pista|piste|faixa|traccia)\s*[#nº°.-]?\s*\d+\s*$'
                         r'|^\s*(?:unknown|desconocid[oa]|sin\s+t[ií]tulo)(?:\s+(?:track|title|song))?\s*$', re.I)

_cache = {'clave': None, 'datos': None}
_lock = threading.Lock()


def artista_en_titulo(t, artistas):
    return quitar_artista(t, artistas) != t


def reglas_titulo(t, artistas, conservar=(), estilizado=False):
    """Reglas que rompe un título."""
    t = t or ''
    r = set()
    if not t.strip() or EXT_RE.search(t) or GENERICO_RE.match(t):
        r.add('generico')
    if PREFIJO_RE.match(t):
        r.add('prefijo')
    if artista_en_titulo(sin_prefijo(t), artistas):
        r.add('artista')
    if t.strip() and not estilizado and mayusculas(t, conservar) != t:
        r.add('mayusculas')
    return r


def revisar_pistas(pistas, artista_album, album=''):
    """pistas: [(titulo, artista_pista)] → {regla: n} con las reglas que valen para el álbum."""
    conservar = {w.upper() for w in (artista_album or '').split() if w.isupper() and len(w) > 1}
    # «folklore», «GALNERYUS — RESURRECTION»: si el álbum o el artista se escriben igual, es a propósito
    estilizado = bool(estilo(album) or estilo(artista_album))
    cuenta = {}
    ejemplos = {}
    for titulo, art in pistas:
        for r in reglas_titulo(titulo, [art, artista_album], conservar, estilizado):
            cuenta[r] = cuenta.get(r, 0) + 1
            ejemplos.setdefault(r, titulo or '(vacío)')
    n = len(pistas)
    valen = {r: c for r, c in cuenta.items() if r == 'generico' or c * 2 >= n}
    return valen, {r: ejemplos[r] for r in valen}


def detectar(db, ignorados=()):
    """Álbumes con títulos irregulares. Se recalcula solo si music.db cambió."""
    try:
        clave = (db, os.path.getmtime(db))
    except OSError:
        return {'albumes': [], 'total': 0, 'por_ruta': {}}
    with _lock:
        if _cache['clave'] != clave:
            _cache['datos'] = _calcular(db)
            _cache['clave'] = clave
        datos = _cache['datos']
    ign = set(ignorados)
    lista = [a for a in datos['albumes'] if a['id'] not in ign]
    return {'albumes': lista, 'total': datos['total'], 'ignorados': len(datos['albumes']) - len(lista),
            'por_ruta': datos['por_ruta']}


def _calcular(db):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        albumes = {r['id']: dict(r) for r in con.execute(
            'SELECT al.id, al.name AS album, al.year AS anio, al.path AS ruta, ar.name AS artista '
            'FROM albums al LEFT JOIN artists ar ON ar.id = al.artist_id')}
        pistas = {}
        for r in con.execute('SELECT album_id, title, artist, album_artist FROM tracks'):
            pistas.setdefault(r['album_id'], []).append((r['title'], r['artist'], r['album_artist']))
    finally:
        con.close()
    out, por_ruta = [], {}
    for aid, ps in pistas.items():
        al = albumes.get(aid)
        if not al:
            continue
        artista = next((p[2] for p in ps if p[2]), None) or al['artista'] or ''
        reglas, ejemplos = revisar_pistas([(p[0], p[1]) for p in ps], artista, al['album'])
        if not reglas:
            continue
        item = dict(al, artista=artista or al['artista'], n_pistas=len(ps), reglas=reglas, ejemplos=ejemplos)
        out.append(item)
        if al['ruta']:
            por_ruta[al['ruta'].rstrip('/')] = item
    out.sort(key=lambda a: (analisis._norm(a['artista'] or ''), str(a['anio'] or ''), a['album'] or ''))
    return {'albumes': out, 'total': len(pistas), 'por_ruta': por_ruta}
