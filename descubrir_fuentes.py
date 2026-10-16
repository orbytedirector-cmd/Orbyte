"""Ticket C-11 (Descubrir) — fuentes externas gratuitas, módulo aislado.

Solo consultas HTTP y normalización de respuestas; nada de base de datos.
app.py decide qué guardar. Todas las funciones devuelven estructuras chicas
(lo mínimo para un borrador) y nunca lanzan por errores de red: devuelven
None / [] y anotan el problema en `ultimo_error`.

Fuentes:
- Deezer (sin clave): búsqueda de artistas, discografía con fecha, artistas
  relacionados, tops.
- MusicBrainz (sin clave, máx. 1 consulta/s): fecha original de un disco.
- Wikimedia «On this day» (sin clave): nacimientos y muertes del día.
- Ticketmaster Discovery (clave gratuita TICKETMASTER_API_KEY): recitales en Chile.
- Last.fm (clave gratuita LASTFM_API_KEY): top de Chile.
"""
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request

UA = 'Orbyte/1.0 (servidor de musica personal; https://github.com/orbytedirector-cmd)'
TIMEOUT = 12

ultimo_error = {}          # fuente -> mensaje (para el estado del editor)
_ritmo = {}                # fuente -> último instante de consulta
_ritmo_lock = threading.Lock()
_PAUSA = {'deezer': 0.15, 'musicbrainz': 1.1, 'wikimedia': 0.3, 'ticketmaster': 0.25, 'lastfm': 0.25}

# Permite a los tests reemplazar la descarga por datos falsos.
obtener_json_impl = None


def _esperar(fuente):
    with _ritmo_lock:
        falta = _PAUSA.get(fuente, 0.2) - (time.time() - _ritmo.get(fuente, 0))
        if falta > 0:
            time.sleep(falta)
        _ritmo[fuente] = time.time()


def obtener_json(fuente, url):
    if obtener_json_impl:
        return obtener_json_impl(fuente, url)
    _esperar(fuente)
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
        ultimo_error.pop(fuente, None)
        return data
    except Exception as e:  # noqa: BLE001 - una fuente caída no debe romper la recolección
        ultimo_error[fuente] = f'{type(e).__name__}: {e}'[:200]
        return None


def normalizar(texto):
    """Para cotejar nombres: minúsculas, sin tildes, sin «(Deluxe…)»/«[…]»,
    sin «the » inicial ni signos."""
    if not texto:
        return ''
    t = unicodedata.normalize('NFKD', str(texto)).encode('ascii', 'ignore').decode('ascii').lower()
    t = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', t)
    t = re.sub(r'\s+-\s+(remaster|deluxe|edition|expanded|live|anniversary).*$', '', t)
    t = t.replace('&', ' and ')
    t = re.sub(r'[^a-z0-9]+', ' ', t).strip()
    if t.startswith('the '):
        t = t[4:]
    return t


# ---------------------------------------------------------------- Deezer

def deezer_buscar_artista(nombre):
    """id de Deezer del artista con el MISMO nombre (normalizado), o 0 si no hay."""
    data = obtener_json('deezer', 'https://api.deezer.com/search/artist?limit=5&q=' + urllib.parse.quote(nombre))
    if not data or 'data' not in data:
        return None
    objetivo = normalizar(nombre)
    for a in data.get('data') or []:
        if normalizar(a.get('name')) == objetivo:
            return int(a['id'])
    return 0


def deezer_discos(deezer_id):
    """[{id, titulo, fecha, tipo, portada, enlace}] (album/ep/single)."""
    data = obtener_json('deezer', f'https://api.deezer.com/artist/{int(deezer_id)}/albums?limit=50')
    if not data or 'data' not in data:
        return []
    out = []
    for a in data.get('data') or []:
        fecha = a.get('release_date') or ''
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', fecha):
            continue
        out.append({'id': a.get('id'), 'titulo': a.get('title') or '', 'fecha': fecha,
                    'tipo': (a.get('record_type') or 'album').lower(),
                    'portada': a.get('cover_xl') or a.get('cover_big') or a.get('cover_medium') or '',
                    'enlace': a.get('link') or ''})
    return out


def deezer_relacionados(deezer_id, limite=10):
    data = obtener_json('deezer', f'https://api.deezer.com/artist/{int(deezer_id)}/related?limit={int(limite)}')
    if not data or 'data' not in data:
        return []
    return [{'id': a.get('id'), 'nombre': a.get('name') or '', 'fans': a.get('nb_fan') or 0,
             'imagen': a.get('picture_xl') or a.get('picture_big') or a.get('picture_medium') or '',
             'enlace': a.get('link') or ''} for a in data.get('data') or []]


def deezer_top_discos(limite=10):
    data = obtener_json('deezer', f'https://api.deezer.com/chart/0/albums?limit={int(limite)}')
    if not data or 'data' not in data:
        return []
    return [{'titulo': a.get('title') or '', 'artista': (a.get('artist') or {}).get('name') or '',
             'portada': a.get('cover_big') or a.get('cover_medium') or '', 'enlace': a.get('link') or ''}
            for a in data.get('data') or []]


# ---------------------------------------------------------------- MusicBrainz

def musicbrainz_fecha_disco(artista, titulo):
    """Fecha original ('YYYY-MM-DD') del disco, '' si no se encontró con
    fecha completa, None si la consulta falló."""
    q = f'releasegroup:"{titulo}" AND artist:"{artista}"'
    data = obtener_json('musicbrainz', 'https://musicbrainz.org/ws/2/release-group/?fmt=json&limit=5&query='
                        + urllib.parse.quote(q))
    if data is None:
        return None
    objetivo, art = normalizar(titulo), normalizar(artista)
    for rg in data.get('release-groups') or []:
        if int(rg.get('score') or 0) < 85 or normalizar(rg.get('title')) != objetivo:
            continue
        credito = ' '.join(c.get('name', '') for c in rg.get('artist-credit') or [] if isinstance(c, dict))
        if art and art not in normalizar(credito):
            continue
        fecha = rg.get('first-release-date') or ''
        if re.match(r'^\d{4}-\d{2}-\d{2}$', fecha):
            return fecha
    return ''


# ---------------------------------------------------------------- Wikimedia

_PALABRAS_MUSICA = ('music', 'cantant', 'cantaut', 'compositor', 'guitarr', 'banda', 'rapero', 'pianist',
                    'bateri', 'bajista', 'productor', 'dj', 'violin', 'saxof', 'trompet', 'director de orquesta',
                    'vocalista', 'teclad', 'singer', 'songwriter', 'rock', 'jazz', 'rapper')


def wikimedia_efemerides(mes, dia, tipo):
    """tipo 'births' | 'deaths' → [{nombre, anio, descripcion, imagen, enlace}] solo músicos."""
    ruta = f'onthisday/{tipo}/{int(mes):02d}/{int(dia):02d}'
    data = obtener_json('wikimedia', f'https://api.wikimedia.org/feed/v1/wikipedia/es/{ruta}')
    if not data:   # respaldo: la API REST de la propia Wikipedia
        data = obtener_json('wikimedia', f'https://es.wikipedia.org/api/rest_v1/feed/{ruta}')
    if not data:
        return None
    out = []
    for e in data.get(tipo) or []:
        for p in (e.get('pages') or [])[:1]:
            desc = (p.get('description') or '') + ' ' + (e.get('text') or '')
            if not any(w in desc.lower() for w in _PALABRAS_MUSICA):
                continue
            titulos = p.get('titles') or {}
            nombre = titulos.get('normalized') or p.get('normalizedtitle') or p.get('title') or ''
            nombre = re.sub(r'\s*\([^)]*\)$', '', nombre.replace('_', ' '))
            out.append({'nombre': nombre, 'anio': e.get('year'), 'descripcion': p.get('description') or '',
                        'imagen': (p.get('thumbnail') or {}).get('source') or '',
                        'enlace': ((p.get('content_urls') or {}).get('desktop') or {}).get('page') or ''})
    return out


# ---------------------------------------------------------------- Ticketmaster (clave gratuita)

def ticketmaster_eventos(ciudad, clave):
    """Recitales de una ciudad de Chile: [{id, nombre, artistas, fecha, recinto, ciudad, enlace, imagen}]."""
    if not clave:
        return []
    url = ('https://app.ticketmaster.com/discovery/v2/events.json?countryCode=CL&classificationName=music'
           '&size=200&sort=date,asc&city=' + urllib.parse.quote(ciudad) + '&apikey=' + urllib.parse.quote(clave))
    data = obtener_json('ticketmaster', url)
    if not data:
        return []
    out = []
    for ev in ((data.get('_embedded') or {}).get('events') or []):
        emb = ev.get('_embedded') or {}
        venue = (emb.get('venues') or [{}])[0]
        imgs = sorted(ev.get('images') or [], key=lambda i: -(i.get('width') or 0))
        out.append({'id': ev.get('id'), 'nombre': ev.get('name') or '',
                    'artistas': [a.get('name') for a in emb.get('attractions') or [] if a.get('name')],
                    'fecha': ((ev.get('dates') or {}).get('start') or {}).get('localDate') or '',
                    'recinto': venue.get('name') or '', 'ciudad': (venue.get('city') or {}).get('name') or ciudad,
                    'enlace': ev.get('url') or '', 'imagen': imgs[0]['url'] if imgs else ''})
    return out


# ---------------------------------------------------------------- Last.fm (clave gratuita)

def lastfm_top_chile(clave, limite=10):
    if not clave:
        return []
    data = obtener_json('lastfm', 'https://ws.audioscrobbler.com/2.0/?method=geo.gettoptracks&country=chile&format=json'
                        f'&limit={int(limite)}&api_key=' + urllib.parse.quote(clave))
    if not data:
        return []
    return [{'titulo': t.get('name') or '', 'artista': (t.get('artist') or {}).get('name') or '',
             'enlace': t.get('url') or '', 'portada': ''}
            for t in ((data.get('tracks') or {}).get('track') or [])]


def claves():
    return {'ticketmaster': os.environ.get('TICKETMASTER_API_KEY', '').strip(),
            'lastfm': os.environ.get('LASTFM_API_KEY', '').strip()}
