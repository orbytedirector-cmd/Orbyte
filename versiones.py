"""Versiones de un álbum: una sola carta en la discografía.

Agrupa los álbumes del mismo artista que son la misma obra en otra calidad o
edición («Master of Puppets», «Master of Puppets (Remastered)»,
«Master of Puppets (Deluxe Box Set / Remastered)», la copia DSD…).

· Clave del grupo = artista + título sin los sufijos de edición/calidad.
  Solo se quitan paréntesis/corchetes FINALES (o un « - … » final) que traen
  una palabra de edición (Remaster, Deluxe, Edition, Anniversary…), de calidad
  (CD, HR, 24/96, DSD…) o solo un año. «(What's the Story) Morning Glory?»,
  «Part 1», «Vol. 2» o «(Live)» no se tocan: siguen siendo álbumes distintos.
· Edición de cada versión: lo que dice el título; si no dice nada, el año de la
  metadata separa reediciones («Edición 2014») de la original (el año menor).
· Edición original: la que marcó el administrador (se guarda por ruta, que
  sobrevive al reindexado) o, si no, la que no trae sufijo / la más antigua.
· La carta del frente usa la portada y el nombre (sin sufijos) de la edición
  original; ▶ reproduce su versión de mejor calidad.

El mapa completo se cachea en memoria y se rehace solo cuando cambia la tabla
albums (firma barata) o una marca de «original».
"""
import os
import re
import threading
import unicodedata

# Palabras que hacen de un paréntesis final un sufijo de EDICIÓN.
_EDICION_RE = re.compile(
    r'\b(re-?master(?:ed|izado)?|remaster|deluxe|expanded|anniversary|aniversario|'
    r'edition|edici[oó]n|reissue|re-?edici[oó]n|bonus|legacy|collector\'?s?|special|'
    r'limited|box ?set|version|versi[oó]n|mono|stereo|est[eé]reo|mfsl|mobile fidelity|'
    r'original master|japan(?:ese)?|japon[eé]s|import|digipak|remix(?:ed)?|'
    r'definitive|complete|edici[oó]n especial)\b', re.I)
# «CD 2», «Disc 1», «Disco 3»: un disco suelto de una obra, no una versión.
_DISCO_RE = re.compile(r'\b(?:cd|disc|disco|disk)\s*\d{1,3}\b', re.I)
# Carpeta «CD 01», «Disc 2»…: disco de una caja que aún no se unió (ver Salud).
_CARPETA_DISCO_RE = re.compile(r'^\s*(?:cd|disc|disco|disk)\s*[-_.]?\s*\d{1,3}\s*$', re.I)
# Sufijos de CALIDAD o de año: se quitan sin contar como edición.
_CALIDAD_RE = re.compile(
    r'^\s*(?:cd|hr|hi-?res(?:olution)?|hd|sacd|dsd\s*\d*|mqa|flac|wav|alac|mp3|lossy|'
    r'\d{2}\s*-?\s*bits?|\d{2}\s*bit\s*[/-]?\s*\d{2,3}(?:\.\d)?\s*(?:khz)?|'
    r'\d{2}\s*[/-]\s*\d{2,3}(?:\.\d)?\s*(?:khz)?|\d{2,3}(?:\.\d)?\s*khz|'
    r'(?:cd|hr)\s*-?\s*\d{2}\s*[/-]\s*\d{2,3}(?:\.\d)?|'
    r'(?:19|20)\d{2})\s*$', re.I)
_FINAL_RE = re.compile(r'\s*(?:\(([^()]*)\)|\[([^\[\]]*)\])\s*$')
_GUION_RE = re.compile(r'\s+[-–—]\s+([^-–—]+)$')


def _partes(nombre):
    """('Master of Puppets', ['Remastered']) — título base y sufijos de edición."""
    base = (nombre or '').strip()
    ediciones = []
    for _ in range(6):
        m = _FINAL_RE.search(base)
        if m:
            dentro = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        else:
            m = _GUION_RE.search(base)
            if not m:
                break
            dentro = m.group(1).strip()
        if _CALIDAD_RE.match(dentro):
            pass
        elif _EDICION_RE.search(dentro):
            # «Deluxe Box Set / Remastered» → dos etiquetas
            ediciones.insert(0, ' / '.join(p.strip() for p in dentro.split('/') if p.strip()))
        else:
            break
        nuevo = base[:m.start()].rstrip(' -–—')
        if not nuevo:
            break
        base = nuevo
    return base.strip(), ediciones


def _plano(s):
    s = unicodedata.normalize('NFKD', s or '')
    s = ''.join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace('&', ' and ')
    s = re.sub(r'[^\w]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def titulo_base(nombre):
    return _partes(nombre)[0] or (nombre or '')


def clave(artist_id, nombre):
    return f'{artist_id}|{_plano(titulo_base(nombre))}'


def _clave_edicion(etiqueta):
    e = _plano(etiqueta)
    e = re.sub(r'\bre ?master(?:ed|izado)?\b', 'remaster', e)
    e = re.sub(r'\b(?:19|20)\d{2}\b', '', e)
    return re.sub(r'\s+', ' ', e).strip()


def _anio(a):
    m = re.search(r'(1[89]|20)\d{2}', str(a.get('year') or ''))
    return int(m.group(0)) if m else None


# ── Tabla de marcas («Marcar como original») ────────────────────────────────

def asegurar_tabla(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS album_version_original (
                        path TEXT PRIMARY KEY, clave TEXT,
                        marcado_por INTEGER, marcado_en TEXT DEFAULT CURRENT_TIMESTAMP)''')


def marcar_original(conn, album_id, user_id=None):
    """Marca la edición de este álbum como la original de su grupo (por ruta)."""
    k, miembros = grupo_de(conn, album_id)
    a = conn.execute('SELECT id, path FROM albums WHERE id=?', (album_id,)).fetchone()
    if not k or not a:
        return False
    asegurar_tabla(conn)
    rutas = [m['path'] for m in miembros]
    conn.execute(f'DELETE FROM album_version_original WHERE path IN ({",".join("?" * len(rutas))})', rutas)
    conn.execute('INSERT OR REPLACE INTO album_version_original (path, clave, marcado_por) VALUES (?,?,?)',
                 (a['path'], k, user_id))
    conn.commit()
    _cache['firma'] = None
    return True


# ── Mapa en memoria ─────────────────────────────────────────────────────────
_lock = threading.Lock()
_cache = {'firma': None, 'grupos': {}, 'de': {}}


def _firma(conn):
    f = conn.execute('SELECT COUNT(*), MAX(id), TOTAL(LENGTH(name)), TOTAL(artist_id), '
                     'TOTAL(LENGTH(COALESCE(year,""))) FROM albums').fetchone()
    try:
        m = conn.execute('SELECT COUNT(*), GROUP_CONCAT(path || COALESCE(marcado_en, \'\')) '
                         'FROM album_version_original').fetchone()
        m = (m[0], hash(m[1]))
    except Exception:
        m = (0, 0)
    return tuple(f) + m


def _ediciones(miembros, marcada_path):
    """Asigna a cada miembro su edición; devuelve la clave de la edición original."""
    for a in miembros:
        etiquetas = _partes(a['name'])[1]
        a['_fuente'] = 'titulo' if etiquetas else None
        if not etiquetas:
            # El título no dice nada: probar con el nombre de la carpeta
            # («Temple Of Shadows (Japanese Edition)»).
            carpeta = re.sub(r'^\s*(?:19|20)\d{2}\s*[-–]\s*', '', os.path.basename((a.get('path') or '').rstrip('/')))
            etiquetas = _partes(carpeta)[1]
            a['_fuente'] = 'carpeta' if etiquetas else None
        a['_etiqueta'] = ' · '.join(etiquetas)
        a['_ed'] = _clave_edicion(a['_etiqueta'])
    # Sin sufijo: el año separa la original de las reediciones.
    sin = [a for a in miembros if not a['_ed']]
    anios = sorted({_anio(a) for a in sin if _anio(a)})
    if len(anios) > 1:
        for a in sin:
            y = _anio(a)
            if y and y != anios[0]:
                a['_ed'] = f'reedicion {y}'
                a['_etiqueta'] = f'Edición {y}'
                a['_fuente'] = 'anio'
    for a in sin:
        if not a['_ed']:
            a['_etiqueta'] = 'Original'
            a['_fuente'] = 'sin_sufijo'
    marcada = next((a for a in miembros if marcada_path and a['path'] == marcada_path), None)
    if marcada:
        return marcada['_ed']
    if any(not a['_ed'] for a in miembros):
        return ''
    # Ninguna sin sufijo: la edición más antigua.
    return min(miembros, key=lambda a: (_anio(a) or 9999, a['id']))['_ed']


def _comparten_pistas(a, b):
    """Dos álbumes con el mismo título son la misma obra si comparten al menos la
    mitad de las pistas del más corto (los discos separados de una obra o los
    álbumes homónimos —«Peter Gabriel» 1, 2, 3…— no las comparten)."""
    if not a or not b:
        return not a and not b
    return len(a & b) * 2 >= min(len(a), len(b))


def _subgrupos(miembros, titulos):
    padre = {m['id']: m['id'] for m in miembros}

    def raiz(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x
    for i, a in enumerate(miembros):
        for b in miembros[i + 1:]:
            if raiz(a['id']) != raiz(b['id']) and _comparten_pistas(titulos.get(a['id']), titulos.get(b['id'])):
                padre[raiz(a['id'])] = raiz(b['id'])
    out = {}
    for m in miembros:
        out.setdefault(raiz(m['id']), []).append(m)
    return [g for g in out.values() if len(g) > 1]


def _construir(conn):
    filas = conn.execute('SELECT id, artist_id, name, year, path, cover_path, track_count, '
                         'total_duration FROM albums').fetchall()
    por_clave = {}
    for f in filas:
        if f['artist_id'] is None or not (f['name'] or '').strip() or _DISCO_RE.search(f['name']) \
                or _CARPETA_DISCO_RE.match(os.path.basename((f['path'] or '').rstrip('/'))):
            continue
        por_clave.setdefault(clave(f['artist_id'], f['name']), []).append(dict(f))
    por_clave = {k: v for k, v in por_clave.items() if len(v) > 1}
    ids = [m['id'] for v in por_clave.values() for m in v]
    titulos = {}
    for i in range(0, len(ids), 500):
        lote = ids[i:i + 500]
        for r in conn.execute(f'SELECT album_id, title FROM tracks WHERE album_id IN ({",".join("?" * len(lote))})',
                              lote):
            t = _titulo_norm(r['title'])
            if t:
                titulos.setdefault(r['album_id'], set()).add(t)
    try:
        marcadas = {r[0] for r in conn.execute('SELECT path FROM album_version_original')}
    except Exception:
        marcadas = set()
    grupos, de = {}, {}
    for k, candidatos in por_clave.items():
        for miembros in _subgrupos(candidatos, titulos):
            gk = f'{k}#{min(m["id"] for m in miembros)}'
            marcada = next((m['path'] for m in miembros if m['path'] in marcadas), None)
            original = _ediciones(miembros, marcada)
            for a in miembros:
                a['_original'] = a['_ed'] == original
            grupos[gk] = miembros
            for a in miembros:
                de[a['id']] = gk
    return grupos, de


def _mapa(conn):
    try:
        firma = _firma(conn)
    except Exception:
        return {}, {}
    with _lock:
        if _cache['firma'] != firma:
            _cache['grupos'], _cache['de'] = _construir(conn)
            _cache['firma'] = firma
        return _cache['grupos'], _cache['de']


def grupo_de(conn, album_id):
    grupos, de = _mapa(conn)
    k = de.get(int(album_id))
    return (k, grupos[k]) if k else (None, None)


# ── Calidad: orden de «mejor» ───────────────────────────────────────────────

def _orden_calidad(conn, ids, calidades, rango):
    """{id: tupla comparable} — color del LED, luego bits/frecuencia, luego pistas."""
    ids = list(ids)
    if not ids:
        return {}
    marcas = ','.join('?' * len(ids))
    res = {i: (0, 0, 0, 0) for i in ids}
    for f in conn.execute(
            f'''SELECT album_id, MAX(COALESCE(bit_depth,0)) AS b,
                       MAX(COALESCE(NULLIF(sample_rate_real,0), sample_rate, 0)) AS sr, COUNT(*) AS n
                FROM tracks WHERE album_id IN ({marcas}) GROUP BY album_id''', ids).fetchall():
        q = (calidades.get(f['album_id']) or {}).get('calidad') or {}
        res[f['album_id']] = (rango.get((q.get('color') or 'yellow').lower(), 1),
                              0 if q.get('mixto') else 1, (f['b'] or 0) * 1_000_000 + (f['sr'] or 0), f['n'] or 0)
    return res


def _familia(q):
    t = (q.get('texto') or '').split(' ')[0]
    if t.upper().startswith('DSD'):
        return 'DSD'
    return t or 'CD'


def resumen_calidades(calidades, rango):
    """{'colores': [...], 'texto': 'DSD · HR · CD'} — las calidades que hay en el grupo."""
    vistos = {}
    for q in calidades:
        if not q:
            continue
        c = (q.get('color') or 'yellow').lower()
        vistos.setdefault(c, _familia(q))
    orden = sorted(vistos, key=lambda c: -rango.get(c, 1))
    textos = []
    for c in orden:
        if vistos[c] not in textos:
            textos.append(vistos[c])
    return {'colores': orden, 'texto': ' · '.join(textos)} if orden else None


def frente(miembros, orden):
    """La versión de la carta: la mejor calidad dentro de la edición original."""
    originales = [a for a in miembros if a['_original']] or miembros
    return max(originales, key=lambda a: (orden.get(a['id'], (0,)), -a['id']))


# ── Para las listas ─────────────────────────────────────────────────────────

def anotar(conn, albums, clave_id='id'):
    """Agrega `n_versiones` (≥1) y `version_grupo` (clave o None) a cada álbum."""
    try:
        grupos, de = _mapa(conn)
    except Exception:
        grupos, de = {}, {}
    for a in albums:
        try:
            k = de.get(int(a.get(clave_id)))
        except (TypeError, ValueError):
            k = None
        a['n_versiones'] = len(grupos[k]) if k else 1
        a['version_grupo'] = k
    return albums


def agrupar_artista(conn, albums, rango):
    """Vista de artista: marca en cada álbum `version_frente` (va como carta) y
    `titulo_base`. Los que no son frente se ocultan en los clientes nuevos (los
    viejos siguen viendo la lista completa). Espera `calidad` ya anotada."""
    anotar(conn, albums)
    por_k = {}
    for a in albums:
        if a.get('version_grupo'):
            por_k.setdefault(a['version_grupo'], []).append(a)
    calidades = {a['id']: a for a in albums}
    grupos, _ = _mapa(conn)
    for a in albums:
        a['version_frente'] = True
        a['titulo_base'] = a.get('name')
    for k, lista in por_k.items():
        miembros = [m for m in grupos.get(k, []) if m['id'] in calidades]
        if len(miembros) < 2:
            continue
        orden = _orden_calidad(conn, [m['id'] for m in miembros], calidades, rango)
        f = frente(miembros, orden)
        base = titulo_base(f['name'])
        for a in lista:
            a['version_frente'] = a['id'] == f['id']
            a['titulo_base'] = base
            a['n_versiones'] = len(miembros)
            # Las otras versiones (portadas para el abanico; máx. 2 detrás del frente).
            if a['version_frente']:
                otras = sorted((m for m in miembros if m['id'] != f['id']),
                               key=lambda m: orden.get(m['id'], (0,)), reverse=True)
                a['versiones_ids'] = [m['id'] for m in otras]
                a['versiones_covers'] = [m.get('cover_path') for m in otras[:2]]
                a['versiones_calidad'] = resumen_calidades(
                    [(calidades[m['id']].get('calidad') or {}) for m in miembros], rango)
    return albums


# ── Detalle para el modal (pestañas Calidad y Ediciones) ────────────────────

def _titulo_norm(t):
    return _plano(re.sub(r'\s*(?:[\(\[][^()\[\]]*(?:remaster|version|mono|stereo|mix)[^()\[\]]*[\)\]]'
                         r'|\s[-–]\s[^-–]*(?:remaster|version|mono|stereo|mix)[^-–]*)\s*$',
                         '', t or '', flags=re.I))


def _titulos(conn, album_id):
    return [_titulo_norm(r['title'])
            for r in conn.execute('SELECT title FROM tracks WHERE album_id=? ORDER BY '
                                  'CAST(disc_number AS INTEGER), CAST(track_number AS INTEGER)', (album_id,))]


def detalle(conn, album_id, calidad_map, rango, cover_url, fmt_seg):
    """Datos del modal. None si el álbum no tiene otras versiones."""
    k, miembros = grupo_de(conn, album_id)
    if not k:
        return None
    ids = [m['id'] for m in miembros]
    calidades = calidad_map(conn, ids)
    orden = _orden_calidad(conn, ids, calidades, rango)
    f = frente(miembros, orden)
    pub = {}
    marcas = ','.join('?' * len(ids))
    for r in conn.execute(f'''SELECT album_id, publisher FROM tracks WHERE album_id IN ({marcas})
                              AND publisher IS NOT NULL AND publisher != '' GROUP BY album_id''', ids):
        pub[r['album_id']] = r['publisher']
    fuentes = {'titulo': None, 'carpeta': 'Edición según el nombre de la carpeta',
               'anio': 'Edición según el año de la metadata',
               'sin_sufijo': 'Sin edición indicada en el título'}

    def version(m):
        q = calidades.get(m['id']) or {}
        etiqueta = m['_etiqueta']
        return {
            'id': m['id'], 'name': m['name'], 'year': m.get('year'),
            'edicion': etiqueta,
            'edicion_fuente': (f'Edición según el título «({etiqueta})»' if m['_fuente'] == 'titulo'
                               else fuentes.get(m['_fuente'])),
            'es_original': bool(m['_original']), 'es_frente': m['id'] == f['id'],
            'calidad': q.get('calidad'), 'n_discos': q.get('n_discos', 1),
            'track_count': m.get('track_count'),
            'duracion_fmt': fmt_seg(m.get('total_duration')),
            'cover_url': cover_url(m.get('cover_path')),
            'publisher': pub.get(m['id']),
        }

    por_calidad = sorted(miembros, key=lambda m: (orden.get(m['id'], (0,)), m['_original']), reverse=True)
    versiones = [version(m) for m in por_calidad]
    # Ediciones: cada una con sus calidades (mejor primero); la original va primero.
    eds = {}
    for v, m in zip(versiones, por_calidad):
        eds.setdefault(m['_ed'], []).append((v, m))
    ediciones = []
    for ed, lista in eds.items():
        v0, m0 = lista[0]
        anios = [y for y in (_anio(m) for _, m in lista) if y]
        ediciones.append({
            'clave': ed, 'etiqueta': v0['edicion'], 'year': min(anios) if anios else None,
            'es_original': v0['es_original'], 'cover_url': v0['cover_url'],
            'publisher': next((v['publisher'] for v, _ in lista if v['publisher']), None),
            'track_count': v0['track_count'], 'duracion_fmt': v0['duracion_fmt'],
            'edicion_fuente': v0['edicion_fuente'], 'album_id': v0['id'],
            'versiones': [{'id': v['id'], 'calidad': v['calidad']} for v, _ in lista],
        })
    ediciones.sort(key=lambda e: (not e['es_original'], e['year'] or 9999))
    # Comparar pistas entre ediciones (la mejor versión de cada una).
    titulos = {e['clave']: _titulos(conn, e['album_id']) for e in ediciones}
    base_ed = ediciones[0]['clave']
    base_set = set(titulos[base_ed])
    diferencias = []
    for e in ediciones[1:]:
        s = set(titulos[e['clave']])
        extra = [t for t in titulos[e['clave']] if t not in base_set]
        faltan = [t for t in titulos[base_ed] if t not in s]
        if extra or faltan:
            nombres = {_titulo_norm(r['title']): r['title'] for r in conn.execute(
                'SELECT title FROM tracks WHERE album_id IN (?,?)', (e['album_id'], ediciones[0]['album_id']))}
            diferencias.append({'edicion': e['etiqueta'], 'extra': [nombres.get(t, t) for t in extra][:40],
                                'faltan': [nombres.get(t, t) for t in faltan][:40]})
    if len(ediciones) < 2:
        resumen = None
    elif not diferencias:
        n = len(titulos[base_ed])
        resumen = ('Las dos ediciones tienen' if len(ediciones) == 2 else 'Todas las ediciones tienen') + \
            f' las mismas {n} pistas.'
    else:
        resumen = 'Las ediciones no tienen las mismas pistas.'
    return {
        'grupo': k, 'titulo': titulo_base(f['name']), 'artist_id': f['artist_id'],
        'year': f.get('year'), 'cover_url': cover_url(f.get('cover_path')),
        'frente_id': f['id'], 'n_versiones': len(versiones), 'n_ediciones': len(ediciones),
        'versiones': versiones, 'ediciones': ediciones,
        'pistas_resumen': resumen, 'pistas_diferencias': diferencias,
    }
