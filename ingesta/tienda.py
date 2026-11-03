"""
Orbyte Ingesta — buscar música en Tidal y Qobuz y descargarla a la carpeta de entrada.

Usa tus herramientas tal como están configuradas (sesión, calidad máxima y carpeta de destino):
  · Tidal → tidal-dl-ng (sesión en ~/.config/tidal_dl_ng[-dev]/token.json)
  · Qobuz → streamrip «rip» (sesión en ~/.config/streamrip/config.toml)
La búsqueda habla directo con las APIs con esas mismas sesiones (solo lectura). Lo descargado cae en la carpeta de
entrada y sigue el flujo normal de la Bandeja (manual o desatendido, según los Ajustes).

- Tidal no informa la frecuencia exacta en la búsqueda: se pide la de una pista («resolución», sin descargar).
- «Medir master»: baja la misma pista de cada servicio a una carpeta temporal de Ingesta (fuera de la entrada),
  mide LUFS/LRA/pico real y la borra; el resultado queda guardado.
- Descargas: de a una, con pausa entre álbumes; mientras baja, el vigilante no revisa esa carpeta.
"""
import base64
import io
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import analisis

log = logging.getLogger('ingesta')
UA = 'OrbyteIngesta/1.0'
EXT_AUDIO = ('.flac', '.m4a', '.mp3', '.wav', '.aiff', '.aif', '.dsf', '.dff', '.alac', '.ogg', '.opus')
PAUSA_ENTRE_ALBUMES = (20, 40)          # segundos, al azar: que no parezca una descarga masiva
VERSION_CACHE = 'v2'          # v2: lo medido con la sesión de dispositivo (solo AAC) no sirve


class FalloTienda(Exception):
    pass


# ─────────────────────────── utilidades ───────────────────────────

def _http(url, cabeceras=None, datos=None, timeout=25):
    req = urllib.request.Request(url, data=datos, headers=dict({'User-Agent': UA, 'Accept': 'application/json'},
                                                               **(cabeceras or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _home(*p):
    return os.path.join(os.path.expanduser('~'), *p)


def _entorno(extra=None):
    """PATH con ~/.local/bin (systemd no lo trae) y sin colores ni barras de progreso."""
    env = dict(os.environ)
    env['PATH'] = _home('.local', 'bin') + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    env.update({'TERM': 'dumb', 'NO_COLOR': '1', 'COLUMNS': '160', 'PYTHONUNBUFFERED': '1'})
    env.update(extra or {})
    return env


def herramienta(nombre):
    for c in (shutil.which(nombre), _home('.local', 'bin', nombre)):
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\r')


def _norm(s):
    return analisis._norm(s or '')


def _con_version(titulo, version):
    """«Rock in Rio (Live) [2015 Remaster]» + «Live; 2015 Remaster» → sin repetir lo que el título ya dice."""
    partes = [p.strip() for p in re.split(r'[;,/]', version or '') if p.strip()]
    nuevas = [p for p in partes if _norm(p) not in _norm(titulo)]
    return f'{titulo} ({"; ".join(nuevas)})' if nuevas else titulo


def _audios(carpeta):
    out = []
    for raiz, _d, archivos in os.walk(carpeta):
        out += [os.path.join(raiz, a) for a in archivos
                if a.lower().endswith(EXT_AUDIO) and not a.startswith('.')]
    return out


SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools', 'metadata')
CODECS_CON_PERDIDA = ('aac', 'mp3', 'opus', 'vorbis', 'eac3', 'ac3', 'mp2', 'wmav1', 'wmav2')


def python_tidal():
    """El Python de tidal-dl-ng (primera línea de su ejecutable): es el que tiene tidalapi."""
    exe = herramienta('tidal-dl-ng')
    if exe:
        try:
            with open(exe, encoding='utf-8', errors='replace') as f:
                linea = f.readline().strip()
            if linea.startswith('#!'):
                partes = linea[2:].split()
                return partes[1] if partes and os.path.basename(partes[0]) == 'env' and len(partes) > 1 else partes[0]
        except (OSError, IndexError):
            pass
    return 'python3'


def preparar_tidal(album_id=None, pistas=None):
    """
    Antes de bajar de Tidal:
      · deja en el token.json de tidal-dl-ng la sesión PKCE recién renovada (tools/metadata/tidal_pkce.py): la de
        «tidal-dl-ng login» (código de dispositivo) Tidal la limita a AAC 320
      · saca del historial de tidal-dl-ng las pistas de ese álbum, para que no las salte si ya se bajaron antes
    Devuelve cuántas pistas sacó del historial.
    """
    try:
        r = subprocess.run([python_tidal(), os.path.join(SCRIPTS, 'tidal_pkce.py'), 'renovar'], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60, env=_entorno())
        d = json.loads((r.stdout or '{}').strip().splitlines()[-1] or '{}')
    except Exception as e:  # noqa: BLE001
        raise FalloTienda(f'Tidal: no se pudo renovar la sesión PKCE ({type(e).__name__})') from e
    if not d.get('ok'):
        err = d.get('error') or 'no se pudo renovar la sesión PKCE'
        raise FalloTienda(f'Tidal: {err}' + ('' if 'login-pkce' in err else
                          '. En el servidor: python3 tools/metadata/tidal_calidad.py URL_DE_UN_ALBUM --login-pkce'))
    olvidar('tidal')                                  # el cliente de búsqueda toma el token nuevo
    if not album_id and not pistas:
        return 0
    if pistas is None:
        pistas = [p['id'] for p in cliente('tidal').pistas(album_id)]
    ruta = os.path.join(dir_tidal() or '', 'downloaded_history.json')
    if not os.path.isfile(ruta):
        return 0
    with open(ruta, encoding='utf-8') as f:
        h = json.load(f)
    tabla = h.get('tracks') if isinstance(h.get('tracks'), dict) else h
    quitar = [k for k in list(tabla) if k in {str(x) for x in pistas}]
    if quitar:
        for k in quitar:
            tabla.pop(k)
        tmp = ruta + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(h, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, ruta)
    return len(quitar)


def codec(ruta):
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=codec_name',
                            '-of', 'csv=p=0', ruta], capture_output=True, text=True, timeout=60)
        return r.stdout.strip().strip(',').lower() or None
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────── configuración de las herramientas ───────────────────────────

def dir_tidal():
    for n in ('tidal_dl_ng-dev', 'tidal_dl_ng'):
        d = _home('.config', n)
        if os.path.isfile(os.path.join(d, 'settings.json')) or os.path.isfile(os.path.join(d, 'token.json')):
            return d
    return None


def ajustes_tidal():
    d = dir_tidal()
    if not d or not os.path.isfile(os.path.join(d, 'settings.json')):
        return {}
    with open(os.path.join(d, 'settings.json'), encoding='utf-8') as f:
        return json.load(f)


def _toml(ruta):
    with open(ruta, 'rb') as f:
        crudo = f.read()
    try:
        import tomllib
        return tomllib.loads(crudo.decode('utf-8'))
    except ImportError:            # Python < 3.11: lo justo para las claves que se usan
        datos, seccion = {}, None
        for linea in crudo.decode('utf-8').splitlines():
            linea = linea.strip()
            m = re.match(r'^\[([^\]]+)\]$', linea)
            if m:
                seccion = datos.setdefault(m.group(1), {})
                continue
            m = re.match(r'^(\w+)\s*=\s*(.+)$', linea)
            if m and seccion is not None:
                v = m.group(2).strip()
                if v.startswith('"'):
                    v = v.strip('"')
                elif v in ('true', 'false'):
                    v = v == 'true'
                elif re.fullmatch(r'-?\d+', v):
                    v = int(v)
                seccion[m.group(1)] = v
        return datos


def config_rip():
    ruta = _home('.config', 'streamrip', 'config.toml')
    return _toml(ruta) if os.path.isfile(ruta) else {}


def carpeta_destino(servicio):
    """Carpeta (dentro de la entrada) donde la herramienta deja los álbumes."""
    if servicio == 'tidal':
        aj = ajustes_tidal()
        base = aj.get('download_base_path') or _home('download')
        primero = (aj.get('format_album') or 'Albums/').split('/')[0]
        return os.path.join(os.path.expanduser(base), primero if '{' not in primero else '')
    return os.path.expanduser((config_rip().get('downloads') or {}).get('folder') or _home('StreamripDownloads'))


# ─────────────────────────── Qobuz ───────────────────────────

class Qobuz:
    BASE = 'https://www.qobuz.com/api.json/0.2/'

    def __init__(self):
        cfg = config_rip()
        q = cfg.get('qobuz') or {}
        if not q.get('app_id') or not q.get('password_or_token'):
            raise FalloTienda('Qobuz: falta la sesión en la configuración de streamrip')
        self.app_id = str(q['app_id'])
        if q.get('use_auth_token'):
            self.token = q['password_or_token']
        else:
            d = self._get('user/login', token=False, email=q.get('email_or_userid'), password=q['password_or_token'])
            self.token = d['user_auth_token']

    def _get(self, ep, token=True, **params):
        params['app_id'] = self.app_id
        cab = {'X-App-Id': self.app_id}
        if token:
            cab['X-User-Auth-Token'] = self.token
        try:
            return _http(self.BASE + ep + '?' + urllib.parse.urlencode(params), cab)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise FalloTienda('Qobuz rechazó la sesión: renuévala en streamrip (rip config --qobuz)') from e
            raise

    @staticmethod
    def _tipo(a):
        rt = (a.get('release_type') or a.get('product_type') or '').lower()
        titulo = (a.get('title') or '') + ' ' + (a.get('version') or '')
        if 'live' in rt or re.search(r'\blive\b|en vivo|en directo', titulo, re.I):
            return 'en_vivo'
        if 'compil' in rt or 'best of' in titulo.lower() or 'greatest hits' in titulo.lower():
            return 'recopilatorio'
        if 'single' in rt or rt in ('ep', 'epmini', 'epsingle') or (a.get('tracks_count') or 9) <= 4:
            return 'ep'
        return 'album'

    def _album(self, a):
        sr = a.get('maximum_sampling_rate')
        titulo = a.get('title') or ''
        titulo = _con_version(titulo, a.get('version'))
        img = a.get('image') or {}
        return {'servicio': 'qobuz', 'id': str(a['id']), 'titulo': titulo,
                'artista': (a.get('artist') or {}).get('name') or '',
                'anio': (a.get('release_date_original') or '')[:4] or None,
                'n_pistas': a.get('tracks_count'), 'duracion': a.get('duration'),
                'bits': a.get('maximum_bit_depth'), 'sr': int(round(float(sr) * 1000)) if sr else None,
                'upc': a.get('upc'), 'sello': (a.get('label') or {}).get('name'),
                'portada': img.get('small') or img.get('thumbnail'), 'tipo': self._tipo(a),
                'explicito': bool(a.get('parental_warning')), 'url': f'https://open.qobuz.com/album/{a["id"]}',
                'disponible': a.get('streamable', True) is not False, 'resolucion_exacta': True}

    def buscar_albumes(self, q, limite=30):
        d = self._get('catalog/search', query=q, type='albums', limit=limite)
        return [self._album(a) for a in (d.get('albums') or {}).get('items', [])]

    def buscar_artista(self, q):
        d = self._get('catalog/search', query=q, type='artists', limit=5)
        artistas = (d.get('artists') or {}).get('items', [])
        if not artistas:
            return None, []
        art = max(artistas, key=lambda a: (_norm(a.get('name')) == _norm(q), a.get('albums_count') or 0))
        d = self._get('artist/get', artist_id=art['id'], extra='albums', limit=500)
        return art.get('name'), [self._album(a) for a in (d.get('albums') or {}).get('items', [])]

    def calidad_real(self, album_id):
        """Lo que esta sesión recibe de verdad (no lo que dice el catálogo): 'HI_RES_LOSSLESS'|'LOSSLESS'|'HIGH'…"""
        if getattr(self, '_calidad_real', None) is None:
            pistas = self._get(f'albums/{album_id}/tracks', limit=1).get('items', [])
            if not pistas:
                return None
            p = self._get(f'tracks/{pistas[0]["id"]}/playbackinfopostpaywall', audioquality='LOSSLESS',
                          playbackmode='STREAM', assetpresentation='FULL')
            self._calidad_real = p.get('audioQuality') or ''
        return self._calidad_real

    def pistas(self, album_id):
        d = self._get('album/get', album_id=album_id)
        return [{'id': str(t['id']), 'titulo': t.get('title') or '', 'numero': t.get('track_number'),
                 'disco': t.get('media_number') or 1, 'duracion': t.get('duration')}
                for t in (d.get('tracks') or {}).get('items', [])]


# ─────────────────────────── Tidal ───────────────────────────

class Tidal:
    API = 'https://api.tidal.com/v1/'

    def __init__(self):
        self.dir = dir_tidal()
        if not self.dir or not os.path.isfile(os.path.join(self.dir, 'token.json')):
            raise FalloTienda('Tidal: no se encontró la sesión de tidal-dl-ng (token.json); inicia sesión con «tidal-dl-ng login»')
        self._leer_token()
        self.pais = None

    def _leer_token(self):
        with open(os.path.join(self.dir, 'token.json'), encoding='utf-8') as f:
            t = json.load(f)
        self.token = t.get('access_token') or (t.get('session') or {}).get('access_token')
        if not self.token:
            raise FalloTienda('Tidal: token.json no trae una sesión válida; inicia sesión con «tidal-dl-ng login»')

    def _refrescar(self):
        """Token vigente con la lógica de tidal-dl-ng (tools/metadata/tidal_sesion.py): el de token.json puede
        estar vencido aunque tidal-dl-ng diga que la sesión está bien (renueva solo en memoria)."""
        py = python_tidal()
        script = os.path.join(SCRIPTS, 'tidal_sesion.py')
        try:
            r = subprocess.run([py, script, os.path.join(self.dir, 'token.json')], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=45, env=_entorno())
            d = json.loads((r.stdout or '{}').strip().splitlines()[-1] or '{}')
        except Exception as e:  # noqa: BLE001
            raise FalloTienda(f'Tidal: no se pudo renovar la sesión ({type(e).__name__})') from e
        if not d.get('access_token'):
            raise FalloTienda('La sesión de Tidal expiró: en el servidor ejecuta «tidal-dl-ng login» '
                              f'({d.get("error") or "sin respuesta"})')
        self.token = d['access_token']
        self.pais = d.get('country_code') or self.pais

    def _get(self, ep, reintento=True, **params):
        if ep != 'sessions':
            params.setdefault('countryCode', self._pais())
        url = (ep if ep.startswith('http') else self.API + ep) + ('?' + urllib.parse.urlencode(params) if params else '')
        try:
            return _http(url, {'Authorization': f'Bearer {self.token}'})
        except urllib.error.HTTPError as e:
            if e.code == 401 and reintento:
                self._refrescar()
                return self._get(ep, reintento=False, **params)
            if e.code in (401, 403):
                raise FalloTienda('Tidal rechazó la sesión: en el servidor ejecuta «tidal-dl-ng login»') from e
            raise

    def _pais(self):
        if not self.pais:
            self.pais = self._get('sessions').get('countryCode') or 'US'
        return self.pais

    @staticmethod
    def _tipo(a):
        t = (a.get('type') or '').upper()
        titulo = (a.get('title') or '') + ' ' + (a.get('version') or '')
        if re.search(r'\blive\b|en vivo|en directo', titulo, re.I):
            return 'en_vivo'
        if t in ('EP', 'SINGLE'):
            return 'ep'
        if t == 'COMPILATION' or 'greatest hits' in titulo.lower() or 'best of' in titulo.lower():
            return 'recopilatorio'
        return 'album'

    def _album(self, a):
        tags = (a.get('mediaMetadata') or {}).get('tags') or []
        hires = 'HIRES_LOSSLESS' in tags or a.get('audioQuality') == 'HI_RES_LOSSLESS'
        lossless = hires or 'LOSSLESS' in tags or a.get('audioQuality') in ('LOSSLESS', 'HI_RES')
        titulo = a.get('title') or ''
        titulo = _con_version(titulo, a.get('version'))
        cover = a.get('cover')
        artista = (a.get('artist') or {}).get('name') or ', '.join(x.get('name', '') for x in a.get('artists') or [])
        return {'servicio': 'tidal', 'id': str(a['id']), 'titulo': titulo, 'artista': artista,
                'anio': (a.get('releaseDate') or '')[:4] or None, 'n_pistas': a.get('numberOfTracks'),
                'duracion': a.get('duration'),
                # sin Hi-Res, Tidal entrega 16/44.1; con Hi-Res, la frecuencia se averigua aparte (resolucion())
                'bits': None if hires else (16 if lossless else None), 'sr': None if hires else (44100 if lossless else None),
                'hires': hires, 'lossless': lossless, 'upc': a.get('upc'), 'sello': a.get('copyright'),
                'portada': f'https://resources.tidal.com/images/{cover.replace("-", "/")}/320x320.jpg' if cover else None,
                'tipo': self._tipo(a), 'explicito': bool(a.get('explicit')),
                'url': f'https://tidal.com/browse/album/{a["id"]}',
                # solo Dolby Atmos (sin versión estéreo sin pérdida): no se ofrece
                'disponible': a.get('streamReady', True) is not False and not (tags and set(tags) <= {'DOLBY_ATMOS'}),
                'resolucion_exacta': not hires}

    def buscar_albumes(self, q, limite=30):
        d = self._get('search', query=q, types='ALBUMS', limit=limite)
        return [self._album(a) for a in (d.get('albums') or {}).get('items', [])]

    def buscar_artista(self, q):
        d = self._get('search', query=q, types='ARTISTS', limit=5)
        artistas = (d.get('artists') or {}).get('items', [])
        if not artistas:
            return None, []
        art = max(artistas, key=lambda a: (_norm(a.get('name')) == _norm(q), a.get('popularity') or 0))
        out, vistos = [], set()
        for filtro in (None, 'EPSANDSINGLES', 'COMPILATIONS'):
            params = {'limit': 100}
            if filtro:
                params['filter'] = filtro
            try:
                items = self._get(f'artists/{art["id"]}/albums', **params).get('items', [])
            except urllib.error.HTTPError:
                continue
            for a in items:
                if a['id'] not in vistos:
                    vistos.add(a['id'])
                    out.append(self._album(a))
        return art.get('name'), out

    def pistas(self, album_id):
        d = self._get(f'albums/{album_id}/tracks', limit=200)
        return [{'id': str(t['id']), 'titulo': t.get('title') or '', 'numero': t.get('trackNumber'),
                 'disco': t.get('volumeNumber') or 1, 'duracion': t.get('duration')} for t in d.get('items', [])]

    def resolucion(self, album_id):
        """(bits, frecuencia) que entregaría Tidal, de una pista del álbum — sin descargar nada."""
        pistas = self._get(f'albums/{album_id}/tracks', limit=1).get('items', [])
        if not pistas:
            return None, None
        p = self._get(f'tracks/{pistas[0]["id"]}/playbackinfopostpaywall', audioquality='HI_RES_LOSSLESS',
                      playbackmode='STREAM', assetpresentation='FULL')
        bits, sr = p.get('bitDepth'), p.get('sampleRate')
        if not sr and p.get('manifest'):
            try:
                mpd = base64.b64decode(p['manifest']).decode('utf-8', 'replace')
                m = re.search(r'audioSamplingRate="(\d+)"', mpd)
                sr = int(m.group(1)) if m else None
            except Exception:
                pass
        if not bits:
            bits = 24 if p.get('audioQuality') == 'HI_RES_LOSSLESS' else 16
        return bits, sr


# ─────────────────────────── clientes (una vez por proceso, se rehacen si fallan) ───────────────────────────

_clientes = {}
_lock_clientes = threading.Lock()


def cliente(servicio):
    with _lock_clientes:
        if servicio not in _clientes:
            _clientes[servicio] = (Tidal if servicio == 'tidal' else Qobuz)()
        return _clientes[servicio]


def olvidar(servicio):
    with _lock_clientes:
        _clientes.pop(servicio, None)


def estado():
    out = {}
    for s, exe in (('tidal', 'tidal-dl-ng'), ('qobuz', 'rip')):
        e = {'herramienta': bool(herramienta(exe)), 'destino': carpeta_destino(s)}
        try:
            cliente(s)
            e['sesion'] = True
        except Exception as ex:
            olvidar(s)
            e['sesion'], e['error'] = False, str(ex)
        out[s] = e
    return out


# ─────────────────────────── búsqueda: agrupar versiones y comparar con la biblioteca ───────────────────────────

def _rango(v):
    """Para comparar calidad: lossless primero, luego bits y frecuencia."""
    return (1 if (v.get('bits') or v.get('lossless') or v['servicio'] == 'qobuz') else 0,
            v.get('bits') or 0, v.get('sr') or 0)


def _clave(v):
    upc = re.sub(r'\D', '', v.get('upc') or '').lstrip('0')
    if upc:
        return 'upc:' + upc
    return 'n:' + '|'.join([_norm(v['artista']), _norm(v['titulo']), v.get('anio') or '', str(v.get('n_pistas') or '')])


def _tag_lib(g):
    return g.get('tag') or ''


def _en_biblioteca(bib, artista, titulo):
    if not bib:
        return None
    for d in analisis.buscar_duplicados(bib, artista, titulo)[:1]:
        g = next((x for x in bib['grupos'] if x['ruta'] == d['ruta']), {})
        return {'ruta': d['ruta'], 'album': d['album'], 'tag': d.get('tag'), 'formato': d.get('formato'),
                'pistas': d.get('pistas'), 'sr': g.get('sr') or None, 'similitud': d.get('similitud')}
    return None


def agrupar(versiones, bib):
    grupos = {}
    for v in versiones:
        if not v.get('disponible', True):
            continue
        k = _clave(v)
        g = grupos.get(k)
        if not g:
            # segundo intento: mismo artista, título y año (los UPC a veces difieren entre servicios)
            alt = 'n:' + '|'.join([_norm(v['artista']), _norm(v['titulo']), v.get('anio') or ''])
            g = next((x for x in grupos.values() if x['_alt'] == alt and v['servicio'] not in x['servicios']), None)
            if g is None:
                g = grupos[k] = {'clave': k, '_alt': alt, 'artista': v['artista'], 'titulo': v['titulo'],
                                 'anio': v.get('anio'), 'n_pistas': v.get('n_pistas'), 'tipo': v['tipo'],
                                 'portada': v.get('portada'), 'versiones': [], 'servicios': []}
        g['versiones'].append(v)
        g['servicios'].append(v['servicio'])
        g['n_pistas'] = max(g['n_pistas'] or 0, v.get('n_pistas') or 0) or None
        g['portada'] = g['portada'] or v.get('portada')
    out = []
    for g in grupos.values():
        g.pop('_alt')
        g['versiones'].sort(key=lambda v: v['servicio'] != 'qobuz')
        g['biblioteca'] = _en_biblioteca(bib, g['artista'], g['titulo'])
        sugerir(g)
        out.append(g)
    return out


def sugerir(g):
    """Marca la versión de mejor calidad (si hay una sola mejor) y si mejora lo que ya tienes."""
    vs = g['versiones']
    pendientes = [v for v in vs if not v.get('resolucion_exacta')]
    for v in vs:
        v['sugerida'] = False
        v['misma_calidad'] = False
    g['sugerencia'] = None
    if pendientes:
        g['sugerencia'] = 'Averiguando la resolución exacta de Tidal…'
    else:
        mejor = max(_rango(v) for v in vs)
        top = [v for v in vs if _rango(v) == mejor]
        if len(top) == 1 and len(vs) > 1:
            top[0]['sugerida'] = True
            g['sugerencia'] = f'Mejor calidad en {top[0]["servicio"].capitalize()}'
        elif len(top) > 1:
            for v in top:
                v['misma_calidad'] = True
            g['sugerencia'] = 'Misma resolución en ambos: «Medir master» dice si el master es el mismo'
        elif len(vs) == 1:
            vs[0]['sugerida'] = True
    lib = g.get('biblioteca')
    for v in vs:
        v['mejora'] = None
        if lib and v.get('sr'):
            tag = _tag_lib(lib)
            if tag in ('DSD', 'MQA'):
                v['mejora'] = 'no'
            elif tag == 'Lossy':
                v['mejora'] = 'si'
            elif lib.get('sr'):
                v['mejora'] = 'si' if v['sr'] > lib['sr'] else ('igual' if v['sr'] == lib['sr'] else 'no')
    return g


def buscar(texto, modo, bib):
    """{'artista': nombre|None, 'grupos': [...], 'errores': {servicio: texto}}"""
    versiones, errores, nombre = [], {}, None
    for s in ('qobuz', 'tidal'):
        try:
            c = cliente(s)
            if modo == 'artista':
                n, vs = c.buscar_artista(texto)
                nombre = nombre or n
            else:
                vs = c.buscar_albumes(texto)
            versiones += vs
        except FalloTienda as e:
            olvidar(s)
            errores[s] = str(e)
        except Exception as e:  # noqa: BLE001
            olvidar(s)
            errores[s] = f'{type(e).__name__}: {e}'
            log.warning('Buscar en %s: %s', s, e)
    # la sesión de Tidal puede estar limitada a AAC aunque el catálogo diga sin pérdida: se pregunta una vez
    sin_perdida_tidal = next((v for v in versiones if v['servicio'] == 'tidal' and v.get('lossless')), None)
    if sin_perdida_tidal and 'tidal' not in errores:
        try:
            real = cliente('tidal').calidad_real(sin_perdida_tidal['id'])
        except Exception as e:  # noqa: BLE001
            real = None
            log.warning('Calidad real de Tidal: %s', e)
        if real in ('HIGH', 'LOW'):
            errores['tidal'] = ('tu sesión solo recibe AAC 320 (con pérdida), aunque el disco exista sin pérdida. '
                                'Inicia la sesión PKCE en el servidor: python3 tools/metadata/tidal_calidad.py '
                                'URL_DE_UN_ALBUM --login-pkce')
            for v in versiones:
                if v['servicio'] == 'tidal':
                    v.update(lossless=False, hires=False, bits=None, sr=None, resolucion_exacta=True, aac=True)
    if modo == 'artista' and nombre:
        versiones = [v for v in versiones if _norm(nombre) in _norm(v['artista']) or _norm(v['artista']) in _norm(nombre)]
    grupos = agrupar(versiones, bib)
    if modo == 'artista':
        orden = {'album': 0, 'ep': 1, 'recopilatorio': 2, 'en_vivo': 3}
        grupos.sort(key=lambda g: (orden.get(g['tipo'], 9), -(int(g['anio'] or 0))))
    return {'artista': nombre, 'grupos': grupos, 'errores': errores}


def resolucion_tidal(almacen, ids):
    """{id: {'bits', 'sr'}} con caché (el dato no cambia)."""
    out, faltan = {}, []
    for i in ids:
        c = almacen.sugerencia(f'tienda:res:tidal:{i}', VERSION_CACHE)
        if c:
            out[i] = c
        else:
            faltan.append(i)
    if faltan:
        t = cliente('tidal')
        from concurrent.futures import ThreadPoolExecutor
        def una(i):
            try:
                bits, sr = t.resolucion(i)
                return i, {'bits': bits, 'sr': sr}
            except Exception as e:  # noqa: BLE001
                return i, {'error': str(e)}
        with ThreadPoolExecutor(max_workers=3) as ex:
            for i, r in ex.map(una, faltan[:40]):
                out[i] = r
                if 'error' not in r and r.get('sr'):
                    almacen.guardar_sugerencia(f'tienda:res:tidal:{i}', VERSION_CACHE, r)
    return out


# ─────────────────────────── «Medir master»: la misma pista de cada servicio ───────────────────────────

_mediciones = {}            # clave → {'estado', 'texto', 'resultado'}
_lock_medir = threading.Lock()


def clave_medicion(tidal_id, qobuz_id):
    return f'tienda:medir:{tidal_id}:{qobuz_id}'


def _correr(cmd, env=None, timeout=900, cwd=None):
    r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
                       timeout=timeout, env=env or _entorno(), cwd=cwd)
    return r.returncode, ANSI.sub('', (r.stdout or '') + (r.stderr or ''))


def _bajar_tidal_a(url, carpeta, casa):
    """tidal-dl-ng con una copia de su configuración que descarga a `carpeta` (la sesión es la tuya, enlazada)."""
    origen = dir_tidal()
    exe = herramienta('tidal-dl-ng')
    if not origen or not exe:
        raise FalloTienda('No se encontró tidal-dl-ng o su configuración')
    cfg = os.path.join(casa, '.config', os.path.basename(origen))
    os.makedirs(cfg, exist_ok=True)
    aj = ajustes_tidal()
    aj.update({'download_base_path': carpeta, 'format_track': 'Tracks/{track_title}', 'skip_existing': False,
               'downloads_concurrent_max': 1, 'lyrics_embed': False, 'video_download': False})
    with open(os.path.join(cfg, 'settings.json'), 'w', encoding='utf-8') as f:
        json.dump(aj, f)
    os.symlink(os.path.join(origen, 'token.json'), os.path.join(cfg, 'token.json'))
    # HOME apunta a la casa temporal (su configuración y su historial, no los tuyos), pero Python debe seguir
    # encontrando tidal-dl-ng instalado con «pip --user» en tu ~/.local
    env = _entorno({'HOME': casa, 'XDG_CONFIG_HOME': os.path.join(casa, '.config'),
                    'PYTHONUSERBASE': os.environ.get('PYTHONUSERBASE') or _home('.local')})
    return _correr([exe, 'dl', url], env=env,
                   timeout=600, cwd=casa)


def _bajar_qobuz_a(url, carpeta):
    exe = herramienta('rip')
    if not exe:
        raise FalloTienda('No se encontró rip (streamrip)')
    return _correr([exe, '--no-db', '--no-progress', '--folder', carpeta, 'url', url], timeout=600)


def _medir_archivo(ruta):
    from . import repetidos
    lufs, lra, pico = repetidos._medir_pista(ruta)
    bits = sr = None
    try:
        import mutagen
        f = mutagen.File(ruta)
        sr = getattr(f.info, 'sample_rate', None)
        bits = getattr(f.info, 'bits_per_sample', None)
    except Exception:
        pass
    return {'lufs': round(lufs, 1) if lufs is not None else None, 'lra': lra, 'pico': pico,
            'plr': round(pico - lufs, 1) if (pico is not None and lufs is not None) else None,
            'bits': bits, 'sr': sr}


def _veredicto(t, q):
    """Por compresión (PLR = pico real − volumen): menos de 0,5 dB de diferencia = mismo master."""
    if t.get('plr') is None or q.get('plr') is None:
        return None, 'No se pudo medir una de las dos'
    dplr, dlufs = q['plr'] - t['plr'], q['lufs'] - t['lufs']
    res_t, res_q = (t.get('bits') or 0, t.get('sr') or 0), (q.get('bits') or 0, q.get('sr') or 0)
    if abs(dplr) < 0.5:
        base = 'Mismo master' if abs(dlufs) < 0.5 else \
            f'Misma compresión con {abs(dlufs):.1f} dB de diferencia de volumen (probablemente el mismo master)'
        if res_t != res_q:
            mejor = 'tidal' if res_t > res_q else 'qobuz'
            return mejor, f'{base}; {mejor.capitalize()} tiene más resolución'
        return None, f'{base} y misma resolución: da igual cuál'
    mejor = 'qobuz' if dplr > 0 else 'tidal'
    grado = 'claramente menos comprimido' if abs(dplr) >= 1.5 else 'algo menos comprimido'
    return mejor, (f'Masters distintos: {mejor.capitalize()} está {grado} '
                   f'(PLR {max(t["plr"], q["plr"])} vs {min(t["plr"], q["plr"])} dB)')


def medir(almacen, datos, tidal_id, qobuz_id):
    """Arranca (en segundo plano) o devuelve la medición de la misma pista en Tidal y en Qobuz."""
    clave = clave_medicion(tidal_id, qobuz_id)
    guardada = almacen.sugerencia(clave, VERSION_CACHE)
    if guardada:
        return dict(guardada, estado='listo')
    with _lock_medir:
        st = _mediciones.get(clave)
        if st and st['estado'] in ('midiendo', 'error'):
            return st
        st = _mediciones[clave] = {'estado': 'midiendo', 'texto': 'Eligiendo una pista…'}

    def trabajo():
        tmp = os.path.join(datos, 'medir', f'{int(time.time())}-{tidal_id}-{qobuz_id}')
        try:
            pt, pq = cliente('tidal').pistas(tidal_id), cliente('qobuz').pistas(qobuz_id)
            pares = [(a, b) for a, b in zip(pt, pq) if _norm(a['titulo'])[:20] == _norm(b['titulo'])[:20]] \
                or list(zip(pt, pq))
            if not pares:
                raise FalloTienda('No hay pistas para comparar')
            a, b = random.choice(pares)
            st['texto'] = f'Descargando «{a["titulo"]}» de Tidal…'
            preparar_tidal()
            os.makedirs(os.path.join(tmp, 't'), exist_ok=True)
            rc, sal = _bajar_tidal_a(f'https://tidal.com/browse/track/{a["id"]}', os.path.join(tmp, 't'), os.path.join(tmp, 'casa'))
            ft = _audios(os.path.join(tmp, 't'))
            if not ft:
                raise FalloTienda('Tidal no descargó la pista de prueba: ' + (sal.strip().splitlines() or ['sin salida'])[-1])
            st['texto'] = f'Descargando «{b["titulo"]}» de Qobuz…'
            os.makedirs(os.path.join(tmp, 'q'), exist_ok=True)
            rc, sal = _bajar_qobuz_a(f'https://open.qobuz.com/track/{b["id"]}', os.path.join(tmp, 'q'))
            fq = _audios(os.path.join(tmp, 'q'))
            if not fq:
                raise FalloTienda('Qobuz no descargó la pista de prueba: ' + (sal.strip().splitlines() or ['sin salida'])[-1])
            st['texto'] = 'Midiendo…'
            mt, mq = _medir_archivo(ft[0]), _medir_archivo(fq[0])
            mejor, texto = _veredicto(mt, mq)
            res = {'pista': a['titulo'], 'tidal': mt, 'qobuz': mq, 'mejor': mejor, 'texto': texto, 'ts': time.time()}
            almacen.guardar_sugerencia(clave, VERSION_CACHE, res)
            st.update(estado='listo', **res)
        except Exception as e:  # noqa: BLE001
            log.warning('Medir master %s: %s', clave, e)
            st.update(estado='error', texto=str(e) if isinstance(e, FalloTienda) else f'{type(e).__name__}: {e}')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)          # las pistas de prueba no quedan en ningún lado

    threading.Thread(target=trabajo, daemon=True, name='medir-master').start()
    return st


def olvidar_error_medicion(tidal_id, qobuz_id):
    with _lock_medir:
        st = _mediciones.get(clave_medicion(tidal_id, qobuz_id))
        if st and st['estado'] == 'error':
            _mediciones.pop(clave_medicion(tidal_id, qobuz_id), None)


# ─────────────────────────── cola de descargas ───────────────────────────

class Descargador(threading.Thread):
    """Una descarga a la vez. Mientras baja, el vigilante no revisa la carpeta destino (pausa_rutas)."""

    def __init__(self, almacen, vigilante):
        super().__init__(daemon=True, name='descargas')
        self.almacen = almacen
        self.vigilante = vigilante
        self.despertar = threading.Event()
        self.proc = None
        self.actual = None
        self.cancelar = False

    def ya(self):
        self.despertar.set()

    def cancelar_actual(self, did):
        if self.actual == did and self.proc:
            self.cancelar = True
            try:
                self.proc.terminate()
            except Exception:
                pass
            return True
        return False

    def run(self):
        self.almacen.reiniciar_descargas()
        while True:
            d = self.almacen.siguiente_descarga()
            if not d:
                self.despertar.wait(30)
                self.despertar.clear()
                continue
            try:
                self._una(d)
            except Exception as e:  # noqa: BLE001
                log.error('Descarga %s: %s', d['id'], e)
                self.almacen.actualizar_descarga(d['id'], estado='error', progreso=None, fin=time.time(),
                                                 error=str(e) if isinstance(e, FalloTienda) else f'{type(e).__name__}: {e}')
            finally:
                self.actual, self.proc, self.cancelar = None, None, False
                self.vigilante.pausa_rutas = set()
                self.vigilante.ya()
            if self.almacen.siguiente_descarga():
                time.sleep(random.uniform(*PAUSA_ENTRE_ALBUMES))

    @staticmethod
    def _foto(base):
        out = {}
        try:
            for n in os.listdir(base):
                p = os.path.join(base, n)
                if os.path.isdir(p):
                    out[p] = os.path.getmtime(p)
        except OSError:
            pass
        return out

    def _una(self, d):
        did = d['id']
        self.actual, self.cancelar = did, False
        base = carpeta_destino(d['servicio'])
        exe = herramienta('tidal-dl-ng' if d['servicio'] == 'tidal' else 'rip')
        if not exe:
            raise FalloTienda('No se encontró ' + ('tidal-dl-ng' if d['servicio'] == 'tidal' else 'rip'))
        os.makedirs(base, exist_ok=True)
        self.vigilante.pausa_rutas = {base.rstrip('/')}
        antes = self._foto(base)
        inicio = time.time()
        self.almacen.actualizar_descarga(did, estado='descargando', inicio=inicio, progreso='Empezando…', error=None)
        lineas = []
        if d['servicio'] == 'tidal':
            self.almacen.actualizar_descarga(did, progreso='Renovando la sesión de Tidal…')
            n_hist = preparar_tidal(d.get('item_id'))
            lineas.append('Sesión PKCE renovada' + (f' · {n_hist} pista(s) quitadas del historial de tidal-dl-ng'
                                                    if n_hist else ''))
        cmd = [exe, 'dl', d['url']] if d['servicio'] == 'tidal' else [exe, '--no-db', '--no-progress', 'url', d['url']]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, errors='replace', env=_entorno(), cwd=base)

        def leer():
            for linea in self.proc.stdout:
                linea = ANSI.sub('', linea).strip()
                if linea:
                    lineas.append(linea)
                    del lineas[:-300]
        hilo = threading.Thread(target=leer, daemon=True)
        hilo.start()

        def nuevas():
            ahora = self._foto(base)
            return [p for p, m in ahora.items() if p not in antes or m > antes[p] + 1 or m >= inicio - 1]

        while self.proc.poll() is None:
            time.sleep(2)
            n = sum(len(_audios(p)) for p in nuevas())
            total = d.get('n_pistas') or 0
            self.almacen.actualizar_descarga(did, progreso=f'Descargando · pista {n} de {total}' if total else
                                             f'Descargando · {n} pista(s)', registro='\n'.join(lineas[-80:]))
        hilo.join(timeout=5)
        rc = self.proc.returncode
        carpetas = [p for p in nuevas() if _audios(p)]
        recien = [p for p in nuevas() if p not in antes]
        if self.cancelar:
            for p in recien:                         # lo que alcanzó a bajar no debe revisarse como álbum incompleto
                shutil.rmtree(p, ignore_errors=True)
            self.almacen.actualizar_descarga(did, estado='cancelada', progreso=None, fin=time.time(),
                                             registro='\n'.join(lineas[-80:]))
            return
        if rc != 0:
            for p in recien:
                shutil.rmtree(p, ignore_errors=True)
            ultima = next((x for x in reversed(lineas) if x), f'código {rc}')
            raise FalloTienda(f'La herramienta terminó con error: {ultima}')
        if not carpetas:
            pista = ('¿ya estaba en el historial de tidal-dl-ng?' if d['servicio'] == 'tidal'
                     else 'revisa la sesión de Qobuz')
            self.almacen.actualizar_descarga(did, estado='error', progreso=None, fin=time.time(),
                                             error=f'No se descargó nada ({pista})', registro='\n'.join(lineas[-80:]))
            return
        pedida_con_perdida = bool(re.search(r'lossy|aac|mp3', d.get('calidad') or '', re.I))
        if not pedida_con_perdida:
            llegados = {codec(sorted(_audios(p))[0]) for p in carpetas}
            malos = sorted(c for c in llegados if c in CODECS_CON_PERDIDA)
            if malos:
                for p in carpetas + recien:          # no debe llegar a la Bandeja como si fuera la versión sin pérdida
                    shutil.rmtree(p, ignore_errors=True)
                raise FalloTienda(f'{d["servicio"].capitalize()} entregó {"/".join(m.upper() for m in malos)} '
                                  f'(con pérdida) en vez de {d.get("calidad") or "sin pérdida"}: se borró lo descargado. '
                                  + ('Revisa la sesión PKCE: python3 tools/metadata/tidal_calidad.py URL_DEL_ALBUM'
                                     if d['servicio'] == 'tidal' else 'Revisa la calidad configurada en streamrip.'))
            lineas.append('Formato verificado: ' + ', '.join(sorted(c or '?' for c in llegados)))
        n = sum(len(_audios(p)) for p in carpetas)
        self.almacen.actualizar_descarga(did, estado='terminada', fin=time.time(), carpetas=json.dumps(carpetas),
                                         progreso=f'{n} pista(s) descargadas', registro='\n'.join(lineas[-80:]))
        self.almacen.avisar('ok', f'Descargado: {d["artista"]} — {d["album"]}',
                            f'{n} pista(s) desde {d["servicio"].capitalize()}; pasa a la Bandeja')
