"""Género automático para álbumes que llegan sin género (pedido de Niko, 1-11-2026).

Orden de fuentes (la primera que entregue algo útil gana; los secundarios salen de la misma):
  1. Orbyte: el género de los otros discos del mismo artista en la biblioteca (lo curado a mano).
  2. MusicBrainz: géneros del grupo de la edición identificada.
  3. Last.fm: etiquetas más usadas del álbum (o del artista), solo si son géneros que ya
     usas en la biblioteca o de la lista canónica (así no entra «seen live» ni «favorites»).

Todo queda escrito en el editor de tags y se puede cambiar antes de ubicar el álbum.
"""
import os
import sqlite3
import threading
import time
import urllib.parse
from collections import Counter

from . import analisis, edicion

MAX_SECUNDARIOS = 3
LASTFM_MIN = 15                  # relevancia mínima (0-100) de una etiqueta de Last.fm
BASURA = {'seen live', 'favorites', 'favourite', 'favorite', 'albums i own', 'my favorite', 'awesome',
          'beautiful', 'love', 'cool', 'best', 'vinyl', 'cd', 'spotify', 'under 2000 listeners'}

_vocab_cache = {}
_lock = threading.Lock()


def _canonicos():
    from . import autocorregir   # importado acá: autocorregir importa analisis
    return autocorregir.CANONICOS


def _bonito(g):
    g = (g or '').strip()
    return g.title() if g.islower() or g.isupper() else g


def _columnas(con, tabla):
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info({tabla})')}
    except sqlite3.Error:
        return set()


def vocabulario(db):
    """{género normalizado: cómo lo escribes tú} con los géneros que ya hay en la biblioteca,
    más la lista canónica. Se recalcula cada 30 min."""
    with _lock:
        c = _vocab_cache.get(db)
        if c and time.time() - c[0] < 1800:
            return c[1]
    vocab = {analisis._norm(g): g for g in _canonicos()}
    if db and os.path.exists(db):
        try:
            con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)
            try:
                cuenta = Counter()
                if 'genre_primary' in _columnas(con, 'album_meta'):
                    cuenta.update(r[0] for r in con.execute(
                        "SELECT genre_primary FROM album_meta WHERE genre_primary IS NOT NULL AND genre_primary != ''"))
                if 'genre' in _columnas(con, 'tracks'):
                    cuenta.update(r[0] for r in con.execute(
                        "SELECT genre FROM tracks WHERE genre IS NOT NULL AND genre != ''"))
                for g, _n in cuenta.most_common():
                    vocab.setdefault(analisis._norm(g), g.strip())
            finally:
                con.close()
        except sqlite3.Error:
            pass
    with _lock:
        _vocab_cache[db] = (time.time(), vocab)
    return vocab


def desde_orbyte(db, artista):
    """Géneros de los otros álbumes del artista en la biblioteca, del más usado al menos usado."""
    if not db or not os.path.exists(db) or not artista:
        return []
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)
    except sqlite3.Error:
        return []
    try:
        ids = [r[0] for r in con.execute('SELECT id, name FROM artists')
               if analisis._norm(r[1]) == analisis._norm(artista)]
        if not ids:
            return []
        marcas = ','.join('?' * len(ids))
        cuenta = Counter()
        cols = _columnas(con, 'album_meta')
        if 'genre_primary' in cols:
            sec = ', am.genre_secondary' if 'genre_secondary' in cols else ''
            for r in con.execute(f'SELECT am.genre_primary{sec} FROM albums al JOIN album_meta am '
                                 f'ON am.album_id = al.id WHERE al.artist_id IN ({marcas})', ids):
                if r[0]:
                    cuenta[r[0].strip()] += 3          # el principal pesa más
                if len(r) > 1 and r[1]:
                    for g in str(r[1]).replace('|', ';').replace(',', ';').split(';'):
                        if g.strip():
                            cuenta[g.strip()] += 1
        if not cuenta and 'genre' in _columnas(con, 'tracks') and 'album_id' in _columnas(con, 'tracks'):
            cuenta.update(r[0].strip() for r in con.execute(
                f"SELECT t.genre FROM tracks t JOIN albums al ON al.id = t.album_id "
                f"WHERE al.artist_id IN ({marcas}) AND t.genre IS NOT NULL AND t.genre != ''", ids))
        return [g for g, _n in cuenta.most_common()]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def desde_musicbrainz(cliente, grupo_id):
    if not cliente or not grupo_id:
        return []
    d = cliente.mb(f'release-group/{grupo_id}', inc='genres+tags') or {}
    generos = sorted(d.get('genres') or [], key=lambda g: -(g.get('count') or 0))
    return [g['name'] for g in generos if g.get('name') and (g.get('count') or 0) > 0]


def desde_lastfm(cliente, artista, album, varios):
    clave = os.environ.get('LASTFM_API_KEY')
    if not cliente or not clave or not album:
        return []
    consultas = [{'method': 'album.gettoptags', 'artist': artista, 'album': album}]
    if not varios:
        consultas.append({'method': 'artist.gettoptags', 'artist': artista})
    for q in consultas:
        url = 'https://ws.audioscrobbler.com/2.0/?' + urllib.parse.urlencode(
            dict(q, api_key=clave, format='json', autocorrect=1))
        d = cliente.get(url, cachear=True, pausa=0.3) or {}
        etiquetas = (d.get('toptags') or {}).get('tag') or []
        if isinstance(etiquetas, dict):
            etiquetas = [etiquetas]
        nombres = [t['name'] for t in etiquetas
                   if t.get('name') and int(t.get('count') or 0) >= LASTFM_MIN and t['name'].lower() not in BASURA]
        if nombres:
            return nombres
    return []


def proponer(db, cliente, artista, album, grupo_id=None, varios=False):
    """→ (principal, [secundarios], fuente) o (None, [], None). Nunca lanza."""
    vocab = vocabulario(db)

    def ajustar(lista, solo_conocidos):
        fuera, vistos = [], set()
        for g in lista:
            n = analisis._norm(g)
            if not n or n in vistos or g.lower() in BASURA:
                continue
            if n in vocab:
                fuera.append(vocab[n])
            elif not solo_conocidos:
                fuera.append(_bonito(g))
            else:
                continue
            vistos.add(n)
        return fuera

    fuentes = []
    if not varios:
        fuentes.append(('tu biblioteca', lambda: ajustar(desde_orbyte(db, artista), False)))
    fuentes.append(('MusicBrainz', lambda: ajustar(desde_musicbrainz(cliente, grupo_id), False)))
    fuentes.append(('Last.fm', lambda: ajustar(desde_lastfm(cliente, artista, album, varios), True)))
    for nombre, obtener in fuentes:
        try:
            lista = obtener()
        except edicion.SinRed:
            break
        except Exception:  # noqa: BLE001 - una fuente caída no frena la revisión
            continue
        if lista:
            return lista[0], lista[1:1 + MAX_SECUNDARIOS], nombre
    return None, [], None
