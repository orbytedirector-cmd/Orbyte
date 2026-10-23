"""
Orbyte Ingesta — identificación de la edición exacta (etapa 1b).

Orden (se detiene en el primer resultado seguro):
  1. ID de MusicBrainz en los tags.
  2. TOC del CD (desde .log de EAC/XLD, .cue + imagen a 44,1 kHz, o pistas CD exactas) → MusicBrainz.
  3. Número de catálogo / código de barras (tags, nombre de carpeta, .log).
  4. Búsqueda por artista + álbum, puntuando cada edición por estructura y duración de las pistas.
  5. AcoustID (si hay clave y fpcalc), solo cuando no hay tags útiles o la búsqueda no encontró nada.

Solo consultas: no escribe ni mueve archivos. MusicBrainz admite 1 consulta por segundo;
todas las respuestas quedan guardadas en ingesta.db.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger('ingesta')

MB_URL = os.environ.get('INGESTA_MB_URL') or 'https://musicbrainz.org/ws/2'
ACOUSTID_URL = os.environ.get('INGESTA_ACOUSTID_URL') or 'https://api.acoustid.org/v2/lookup'
USER_AGENT = 'OrbyteIngesta/1.0 ( https://github.com/orbytedirector-cmd/Orbyte )'
CACHE_DIAS = 30
INC_RELEASE = 'recordings+labels+release-groups+artist-credits+media'

_lock_red = threading.Lock()
_ultima = [0.0]


class SinRed(Exception):
    pass


class Cliente:
    """Cliente mínimo de MusicBrainz (y AcoustID) con caché en sqlite y límite de 1 consulta/s."""

    def __init__(self, ruta_cache):
        self.ruta_cache = ruta_cache
        with sqlite3.connect(ruta_cache, timeout=30) as c:
            c.execute('CREATE TABLE IF NOT EXISTS mb_cache (url TEXT PRIMARY KEY, ts REAL, json TEXT)')
        self.consultas = 0

    def _cache(self, url):
        with sqlite3.connect(self.ruta_cache, timeout=30) as c:
            r = c.execute('SELECT ts, json FROM mb_cache WHERE url=?', (url,)).fetchone()
        if r and time.time() - r[0] < CACHE_DIAS * 86400:
            return json.loads(r[1])
        return None

    def _guardar(self, url, datos):
        with sqlite3.connect(self.ruta_cache, timeout=30) as c:
            c.execute('INSERT OR REPLACE INTO mb_cache VALUES (?, ?, ?)', (url, time.time(), json.dumps(datos)))

    def get(self, url, cachear=True, pausa=1.1):
        if cachear:
            d = self._cache(url)
            if d is not None:
                return d
        ultimo_error = None
        for intento in range(4):
            with _lock_red:
                espera = _ultima[0] + pausa - time.time()
                if espera > 0:
                    time.sleep(espera)
                _ultima[0] = time.time()
            try:
                req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
                with urllib.request.urlopen(req, timeout=20) as r:
                    datos = json.loads(r.read().decode('utf-8'))
                self.consultas += 1
                if cachear:
                    self._guardar(url, datos)
                return datos
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    datos = {}
                    if cachear:
                        self._guardar(url, datos)
                    return datos
                ultimo_error = e
                if e.code in (429, 503):
                    time.sleep(2 + intento * 3)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                ultimo_error = e
                time.sleep(1 + intento * 2)
        raise SinRed(str(ultimo_error))

    def mb(self, ruta, **params):
        params['fmt'] = 'json'
        return self.get(f'{MB_URL}/{ruta}?{urllib.parse.urlencode(params, safe="+:")}')

    def release(self, mbid):
        return self.mb(f'release/{mbid}', inc=INC_RELEASE)

    def buscar(self, consulta, limite=25):
        return (self.mb('release/', query=consulta, limit=limite) or {}).get('releases', [])

    def toc(self, toc):
        d = self.mb('discid/-', toc=toc, cdstubs='no', inc='labels+release-groups')
        return (d or {}).get('releases', [])


# ─────────────────────────── normalización de una edición ───────────────────────────

def _anio(s):
    m = re.match(r'\s*(\d{4})', str(s or ''))
    return m.group(1) if m else None


def normalizar(rel):
    """Release de MusicBrainz (con inc=recordings…) → dict compacto para el reporte."""
    medios = rel.get('media') or []
    formatos = {}
    for m in medios:
        f = m.get('format') or '?'
        formatos[f] = formatos.get(f, 0) + 1
    formato = ' + '.join(f'{n}×{f}' if n > 1 else f for f, n in formatos.items()) or None
    labels = rel.get('label-info') or []
    sello = next((li['label']['name'] for li in labels if li.get('label') and li['label'].get('name')), None)
    catalogo = next((li.get('catalog-number') for li in labels if li.get('catalog-number')), None)
    grupo = rel.get('release-group') or {}
    duraciones = []
    for m in medios:
        pistas = m.get('tracks') or []
        if pistas:
            duraciones.append([round((t.get('length') or (t.get('recording') or {}).get('length') or 0) / 1000, 1)
                               for t in pistas])
        else:
            duraciones.append([None] * (m.get('track-count') or 0))
    titulos = [[t.get('title') for t in (m.get('tracks') or [])] for m in medios]
    artista = ''.join((a.get('name') or '') + (a.get('joinphrase') or '') for a in rel.get('artist-credit') or [])
    caa = rel.get('cover-art-archive') or {}
    return {
        'id': rel.get('id'), 'titulo': rel.get('title'), 'artista': artista or None,
        'fecha': rel.get('date'), 'anio': _anio(rel.get('date')),
        'anio_original': _anio(grupo.get('first-release-date')), 'grupo_id': grupo.get('id'),
        'pais': rel.get('country'), 'sello': sello, 'catalogo': catalogo,
        'barcode': rel.get('barcode') or None, 'formato': formato, 'estado': rel.get('status'),
        'desambiguacion': rel.get('disambiguation') or None,
        'discos': [len(d) for d in duraciones], 'duraciones': duraciones, 'titulos': titulos,
        'portada': bool(caa.get('front')) if caa else None,
        'url': f'https://musicbrainz.org/release/{rel.get("id")}',
    }


# ─────────────────────────── TOC del CD ───────────────────────────

def toc_desde_log(texto):
    """TOC de un .log de EAC o XLD: [(inicio, fin), ...] en sectores."""
    filas = re.findall(r'^\s*(\d{1,2})\s*\|\s*[\d:.]+\s*\|\s*[\d:.]+\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$', texto, re.M)
    pistas = {}
    for n, ini, fin in filas:
        pistas.setdefault(int(n), (int(ini), int(fin)))
    if not pistas:
        return None
    orden = [pistas[k] for k in sorted(pistas)]
    if sorted(pistas) != list(range(1, len(orden) + 1)):
        return None
    return orden


def toc_str(inicios, leadout):
    """«1 N leadout off1 … offN» con offsets en sectores (+150 de pregap)."""
    return '+'.join(str(x) for x in [1, len(inicios), leadout + 150] + [i + 150 for i in inicios])


def toc_desde_log_str(texto):
    t = toc_desde_log(texto)
    if not t:
        return None
    return toc_str([a for a, _ in t], t[-1][1] + 1)


def toc_desde_cue(inicios_s, muestras_total, sr):
    """Imagen + cue a 44,1 kHz: inicios en segundos (múltiplos de 1/75) y largo exacto en muestras."""
    if sr != 44100 or not muestras_total or muestras_total % 588:
        return None
    return toc_str([round(s * 75) for s in inicios_s], muestras_total // 588)


def toc_desde_pistas(muestras):
    """Pistas separadas de un rip de CD: todas con largo múltiplo de 588 muestras (un sector)."""
    if not muestras or any(not m or m % 588 for m in muestras):
        return None
    inicios, pos = [], 0
    for m in muestras:
        inicios.append(pos)
        pos += m // 588
    return toc_str(inicios, pos)


# ─────────────────────────── puntaje ───────────────────────────

def comparar_duraciones(nuestras, de_ellos, tolerancia=3.0):
    """Fracción de pistas (en orden) cuya duración calza dentro de ±tolerancia."""
    planas_n = [d for disco in nuestras for d in disco]
    planas_e = [d for disco in de_ellos for d in disco]
    if not planas_n or len(planas_n) != len(planas_e):
        return 0.0
    validas = [(a, b) for a, b in zip(planas_n, planas_e) if a and b]
    if not validas:
        return 0.0
    return sum(1 for a, b in validas if abs(a - b) <= tolerancia) / len(planas_n)


def puntuar(rel, datos):
    """0–100+: estructura, duraciones, años presentes, formato, catálogo."""
    p, motivos = 0.0, []
    nuestras = datos['duraciones']
    estructura_n = [len(d) for d in nuestras]
    if rel['discos'] == estructura_n:
        p += 40
        motivos.append('mismas pistas por disco')
    elif sum(rel['discos']) == sum(estructura_n):
        p += 30
        motivos.append('mismo total de pistas')
    else:
        p -= 40
        motivos.append(f'{sum(rel["discos"])} pistas (aquí {sum(estructura_n)})')
    calce = comparar_duraciones(nuestras, rel['duraciones'])
    rel['calce'] = round(calce, 2)
    p += 40 * calce
    if calce:
        motivos.append(f'duraciones {round(calce * 100)}%')
    if rel.get('anio') and rel['anio'] in datos.get('anios', set()):
        p += 10
        motivos.append(f'año {rel["anio"]} coincide')
    fmt = (rel.get('formato') or '').lower()
    pista = datos.get('formato')
    if pista and ((pista == 'cd' and 'cd' in fmt and 'sacd' not in fmt) or (pista == 'vinilo' and 'vinyl' in fmt)
                  or (pista == 'digital' and 'digital' in fmt) or (pista == 'sacd' and 'sacd' in fmt)):
        p += 8
        motivos.append(f'formato {rel["formato"]}')
    cats = {_norm_cat(x) for x in datos.get('catalogos', [])}
    if rel.get('catalogo') and _norm_cat(rel['catalogo']) in cats:
        p += 30
        motivos.append('catálogo coincide')
    if rel.get('barcode') and rel['barcode'] in datos.get('barcodes', []):
        p += 30
        motivos.append('código de barras coincide')
    if rel.get('estado') == 'Official':
        p += 2
    rel['puntaje'] = round(p, 1)
    rel['motivos'] = motivos
    return p


def _norm_cat(s):
    return re.sub(r'[^A-Z0-9]', '', str(s or '').upper())


def _esc(s):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', s or '')


# ─────────────────────────── AcoustID ───────────────────────────

def acoustid_disponible():
    return bool(os.environ.get('ACOUSTID_API_KEY') or os.environ.get('ACOUSTID_KEY')) and bool(shutil.which('fpcalc'))


def huella(ruta, segundos=120):
    r = subprocess.run(['fpcalc', '-json', '-length', str(segundos), ruta], capture_output=True, text=True, timeout=120)
    d = json.loads(r.stdout or '{}')
    return d.get('duration'), d.get('fingerprint')


def releases_por_acoustid(cliente, rutas):
    clave = os.environ.get('ACOUSTID_API_KEY') or os.environ.get('ACOUSTID_KEY')
    votos = {}
    for ruta in rutas[:3]:
        try:
            dur, fp = huella(ruta)
        except Exception as e:
            log.warning('fpcalc falló en %s: %s', ruta, e)
            continue
        if not fp:
            continue
        url = ACOUSTID_URL + '?' + urllib.parse.urlencode(
            {'client': clave, 'meta': 'recordings releaseids', 'duration': int(dur or 0), 'fingerprint': fp})
        d = cliente.get(url, cachear=True, pausa=0.4)
        for res in (d or {}).get('results', [])[:3]:
            if res.get('score', 0) < 0.7:
                continue
            for rec in res.get('recordings') or []:
                for rel in rec.get('releases') or []:
                    votos[rel['id']] = votos.get(rel['id'], 0) + 1
    return [k for k, _ in sorted(votos.items(), key=lambda x: -x[1])][:8]


# ─────────────────────────── identificación ───────────────────────────

def identificar(cliente, datos, eleccion=None, progreso=None):
    """
    datos = {artista, album, duraciones: [[s, …] por disco], anios: set, formato: 'cd'|'digital'|'vinilo'|'sacd'|None,
             mbid, catalogos: [], barcodes: [], toc: str|None, rutas_audio: [...], tags_utiles: bool}
    Devuelve {'estado': 'alta'|'anio'|'dudosa'|'sin_resultado'|'sin_red', 'metodo', 'elegida', 'candidatas', 'consultas'}
    """
    def aviso(m):
        if progreso:
            progreso(m)

    resultado = {'estado': 'sin_resultado', 'metodo': None, 'elegida': None, 'candidatas': []}
    cand = {}

    def agregar(mbid, metodo):
        if not mbid or mbid in cand:
            return
        rel = cliente.release(mbid)
        if rel and rel.get('id'):
            n = normalizar(rel)
            n['metodo'] = metodo
            puntuar(n, datos)
            cand[mbid] = n

    if eleccion:
        # se hace la identificación normal (queda en caché) para mostrar las candidatas, y manda la elegida
        r = identificar(cliente, datos, None, progreso)
        try:
            rel = cliente.release(eleccion)
        except SinRed as e:
            return dict(r, estado='sin_red', error=str(e))
        if rel and rel.get('id'):
            n = normalizar(rel)
            n['metodo'] = 'elegida por ti'
            puntuar(n, datos)
            otras = [c for c in r.get('candidatas', []) if c['id'] != eleccion]
            r.update(estado='alta', metodo='elegida por ti', elegida=n,
                     candidatas=[{k: v for k, v in n.items() if k not in ('duraciones', 'titulos')}] + otras)
        return r

    try:
        if datos.get('mbid'):
            aviso('MusicBrainz: ID de los tags')
            agregar(datos['mbid'], 'ID en los tags')
            if datos['mbid'] in cand and cand[datos['mbid']]['puntaje'] >= 30:
                resultado.update(estado='alta', metodo='ID en los tags', elegida=cand[datos['mbid']])
                return _cerrar(resultado, cand, cliente)

        if datos.get('toc'):
            aviso('MusicBrainz: TOC del CD')
            por_toc_raw = cliente.toc(datos['toc'])
            if datos.get('tags_utiles'):
                # un TOC puede coincidir con otro disco de igual estructura: debe calzar también el nombre
                por_toc_raw = [r for r in por_toc_raw if _calza_nombre(r, datos)]
            ids = [r['id'] for r in por_toc_raw][:6]
            for i in ids:
                agregar(i, 'TOC del CD')
            por_toc = [cand[i] for i in ids if i in cand]
            if por_toc:
                return _decidir(resultado, cand, por_toc, 'TOC del CD', cliente, exacto=True)

        cats = [c for c in datos.get('catalogos', []) if len(_norm_cat(c)) >= 4]
        for consulta, metodo in ([(f'catno:"{_esc(c)}"', 'catálogo') for c in cats[:2]] +
                                 [(f'barcode:{_esc(b)}', 'código de barras') for b in datos.get('barcodes', [])[:1]]):
            aviso(f'MusicBrainz: {metodo}')
            encontrados = cliente.buscar(consulta, limite=10)
            if datos.get('artista'):
                na = _simple(datos['artista'])
                encontrados = [r for r in encontrados if na in _simple(_credito(r)) or _simple(_credito(r)) in na] or encontrados
            for r in encontrados[:5]:
                agregar(r['id'], metodo)
            ids = [r['id'] for r in encontrados[:5] if r['id'] in cand]
            if ids:
                return _decidir(resultado, cand, [cand[i] for i in ids], metodo, cliente, exacto=True)

        encontrados = []
        if datos.get('tags_utiles') and datos.get('artista') and datos.get('album'):
            aviso('MusicBrainz: búsqueda por artista y álbum')
            consulta = f'release:"{_esc(datos["album"])}" AND artist:"{_esc(datos["artista"])}"'
            encontrados = cliente.buscar(consulta)
            total = sum(len(d) for d in datos['duraciones'])
            encontrados.sort(key=lambda r: (abs((r.get('track-count') or 0) - total), -(r.get('score') or 0)))
            for r in encontrados[:6]:
                aviso(f'MusicBrainz: comparando ediciones ({len(cand) + 1}/6)')
                agregar(r['id'], 'búsqueda')
            if cand:
                return _decidir(resultado, cand, list(cand.values()), 'búsqueda', cliente)

        if acoustid_disponible() and datos.get('rutas_audio'):
            aviso('AcoustID: huella del audio')
            for i in releases_por_acoustid(cliente, datos['rutas_audio']):
                agregar(i, 'AcoustID')
            if cand:
                return _decidir(resultado, cand, list(cand.values()), 'AcoustID', cliente)
    except SinRed as e:
        resultado.update(estado='sin_red', error=str(e))
        log.warning('Sin acceso a MusicBrainz: %s', e)
    resultado['candidatas'] = _top(cand)
    resultado['consultas'] = cliente.consultas
    return resultado


def _decidir(resultado, cand, lista, metodo, cliente, exacto=False):
    lista = sorted(lista, key=lambda r: -r['puntaje'])
    mejor = lista[0]
    segundo = lista[1] if len(lista) > 1 else None
    if mejor['puntaje'] < 40 and not exacto:
        estado = 'dudosa'
    elif exacto and mejor['puntaje'] >= 30 and (not segundo or mejor['puntaje'] - segundo['puntaje'] >= 5):
        estado = 'alta'
    elif mejor['puntaje'] >= 80 and (not segundo or mejor['puntaje'] - segundo['puntaje'] >= 12):
        estado = 'alta'
    elif mejor['puntaje'] >= 70 and all(r['anio'] == mejor['anio'] for r in lista
                                        if mejor['puntaje'] - r['puntaje'] < 12):
        estado = 'anio'           # varias ediciones casi iguales, pero todas del mismo año
    else:
        estado = 'dudosa'
    resultado.update(estado=estado, metodo=metodo, elegida=mejor if estado in ('alta', 'anio') else None)
    return _cerrar(resultado, cand, cliente)


def _cerrar(resultado, cand, cliente):
    resultado['candidatas'] = _top(cand)
    resultado['consultas'] = cliente.consultas
    return resultado


def _top(cand, n=6):
    lista = sorted(cand.values(), key=lambda r: -r.get('puntaje', 0))[:n]
    return [{k: v for k, v in r.items() if k not in ('duraciones', 'titulos')} for r in lista]


def _calza_nombre(r, datos):
    import difflib
    ta, tb = _simple(r.get('title')), _simple(datos.get('album'))
    aa, ab = _simple(_credito(r)), _simple(datos.get('artista'))
    titulo = ta and tb and (ta in tb or tb in ta or difflib.SequenceMatcher(None, ta, tb).ratio() >= 0.6)
    artista = not aa or not ab or aa in ab or ab in aa or difflib.SequenceMatcher(None, aa, ab).ratio() >= 0.6
    return bool(titulo and artista)


def _credito(r):
    return ''.join((a.get('name') or '') + (a.get('joinphrase') or '') for a in r.get('artist-credit') or [])


def _simple(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())
