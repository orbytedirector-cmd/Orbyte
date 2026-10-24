"""
Orbyte Ingesta — álbumes fantasma: registrados en music.db pero su carpeta ya no existe en el disco.

  buscar()   recorre los álbumes de la base y, para cada fantasma, busca un «gemelo» que sí exista
             (mismo artista y mismo nombre de álbum) y empareja sus pistas (disco + número, o título)
  limpiar()  respaldo → lo que apunta a las pistas del fantasma (playlists, favoritos, historial, cola
             colaborativa, recomendaciones) pasa a la pista gemela → se borra el fantasma (álbum, pistas,
             metadata, popularidad). Sin gemelo: se borra igual si lo eliges; el historial de escucha se conserva.
"""
import os
import re
import sqlite3
import unicodedata

from . import analisis
from .artistas import respaldo

# (tabla, columna, condición extra) que apuntan a una pista
REFS_PISTA = [
    ('playlist_tracks', 'track_id', ''),
    ('listening_events', 'track_id', ''),
    ('collab_queue_items', 'track_id', ''),
    ('social_toques', 'track_id', ''),
    ('social_recomendaciones', 'track_id', ''),
    ('social_likes', 'ref_id', "AND tipo='pista'"),
]
# se conservan aunque la pista desaparezca (no hay a dónde moverlas y no molestan)
CONSERVAR_SIN_GEMELO = {'listening_events'}


def _titulo(s):
    s = ''.join(c for c in unicodedata.normalize('NFKD', s or '') if not unicodedata.combining(c)).lower()
    s = re.sub(r'\([^)]*\)|\[[^\]]*\]', ' ', s)
    return re.sub(r'[^a-z0-9]+', '', s)


def _tablas(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def buscar(db, progreso=None):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        albumes = con.execute('SELECT al.id, al.name, al.year, al.path, al.artist_id, ar.name AS artista '
                              'FROM albums al JOIN artists ar ON ar.id = al.artist_id').fetchall()
        existe = {}
        for i, a in enumerate(albumes, 1):
            existe[a['id']] = bool(a['path']) and os.path.isdir(a['path'])
            if progreso and i % 200 == 0:
                progreso(i, len(albumes))
        if progreso:
            progreso(len(albumes), len(albumes))
        faltan = [a for a in albumes if not existe[a['id']]]
        if albumes and len(faltan) > max(50, len(albumes) * 0.2):
            raise RuntimeError(f'Faltan {len(faltan)} de {len(albumes)} carpetas: ¿está montada la biblioteca?')
        tablas = _tablas(con)
        por_artista = {}
        for a in albumes:
            if existe[a['id']]:
                por_artista.setdefault(a['artist_id'], []).append(a)
        salida = []
        for a in faltan:
            pistas = con.execute('SELECT id, title, track_number, disc_number FROM tracks WHERE album_id=?',
                                 (a['id'],)).fetchall()
            ids = [p['id'] for p in pistas]
            refs = {}
            for tabla, col, extra in REFS_PISTA:
                if tabla in tablas and ids:
                    n = con.execute(f'SELECT COUNT(*) FROM {tabla} WHERE {col} IN ({",".join("?" * len(ids))}) {extra}',
                                    ids).fetchone()[0]
                    if n:
                        refs[tabla] = n
            if ids:
                n = con.execute(f"SELECT COUNT(*) FROM user_item_favorites WHERE item_type='track' AND item_id IN "
                                f"({','.join('?' * len(ids))})", ids).fetchone()[0]
                if n:
                    refs['favoritos (pistas)'] = n
            n = con.execute("SELECT COUNT(*) FROM user_item_favorites WHERE item_type='album' AND item_id=?",
                            (a['id'],)).fetchone()[0]
            if n:
                refs['favoritos (álbum)'] = n
            gemelo, emparejadas = None, 0
            candidatos = [b for b in por_artista.get(a['artist_id'], [])
                          if analisis._norm_album(b['name']) == analisis._norm_album(a['name'])]
            mejor = None
            for b in candidatos:
                mapa = emparejar(con, pistas, b['id'])
                if mejor is None or len(mapa) > len(mejor[1]):
                    mejor = (b, mapa)
            if mejor:
                gemelo, emparejadas = mejor[0], len(mejor[1])
            salida.append({
                'id': a['id'], 'album': a['name'], 'anio': a['year'], 'artista': a['artista'], 'ruta': a['path'],
                'pistas': len(pistas), 'refs': refs,
                'gemelo': {'id': gemelo['id'], 'album': gemelo['name'], 'ruta': gemelo['path']} if gemelo else None,
                'emparejadas': emparejadas,
            })
        salida.sort(key=lambda x: (x['artista'].lower(), x['album'].lower()))
        return {'total': len(albumes), 'fantasmas': salida}
    finally:
        con.close()


def emparejar(con, pistas, gemelo_id):
    """{id pista fantasma: id pista gemela} por (disco, número) con título parecido, o por título."""
    otras = con.execute('SELECT id, title, track_number, disc_number FROM tracks WHERE album_id=?',
                        (gemelo_id,)).fetchall()
    por_num = {}
    por_tit = {}
    for o in otras:
        por_num.setdefault((str(o['disc_number'] or 1), str(o['track_number'] or '')), []).append(o)
        por_tit.setdefault(_titulo(o['title']), []).append(o)
    mapa = {}
    usados = set()
    for p in pistas:
        t = _titulo(p['title'])
        elegido = None
        for o in por_num.get((str(p['disc_number'] or 1), str(p['track_number'] or '')), []):
            if o['id'] not in usados and (not t or _titulo(o['title']) == t):
                elegido = o
                break
        if not elegido and t:
            elegido = next((o for o in por_tit.get(t, []) if o['id'] not in usados), None)
        if elegido:
            mapa[p['id']] = elegido['id']
            usados.add(elegido['id'])
    return mapa


def limpiar(db, ids, registro):
    """Borra los álbumes fantasma `ids` (vuelve a comprobar que su carpeta no exista)."""
    respaldo(db, registro)
    con = sqlite3.connect(db, timeout=60)
    con.row_factory = sqlite3.Row
    tablas = _tablas(con)
    resumen = {'borrados': 0, 'pistas': 0, 'movidas': 0, 'quitadas': 0, 'saltados': []}
    try:
        for aid in ids:
            a = con.execute('SELECT id, name, path, artist_id FROM albums WHERE id=?', (aid,)).fetchone()
            if not a:
                continue
            if a['path'] and os.path.isdir(a['path']):
                resumen['saltados'].append(a['name'])
                registro(f'⚠ {a["name"]}: su carpeta sí existe, no se toca')
                continue
            pistas = con.execute('SELECT id, title, track_number, disc_number FROM tracks WHERE album_id=?',
                                 (aid,)).fetchall()
            pids = [p['id'] for p in pistas]
            gemelo = None
            for b in con.execute('SELECT id, name, path FROM albums WHERE artist_id=? AND id<>?',
                                 (a['artist_id'], aid)).fetchall():
                if analisis._norm_album(b['name']) == analisis._norm_album(a['name']) and b['path'] \
                        and os.path.isdir(b['path']):
                    mapa = emparejar(con, pistas, b['id'])
                    if gemelo is None or len(mapa) > len(gemelo[1]):
                        gemelo = (b, mapa)
            mapa = gemelo[1] if gemelo else {}
            with con:
                movidas = quitadas = 0
                for viejo in pids:
                    nuevo = mapa.get(viejo)
                    for tabla, col, extra in REFS_PISTA:
                        if tabla not in tablas:
                            continue
                        if nuevo:
                            movidas += con.execute(f'UPDATE {tabla} SET {col}=? WHERE {col}=? {extra}',
                                                   (nuevo, viejo)).rowcount
                        elif tabla not in CONSERVAR_SIN_GEMELO:
                            quitadas += con.execute(f'DELETE FROM {tabla} WHERE {col}=? {extra}', (viejo,)).rowcount
                    if nuevo:
                        con.execute("INSERT OR IGNORE INTO user_item_favorites (user_id, item_type, item_id, added_at) "
                                    "SELECT user_id, 'track', ?, added_at FROM user_item_favorites "
                                    "WHERE item_type='track' AND item_id=?", (nuevo, viejo))
                    n = con.execute("DELETE FROM user_item_favorites WHERE item_type='track' AND item_id=?",
                                    (viejo,)).rowcount
                    if not nuevo:
                        quitadas += n
                if gemelo:
                    gid = gemelo[0]['id']
                    con.execute("INSERT OR IGNORE INTO user_item_favorites (user_id, item_type, item_id, added_at) "
                                "SELECT user_id, 'album', ?, added_at FROM user_item_favorites "
                                "WHERE item_type='album' AND item_id=?", (gid, aid))
                    if 'descubrir_items' in tablas:
                        con.execute('UPDATE descubrir_items SET album_id=? WHERE album_id=?', (gid, aid))
                    if 'descubrir_albums_ext' in tablas and not con.execute(
                            'SELECT 1 FROM descubrir_albums_ext WHERE album_id=?', (gid,)).fetchone():
                        con.execute('UPDATE descubrir_albums_ext SET album_id=? WHERE album_id=?', (gid, aid))
                else:
                    if 'descubrir_items' in tablas:
                        con.execute('UPDATE descubrir_items SET album_id=NULL WHERE album_id=?', (aid,))
                con.execute("DELETE FROM user_item_favorites WHERE item_type='album' AND item_id=?", (aid,))
                if 'descubrir_albums_ext' in tablas:
                    con.execute('DELETE FROM descubrir_albums_ext WHERE album_id=?', (aid,))
                if pids:
                    m = ','.join('?' * len(pids))
                    for tabla in ('track_meta', 'track_pop_cache'):
                        if tabla in tablas:
                            con.execute(f'DELETE FROM {tabla} WHERE track_id IN ({m})', pids)
                    con.execute(f'DELETE FROM tracks WHERE id IN ({m})', pids)
                for tabla in ('album_meta', 'album_pop_cache'):
                    if tabla in tablas:
                        con.execute(f'DELETE FROM {tabla} WHERE album_id=?', (aid,))
                con.execute('DELETE FROM albums WHERE id=?', (aid,))
                # artista sin álbumes ni favoritos: se va, como hace el indexador
                if not con.execute('SELECT 1 FROM albums WHERE artist_id=?', (a['artist_id'],)).fetchone() and \
                        not con.execute('SELECT 1 FROM user_favorite_artists WHERE artist_id=?',
                                        (a['artist_id'],)).fetchone():
                    con.execute('DELETE FROM artists WHERE id=?', (a['artist_id'],))
            resumen['borrados'] += 1
            resumen['pistas'] += len(pids)
            resumen['movidas'] += movidas
            resumen['quitadas'] += quitadas
            txt = f'Borrado «{a["name"]}» (ID {aid}, {len(pids)} pistas)'
            if gemelo:
                txt += f' · gemelo ID {gemelo[0]["id"]}: {len(mapa)}/{len(pids)} pistas emparejadas, {movidas} referencias movidas'
            if quitadas:
                txt += f' · {quitadas} referencias sin pista equivalente quitadas'
            registro(txt)
    finally:
        con.close()
    registro(f'Listo: {resumen["borrados"]} álbumes fantasma eliminados de Orbyte')
    return resumen
