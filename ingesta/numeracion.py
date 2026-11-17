"""
Orbyte Ingesta — números de disco y de pista desde las fuentes, para ✦ Autocorregir (17-11-2026).

Pedido de Niko: «asignar automáticamente los números de disco y pistas utilizando las APIs disponibles, incluso
Discogs, que he estado ocupando manualmente». Fuentes, en orden de confianza (decide el calce; en empate, este orden):
  1. MusicBrainz: la edición identificada en la revisión y sus candidatas; si no hay, búsqueda por artista + álbum.
  2. Discogs: por código de barras, catálogo o artista + álbum (necesita DISCOGS_TOKEN en el .env), o el enlace /
     ID de un release o master que pegues (ese funciona sin token).
  3. Tidal: por código de barras o artista + álbum (si hay sesión de Tidal).

Cada archivo se empareja con una pista de la fuente por título y duración, no por el orden de los archivos, así
que sirve aunque estén desordenados o mal numerados. Solo consulta: no escribe ni mueve nada.
"""
import concurrent.futures
import difflib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import edicion as edicion_mod
from . import tienda as tienda_mod
from .fantasmas import _titulo
from .rescate import tolerancia

log = logging.getLogger('ingesta')

DISCOGS_URL = os.environ.get('INGESTA_DISCOGS_URL') or 'https://api.discogs.com'
CALCE_MINIMO = 0.8          # sin elegir a mano, se propone solo si calza al menos el 80 % de los archivos
MAX_MB = 3
MAX_DISCOGS = 4
MAX_TIDAL = 3
PRIORIDAD = {'mb': 0, 'discogs': 1, 'tidal': 2}
NOMBRE = {'mb': 'MusicBrainz', 'discogs': 'Discogs', 'tidal': 'Tidal'}
NO_AUDIO = re.compile(r'dvd|blu-?ray|\bbd\b|vhs|laserdisc', re.I)

_lock_dc = threading.Lock()
_ultima_dc = [0.0]


class FalloFuente(Exception):
    pass


# ─────────────────────────── utilidades ───────────────────────────

def _segundos(s):
    """«4:05» / «1:02:03» → segundos (Discogs); vacío → None."""
    partes = [p for p in re.split(r'[:.]', str(s or '').strip()) if p.isdigit()]
    if not partes or len(partes) > 3:
        return None
    total = 0
    for p in partes:
        total = total * 60 + int(p)
    return float(total) or None


def _simple(s):
    from .analisis import _norm
    return re.sub(r'[^a-z0-9]+', '', _norm(s or ''))


def _parecido(a, b):
    a, b = _simple(a), _simple(b)
    return bool(a and b and (a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6))


_REMASTER = re.compile(r'\s*(?:[-–—]\s*|[(\[]\s*)(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?'
                       r'(?:\s+(?:version|\d{4}))*\s*[)\]]?\s*$', re.I)


def limpiar_titulo(t):
    """«Song - Remastered 2015», «Song (2009 Remaster)» → «Song» (Tidal y Discogs a veces lo traen)."""
    t = (t or '').strip()
    return _REMASTER.sub('', t).strip() or t


def _generico(t):
    """Títulos que no dicen nada: vacío, «Track 05», «Pista 3», «05»."""
    return not _titulo(t) or bool(re.fullmatch(r'\s*(?:track|pista|piste|titel|tr)?[\s._-]*\d{1,3}\s*', t or '', re.I))


def album_base(alb):
    """«Today (Legacy Edition) [MQA]» → «Today»: las búsquedas encuentran más sin la edición entre paréntesis."""
    return re.sub(r'\s*[(\[][^)\]]*[)\]]', '', alb or '').strip() or (alb or '')


def _cand(fuente, cid, titulo, artista=None, anio=None, pais=None, formato=None, url=None, pistas=None, nota=None):
    return {'fuente': fuente, 'id': str(cid), 'clave': f'{fuente}:{cid}', 'nombre': NOMBRE[fuente],
            'titulo': titulo or '', 'artista': artista or '', 'anio': anio, 'pais': pais, 'formato': formato,
            'url': url, 'nota': nota, 'pistas': pistas or []}


def _renumerar_discos(pistas):
    """Discos de audio 1..n en el orden de la fuente (sin el DVD que traen algunas ediciones)."""
    orden = []
    for p in pistas:
        if p['disco'] not in orden:
            orden.append(p['disco'])
    mapa = {d: i + 1 for i, d in enumerate(orden)}
    for p in pistas:
        p['disco'] = mapa[p['disco']]
    return pistas


# ─────────────────────────── MusicBrainz ───────────────────────────

def pistas_mb(rel):
    """Release de MusicBrainz (inc=recordings+media) → pistas de los medios de audio."""
    out = []
    for k, m in enumerate(rel.get('media') or []):
        if NO_AUDIO.search(m.get('format') or ''):
            continue
        for j, t in enumerate(m.get('tracks') or []):
            largo = t.get('length') or (t.get('recording') or {}).get('length')
            out.append({'disco': k + 1, 'numero': j + 1, 'titulo': t.get('title') or '',
                        'duracion': round(largo / 1000, 1) if largo else None})
    return _renumerar_discos(out)


def _cand_mb(rel):
    n = edicion_mod.normalizar(rel)
    return _cand('mb', n['id'], n['titulo'], n['artista'], n['anio'], n['pais'], n['formato'], n['url'],
                 pistas_mb(rel))


def candidatas_mb(cliente, rep, ids_edicion, n_pistas):
    ed = rep.get('edicion_mb') or {}
    ids = []
    for c in [ed.get('elegida') or {}] + list(ed.get('candidatas') or []):
        if c.get('id') and c['id'] not in ids:
            ids.append(c['id'])
    if ids_edicion.get('mbid') and ids_edicion['mbid'] not in ids:
        ids.insert(0, ids_edicion['mbid'])
    ta = rep.get('tags_album') or {}
    if not ids and ta.get('artista') and ta.get('album'):
        encontrados = []
        for alb in dict.fromkeys([ta['album'], album_base(ta['album'])]):
            consulta = f'release:"{edicion_mod._esc(alb)}" AND artist:"{edicion_mod._esc(ta["artista"])}"'
            encontrados = cliente.buscar(consulta, limite=15)
            if encontrados:
                break
        encontrados.sort(key=lambda r: (abs((r.get('track-count') or 0) - n_pistas), -(r.get('score') or 0)))
        ids = [r['id'] for r in encontrados if r.get('id')]
    out = []
    for i in ids[:MAX_MB]:
        rel = cliente.release(i)
        if rel and rel.get('id'):
            out.append(_cand_mb(rel))
    return out


# ─────────────────────────── Discogs ───────────────────────────

def token_discogs():
    return (os.environ.get('DISCOGS_TOKEN') or '').strip()


def id_discogs(texto):
    """Enlace o ID que pegues → ('release'|'master', id). Un número solo es un release."""
    t = str(texto or '').strip()
    m = re.search(r'discogs\.com/(?:[a-z]{2}/)?(?:[^/\s]+/)?(release|master)s?/(\d+)', t, re.I)
    if m:
        return m.group(1).lower(), m.group(2)
    m = re.fullmatch(r'\[?\s*([rm])?\s*(\d{2,10})\s*\]?', t, re.I)
    if m:
        return ('master' if (m.group(1) or '').lower() == 'm' else 'release'), m.group(2)
    return None


def posicion_discogs(pos):
    """Posición de Discogs → (disco, número o None si se cuenta en orden, no_audio).
    «1-05», «1.05», «CD2-3», «2-3» → (disco, n); «A1», «B2», «C1» → lado A/B = disco 1, C/D = disco 2…;
    «7», «12» → (1, n); «DVD1-2», «BD-1» → no es audio."""
    p = re.sub(r'\s+', '', str(pos or '')).upper()
    if not p:
        return None, None, False
    if NO_AUDIO.search(p) or p.startswith(('DVD', 'BD', 'VIDEO')):
        return None, None, True
    m = re.fullmatch(r'(?:CD|DISC|DISCO|SACD|HDCD|LP)?(\d{1,2})[-.:/](\d{1,3})[A-Z]?', p)
    if m:
        return int(m.group(1)), int(m.group(2)), False
    m = re.fullmatch(r'(?:CD|SACD|HDCD)(\d{1,3})', p)          # «CD5» suelto en un disco único
    if m:
        return 1, int(m.group(1)), False
    m = re.fullmatch(r'([A-Z])(\d{0,3})[A-Z]?', p)
    if m:
        return (ord(m.group(1)) - ord('A')) // 2 + 1, None, False
    m = re.fullmatch(r'(\d{1,3})[A-Z]?', p)
    if m:
        return 1, int(m.group(1)), False
    return None, None, False


def pistas_discogs(rel):
    """Listado de un release de Discogs → pistas. Sin encabezados; un «index track» cuyas partes son la misma pista
    del CD («1-3a», «1-3b») cuenta como una sola, con el título del índice; partes sueltas «7a», «7b» también."""
    filas = []
    for t in rel.get('tracklist') or []:
        tipo = t.get('type_') or 'track'
        if tipo == 'heading':
            continue
        if tipo == 'index' and not (t.get('position') or '').strip():
            subs = [x for x in t.get('sub_tracks') or [] if (x.get('type_') or 'track') == 'track']
            pos = {posicion_discogs(x.get('position'))[:2] for x in subs}
            if len(pos) == 1 and None not in next(iter(pos)):
                durs = [_segundos(x.get('duration')) for x in subs]
                filas.append({'position': subs[0].get('position'), 'title': t.get('title') or subs[0].get('title'),
                              'duration': None, '_dur': _segundos(t.get('duration')) or
                              (sum(durs) if durs and all(durs) else None)})
            else:
                filas += subs
            continue
        filas.append(t)
    out, disco_prev, n_en_disco = [], None, 0
    for t in filas:
        disco, numero, no_audio = posicion_discogs(t.get('position'))
        if no_audio:
            continue
        if disco is None:                                  # sin posición: sigue en el disco anterior
            disco = disco_prev or 1
        dur = t['_dur'] if '_dur' in t else _segundos(t.get('duration'))
        if numero and out and out[-1]['_explicito'] and (out[-1]['disco'], out[-1]['numero']) == (disco, numero):
            prev = out[-1]                                 # «7a» + «7b»: una sola pista del CD
            prev['titulo'] = f'{prev["titulo"]} / {t.get("title") or ""}'.strip(' /')
            prev['duracion'] = prev['duracion'] + dur if prev['duracion'] and dur else None
            continue
        if disco != disco_prev:
            disco_prev, n_en_disco = disco, 0
        n_en_disco += 1
        out.append({'disco': disco, 'numero': numero or n_en_disco, 'titulo': t.get('title') or '',
                    'duracion': dur, '_explicito': bool(numero)})
    for p in out:
        del p['_explicito']
    return _renumerar_discos(out)


class Discogs:
    """API de Discogs con caché en la misma tabla que MusicBrainz (30 días) y una consulta por segundo."""

    def __init__(self, ruta_cache):
        self.ruta_cache = ruta_cache
        self.token = token_discogs()

    def _get(self, ruta, **params):
        url = f'{DISCOGS_URL}/{ruta}' + ('?' + urllib.parse.urlencode(params) if params else '')
        clave = 'discogs:' + url                            # el token nunca va en la clave ni en el registro
        if self.ruta_cache:
            with edicion_mod._cache_db(self.ruta_cache) as c:
                r = c.execute('SELECT ts, json FROM mb_cache WHERE url=?', (clave,)).fetchone()
            if r and time.time() - r[0] < edicion_mod.CACHE_DIAS * 86400:
                return json.loads(r[1])
        cab = {'User-Agent': edicion_mod.USER_AGENT, 'Accept': 'application/vnd.discogs.v2.discogs+json'}
        if self.token:
            cab['Authorization'] = f'Discogs token={self.token}'
        ultimo = None
        for intento in range(3):
            with _lock_dc:
                espera = _ultima_dc[0] + (1.05 if self.token else 2.5) - time.time()
                if espera > 0:
                    time.sleep(espera)
                _ultima_dc[0] = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=20) as r:
                    datos = json.loads(r.read().decode('utf-8'))
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    datos = {}
                    break
                if e.code == 401:
                    raise FalloFuente('Discogs rechazó el token (revisa DISCOGS_TOKEN en el .env)') from e
                ultimo = e
                if e.code == 429:
                    time.sleep(3 + intento * 5)
                    continue
                raise FalloFuente(f'Discogs respondió {e.code}') from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                ultimo = e
                time.sleep(1 + intento * 2)
        else:
            raise FalloFuente(f'Discogs no responde ({ultimo})')
        if self.ruta_cache:
            with edicion_mod._cache_db(self.ruta_cache) as c:
                c.execute('INSERT OR REPLACE INTO mb_cache VALUES (?, ?, ?)', (clave, time.time(), json.dumps(datos)))
        return datos

    def release(self, rid):
        return self._get(f'releases/{rid}')

    def master(self, mid):
        return self._get(f'masters/{mid}')

    def buscar(self, **params):
        if not self.token:
            return []
        return (self._get('database/search', type='release', per_page=25, **params) or {}).get('results') or []


def _cand_discogs(rel, nota=None):
    formatos = []
    for f in rel.get('formats') or []:
        n = f.get('name') or ''
        q = str(f.get('qty') or '1')
        formatos.append((f'{q}×{n}' if q not in ('', '1') else n) +
                        (f' ({", ".join(f["descriptions"][:2])})' if f.get('descriptions') else ''))
    artista = ' '.join(re.sub(r'\s*\(\d+\)$', '', a.get('name') or '') + (' ' + a['join'] if a.get('join') else '')
                       for a in rel.get('artists') or []).strip()
    return _cand('discogs', rel.get('id'), rel.get('title'), artista, str(rel.get('year') or '') or None,
                 rel.get('country'), ' + '.join(formatos) or None,
                 rel.get('uri') or f'https://www.discogs.com/release/{rel.get("id")}', pistas_discogs(rel), nota)


def discogs_desde(dc, texto):
    """El release (o el principal del master) que Niko pegó."""
    ref = id_discogs(texto)
    if not ref:
        raise FalloFuente('No reconozco ese enlace de Discogs: pega la dirección del release o del master, o su número')
    tipo, n = ref
    if tipo == 'master':
        m = dc.master(n)
        if not m.get('main_release'):
            raise FalloFuente(f'Discogs no tiene el master {n}')
        n = m['main_release']
    rel = dc.release(n)
    if not rel.get('id'):
        raise FalloFuente(f'Discogs no tiene el release {n}')
    return _cand_discogs(rel, 'elegido por ti')


def candidatas_discogs(dc, rep, ids_edicion):
    if not dc.token:
        return []
    ta = rep.get('tags_album') or {}
    art, alb = ta.get('artista') or '', ta.get('album') or ''
    consultas = []
    if ids_edicion.get('barcode'):
        consultas.append({'barcode': re.sub(r'\D', '', ids_edicion['barcode'])})
    if ids_edicion.get('catalogo'):
        consultas.append({'catno': ids_edicion['catalogo'], 'artist': art})
    if art and alb:
        consultas += [{'artist': art, 'release_title': a} for a in dict.fromkeys([alb, album_base(alb)])]
    ids = []
    for q in consultas:
        res = dc.buscar(**q)
        if 'release_title' in q:          # de CD o digital primero (lo que suele haber en la biblioteca)
            res.sort(key=lambda r: 0 if {'CD', 'File', 'SACD', 'CDr'} & set(r.get('format') or []) else 1)
        for r in res:
            if r.get('id') and r['id'] not in ids and _parecido(r.get('title'), f'{art} {alb}' if art else alb):
                ids.append(r['id'])
        if len(ids) >= MAX_DISCOGS:
            break
    out = []
    for i in ids[:MAX_DISCOGS]:
        rel = dc.release(i)
        if rel.get('id'):
            out.append(_cand_discogs(rel))
    return out


# ─────────────────────────── Tidal ───────────────────────────

def candidatas_tidal(rep, ids_edicion, n_pistas):
    try:
        t = tienda_mod.cliente('tidal')
    except Exception:                                     # sin sesión de Tidal: no es un error del álbum
        return []
    ta = rep.get('tags_album') or {}
    art, alb = ta.get('artista') or '', ta.get('album') or ''
    albumes = []
    if ids_edicion.get('barcode'):
        albumes = t.albumes_por_upc(ids_edicion['barcode'])
    for q in dict.fromkeys([alb, album_base(alb)]) if not albumes and art and alb else ():
        albumes = [a for a in t.buscar_albumes(f'{art} {q}', limite=15)
                   if _parecido(a.get('titulo'), q) and _parecido(a.get('artista'), art)]
        if albumes:
            break
    if albumes and not ids_edicion.get('barcode'):
        albumes.sort(key=lambda a: abs((a.get('n_pistas') or 0) - n_pistas))
    out = []
    for a in albumes[:MAX_TIDAL]:
        pistas = [{'disco': p['disco'] or 1, 'numero': p['numero'], 'titulo': p['titulo'],
                   'duracion': float(p['duracion']) if p.get('duracion') else None} for p in t.pistas(a['id'])]
        out.append(_cand('tidal', a['id'], a.get('titulo'), a.get('artista'), a.get('anio'), None,
                         'Digital', a.get('url'), _renumerar_discos(pistas)))
    return out


# ─────────────────────────── emparejar ───────────────────────────

def emparejar(nuestras, suyas):
    """nuestras: [{'i', 'titulo', 'dur'}] en el orden del plan; suyas: pistas de la fuente.
    Por título (igual o parecido) y duración (dentro de la tolerancia), sin depender del orden; el orden solo
    desempata. Devuelve {i: (j, puntaje, por_titulo)}."""
    from .repetidos import _mismo_titulo
    pares = []
    for k, a in enumerate(nuestras):
        ta = _titulo(a['titulo'])
        for j, b in enumerate(suyas):
            tb = _titulo(b['titulo'])
            s, por_titulo = 0.0, False
            if ta and tb and not _generico(a['titulo']):
                if ta == tb:
                    s, por_titulo = 3.0, True
                elif _mismo_titulo(a['titulo'], b['titulo']):
                    s, por_titulo = 2.0, True
            da, db = a.get('dur'), b.get('duracion')
            dif = abs(da - db) if da and db else None
            if dif is not None:
                tol = tolerancia(max(da, db))
                if dif <= tol:
                    s += 2
                elif dif <= 2 * tol + 1:
                    s += 1
                elif dif > 30:
                    continue                              # otra versión de la canción, o no es ella
            if k == j:
                s += 0.5
            if s >= 2:
                pares.append((s, -(dif if dif is not None else 99), k, j, por_titulo))
    pares.sort(key=lambda p: (p[0], p[1]), reverse=True)
    usadas_n, usadas_s, mapa = set(), set(), {}
    for s, _, k, j, pt in pares:
        if k in usadas_n or j in usadas_s:
            continue
        usadas_n.add(k)
        usadas_s.add(j)
        mapa[nuestras[k]['i']] = (j, s, pt)
    return mapa


def evaluar(cand, nuestras):
    """Calce de una fuente con los archivos (y qué pistas suyas no están aquí)."""
    mapa = emparejar(nuestras, cand['pistas'])
    usadas = {j for j, _, _ in mapa.values()}
    cand['mapa'] = mapa
    cand['calzan'] = len(mapa)
    cand['n'] = len(nuestras)
    cand['calce'] = round(len(mapa) / len(nuestras), 2) if nuestras else 0
    cand['faltan'] = [p for j, p in enumerate(cand['pistas']) if j not in usadas]
    cand['discos'] = len({p['disco'] for p in cand['pistas']})
    cand['puntaje'] = (len(mapa) * 10 - len(cand['faltan']) * 3 - PRIORIDAD[cand['fuente']]
                       + sum(s for _, s, _ in mapa.values()) / 100)
    return cand


def resumen(c):
    r = {k: c.get(k) for k in ('clave', 'fuente', 'nombre', 'id', 'titulo', 'artista', 'anio', 'pais',
                               'formato', 'url', 'nota', 'calzan', 'n', 'calce', 'discos')}
    r['n_fuente'] = len(c.get('pistas') or [])
    return r


# ─────────────────────────── búsqueda ───────────────────────────

def ids_de_archivos(rutas):
    """MBID, código de barras y catálogo de los tags del primer archivo que los traiga."""
    from . import analisis
    for r in rutas[:4]:
        try:
            ed = analisis._edicion(analisis.tags_mutagen(r)[1])
        except Exception:
            continue
        if ed.get('mbid') or ed.get('barcode') or ed.get('catalogo'):
            return ed
    return {}


def nuestras_de(plan, ovp):
    """Pistas del plan que se pueden renumerar (las de una imagen con cue o un ISO no tienen archivo propio)."""
    from .autocorregir import _duracion
    out = []
    for i, x in enumerate(plan):
        if '#' in x['clave'] or x.get('corte'):
            continue
        o = ovp.get(x['clave']) or {}
        out.append({'i': i, 'clave': x['clave'], 'titulo': o.get('titulo') or x.get('titulo') or '',
                    'dur': _duracion(x)})
    return out


def buscar(rep, cliente_mb=None, ruta_cache=None, rutas=(), eleccion=None, discogs=None, usar_mb=True):
    """
    Candidatas de las tres fuentes y la elegida (con su emparejamiento).
    eleccion: 'mb:<id>' | 'discogs:<id>' | 'tidal:<id>' para usar esa sola; discogs: enlace o ID pegado.
    Devuelve {'candidatas': [...], 'elegida': cand|None, 'notas': [...], 'nuestras': [...]}.
    """
    ovp = (rep.get('cambios') or {}).get('pistas') or {}
    nuestras = nuestras_de(rep.get('plan') or [], ovp)
    ids = dict(ids_de_archivos(list(rutas)))
    el = ((rep.get('edicion_mb') or {}).get('elegida') or {})
    for k in ('barcode', 'catalogo'):
        ids.setdefault(k, el.get(k))
    notas, cands = [], []
    dc = Discogs(ruta_cache)

    def protegido(nombre, fn):
        try:
            return fn()
        except (FalloFuente, tienda_mod.FalloTienda) as e:
            notas.append(str(e))
        except edicion_mod.SinRed as e:
            notas.append(f'{nombre}: sin respuesta ({e})')
        except Exception as e:                            # una fuente caída no tumba a las otras
            log.warning('Numeración %s: %s', nombre, e)
            notas.append(f'{nombre}: {e}')
        return []

    if discogs:
        c = protegido('Discogs', lambda: [discogs_desde(dc, discogs)])
        cands = c
    elif eleccion and ':' in str(eleccion):
        fuente, cid = str(eleccion).split(':', 1)
        if fuente == 'mb' and cliente_mb is not None:
            cands = protegido('MusicBrainz', lambda: [_cand_mb(cliente_mb.release(cid))])
        elif fuente == 'discogs':
            cands = protegido('Discogs', lambda: [_cand_discogs(dc.release(cid))])
        elif fuente == 'tidal':
            def una_tidal():
                t = tienda_mod.cliente('tidal')
                ps = [{'disco': p['disco'] or 1, 'numero': p['numero'], 'titulo': p['titulo'],
                       'duracion': float(p['duracion']) if p.get('duracion') else None} for p in t.pistas(cid)]
                return [_cand('tidal', cid, rep.get('tags_album', {}).get('album'), pistas=_renumerar_discos(ps))]
            cands = protegido('Tidal', una_tidal)
    else:
        n = len(nuestras)
        tareas = {'Discogs': lambda: candidatas_discogs(dc, rep, ids),
                  'Tidal': lambda: candidatas_tidal(rep, ids, n)}
        if usar_mb and cliente_mb is not None:
            tareas['MusicBrainz'] = lambda: candidatas_mb(cliente_mb, rep, ids, n)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futuros = {nombre: ex.submit(protegido, nombre, fn) for nombre, fn in tareas.items()}
            for nombre in ('MusicBrainz', 'Discogs', 'Tidal'):
                if nombre in futuros:
                    cands += futuros[nombre].result() or []
        if not dc.token:
            notas.append('Discogs: sin DISCOGS_TOKEN en el .env solo funciona el enlace o ID que pegues')
    cands = [evaluar(c, nuestras) for c in cands if c.get('pistas')]
    cands.sort(key=lambda c: -c['puntaje'])
    elegida = None
    if cands:
        mejor = cands[0]
        if eleccion or discogs or mejor['calce'] >= CALCE_MINIMO:
            elegida = mejor
        else:
            notas.append(f'Ninguna fuente calza con al menos el {round(CALCE_MINIMO * 100)} % de los archivos '
                         f'(la mejor: {mejor["calzan"]}/{mejor["n"]}); elige una a mano si quieres usarla igual')
    elif not notas:
        notas.append('No encontré este álbum en MusicBrainz, Discogs ni Tidal')
    return {'candidatas': cands, 'elegida': elegida, 'notas': notas, 'nuestras': nuestras}


def para_pantalla(res):
    """Lo que ve el panel de Autocorregir: candidatas, la elegida, avisos, archivos sin pareja y pistas que faltan."""
    el = res.get('elegida')
    out = {'candidatas': [resumen(c) for c in res.get('candidatas') or []], 'elegida': el['clave'] if el else None,
           'notas': res.get('notas') or [], 'sin_pareja': [], 'faltan': []}
    if el:
        out['sin_pareja'] = [x['clave'] for x in res['nuestras'] if x['i'] not in el['mapa']]
        multi = el['discos'] > 1
        out['faltan'] = [(f'{p["disco"]}-' if multi else '') + f'{int(p["numero"] or 0):02d} {p["titulo"]}' for p in el['faltan']]
    return out
