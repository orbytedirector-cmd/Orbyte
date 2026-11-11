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
            if not candidatos:
                # artista escrito distinto («Christian Castro» / «Cristian Castro»): misma carpeta de álbum y mismo nombre
                candidatos = [b for lista in por_artista.values() for b in lista
                              if _misma_carpeta(a, b)]
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


def _misma_carpeta(a, b):
    return (analisis._norm_album(b['name']) == analisis._norm_album(a['name']) and a['path'] and b['path']
            and os.path.basename(a['path'].rstrip('/')) == os.path.basename(b['path'].rstrip('/')))


def emparejar(con, pistas, gemelo_id):
    """{id pista fantasma: id pista gemela} por (disco, número) con título parecido, o por título."""
    otras = con.execute('SELECT id, title, track_number, disc_number FROM tracks WHERE album_id=?',
                        (gemelo_id,)).fetchall()
    return emparejar_con(pistas, otras)


def emparejar_con(pistas, otras):
    """Igual que emparejar(), contra una lista de filas (id, title, track_number, disc_number)."""
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
            cands = [b for b in con.execute('SELECT id, name, path, artist_id FROM albums WHERE id<>?', (aid,)).fetchall()
                     if analisis._norm_album(b['name']) == analisis._norm_album(a['name'])]
            mismos = [b for b in cands if b['artist_id'] == a['artist_id']] or [b for b in cands if _misma_carpeta(a, b)]
            for b in mismos:
                if b['path'] and os.path.isdir(b['path']):
                    mapa = emparejar(con, pistas, b['id'])
                    if gemelo is None or len(mapa) > len(gemelo[1]):
                        gemelo = (b, mapa)
            mapa = gemelo[1] if gemelo else {}
            with con:
                movidas, quitadas = borrar_album_db(con, tablas, a, pids, mapa,
                                                    gemelo[0]['id'] if gemelo else None)
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


def borrar_album_db(con, tablas, a, pids, mapa, destino_id):
    """
    Saca un álbum de music.db. Lo que apunta a sus pistas pasa a la pista equivalente de `destino_id`
    (mapa {pista vieja: pista nueva}); lo que no tiene equivalente se quita (el historial se conserva).
    Devuelve (referencias movidas, referencias quitadas). Debe llamarse dentro de una transacción.
    """
    aid = a['id']
    movidas, quitadas = borrar_pistas_db(con, tablas, pids, mapa, borrar_filas=False)
    if destino_id:
        con.execute("INSERT OR IGNORE INTO user_item_favorites (user_id, item_type, item_id, added_at) "
                    "SELECT user_id, 'album', ?, added_at FROM user_item_favorites "
                    "WHERE item_type='album' AND item_id=?", (destino_id, aid))
        if 'descubrir_items' in tablas:
            con.execute('UPDATE descubrir_items SET album_id=? WHERE album_id=?', (destino_id, aid))
        if 'descubrir_albums_ext' in tablas and not con.execute(
                'SELECT 1 FROM descubrir_albums_ext WHERE album_id=?', (destino_id,)).fetchone():
            con.execute('UPDATE descubrir_albums_ext SET album_id=? WHERE album_id=?', (destino_id, aid))
    elif 'descubrir_items' in tablas:
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
            not con.execute('SELECT 1 FROM user_favorite_artists WHERE artist_id=?', (a['artist_id'],)).fetchone():
        con.execute('DELETE FROM artists WHERE id=?', (a['artist_id'],))
    return movidas, quitadas


def borrar_pistas_db(con, tablas, pids, mapa, borrar_filas=True):
    """Lo que apunta a las pistas `pids` pasa a su equivalente (mapa {vieja: nueva}); lo que no tiene equivalente se
    quita (el historial se conserva). Con `borrar_filas` también borra las pistas (y su metadata y popularidad).
    Devuelve (referencias movidas, referencias quitadas). Debe llamarse dentro de una transacción."""
    movidas = quitadas = 0
    for viejo in pids:
        nuevo = mapa.get(viejo)
        for tabla, col, extra in REFS_PISTA:
            if tabla not in tablas:
                continue
            if nuevo:
                movidas += con.execute(f'UPDATE {tabla} SET {col}=? WHERE {col}=? {extra}', (nuevo, viejo)).rowcount
            elif tabla not in CONSERVAR_SIN_GEMELO:
                quitadas += con.execute(f'DELETE FROM {tabla} WHERE {col}=? {extra}', (viejo,)).rowcount
        if 'user_item_favorites' not in tablas:
            continue
        if nuevo:
            con.execute("INSERT OR IGNORE INTO user_item_favorites (user_id, item_type, item_id, added_at) "
                        "SELECT user_id, 'track', ?, added_at FROM user_item_favorites "
                        "WHERE item_type='track' AND item_id=?", (nuevo, viejo))
        n = con.execute("DELETE FROM user_item_favorites WHERE item_type='track' AND item_id=?", (viejo,)).rowcount
        if not nuevo:
            quitadas += n
    if borrar_filas and pids:
        m = ','.join('?' * len(pids))
        for tabla in ('track_meta', 'track_pop_cache'):
            if tabla in tablas:
                con.execute(f'DELETE FROM {tabla} WHERE track_id IN ({m})', pids)
        con.execute(f'DELETE FROM tracks WHERE id IN ({m})', pids)
    return movidas, quitadas


def _album_totales(con, aid):
    """track_count y total_duration después de quitar pistas (el formato principal lo deja el indexador)."""
    n, dur = con.execute('SELECT COUNT(*), COALESCE(SUM(duration), 0) FROM tracks WHERE album_id=?', (aid,)).fetchone()
    con.execute('UPDATE albums SET track_count=?, total_duration=? WHERE id=?', (n, dur, aid))


def limpiar_pistas(db, registro, carpeta=None, rutas=None, respaldar=True):
    """Pistas de music.db cuyo archivo ya no está, de un álbum cuya carpeta SÍ existe (si la carpeta entera falta es
    un fantasma: lo ve la página Fantasmas, y así un disco desmontado nunca borra nada). Lo que apunta a ellas
    (playlists, favoritos, colas, recomendaciones) pasa a la pista equivalente del mismo álbum (mismo disco y número
    con el mismo título, o el mismo título); el historial de escucha se conserva. Se elige con `carpeta` (todas las
    de esa carpeta y sus subcarpetas) o con `rutas` (las de Salud). → {'borradas', 'movidas', 'quitadas',
    'albumes_borrados'}"""
    out = {'borradas': 0, 'movidas': 0, 'quitadas': 0, 'albumes_borrados': 0}
    con = sqlite3.connect(db, timeout=60)
    con.row_factory = sqlite3.Row
    try:
        q = 'SELECT t.id, t.title, t.track_number, t.disc_number, t.file_path, t.album_id FROM tracks t'
        if carpeta:
            c = carpeta.rstrip('/') + '/'
            filas = con.execute(q + ' WHERE substr(t.file_path, 1, ?) = ?', (len(c), c)).fetchall()
        else:
            filas = []
            rutas = list(rutas or [])
            for k in range(0, len(rutas), 500):
                parte = rutas[k:k + 500]
                filas += con.execute(q + f' WHERE t.file_path IN ({",".join("?" * len(parte))})', parte).fetchall()
        sin = []
        for f in filas:
            if not f['file_path'] or os.path.exists(f['file_path']):
                continue
            al = con.execute('SELECT id, name, path, artist_id FROM albums WHERE id=?', (f['album_id'],)).fetchone()
            if not al or not al['path'] or not os.path.isdir(al['path']):
                continue
            sin.append((al, f))
        if not sin:
            return out
        if respaldar:
            respaldo(db, registro)
        tablas = _tablas(con)
        por_album = {}
        for al, f in sin:
            por_album.setdefault(al['id'], (al, []))[1].append(f)
        for aid, (al, pistas) in por_album.items():
            ids = {p['id'] for p in pistas}
            ruta = al['path'].rstrip('/')
            # vivas: del mismo álbum, o de otro álbum en la misma carpeta (el reindexado puede haber creado otro ID)
            otras = [o for o in con.execute(
                'SELECT id, title, track_number, disc_number, file_path FROM tracks WHERE album_id=? OR album_id IN '
                '(SELECT id FROM albums WHERE path=? OR path=?)', (aid, ruta, ruta + '/')).fetchall()
                if o['id'] not in ids and o['file_path'] and os.path.exists(o['file_path'])]
            mapa = emparejar_con(pistas, otras)
            with con:
                movidas, quitadas = borrar_pistas_db(con, tablas, list(ids), mapa)
                quedan = con.execute('SELECT 1 FROM tracks WHERE album_id=? LIMIT 1', (aid,)).fetchone()
                if quedan:
                    _album_totales(con, aid)
                else:
                    destino = next((o for o in otras), None)
                    destino_id = con.execute('SELECT album_id FROM tracks WHERE id=?',
                                             (destino['id'],)).fetchone()[0] if destino else None
                    borrar_album_db(con, tablas, al, [], {}, destino_id)
                    out['albumes_borrados'] += 1
            out['borradas'] += len(ids)
            out['movidas'] += movidas
            out['quitadas'] += quitadas
            registro(f'Filas sin archivo: «{al["name"]}» (ID {aid}): {len(ids)} pista{"s" if len(ids) > 1 else ""} '
                     f'quitada{"s" if len(ids) > 1 else ""} de Orbyte · {len(mapa)} con su equivalente '
                     f'({movidas} referencias movidas)' + (f' · {quitadas} referencias sin equivalente quitadas'
                                                           if quitadas else '')
                     + ('' if quedan else ' · el álbum quedó vacío y se quitó'))
    finally:
        con.close()
    return out
