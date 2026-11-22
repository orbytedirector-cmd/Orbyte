"""
Orbyte Ingesta — pistas sueltas (22-11-2026).

Pedido de Niko: «en la parte de búsqueda para descargar, ¿podemos incluir la opción de buscar pistas y descargarlas?
Luego, al procesar en la ingesta, poder asociar algún álbum existente para reemplazar, etc.».

  · Buscar → Pistas: Tidal por artista + título, ISRC o enlace de la pista; cada resultado dice si ya está en la
    biblioteca (y en qué álbum y calidad).
  · Descargas: la pista llega a «<entrada>/Pistas sueltas/<Artista> - <Título> [tidal <id>]/» con sus tags de Tidal y
    una marca (.orbyte-pista.json) con su álbum de origen, disco, número e ISRC.
  · Bandeja: una pista suelta se puede agregar a un álbum de la biblioteca (en un lugar libre, reemplazando la que
    está en ese lugar —la vieja va a la papelera y lo suyo pasa a la nueva al indexar— o como extra al final) o
    ubicarse como sencillo, como siempre.
"""
import difflib
import json
import os
import re
import shutil
import sqlite3

from . import ajustar as ajustar_mod
from . import analisis

MARCA = '.orbyte-pista.json'
CARPETA = 'Pistas sueltas'
CAMPOS_ALBUM = ('ALBUM', 'ALBUMARTIST', 'ALBUM ARTIST', 'DATE', 'YEAR', 'ORIGINALDATE', 'ORIGINALYEAR', 'GENRE',
                'GENRE_SECONDARY', 'LABEL', 'ORGANIZATION', 'PUBLISHER', 'CATALOGNUMBER', 'BARCODE', 'UPC', 'COUNTRY',
                'RELEASECOUNTRY', 'DISCTOTAL', 'TOTALDISCS', 'MUSICBRAINZ_ALBUMID', 'MUSICBRAINZ_RELEASEGROUPID',
                'MUSICBRAINZ_ALBUMARTISTID', 'COMPILATION')


class FalloPista(Exception):
    pass


# ─────────────────────────── marca de la descarga ───────────────────────────

def marca(carpeta):
    try:
        with open(os.path.join(carpeta, MARCA), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def escribir_marca(carpeta, datos):
    with open(os.path.join(carpeta, MARCA), 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)


def nombre_seguro(s, largo=150):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', str(s or '')).strip(' .')
    return (s[:largo].rstrip(' .') or 'pista')


# ─────────────────────────── comparar con la biblioteca ───────────────────────────

def _base(t):
    """Título sin lo que va entre paréntesis ni «Remastered»: para buscar la misma canción."""
    t = re.sub(r'\s*[(\[][^)\]]*[)\]]', '', t or '')
    t = re.sub(r'\s+-\s+.*remaster.*$', '', t, flags=re.I)
    return analisis._norm(t).strip()


def _parecido(a, b):
    a, b = analisis._norm(a or ''), analisis._norm(b or '')
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _con(db):
    c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _calidad(r):
    bits, sr = r['bit_depth'], r['sample_rate']
    if bits and sr:
        return f'{r["codec"] or "FLAC"} {bits}/{round(int(sr) / 1000, 1):g}'.replace('flac', 'FLAC')
    return (r['codec'] or '').upper() or None


def en_biblioteca(db, artista, titulo, limite=4):
    """La misma canción ya en la biblioteca (por título base y artista): [{album_id, album, ruta, titulo, calidad,
    duracion}]."""
    if not db or not os.path.isfile(db) or not titulo:
        return []
    base = _base(titulo)
    if not base:
        return []
    palabra = max(base.split(), key=len)
    out = []
    con = _con(db)
    try:
        filas = con.execute(
            'SELECT t.title, t.duration, t.bit_depth, t.sample_rate, t.codec, al.id AS album_id, al.name AS album, '
            'al.path AS ruta, ar.name AS artista FROM tracks t JOIN albums al ON al.id = t.album_id '
            'LEFT JOIN artists ar ON ar.id = al.artist_id WHERE lower(t.title) LIKE ? LIMIT 400',
            (f'%{palabra.lower()}%',)).fetchall()
    except sqlite3.Error:
        filas = []
    finally:
        con.close()
    art = analisis._norm(artista or '')
    for r in filas:
        if _base(r['title']) != base:
            continue
        if art and _parecido(art, r['artista']) < 0.6 and art not in analisis._norm(r['artista'] or ''):
            continue
        out.append({'album_id': r['album_id'], 'album': r['album'], 'ruta': r['ruta'], 'titulo': r['title'],
                    'calidad': _calidad(r), 'duracion': r['duration']})
        if len(out) >= limite:
            break
    return out


def candidatos(db, artista, album, q=None, limite=8):
    """Álbumes de la biblioteca a los que podría ir la pista: del mismo artista y con el nombre más parecido al
    álbum de origen (o a lo que se busque). Cada uno con sus pistas, para ver los lugares libres u ocupados."""
    if not db or not os.path.isfile(db):
        return []
    con = _con(db)
    try:
        if q:
            filas = con.execute(
                'SELECT al.id, al.name, al.path, al.year, ar.name AS artista FROM albums al '
                'LEFT JOIN artists ar ON ar.id = al.artist_id WHERE lower(al.name) LIKE ? OR lower(ar.name) LIKE ? '
                'LIMIT 200', (f'%{q.lower()}%', f'%{q.lower()}%')).fetchall()
        else:
            art = analisis._norm(artista or '')
            filas = [r for r in con.execute(
                'SELECT al.id, al.name, al.path, al.year, ar.name AS artista FROM albums al '
                'LEFT JOIN artists ar ON ar.id = al.artist_id').fetchall()
                if art and (_parecido(art, r['artista']) >= 0.85 or art == analisis._norm(r['artista'] or ''))]
        ref = album or ''
        filas = sorted(filas, key=lambda r: (-_parecido(_base(ref), _base(r['name'])) if ref else 0,
                                             -(int(r['year']) if str(r['year'] or '').isdigit() else 0)))[:limite]
        return [_album(con, r, ref) for r in filas]
    finally:
        con.close()


def _album(con, r, ref=None):
    pistas = [{'id': t['id'], 'disco': int(t['disc_number'] or 1), 'numero': t['track_number'],
               'titulo': t['title'], 'duracion': t['duration'], 'calidad': _calidad(t),
               'archivo': os.path.basename(t['file_path'] or ''), 'ruta': t['file_path']}
              for t in con.execute('SELECT id, disc_number, track_number, title, duration, bit_depth, '
                                   'sample_rate, codec, file_path FROM tracks WHERE album_id=? '
                                   'ORDER BY disc_number, track_number', (r['id'],))]
    return {'id': r['id'], 'album': r['name'], 'artista': r['artista'], 'anio': r['year'], 'ruta': r['path'],
            'pistas': pistas, 'parecido': round(_parecido(_base(ref), _base(r['name'])), 2) if ref else None}


def album_por_id(db, album_id):
    con = _con(db)
    try:
        r = con.execute('SELECT al.id, al.name, al.path, al.year, ar.name AS artista FROM albums al '
                        'LEFT JOIN artists ar ON ar.id = al.artist_id WHERE al.id=?', (album_id,)).fetchone()
        return _album(con, r) if r else None
    finally:
        con.close()


# ─────────────────────────── agregar al álbum ───────────────────────────

def _numero(v):
    try:
        return int(str(v).split('/')[0])
    except (TypeError, ValueError):
        return None


def etiquetar(ruta, referencia, titulo, artista, disco, numero, isrc=None):
    """Tags de la pista: los del álbum (de una pista que ya está en él) + los suyos. Carátula: la del álbum si la
    pista no trae (la de Tidal es la de su álbum de origen, que puede ser otro)."""
    import mutagen
    from mutagen.flac import FLAC
    if ruta.lower().endswith('.flac'):
        f = FLAC(ruta)
        ref = None
        if referencia and referencia.lower().endswith('.flac'):
            try:
                ref = FLAC(referencia)
            except Exception:  # noqa: BLE001
                ref = None
        if ref is not None:
            for k in list(f.keys()):
                if k.upper() in CAMPOS_ALBUM:
                    del f[k]
            for k, v in ref.items():
                if k.upper() in CAMPOS_ALBUM:
                    f[k] = v
            f.clear_pictures()
            for p in ref.pictures:
                f.add_picture(p)
        f['TITLE'] = [titulo]
        if artista:
            f['ARTIST'] = [artista]
        f['TRACKNUMBER'] = [str(numero)]
        f['DISCNUMBER'] = [str(disco)]
        for k in ('TRACKTOTAL', 'TOTALTRACKS'):
            if k in f:
                del f[k]
        if isrc:
            f['ISRC'] = [isrc]
        f.save()
        return
    f = mutagen.File(ruta, easy=True)
    if f is None:
        raise FalloPista('No se pudo leer el archivo para escribirle los tags')
    try:
        r = mutagen.File(referencia, easy=True) if referencia else None
    except Exception:  # noqa: BLE001
        r = None
    for k in ('album', 'albumartist', 'date', 'genre'):
        if r is not None and r.get(k):
            f[k] = r[k]
    f['title'] = [titulo]
    if artista:
        f['artist'] = [artista]
    f['tracknumber'] = [str(numero)]
    f['discnumber'] = [str(disco)]
    f.save()


def plan(album, disco, numero, modo, titulo, ext):
    """Dónde queda la pista en `album` (de candidatos): {disco, numero, nombre, ocupada, n_discos}.
    modo: 'agregar' (lugar libre) · 'reemplazar' (la que está ahí va a la papelera) · 'extra' (al final del disco)."""
    pistas = album['pistas']
    disco = max(1, int(disco or 1))
    if modo == 'extra':
        numero = max([_numero(p['numero']) or 0 for p in pistas if p['disco'] == disco] or [0]) + 1
    numero = _numero(numero)
    if not numero or numero < 1:
        raise FalloPista('Indica el número de pista')
    ocupada = next((p for p in pistas if p['disco'] == disco and _numero(p['numero']) == numero), None)
    if ocupada and modo == 'agregar':
        raise FalloPista(f'En el disco {disco}, pista {numero}, ya está «{ocupada["titulo"]}»: elige reemplazarla '
                         'o agregarla como extra')
    if modo == 'reemplazar' and not ocupada:
        modo = 'agregar'
    n_discos = len({p['disco'] for p in pistas} | {disco})
    nombre = ajustar_mod.nombre_pista(disco, numero, titulo, ext, n_discos)
    return {'disco': disco, 'numero': numero, 'nombre': nombre, 'ocupada': ocupada if modo == 'reemplazar' else None,
            'n_discos': n_discos, 'modo': modo}
