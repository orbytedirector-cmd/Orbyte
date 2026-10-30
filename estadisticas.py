"""Estadísticas de uso de la plataforma (solo administrador).

Módulo aislado, como behavior_engine.py: solo LECTURA sobre las tablas que ya existen
(listening_events, session_log, search_history, ai_playlist_requests, collab_*, favoritos…).
app.py solo registra las rutas (web y /api/v1 para las apps) y le pasa la conexión.

  general(conn, dias, lossy_sql)          toda la plataforma, con una fila por usuario
  usuario(conn, user_id, dias, lossy_sql) el detalle de un usuario

dias = 0 → todo el historial.

Sesión de escucha: desde que empieza la primera pista hasta que pasan 10 minutos sin reproducir
(cerrar la app corta la música, así que queda cubierto por la misma regla). Cada escucha llega al
terminar la pista con los segundos escuchados: empezó en created_at − segundos.
  duración  = reloj, desde el inicio de la primera pista hasta el fin de la última (pausas cortas incluidas)
  escuchado = suma de los segundos escuchados

Conexión: un uso real de la app con la sesión iniciada (session_log solo se registra con login).
Si sale y vuelve antes de 10 minutos es la misma conexión; cuenta solo si estuvo abierta al menos
2 minutos o hubo alguna interacción (reproducir, buscar, Orbitron, favoritos). Lo demás (abrirla por
error, volver un segundo desde la multitarea) queda solo como «apertura».
"""
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

PAUSA_SESION_MIN = 10
VUELTA_MIN = 10
CONEXION_MIN_SEG = 120
TOP = 10
DIAS_SEMANA = ['Dom', 'Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb']


def _desde(dias):
    if not dias:
        return '0000-00-00 00:00:00'
    return (datetime.now(timezone.utc) - timedelta(days=dias)).strftime('%Y-%m-%d %H:%M:%S')


def _tabla(conn, nombre):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nombre,)).fetchone() is not None


def _nombre_usuario(u):
    return (u['nickname'] or '').strip() or (u['email'] or '').split('@')[0] or f'Usuario {u["id"]}'


def _escuchas(conn, desde, lossy_sql, uid=None):
    """Escuchas del período con los datos de la pista (en hora local del servidor para los hábitos)."""
    genero = ('(SELECT m.genre_primary FROM track_meta m WHERE m.track_id = t.id)'
              if _tabla(conn, 'track_meta') else 'NULL')
    q = f'''
        SELECT le.user_id, le.track_id, le.duration_played_seconds AS seg, le.completed, le.created_at,
               datetime(le.created_at, 'localtime') AS local,
               t.title, t.duration, t.album_id, t.year AS anio_pista, t.is_dsd, t.is_mqa, t.sample_rate,
               ({lossy_sql}) AS lossy, al.name AS album, al.year AS anio_album, ar.id AS artist_id,
               ar.name AS artista, {genero} AS genero
          FROM listening_events le
          LEFT JOIN tracks t ON t.id = le.track_id
          LEFT JOIN albums al ON al.id = t.album_id
          LEFT JOIN artists ar ON ar.id = al.artist_id
         WHERE le.created_at >= ? {'AND le.user_id = ?' if uid else ''}
         ORDER BY le.user_id, le.created_at'''
    return [dict(r) for r in conn.execute(q, (desde, uid) if uid else (desde,))]


def _calidad(e):
    if e['is_dsd']:
        return 'DSD'
    if e['is_mqa']:
        return 'MQA'
    if e['lossy']:
        return 'LOSSY'
    if (e['sample_rate'] or 0) > 48000:
        return 'HR'
    return 'CD' if e['track_id'] and e['title'] is not None else 'Desconocida'


def _decada(e):
    for v in (e['anio_album'], e['anio_pista']):
        try:
            a = int(str(v)[:4])
            if 1900 <= a <= 2100:
                return f'{a // 10 * 10}s'
        except (TypeError, ValueError):
            continue
    return None


def _sesiones(eventos):
    """[{user_id, inicio, fin, seg (escuchado), pistas}] — de la primera pista hasta 10 min sin reproducir."""
    tramos = []
    for e in eventos:
        fin = _ts(e['created_at'])
        seg = max(0, e['seg'] or 0)
        tramos.append((e['user_id'], fin - timedelta(seconds=seg), fin, seg))
    tramos.sort()
    out, actual = [], None
    for uid, ini, fin, seg in tramos:
        if actual and actual['user_id'] == uid and ini - actual['fin'] <= timedelta(minutes=PAUSA_SESION_MIN):
            actual['fin'] = max(actual['fin'], fin)
            actual['seg'] += seg
            actual['pistas'] += 1
        else:
            actual = {'user_id': uid, 'inicio': ini, 'fin': fin, 'seg': seg, 'pistas': 1}
            out.append(actual)
    return out


def _resumen(eventos):
    ses = _sesiones(eventos)
    seg = sum(e['seg'] or 0 for e in eventos)
    con_dur = [e for e in eventos if e['seg']]
    completas = sum(1 for e in eventos if e['completed'])
    return {
        'escuchas': len(eventos), 'minutos': round(seg / 60), 'horas': round(seg / 3600, 1),
        'sesiones_escucha': len(ses),
        'sesion_prom_min': round(sum((s['fin'] - s['inicio']).total_seconds() for s in ses) / len(ses) / 60, 1)
        if ses else 0,
        'sesion_escuchada_min': round(sum(s['seg'] for s in ses) / len(ses) / 60, 1) if ses else 0,
        'pistas_por_sesion': round(sum(s['pistas'] for s in ses) / len(ses), 1) if ses else 0,
        'completadas_pct': round(100 * completas / len(eventos)) if eventos else 0,
        'saltadas_pct': round(100 * (len(eventos) - completas) / len(eventos)) if eventos else 0,
        'escucha_prom_seg': round(sum(e['seg'] for e in con_dur) / len(con_dur)) if con_dur else 0,
    }


def _top(eventos, clave, etiqueta, extra=None, n=TOP):
    cuenta, seg, datos = Counter(), Counter(), {}
    for e in eventos:
        k = e[clave]
        if k is None:
            continue
        cuenta[k] += 1
        seg[k] += e['seg'] or 0
        if k not in datos:
            datos[k] = {'id': k, 'nombre': e[etiqueta] or '—'}
            for x in extra or ():
                datos[k][x] = e[x]
    filas = [dict(datos[k], escuchas=c, minutos=round(seg[k] / 60)) for k, c in cuenta.items()]
    filas.sort(key=lambda f: (-f['escuchas'], -f['minutos']))
    return filas[:n]


def _reparto(eventos, fn):
    """«Power Metal» y «Power metal» cuentan juntos (se muestra la grafía más usada)."""
    cuenta, seg, grafias = Counter(), Counter(), defaultdict(Counter)
    for e in eventos:
        v = fn(e)
        if v:
            k = str(v).strip().lower()
            cuenta[k] += 1
            seg[k] += e['seg'] or 0
            grafias[k][str(v).strip()] += 1
    total = sum(cuenta.values()) or 1
    return [{'nombre': grafias[k].most_common(1)[0][0], 'escuchas': c, 'minutos': round(seg[k] / 60),
             'pct': round(100 * c / total)} for k, c in cuenta.most_common()]


def _calor(eventos):
    """7×24 minutos escuchados por día de la semana y hora (hora local)."""
    m = [[0] * 24 for _ in range(7)]
    for e in eventos:
        t = datetime.strptime(e['local'][:19], '%Y-%m-%d %H:%M:%S')
        m[(t.weekday() + 1) % 7][t.hour] += max(1, round((e['seg'] or 0) / 60))
    return {'dias': DIAS_SEMANA, 'valores': m}


def _tendencia(eventos, conexiones, dias):
    por_dia = defaultdict(lambda: {'escuchas': 0, 'minutos': 0.0, 'usuarios': set(), 'conexiones': 0})
    for e in eventos:
        d = por_dia[e['local'][:10]]
        d['escuchas'] += 1
        d['minutos'] += (e['seg'] or 0) / 60
        d['usuarios'].add(e['user_id'])
    for c in conexiones:
        por_dia[c['local'][:10]]['conexiones'] += 1
        por_dia[c['local'][:10]]['usuarios'].add(c['user_id'])
    if not por_dia:
        return []
    fin = datetime.now().date()
    ini = fin - timedelta(days=(dias or 0) - 1) if dias else datetime.strptime(min(por_dia), '%Y-%m-%d').date()
    out, d = [], ini
    while d <= fin:
        k = d.isoformat()
        x = por_dia.get(k)
        out.append({'dia': k, 'escuchas': x['escuchas'] if x else 0, 'minutos': round(x['minutos']) if x else 0,
                    'usuarios': len(x['usuarios']) if x else 0, 'conexiones': x['conexiones'] if x else 0})
        d += timedelta(days=1)
    return out


def _interacciones(conn, desde, uid=None):
    """{user_id: [instantes ordenados]} de lo que demuestra uso: escuchas, búsquedas, Orbitron, favoritos."""
    fuentes = [('listening_events', 'created_at'), ('search_history', 'created_at'),
               ('ai_playlist_requests', 'created_at'), ('user_item_favorites', 'added_at')]
    out = defaultdict(list)
    for tabla, col in fuentes:
        if not _tabla(conn, tabla):
            continue
        try:
            for r in conn.execute(f'SELECT user_id, {col} FROM {tabla} WHERE {col} >= ? '
                                  f'{"AND user_id = ?" if uid else ""}', (desde, uid) if uid else (desde,)):
                if r[1]:
                    out[r[0]].append(_ts(r[1]))
        except Exception:
            continue
    for v in out.values():
        v.sort()
    return out


def _hay_interaccion(instantes, ini, fin):
    # una escucha se registra al terminar la pista: puede llegar unos minutos después de dejar la app
    i = bisect_left(instantes, ini)
    return i < len(instantes) and instantes[i] <= fin + timedelta(minutes=VUELTA_MIN)


def _conexiones(conn, desde, uid=None):
    """(conexiones, aperturas): aperturas = cada vez que la app pasó a primer plano (lo que llega de
    session_log); conexiones = aperturas unidas (vuelta antes de 10 min) que fueron un uso real."""
    filas = [dict(r) for r in conn.execute(
        f'''SELECT user_id, device, started_at, ended_at, datetime(started_at, 'localtime') AS local
              FROM session_log WHERE started_at >= ? {'AND user_id = ?' if uid else ''}
             ORDER BY user_id, device, started_at''', (desde, uid) if uid else (desde,))]
    inter = _interacciones(conn, desde, uid)
    unidas, actual = [], None
    for f in filas:
        ini = _ts(f['started_at'])
        fin = _ts(f['ended_at']) if f['ended_at'] else ini
        if fin < ini:
            fin = ini
        if actual and actual['user_id'] == f['user_id'] and actual['device'] == f['device'] \
                and ini - actual['fin'] <= timedelta(minutes=VUELTA_MIN):
            actual['fin'] = max(actual['fin'], fin)
            actual['aperturas'] += 1
            actual['sin_cierre'] = actual['sin_cierre'] or not f['ended_at']
        else:
            actual = {'user_id': f['user_id'], 'device': f['device'], 'local': f['local'], 'inicio': ini,
                      'fin': fin, 'aperturas': 1, 'sin_cierre': not f['ended_at']}
            unidas.append(actual)
    conexiones = []
    for c in unidas:
        instantes = inter.get(c['user_id'], [])
        if c['sin_cierre']:
            # la app se cerró sin avisar (iOS la mata en segundo plano): hasta la última interacción de la hora
            i = bisect_left(instantes, c['inicio'])
            while i < len(instantes) and instantes[i] <= c['inicio'] + timedelta(hours=1):
                c['fin'] = max(c['fin'], instantes[i])
                i += 1
        c['seg'] = (c['fin'] - c['inicio']).total_seconds()
        if c['seg'] >= CONEXION_MIN_SEG or _hay_interaccion(instantes, c['inicio'], c['fin']):
            conexiones.append(c)
    return conexiones, len(filas)


def _dispositivos(conexiones):
    cuenta, usuarios, seg = Counter(), defaultdict(set), Counter()
    for c in conexiones:
        k = c['device'] or 'Desconocido'
        cuenta[k] += 1
        usuarios[k].add(c['user_id'])
        seg[k] += c['seg'] or 0
    return [{'nombre': k, 'conexiones': n, 'usuarios': len(usuarios[k]),
             'abierta_prom_min': round(seg[k] / n / 60, 1) if n else 0} for k, n in cuenta.most_common()]


def _ts(s):
    return datetime.strptime(str(s)[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')


def _busquedas(conn, desde, uid=None):
    if not _tabla(conn, 'search_history'):
        return {'total': 0, 'top': [], 'sin_resultado': []}
    todas = conn.execute(f'''SELECT user_id, created_at, LOWER(TRIM(query_text)) AS q, result_count FROM search_history
                              WHERE created_at >= ? {'AND user_id = ?' if uid else ''} ORDER BY user_id, created_at''',
                         (desde, uid) if uid else (desde,)).fetchall()
    # la búsqueda en vivo registra lo que se va escribiendo («stra», «strato», «stratovarius»): una consulta que
    # el mismo usuario siguió escribiendo en menos de 2 minutos no cuenta, solo la última
    filas = []
    for i, r in enumerate(todas):
        sig = todas[i + 1] if i + 1 < len(todas) else None
        if sig and sig['user_id'] == r['user_id'] and r['q'] and sig['q'] and sig['q'] != r['q'] \
                and (sig['q'].startswith(r['q']) or r['q'].startswith(sig['q'])) \
                and (_ts(sig['created_at']) - _ts(r['created_at'])) <= timedelta(minutes=2):
            continue
        filas.append(r)
    top = Counter(r['q'] for r in filas if r['q'])
    nada = Counter(r['q'] for r in filas if r['q'] and r['result_count'] == 0)
    return {'total': len(filas), 'top': [{'q': q, 'n': n} for q, n in top.most_common(TOP)],
            'sin_resultado': [{'q': q, 'n': n} for q, n in nada.most_common(15)]}


def _ia(conn, desde, uid=None):
    if not _tabla(conn, 'ai_playlist_requests'):
        return {'pedidos': 0}
    r = conn.execute(f'''SELECT COUNT(*) AS n, AVG(rating) AS rating, SUM(CASE WHEN used_fallback THEN 1 ELSE 0 END) AS fb,
                                SUM(CASE WHEN saved_playlist_id IS NOT NULL THEN 1 ELSE 0 END) AS guardadas,
                                SUM(CASE WHEN rating IS NOT NULL THEN 1 ELSE 0 END) AS calificadas
                           FROM ai_playlist_requests WHERE created_at >= ? {'AND user_id = ?' if uid else ''}''',
                     (desde, uid) if uid else (desde,)).fetchone()
    n = r['n'] or 0
    return {'pedidos': n, 'rating_prom': round(r['rating'], 1) if r['rating'] is not None else None,
            'calificadas': r['calificadas'] or 0, 'guardadas': r['guardadas'] or 0,
            'respaldo_pct': round(100 * (r['fb'] or 0) / n) if n else 0}


def _contar(conn, tabla, col, desde, uid_col=None, uid=None):
    if not _tabla(conn, tabla):
        return 0
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {tabla} WHERE {col} >= ? {f"AND {uid_col} = ?" if uid else ""}',
                            (desde, uid) if uid else (desde,)).fetchone()[0]
    except Exception:
        return 0


def _salud(conn, desde, usuarios):
    """Avisos sobre la calidad de los datos: dispositivos que se conectan pero no informan escuchas, etc."""
    avisos = []
    for u in usuarios:
        ult_con = conn.execute('SELECT MAX(started_at), device FROM session_log WHERE user_id=?', (u['id'],)).fetchone()
        ult_esc = conn.execute('SELECT MAX(created_at) FROM listening_events WHERE user_id=?', (u['id'],)).fetchone()[0]
        if ult_con[0] and (not ult_esc or ult_con[0][:10] > ult_esc[:10]):
            dif = (datetime.strptime(ult_con[0][:10], '%Y-%m-%d') -
                   datetime.strptime((ult_esc or '2000-01-01')[:10], '%Y-%m-%d')).days
            if dif >= 3:
                esc_txt = f'su última escucha registrada es del {ult_esc[:10]}' if ult_esc else 'no tiene escuchas registradas'
                avisos.append({'nivel': 'aviso', 'texto': f'{u["nombre"]}: se conectó por última vez el {ult_con[0][:10]} '
                                                         f'({ult_con[1] or "?"}) pero {esc_txt}. Si escuchó desde un equipo '
                                                         f'que no informa escuchas (o desde la TV), no aparece aquí.'})
        r = conn.execute('SELECT COUNT(*), SUM(duration_played_seconds = 0) FROM listening_events '
                         'WHERE user_id=? AND created_at >= ?', (u['id'], desde)).fetchone()
        if r[0] >= 10 and (r[1] or 0) / r[0] >= 0.25:
            avisos.append({'nivel': 'aviso', 'texto': f'{u["nombre"]}: {round(100 * r[1] / r[0])} % de sus escuchas llegan '
                                                     f'con 0 segundos: sus minutos y pistas completas están subestimados. '
                                                     f'La app Android anterior al 30-10-2026 registraba mal el tiempo '
                                                     f'escuchado (corregido en la versión nueva).'})
    return avisos


def _usuarios(conn):
    return [dict(r, nombre=_nombre_usuario(r)) for r in conn.execute(
        'SELECT id, email, nickname, avatar, is_admin, is_approved, created_at, last_seen, last_device FROM users')]


def general(conn, dias, lossy_sql):
    desde = _desde(dias)
    eventos = _escuchas(conn, desde, lossy_sql)
    conexiones, aperturas = _conexiones(conn, desde)
    usuarios = _usuarios(conn)
    activos = {e['user_id'] for e in eventos} | {c['user_id'] for c in conexiones}

    por_usuario = defaultdict(list)
    for e in eventos:
        por_usuario[e['user_id']].append(e)
    con_por_usuario = defaultdict(list)
    for c in conexiones:
        con_por_usuario[c['user_id']].append(c)
    busq = Counter(r[0] for r in conn.execute('SELECT user_id FROM search_history WHERE created_at >= ?', (desde,))) \
        if _tabla(conn, 'search_history') else Counter()
    ia = Counter(r[0] for r in conn.execute('SELECT user_id FROM ai_playlist_requests WHERE created_at >= ?', (desde,))) \
        if _tabla(conn, 'ai_playlist_requests') else Counter()
    filas = []
    for u in usuarios:
        ev = por_usuario.get(u['id'], [])
        res = _resumen(ev)
        top_art = _top(ev, 'artist_id', 'artista', n=1)
        cons = con_por_usuario.get(u['id'], [])
        disp = Counter(c['device'] or '?' for c in cons).most_common(1)
        filas.append({'id': u['id'], 'nombre': u['nombre'], 'email': u['email'], 'admin': bool(u['is_admin']),
                      'aprobado': bool(u['is_approved']), 'ultimo_acceso': u['last_seen'],
                      'dispositivo': disp[0][0] if disp else u['last_device'], 'conexiones': len(cons),
                      'top_artista': top_art[0]['nombre'] if top_art else None,
                      'busquedas': busq.get(u['id'], 0), 'pedidos_ia': ia.get(u['id'], 0), **res})
    filas.sort(key=lambda f: (-f['minutos'], -f['escuchas'], -f['conexiones']))

    dias_activos = defaultdict(set)
    for e in eventos:
        dias_activos[e['local'][:10]].add(e['user_id'])
    for c in conexiones:
        dias_activos[c['local'][:10]].add(c['user_id'])

    resumen = dict(_resumen(eventos), usuarios_total=len(usuarios), usuarios_activos=len(activos),
                   activos_dia_prom=round(sum(len(v) for v in dias_activos.values()) / len(dias_activos), 1)
                   if dias_activos else 0,
                   conexiones=len(conexiones), aperturas=aperturas,
                   abierta_prom_min=round(sum(c['seg'] for c in conexiones) / len(conexiones) / 60, 1)
                   if conexiones else 0,
                   busquedas=busq and sum(busq.values()) or 0,
                   pedidos_ia=sum(ia.values()))
    return {
        'periodo': {'dias': dias, 'desde': desde[:10] if dias else None},
        'resumen': resumen,
        'tendencia': _tendencia(eventos, conexiones, dias),
        'usuarios': filas,
        'top': _tops(eventos),
        'habitos': {'calor': _calor(eventos), 'dispositivos': _dispositivos(conexiones)},
        'biblioteca': _biblioteca(conn, desde, dias),
        'funciones': {
            'busquedas': _busquedas(conn, desde), 'ia': _ia(conn, desde),
            'colaborativa': {'sesiones': _contar(conn, 'collab_sessions', 'created_at', desde),
                             'participantes': _contar(conn, 'collab_participants', 'joined_at', desde)},
            'favoritos': _contar(conn, 'user_item_favorites', 'added_at', desde),
            'social': {'toques': _contar(conn, 'social_toques', 'creada_at', desde),
                       'recomendaciones': _contar(conn, 'social_recomendaciones', 'creada_at', desde),
                       'likes': _contar(conn, 'social_likes', 'creada_at', desde)},
        },
        'salud': _salud(conn, desde, usuarios),
    }


def _tops(eventos):
    return {
        'artistas': _top(eventos, 'artist_id', 'artista'),
        'albumes': _top(eventos, 'album_id', 'album', extra=('artista', 'anio_album')),
        'pistas': _top(eventos, 'track_id', 'title', extra=('artista', 'album')),
        'generos': _reparto(eventos, lambda e: e['genero'])[:TOP],
        'calidad': _reparto(eventos, _calidad),
        'decadas': sorted(_reparto(eventos, _decada), key=lambda d: d['nombre']),
    }


def _biblioteca(conn, desde, dias):
    total_al = conn.execute('SELECT COUNT(*) FROM albums').fetchone()[0]
    total_tr = conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]
    esc_tr = conn.execute('SELECT COUNT(DISTINCT track_id) FROM listening_events').fetchone()[0]
    ventana = desde if dias else _desde(30)
    esc_por_album = dict(conn.execute('SELECT t.album_id, COUNT(*) FROM listening_events le '
                                      'JOIN tracks t ON t.id = le.track_id GROUP BY t.album_id').fetchall())
    recientes = [dict(r, escuchas=esc_por_album.get(r['id'], 0)) for r in conn.execute(
        '''SELECT al.id, al.name AS nombre, ar.name AS artista, al.created_at AS agregado
             FROM albums al LEFT JOIN artists ar ON ar.id = al.artist_id
            WHERE al.created_at >= ? ORDER BY al.created_at DESC''', (ventana,))]
    esc_al = len(esc_por_album)
    return {'albumes_total': total_al, 'albumes_escuchados': esc_al,
            'albumes_pct': round(100 * esc_al / total_al, 1) if total_al else 0,
            'pistas_total': total_tr, 'pistas_escuchadas': esc_tr,
            'pistas_pct': round(100 * esc_tr / total_tr, 1) if total_tr else 0,
            'recientes_dias': dias or 30, 'recientes_total': len(recientes),
            'recientes_sin_escuchar': sum(1 for r in recientes if not r['escuchas']),
            'recientes': sorted(recientes, key=lambda r: -r['escuchas'])[:TOP]}


def usuario(conn, user_id, dias, lossy_sql):
    u = next((x for x in _usuarios(conn) if x['id'] == user_id), None)
    if not u:
        return None
    desde = _desde(dias)
    eventos = _escuchas(conn, desde, lossy_sql, user_id)
    conexiones, aperturas = _conexiones(conn, desde, user_id)
    historial = [{'fecha': e['local'], 'pista': e['title'], 'artista': e['artista'], 'album': e['album'],
                  'segundos': e['seg'], 'duracion': round(e['duration'] or 0), 'completa': bool(e['completed']),
                  'track_id': e['track_id'], 'album_id': e['album_id']} for e in reversed(eventos[-60:])]
    return {
        'periodo': {'dias': dias, 'desde': desde[:10] if dias else None},
        'usuario': {'id': u['id'], 'nombre': u['nombre'], 'email': u['email'], 'admin': bool(u['is_admin']),
                    'creado': u['created_at'], 'ultimo_acceso': u['last_seen'], 'dispositivo': u['last_device']},
        'resumen': dict(_resumen(eventos), conexiones=len(conexiones), aperturas=aperturas,
                        abierta_prom_min=round(sum(c['seg'] or 0 for c in conexiones) / len(conexiones) / 60, 1)
                        if conexiones else 0),
        'tendencia': _tendencia(eventos, conexiones, dias),
        'top': _tops(eventos),
        'habitos': {'calor': _calor(eventos), 'dispositivos': _dispositivos(conexiones)},
        'funciones': {'busquedas': _busquedas(conn, desde, user_id), 'ia': _ia(conn, desde, user_id),
                      'favoritos': _contar(conn, 'user_item_favorites', 'added_at', desde, 'user_id', user_id)},
        'historial': historial,
        'salud': _salud(conn, desde, [u]),
    }
