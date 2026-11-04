"""
Orbyte Ingesta — qué discos de un artista son los más reconocidos (Buscar → Artista).

score (0–100) = 0,7 · Last.fm + 0,3 · Tidal        (si solo responde uno, ese vale el 100 %)

  Last.fm  50 % oyentes del disco · 30 % sus pistas en el top 50 del artista (oyentes de esas pistas)
           · 20 % reproducciones del disco
  Tidal    50 % popularidad del disco (0–100) · 50 % sus pistas en las más escuchadas del artista
           (más peso cuanto más arriba)

Cada valor se compara con el máximo del artista en escala logarítmica (un mega éxito no aplasta al resto).
Si a un componente le falta el dato en todo el artista, su peso se reparte entre los que sí hay.
Last.fm se guarda 7 días en el almacén (sugerencias_ia), por artista.
"""
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import analisis

log = logging.getLogger('ingesta.popularidad')

VERSION = 'pop1'
LFM = 'https://ws.audioscrobbler.com/2.0/?'
UA = 'OrbyteIngesta/1.0'


def _firma():
    return f'{VERSION}:{int(time.time() // (7 * 86400))}'          # cambia cada 7 días


def _lfm(metodo, clave, **params):
    url = LFM + urllib.parse.urlencode(dict(params, method=metodo, api_key=clave, format='json', autocorrect=1))
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def lastfm(almacen, artista):
    """{'albumes': {norm: {'titulo','oyentes','reproducciones'}}, 'pistas': [{'titulo','album','oyentes'}]} o None."""
    clave = os.environ.get('LASTFM_API_KEY')
    if not clave or not artista:
        return None
    ck = f'pop:lfm:{analisis._norm(artista)}'
    guardado = almacen.sugerencia(ck, _firma())
    if guardado is not None:
        return guardado
    try:
        top = (_lfm('artist.gettopalbums', clave, artist=artista, limit=60).get('topalbums') or {}).get('album') or []
        pistas = (_lfm('artist.gettoptracks', clave, artist=artista, limit=50).get('toptracks') or {}).get('track') or []
    except Exception as e:  # noqa: BLE001
        log.warning('Last.fm %s: %s', artista, e)
        return None

    def info_album(a):
        try:
            d = _lfm('album.getinfo', clave, artist=artista, album=a['name']).get('album') or {}
            return a['name'], _int(d.get('listeners')), _int(d.get('playcount') or a.get('playcount'))
        except Exception:  # noqa: BLE001
            return a['name'], 0, _int(a.get('playcount'))

    def info_pista(t):
        try:
            d = _lfm('track.getinfo', clave, artist=artista, track=t['name']).get('track') or {}
            return t['name'], (d.get('album') or {}).get('title'), _int(d.get('listeners') or t.get('listeners'))
        except Exception:  # noqa: BLE001
            return t['name'], None, _int(t.get('listeners'))

    with ThreadPoolExecutor(max_workers=5) as ex:
        albumes = list(ex.map(info_album, [a for a in top if a.get('name') and a['name'] != '(null)'][:40]))
        detalle = list(ex.map(info_pista, [t for t in pistas if t.get('name')]))
    datos = {'albumes': {}, 'pistas': []}
    for nombre, oy, rep in albumes:
        k = analisis._norm_album(nombre)
        if k and (k not in datos['albumes'] or oy > datos['albumes'][k]['oyentes']):
            datos['albumes'][k] = {'titulo': nombre, 'oyentes': oy, 'reproducciones': rep}
    for nombre, album, oy in detalle:
        datos['pistas'].append({'titulo': nombre, 'album': album, 'oyentes': oy})
    almacen.guardar_sugerencia(ck, _firma(), datos)
    return datos


def tidal_top(cliente_tidal, artista_id):
    """[(album_id, puesto 1..n, titulo de la pista)] de las pistas más escuchadas del artista en Tidal."""
    if not artista_id:
        return []
    try:
        items = cliente_tidal._get(f'artists/{artista_id}/toptracks', limit=50).get('items', [])
    except Exception as e:  # noqa: BLE001
        log.warning('Tidal top %s: %s', artista_id, e)
        return []
    return [(str((t.get('album') or {}).get('id')), i + 1, t.get('title')) for i, t in enumerate(items) if t.get('album')]


def _log_norm(v, maximo):
    return math.log1p(v) / math.log1p(maximo) if maximo and v else 0.0


def _ponderado(partes):
    """partes: [(peso, valor 0..1 | None)]: promedio ponderado con los que existen (None = sin dato en el artista)."""
    hay = [(p, v) for p, v in partes if v is not None]
    total = sum(p for p, _ in hay)
    return 100 * sum(p * v for p, v in hay) / total if total else None


def anotar(grupos, lfm, top_tidal, estrellas=5):
    """Agrega g['pop'] a cada grupo y el puesto dentro de su sección (tipo)."""
    lfm = lfm or {'albumes': {}, 'pistas': []}
    # pistas del top de Last.fm por álbum (por el álbum que informa Last.fm)
    pistas_lfm = {}
    for p in lfm['pistas']:
        if p.get('album'):
            k = analisis._norm_album(p['album'])
            pistas_lfm.setdefault(k, []).append(p)
    top_por_album = {}
    for alb, puesto, _t in top_tidal:
        top_por_album.setdefault(alb, []).append(puesto)
    n_top = max((p for _a, p, _t in top_tidal), default=50)

    def clave_en(titulo, claves):
        k = analisis._norm_album(titulo)
        if k in claves:
            return k
        for kk in claves:                             # «Ghostlights» ↔ «Ghostlights (Deluxe Edition)» ya normalizados
            if kk and k and (kk in k or k in kk) and min(len(kk), len(k)) >= 4:
                return kk
        return None

    def buscar_lfm(titulo):
        ka, kp = clave_en(titulo, lfm['albumes']), clave_en(titulo, pistas_lfm)
        return (lfm['albumes'][ka] if ka else None), (pistas_lfm[kp] if kp else [])

    for g in grupos:
        a, ps = buscar_lfm(g['titulo'])
        tidal = [v for v in g['versiones'] if v['servicio'] == 'tidal']
        g['_pop'] = {
            'oyentes': a['oyentes'] if a else None,
            'reproducciones': a['reproducciones'] if a else None,
            'pistas_top': len(ps),
            'oyentes_top': sum(p['oyentes'] for p in ps),
            'pop_tidal': max((v['popularidad'] for v in tidal if v.get('popularidad') is not None), default=None),
            'top_tidal': sum((n_top + 1 - p) / n_top for v in tidal for p in top_por_album.get(v['id'], [])),
            'pistas_top_tidal': sum(len(top_por_album.get(v['id'], [])) for v in tidal),
        }
    mx = {c: max((g['_pop'][c] or 0 for g in grupos), default=0)
          for c in ('oyentes', 'reproducciones', 'oyentes_top', 'top_tidal')}
    for g in grupos:
        p = g.pop('_pop')
        s_lfm = None
        if p['oyentes'] is not None or p['pistas_top']:
            s_lfm = _ponderado([(0.5, _log_norm(p['oyentes'] or 0, mx['oyentes']) if mx['oyentes'] else None),
                                (0.3, _log_norm(p['oyentes_top'], mx['oyentes_top']) if mx['oyentes_top'] else None),
                                (0.2, _log_norm(p['reproducciones'] or 0, mx['reproducciones']) if mx['reproducciones'] else None)])
        s_tidal = None
        if p['pop_tidal'] is not None or p['top_tidal']:
            s_tidal = _ponderado([(0.5, p['pop_tidal'] / 100 if p['pop_tidal'] is not None else None),
                                  (0.5, (p['top_tidal'] / mx['top_tidal']) if mx['top_tidal'] else None)])
        if s_lfm is not None and s_tidal is not None:
            score = 0.7 * s_lfm + 0.3 * s_tidal
        else:
            score = s_lfm if s_lfm is not None else s_tidal
        g['pop'] = {'score': round(score, 1) if score is not None else None,
                    'oyentes': p['oyentes'], 'reproducciones': p['reproducciones'],
                    'pistas_top': max(p['pistas_top'], p['pistas_top_tidal']),
                    'fuentes': [n for n, s in (('Last.fm', s_lfm), ('Tidal', s_tidal)) if s is not None],
                    'puesto': None, 'estrella': False}
    # puesto dentro de cada sección
    for tipo in {g['tipo'] for g in grupos}:
        sec = sorted([g for g in grupos if g['tipo'] == tipo and g['pop']['score'] is not None],
                     key=lambda g: -g['pop']['score'])
        for i, g in enumerate(sec):
            g['pop']['puesto'] = i + 1
            g['pop']['estrella'] = i < estrellas and g['pop']['score'] > 0
    return grupos
