"""
Orbyte Ingesta — artistas duplicados en music.db («Vilma Palma E Vampiros» / «VILMA PALMA E VAMPIROS»).

  detectar()       grupos de artistas cuyo nombre coincide ignorando mayúsculas, tildes, signos, «The» y «&/and»
  nombre_oficial() MusicBrainz → Last.fm (artist.getCorrection) → variante con más álbumes
  unificar()       respaldo de music.db → carpetas (mover/renombrar, en dos pasos si solo cambian mayúsculas)
                   → tags de los archivos → base (álbumes, pistas, datos, favoritos, Descubrir) → registro
"""
import os
import sqlite3
import threading
import time
import urllib.parse
from collections import Counter

from . import analisis, edicion

try:
    import mutagen
    from mutagen.flac import FLAC
except ImportError:
    mutagen = None


BLOQUEO = threading.Lock()      # una unificación a la vez; la cola de proceso espera mientras tanto


def clave(nombre):
    return analisis._norm(nombre).replace(' ', '')


def _con(db, solo_lectura=False):
    if solo_lectura:
        c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    else:
        c = sqlite3.connect(db, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def _carpeta_artista(ruta_album, biblioteca):
    """'/mnt/musica/V/Vilma Palma/Histeria/CD 01' → '/mnt/musica/V/Vilma Palma'."""
    rel = os.path.relpath(ruta_album, biblioteca).split(os.sep)
    if len(rel) >= 3 and not rel[0].startswith('..'):
        return os.path.join(biblioteca, rel[0], rel[1])
    return None


def detectar(db, biblioteca, ignorados=()):
    con = _con(db, solo_lectura=True)
    try:
        artistas = con.execute('SELECT id, name, nationality, lastfm_listeners FROM artists').fetchall()
        grupos = {}
        for a in artistas:
            grupos.setdefault(clave(a['name']), []).append(a)
        grupos = {k: v for k, v in grupos.items() if len(v) > 1 and k and k not in ignorados}
        salida = []
        for k, filas in sorted(grupos.items()):
            variantes = []
            for a in filas:
                albumes = con.execute('SELECT id, name, year, path FROM albums WHERE artist_id=? ORDER BY year, name',
                                      (a['id'],)).fetchall()
                n_pistas = con.execute('SELECT COUNT(*) FROM tracks t JOIN albums al ON al.id=t.album_id '
                                       'WHERE al.artist_id=?', (a['id'],)).fetchone()[0]
                favs = con.execute('SELECT COUNT(*) FROM user_favorite_artists WHERE artist_id=?',
                                   (a['id'],)).fetchone()[0]
                carpetas = Counter(_carpeta_artista(al['path'], biblioteca) for al in albumes)
                variantes.append({
                    'id': a['id'], 'nombre': a['name'], 'nacionalidad': a['nationality'],
                    'oyentes': a['lastfm_listeners'], 'favoritos': favs, 'pistas': n_pistas,
                    'albumes': [{'id': al['id'], 'nombre': al['name'], 'anio': al['year'], 'ruta': al['path']}
                                for al in albumes],
                    'carpetas': [os.path.relpath(c, biblioteca) for c in carpetas if c],
                })
            variantes.sort(key=lambda v: (-len(v['albumes']), -(v['oyentes'] or 0)))
            salida.append({'clave': k, 'variantes': variantes})
        return salida
    finally:
        con.close()


def nombre_oficial(cliente, variantes):
    """{'nombre', 'fuente', 'detalle', 'url'} — se valida que calce con el grupo."""
    k = clave(variantes[0]['nombre'])
    for v in variantes:
        try:
            res = cliente.mb('artist/', query=f'artist:"{edicion._esc(v["nombre"])}"', limit=10) or {}
        except edicion.SinRed:
            break
        for a in res.get('artists', []):
            if clave(a.get('name')) == k and (a.get('score') or 0) >= 80:
                detalle = ' · '.join(x for x in [a.get('type'), (a.get('area') or {}).get('name') or a.get('country'),
                                                 a.get('disambiguation')] if x)
                return {'nombre': a['name'], 'fuente': 'MusicBrainz', 'detalle': detalle,
                        'url': f'https://musicbrainz.org/artist/{a["id"]}'}
    clave_lfm = os.environ.get('LASTFM_API_KEY')
    if clave_lfm:
        for v in variantes:
            url = 'https://ws.audioscrobbler.com/2.0/?' + urllib.parse.urlencode(
                {'method': 'artist.getcorrection', 'artist': v['nombre'], 'api_key': clave_lfm, 'format': 'json'})
            try:
                d = cliente.get(url, cachear=True, pausa=0.3) or {}
            except edicion.SinRed:
                break
            corr = ((d.get('corrections') or {}).get('correction') or {})
            if isinstance(corr, list):
                corr = corr[0] if corr else {}
            nombre = (corr.get('artist') or {}).get('name')
            if nombre and clave(nombre) == k:
                return {'nombre': nombre, 'fuente': 'Last.fm', 'detalle': 'corrección oficial de Last.fm',
                        'url': f'https://www.last.fm/music/{urllib.parse.quote(nombre)}'}
    return {'nombre': variantes[0]['nombre'], 'fuente': None,
            'detalle': 'No se pudo validar en línea: se propone la variante con más álbumes', 'url': None}


# ─────────────────────────── unificar ───────────────────────────

def respaldo(db, registro):
    destino = f'{db}.bak-{time.strftime("%Y%m%d-%H%M%S")}'
    src = sqlite3.connect(db, timeout=60)
    dst = sqlite3.connect(destino)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    registro(f'Respaldo de la base: {destino}')
    return destino


def _renombrar(origen, destino):
    """Renombra aunque solo cambien mayúsculas (el disco compartido no las distingue): pasa por un nombre temporal."""
    if origen == destino:
        return
    if origen.lower() == destino.lower():
        tmp = f'{origen}.unificando-{int(time.time())}'
        os.rename(origen, tmp)
        os.rename(tmp, destino)
    else:
        os.rename(origen, destino)


def _escribir_artista(ruta, nombre, cambiar_artista, cambiar_album_artist):
    ext = os.path.splitext(ruta)[1].lower()
    if ext == '.flac':
        f = FLAC(ruta)
        if f.tags is None:
            return
        if cambiar_artista:
            f.tags['ARTIST'] = nombre
        if cambiar_album_artist:
            for k in ('ALBUMARTIST', 'ALBUM ARTIST', 'ALBUM_ARTIST'):
                if k in f.tags or k == 'ALBUMARTIST':
                    f.tags[k] = nombre
        f.save()
    elif ext in ('.mp3', '.dsf', '.dff', '.aif', '.aiff'):
        from mutagen.id3 import ID3, TPE1, TPE2
        if ext == '.dsf':
            from mutagen.dsf import DSF
            f = DSF(ruta)
            t = f.tags
            if t is None:
                return
        else:
            f, t = None, ID3(ruta)
        if cambiar_artista:
            t.setall('TPE1', [TPE1(encoding=3, text=nombre)])
        if cambiar_album_artist:
            t.setall('TPE2', [TPE2(encoding=3, text=nombre)])
        if f is not None:
            f.save()
        else:
            t.save(ruta, v2_version=3)
    elif ext in ('.m4a', '.mp4'):
        from mutagen.easymp4 import EasyMP4
        f = EasyMP4(ruta)
        if cambiar_artista:
            f['artist'] = nombre
        if cambiar_album_artist:
            f['albumartist'] = nombre
        f.save()
    elif ext in ('.ogg', '.oga', '.opus'):
        f = mutagen.File(ruta)
        if cambiar_artista:
            f['ARTIST'] = nombre
        if cambiar_album_artist:
            f['ALBUMARTIST'] = nombre
        f.save()


def unificar(db, biblioteca, ids, nombre_final, registro):
    """Une los artistas `ids` bajo `nombre_final`. Devuelve un resumen. Lanza excepción si algo grave falla."""
    nombre_final = (nombre_final or '').strip()
    if not nombre_final:
        raise ValueError('Falta el nombre final')
    biblioteca = os.path.abspath(biblioteca)
    con = _con(db)
    filas = con.execute(f'SELECT * FROM artists WHERE id IN ({",".join("?" * len(ids))})', ids).fetchall()
    if len(filas) < 2:
        raise ValueError('El grupo ya no tiene duplicados')
    k = clave(filas[0]['name'])
    if any(clave(f['name']) != k for f in filas):
        raise ValueError('Los artistas elegidos no son del mismo grupo')
    if clave(nombre_final) != k:
        raise ValueError('El nombre final no corresponde a este artista')
    registro(f'Unificar {" / ".join(f["name"] for f in filas)} → «{nombre_final}»')
    respaldo(db, registro)

    # ── 1) carpetas ──
    albumes = con.execute(f'SELECT id, name, path, cover_path FROM albums WHERE artist_id IN ({",".join("?" * len(ids))})',
                          ids).fetchall()
    carpetas = Counter(_carpeta_artista(a['path'], biblioteca) for a in albumes)
    carpetas.pop(None, None)
    letra = analisis.letra_de(nombre_final)
    objetivo = os.path.join(biblioteca, letra, analisis.nombre_seguro(nombre_final))
    cambios_ruta = []           # (prefijo viejo, prefijo nuevo)
    avisos = []
    if carpetas:
        # la carpeta que ya es (o casi es) la del nombre final se renombra; las demás se vacían en ella
        base = next((c for c in carpetas if c.lower() == objetivo.lower()), None)
        if base:
            if base != objetivo:
                _renombrar(base, objetivo)
                cambios_ruta.append((base, objetivo))
                registro(f'Carpeta renombrada: {os.path.relpath(base, biblioteca)} → {os.path.relpath(objetivo, biblioteca)}')
        else:
            os.makedirs(objetivo, exist_ok=True)
            registro(f'Carpeta del artista: {os.path.relpath(objetivo, biblioteca)}')
        for c in carpetas:
            if c.lower() == objetivo.lower() or not os.path.isdir(c):
                continue
            # solo las carpetas de los álbumes de este artista (y, si al final no queda ninguna carpeta,
            # los archivos sueltos como fotos del artista)
            propias = {os.path.relpath(a['path'], c).split(os.sep)[0] for a in albumes
                       if a['path'] and a['path'].startswith(c + os.sep)}
            for nombre in sorted(propias):
                o, d = os.path.join(c, nombre), os.path.join(objetivo, nombre)
                if not os.path.exists(o):
                    avisos.append(f'{os.path.relpath(o, biblioteca)} no existe en el disco: es un registro de Orbyte '
                                  f'sin carpeta (álbum fantasma)')
                    continue
                ajenos = con.execute(
                    f'SELECT COUNT(*) FROM albums WHERE (path = ? OR substr(path, 1, ?) = ?) '
                    f'AND artist_id NOT IN ({",".join("?" * len(ids))})',
                    [o, len(o) + 1, o + os.sep] + list(ids)).fetchone()[0]
                if ajenos:
                    avisos.append(f'No se movió {os.path.relpath(o, biblioteca)}: contiene {ajenos} álbum(es) de otro '
                                  f'artista en Orbyte')
                    continue
                if os.path.exists(d):
                    avisos.append(f'No se movió {os.path.relpath(o, biblioteca)}: ya existe en la carpeta destino')
                    continue
                os.rename(o, d)
                cambios_ruta.append((o, d))
                registro(f'Movido: {os.path.relpath(o, biblioteca)} → {os.path.relpath(d, biblioteca)}')
            restantes = os.listdir(c)
            if restantes and not any(os.path.isdir(os.path.join(c, x)) for x in restantes):
                for nombre in restantes:
                    o, d = os.path.join(c, nombre), os.path.join(objetivo, nombre)
                    if not os.path.exists(d):
                        os.rename(o, d)
            try:
                os.rmdir(c)
                registro(f'Carpeta vacía eliminada: {os.path.relpath(c, biblioteca)}')
            except OSError:
                quedan = sorted(os.listdir(c)) if os.path.isdir(c) else []
                avisos.append(f'La carpeta {os.path.relpath(c, biblioteca)} quedó con contenido que no pertenece a '
                              f'los álbumes de este artista en Orbyte: ' + ', '.join(quedan[:8]) +
                              (' …' if len(quedan) > 8 else ''))

    # ── 2) rutas en la base ──
    def nueva_ruta(p):
        if not p:
            return p
        for viejo, nuevo in cambios_ruta:
            if p == viejo or p.startswith(viejo + os.sep):
                return nuevo + p[len(viejo):]
        return p

    with con:
        if cambios_ruta:
            for a in albumes:
                con.execute('UPDATE albums SET path=?, cover_path=? WHERE id=?',
                            (nueva_ruta(a['path']), nueva_ruta(a['cover_path']), a['id']))
            for t in con.execute(f'SELECT id, file_path FROM tracks WHERE album_id IN ({",".join("?" * len(albumes))})',
                                 [a['id'] for a in albumes]).fetchall():
                nueva = nueva_ruta(t['file_path'])
                if nueva != t['file_path']:
                    con.execute('UPDATE tracks SET file_path=? WHERE id=?', (nueva, t['id']))
            registro(f'Rutas actualizadas en la base ({len(albumes)} álbumes)')

    # ── 3) tags de los archivos ──
    pistas = con.execute(f'SELECT id, file_path, artist, album_artist FROM tracks WHERE album_id IN '
                         f'({",".join("?" * len(albumes))})', [a['id'] for a in albumes]).fetchall() if albumes else []
    escritas = fallidas = 0
    for t in pistas:
        ca = bool(t['artist']) and clave(t['artist']) == k and t['artist'] != nombre_final
        caa = (not t['album_artist']) or (clave(t['album_artist']) == k and t['album_artist'] != nombre_final)
        if not (ca or caa) or not t['file_path'] or not os.path.isfile(t['file_path']):
            continue
        try:
            _escribir_artista(t['file_path'], nombre_final, ca, caa)
            escritas += 1
        except Exception as e:
            fallidas += 1
            if fallidas <= 5:
                avisos.append(f'No se pudo escribir el tag en {os.path.basename(t["file_path"])}: {e}')
    registro(f'Tags reescritos en {escritas} archivos' + (f' ({fallidas} fallaron)' if fallidas else ''))

    # ── 4) base: un solo artista ──
    con.row_factory = sqlite3.Row
    with con:
        quedan = sorted(filas, key=lambda f: (f['name'] != nombre_final,
                                              -con.execute('SELECT COUNT(*) FROM albums WHERE artist_id=?',
                                                           (f['id'],)).fetchone()[0],
                                              -(f['lastfm_listeners'] or 0)))
        keep = quedan[0]
        otros = [f['id'] for f in quedan[1:]]
        marcas = ','.join('?' * len(otros))
        # datos que el artista elegido no tenga, desde los otros
        for col in ('nationality', 'similar_artists_json', 'lastfm_listeners', 'lastfm_playcount',
                    'lastfm_fetched_at', 'website_url'):
            if keep[col] in (None, ''):
                v = next((f[col] for f in quedan[1:] if f[col] not in (None, '')), None)
                if v is not None:
                    con.execute(f'UPDATE artists SET {col}=? WHERE id=?', (v, keep['id']))
        con.execute(f'UPDATE albums SET artist_id=? WHERE artist_id IN ({marcas})', [keep['id']] + otros)
        con.execute(f'INSERT OR IGNORE INTO user_favorite_artists (user_id, artist_id, added_at) '
                    f'SELECT user_id, ?, added_at FROM user_favorite_artists WHERE artist_id IN ({marcas})',
                    [keep['id']] + otros)
        con.execute(f'DELETE FROM user_favorite_artists WHERE artist_id IN ({marcas})', otros)
        con.execute(f"INSERT OR IGNORE INTO user_item_favorites (user_id, item_type, item_id, added_at) "
                    f"SELECT user_id, 'artist', ?, added_at FROM user_item_favorites "
                    f"WHERE item_type='artist' AND item_id IN ({marcas})", [keep['id']] + otros)
        con.execute(f"DELETE FROM user_item_favorites WHERE item_type='artist' AND item_id IN ({marcas})", otros)
        tablas = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'descubrir_items' in tablas:
            con.execute(f'UPDATE descubrir_items SET artista_id=? WHERE artista_id IN ({marcas})', [keep['id']] + otros)
        if 'descubrir_artistas_ext' in tablas:
            if not con.execute('SELECT 1 FROM descubrir_artistas_ext WHERE artista_id=?', (keep['id'],)).fetchone():
                con.execute(f'UPDATE descubrir_artistas_ext SET artista_id=? WHERE rowid = (SELECT rowid FROM '
                            f'descubrir_artistas_ext WHERE artista_id IN ({marcas}) LIMIT 1)', [keep['id']] + otros)
            con.execute(f'DELETE FROM descubrir_artistas_ext WHERE artista_id IN ({marcas})', otros)
        con.execute(f'DELETE FROM artists WHERE id IN ({marcas})', otros)
        con.execute('UPDATE artists SET name=?, letter=? WHERE id=?',
                    (nombre_final, nombre_final[0].upper(), keep['id']))
        ids_alb = [a['id'] for a in albumes]
        if ids_alb:
            m2 = ','.join('?' * len(ids_alb))
            for col in ('artist', 'album_artist'):
                for t in con.execute(f'SELECT id, {col} FROM tracks WHERE album_id IN ({m2})', ids_alb).fetchall():
                    if t[col] and clave(t[col]) == k and t[col] != nombre_final:
                        con.execute(f'UPDATE tracks SET {col}=? WHERE id=?', (nombre_final, t['id']))
    fantasmas = [a for a in albumes if a['path'] and not os.path.isdir(nueva_ruta(a['path']))]
    if fantasmas:
        avisos.append(f'{len(fantasmas)} álbum(es) de este artista están en Orbyte pero su carpeta ya no existe '
                      f'(fantasmas): ' + ', '.join(f'{a["name"]} [{os.path.relpath(nueva_ruta(a["path"]), biblioteca)}]'
                                                  for a in fantasmas[:6]))
    con.close()
    registro(f'Base actualizada: queda un solo artista (ID {keep["id"]}) con {len(albumes)} álbumes')
    for a in avisos:
        registro('⚠ ' + a)
    return {'artista_id': keep['id'], 'albumes': len(albumes), 'tags': escritas, 'avisos': avisos,
            'fantasmas': [a['id'] for a in fantasmas],
            'carpeta': os.path.relpath(objetivo, biblioteca)}

