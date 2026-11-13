"""
Orbyte Ingesta — eliminar pistas o un álbum de la biblioteca (pedido de Niko, 13-11-2026).

Los archivos van a la papelera (papelera.py); aquí se saca lo eliminado de music.db, igual que con las filas sin
archivo: lo que apuntaba a una pista eliminada pasa a su equivalente del mismo álbum (mismo disco y número con el
mismo título, o el mismo título: el caso de dos copias del disco en distinta calidad). Lo que no tiene equivalente
se quita, pero se guarda (`extra`) para devolverlo si se restaura; el historial de escucha se conserva siempre.
"""
import os
import sqlite3

from .artistas import respaldo
from .fantasmas import REFS_PISTA, _album_totales, _tablas, borrar_album_db, borrar_pistas_db, emparejar_con

# filas que se quitan con la pista (el historial no: se conserva con el id viejo y se le devuelve al restaurar)
GUARDAR = [(t, c, e) for t, c, e in REFS_PISTA if t != 'listening_events'] + [
    ('user_item_favorites', 'item_id', "AND item_type='track'")]


def _con(db, uri=False):
    con = sqlite3.connect(db, timeout=60, uri=uri)
    con.row_factory = sqlite3.Row
    return con


def pistas_de(db, rutas):
    """Filas de music.db de esos archivos → [{id, title, track_number, disc_number, file_path, album_id}]"""
    rutas = list(rutas)
    out = []
    con = _con(f'file:{db}?mode=ro', uri=True)
    try:
        for k in range(0, len(rutas), 500):
            parte = rutas[k:k + 500]
            out += [dict(r) for r in con.execute(
                'SELECT id, title, track_number, disc_number, file_path, album_id FROM tracks '
                f'WHERE file_path IN ({",".join("?" * len(parte))})', parte)]
    finally:
        con.close()
    return out


def _guardar_filas(con, tablas, pids):
    filas = {}
    for tabla, col, extra in GUARDAR:
        if tabla not in tablas:
            continue
        for pid in pids:
            for r in con.execute(f'SELECT * FROM {tabla} WHERE {col}=? {extra}', (pid,)):
                filas.setdefault(tabla, []).append(dict(r))
    return filas


def quitar_pistas(db, pistas, registro):
    """Saca `pistas` (de pistas_de, cuyos archivos ya están en la papelera) de music.db. → extra para restaurar."""
    if not pistas:
        return {'pistas': [], 'filas': {}}
    respaldo(db, registro)
    con = _con(db)
    try:
        tablas = _tablas(con)
        extra = {'pistas': [{'id': p['id'], 'ruta': p['file_path']} for p in pistas], 'filas': {}, 'albumes': [],
                 'movidas': {}}
        por_album = {}
        for p in pistas:
            por_album.setdefault(p['album_id'], []).append(p)
        for aid, lista in por_album.items():
            ids = {p['id'] for p in lista}
            otras = [dict(o) for o in con.execute(
                'SELECT id, title, track_number, disc_number, file_path FROM tracks WHERE album_id=?', (aid,))
                if o['id'] not in ids and o['file_path'] and os.path.exists(o['file_path'])]
            mapa = emparejar_con(lista, otras)
            sin = [i for i in ids if i not in mapa]
            for t, fs in _guardar_filas(con, tablas, sin).items():
                extra['filas'].setdefault(t, []).extend(fs)
            extra['movidas'].update({str(k): v for k, v in mapa.items()})
            al = con.execute('SELECT id, name, path, artist_id FROM albums WHERE id=?', (aid,)).fetchone()
            with con:
                movidas, quitadas = borrar_pistas_db(con, tablas, list(ids), mapa)
                if con.execute('SELECT 1 FROM tracks WHERE album_id=? LIMIT 1', (aid,)).fetchone():
                    _album_totales(con, aid)
                    vacio = False
                elif al:
                    extra['albumes'].append(_guardar_album(con, al))
                    borrar_album_db(con, tablas, al, [], {}, None)
                    vacio = True
            registro(f'Eliminar: «{al["name"] if al else aid}»: {len(ids)} pista{"s" if len(ids) != 1 else ""} '
                     f'fuera de Orbyte (a la papelera) · {len(mapa)} con su equivalente ({movidas} referencias '
                     f'movidas)' + (f' · {quitadas} referencias sin equivalente guardadas para restaurar'
                                    if quitadas else '') + (' · el álbum quedó vacío y se quitó' if vacio else ''))
        return extra
    finally:
        con.close()


def _guardar_album(con, al):
    return {'ruta': (al['path'] or '').rstrip('/'), 'favoritos': [dict(r) for r in con.execute(
        "SELECT * FROM user_item_favorites WHERE item_type='album' AND item_id=?", (al['id'],))]}


def quitar_album(db, oid, registro):
    """Saca el álbum `oid` (su carpeta ya está en la papelera) de music.db. → extra para restaurar."""
    respaldo(db, registro)
    con = _con(db)
    try:
        tablas = _tablas(con)
        al = con.execute('SELECT id, name, path, artist_id FROM albums WHERE id=?', (oid,)).fetchone()
        if not al:
            return {'pistas': [], 'filas': {}, 'albumes': []}
        pistas = [dict(r) for r in con.execute('SELECT id, file_path FROM tracks WHERE album_id=?', (oid,))]
        pids = [p['id'] for p in pistas]
        extra = {'pistas': [{'id': p['id'], 'ruta': p['file_path']} for p in pistas],
                 'filas': _guardar_filas(con, tablas, pids), 'albumes': [_guardar_album(con, al)]}
        with con:
            borrar_album_db(con, tablas, al, pids, {}, None)
        registro(f'Eliminar: álbum «{al["name"]}» (ID {oid}, {len(pids)} pistas) fuera de Orbyte; su carpeta '
                 f'quedó en la papelera')
        return extra
    finally:
        con.close()


def reponer(db, extra, registro):
    """Tras restaurar y reindexar: devuelve el historial y las filas guardadas a las pistas nuevas (mismo archivo).
    → True si ya se pudo (o no había nada que devolver), False si las pistas todavía no están en music.db."""
    pistas = extra.get('pistas') or []
    if not pistas and not extra.get('albumes'):
        return True
    con = _con(db)
    try:
        tablas = _tablas(con)
        nuevo = {}
        for p in pistas:
            r = con.execute('SELECT id FROM tracks WHERE file_path=?', (p['ruta'],)).fetchone()
            if r:
                nuevo[p['id']] = r['id']
        albumes = {}
        for a in extra.get('albumes') or []:
            r = con.execute('SELECT id FROM albums WHERE path=? OR path=?', (a['ruta'], a['ruta'] + '/')).fetchone()
            if r:
                albumes[a['ruta']] = r['id']
        if len(nuevo) < len(pistas) or len(albumes) < len(extra.get('albumes') or []):
            return False
        movidas = {int(k) for k in (extra.get('movidas') or {})}
        n = 0
        with con:
            if 'listening_events' in tablas:
                for viejo, nv in nuevo.items():
                    if viejo not in movidas and viejo != nv:
                        n += con.execute('UPDATE listening_events SET track_id=? WHERE track_id=?', (nv, viejo)).rowcount
            for tabla, filas in (extra.get('filas') or {}).items():
                col = next((c for t, c, _e in GUARDAR if t == tabla), None)
                if tabla not in tablas or not col:
                    continue
                for f in filas:
                    nv = nuevo.get(f.get(col))
                    if not nv:
                        continue
                    f = {k: v for k, v in f.items() if k != 'id'}
                    f[col] = nv
                    con.execute(f'INSERT OR IGNORE INTO {tabla} ({",".join(f)}) VALUES ({",".join("?" * len(f))})',
                                list(f.values()))
                    n += 1
            for a in extra.get('albumes') or []:
                for f in a.get('favoritos') or []:
                    f = {k: v for k, v in f.items() if k != 'id'}
                    f['item_id'] = albumes[a['ruta']]
                    con.execute(f'INSERT OR IGNORE INTO user_item_favorites ({",".join(f)}) '
                                f'VALUES ({",".join("?" * len(f))})', list(f.values()))
                    n += 1
        registro(f'Restaurado «{extra.get("titulo") or "?"}»: {n} escuchas, favoritos y entradas de playlist devueltas')
        return True
    finally:
        con.close()
