"""
Orbyte Ingesta — reparar solo las pistas dañadas de un álbum (sin re-descargarlo entero ni reprocesarlo).

Si la pista está dañada desde el inicio y sus tags no se leen, artista y álbum salen de la biblioteca de Orbyte, de
las pistas sanas del álbum o del nombre de las carpetas; si ni así aparece, la ventana ofrece buscar a mano (texto
o enlace de Tidal) y elegir la pista. La metadata nueva se arma con la del álbum (pista sana) y la de la pista
(Orbyte y, si falta algo, Tidal).

Por pista dañada (FLAC):
  1. la misma edición en Tidal (código de barras si el álbum lo trae, si no artista + título, y la discografía del
     artista si la búsqueda no la trae) y la misma pista: disco + número, título y duración (±2 s);
  2. se baja SOLO esa pista; se verifica que se decodifique sin errores, que sea FLAC y que dure lo mismo;
  3. recibe la metadata de la pista vieja (todos los tags, carátula incrustada, letra); el ReplayGain de la pista se
     recalcula porque el audio es otro. Si su calidad es distinta a la del resto del álbum, su título lleva al final
     la calidad nueva, p. ej. «Loverman (CD-16/44.1)»;
  4. reemplaza a la vieja en el mismo lugar y con el mismo nombre (la vieja se borra recién ahí): para Orbyte es el
     mismo archivo, así que escuchas, favoritos y playlists no se tocan; solo se releen sus datos técnicos.

Se hace de a una pista, con pausa, y espera mientras haya una descarga en curso (misma sesión de Tidal). Los trabajos
quedan en ingesta_datos/salud.db: un reinicio retoma los que estaban en cola.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

from . import analisis, rescate, tienda
from . import salud as salud_mod

log = logging.getLogger('ingesta.reparar')

PAUSA_S = 5
# Una descarga que se corta o llega dañada («File is corrupt!») se reintenta: suele ser pasajero.
INTENTOS_DESCARGA = 3
PAUSA_REINTENTO_S = 20
# Pista elegida a mano que dura distinto de la vieja: hasta este margen se puede confirmar («Usar igual»);
# más allá no es la misma pista y se bloquea.
MAX_DIF_CONFIRMABLE_S = 10.0
_TIDAL_URL_RE = re.compile(r'tidal\.com/(?:browse/)?(album|track)/(\d+)', re.I)
# Tags de la pista (no del álbum): no se copian de una pista vecina.
_TAGS_DE_PISTA = ('title', 'tracknumber', 'isrc', 'lyrics', 'unsyncedlyrics', 'syncedlyrics', 'length', 'comment',
                  'musicbrainz_trackid', 'musicbrainz_releasetrackid', 'musicbrainz_recordingid', 'acoustid_id',
                  'acoustid_fingerprint', 'replaygain_track_gain', 'replaygain_track_peak', 'bpm', 'initialkey',
                  'composer', 'lyricist', 'artist', 'artists', 'artistsort', 'discnumber', 'part', 'version')
SUFIJO_RE = re.compile(r'\s*\((?:CD|HR)-\d+/[\d.]+\)\s*$')


class Fallo(Exception):
    pass


# ─────────────────────────── calidad ───────────────────────────

def calidad_archivo(ruta):
    """(bits, frecuencia) según ffprobe (la cabecera se lee aunque el audio esté dañado)."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                            'stream=sample_rate,bits_per_raw_sample,bits_per_sample', '-of', 'json', ruta],
                           capture_output=True, text=True, timeout=60)
        st = json.loads(r.stdout)['streams'][0]
        bits = int(st.get('bits_per_raw_sample') or st.get('bits_per_sample') or 0) or None
        return bits, int(st.get('sample_rate') or 0) or None
    except Exception:  # noqa: BLE001
        return None, None


def etiqueta(bits, sr):
    """«CD-16/44.1», «HR-24/96» (misma regla que el LED: más de 48 kHz es HR)."""
    if not sr:
        return None
    return f'{"HR" if sr > 48000 else "CD"}-{bits or "?"}/{sr / 1000:g}'


def _mayoria(pares):
    pares = [p for p in pares if p and p[1]]
    return max(set(pares), key=pares.count) if pares else (None, None)


def calidad_album(carpeta, db=None, excluir=()):
    """(bits, frecuencia) de la mayoría de las pistas del álbum (sin contar las dañadas)."""
    audios = [a for a in tienda._audios(carpeta) if os.path.realpath(a) not in excluir]
    pares = []
    if db and os.path.isfile(db):
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
        try:
            for k in range(0, len(audios), 500):
                parte = [os.path.realpath(a) for a in audios[k:k + 500]]
                pares += [(b, s) for b, s in con.execute(
                    f'SELECT bit_depth, sample_rate FROM tracks WHERE file_path IN ({",".join("?" * len(parte))})', parte)]
        finally:
            con.close()
    if len(pares) < min(len(audios), 3):          # no está en Orbyte (Bandeja): se mide una muestra
        pares = [calidad_archivo(a) for a in audios[:40]]
    return _mayoria(pares)


def calidad_tidal(pista_id):
    """(bits, frecuencia) que entregaría Tidal para esa pista, sin descargarla."""
    c = tienda.cliente('tidal')
    p = c._get(f'tracks/{pista_id}/playbackinfopostpaywall', audioquality='HI_RES_LOSSLESS', playbackmode='STREAM',
               assetpresentation='FULL')
    bits, sr = p.get('bitDepth'), p.get('sampleRate')
    if not sr and p.get('manifest'):
        try:
            import base64
            mpd = base64.b64decode(p['manifest']).decode('utf-8', 'replace')
            m = re.search(r'audioSamplingRate="(\d+)"', mpd)
            sr = int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            pass
    if not bits:
        bits = 24 if p.get('audioQuality') == 'HI_RES_LOSSLESS' else (16 if p.get('audioQuality') == 'LOSSLESS' else None)
    return bits, sr, p.get('audioQuality')


# ─────────────────────────── plan ───────────────────────────

def _tags(ruta):
    try:
        import mutagen
        f = mutagen.File(ruta, easy=True)
        t = (f.tags or {}) if f is not None else {}
        return {k: (t.get(k) or [None])[0] for k in ('artist', 'albumartist', 'album', 'barcode', 'upc')}
    except Exception:  # noqa: BLE001
        return {}


def _bd_pista(db, ruta):
    """Lo que la biblioteca de Orbyte guardó de la pista cuando se indexó (sirve si sus tags ya no se leen)."""
    if not db or not os.path.isfile(db):
        return {}
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute('SELECT t.title, t.artist, t.album_artist, t.track_number, t.disc_number, t.duration, t.isrc, '
                        'a.name AS album FROM tracks t LEFT JOIN albums a ON a.id = t.album_id WHERE t.file_path=?',
                        (ruta,)).fetchone()
    except sqlite3.Error:
        r = None
    finally:
        con.close()
    if not r:
        return {}
    return {'titulo': SUFIJO_RE.sub('', r['title'] or '') or None, 'artista': r['artist'] or None,
            'artista_album': r['album_artist'] or None, 'album': r['album'] or None,
            'numero': rescate._num(r['track_number']), 'disco': rescate._num(r['disc_number']) or 1,
            'duracion': r['duration'], 'isrc': r['isrc'] or None}


def _vecina(carpeta, excluir):
    """Una pista FLAC sana del mismo álbum con sus tags legibles (para los datos del álbum y la carátula)."""
    from mutagen.flac import FLAC
    for a in sorted(tienda._audios(carpeta)):
        if os.path.realpath(a) in excluir or not a.lower().endswith('.flac'):
            continue
        try:
            f = FLAC(a)
        except Exception:  # noqa: BLE001
            continue
        if f.tags is not None and (f.tags.get('album') or [''])[0]:
            return a
    return None


_ANIO_CALIDAD_RE = re.compile(r'\s*(?:\((?:19|20)\d{2}\)|\[[^\]]*\]|\((?:CD|HR|Hi-?Res|24[^)]*|16[^)]*)\))\s*$', re.I)


def _de_ruta(carpeta):
    """(artista, álbum) desde «…/B.B. King/Lucille (1968) [CD]»."""
    album = re.sub(r'^\s*(?:19|20)\d{2}\s*[-–]\s*', '', os.path.basename(carpeta.rstrip('/')))
    for _ in range(4):
        nuevo = _ANIO_CALIDAD_RE.sub('', album)
        if nuevo == album:
            break
        album = nuevo
    artista = os.path.basename(os.path.dirname(carpeta.rstrip('/')))
    return artista.strip(), album.strip()


_DESCONOCIDO_RE = re.compile(r'^\s*(?:unknown(?: artist| album)?|artista desconocido|desconocido|various artists?|va|n/?a|-)?\s*$', re.I)


def _valido(v):
    """Descarta los valores de relleno («Unknown Artist», «Various Artists», vacío)."""
    return v if v and not _DESCONOCIDO_RE.match(str(v)) else ''


def _datos_busqueda(rutas, carpeta, db):
    """Artista, álbum y código de barras para buscar en Tidal, y de dónde salió cada uno.
    Orden: tags de la pista → biblioteca de Orbyte → pistas sanas del álbum → nombre de las carpetas."""
    t = _tags(rutas[0])
    artista = _valido(t.get('albumartist')) or _valido(t.get('artist'))
    album = _valido(t.get('album'))
    upc = t.get('barcode') or t.get('upc')
    fuentes = []
    if artista or album:
        fuentes.append('tags de la pista')
    if not artista or not album:
        bd = _bd_pista(db, rutas[0])
        nuevo_art = '' if artista else (_valido(bd.get('artista_album')) or _valido(bd.get('artista')))
        nuevo_alb = '' if album else _valido(bd.get('album'))
        if nuevo_art or nuevo_alb:
            artista, album = artista or nuevo_art, album or nuevo_alb
            fuentes.append('biblioteca de Orbyte')
    vecina = _vecina(carpeta, set(rutas))
    if vecina and (not artista or not album or not upc):
        tv = _tags(vecina)
        usada = False
        if not artista and (_valido(tv.get('albumartist')) or _valido(tv.get('artist'))):
            artista, usada = _valido(tv.get('albumartist')) or _valido(tv.get('artist')), True
        if not album and _valido(tv.get('album')):
            album, usada = _valido(tv['album']), True
        if not upc and (tv.get('barcode') or tv.get('upc')):
            upc, usada = tv.get('barcode') or tv.get('upc'), True
        if usada:
            fuentes.append('pistas sanas del álbum')
    if not artista or not album:
        ra, rb = _de_ruta(carpeta)
        ra, rb = _valido(ra), _valido(rb)
        if (not artista and ra) or (not album and rb):
            artista, album = artista or ra, album or rb
            fuentes.append('nombre de las carpetas')
    return artista, album, upc, fuentes, vecina


def _ficha(ruta, db):
    """Ficha de la pista dañada (título, disco, número, duración); lo que sus tags no traen sale de Orbyte."""
    f = rescate.ficha(ruta)
    bd = _bd_pista(db, ruta)
    if bd:
        if (f.get('titulo_de_nombre') or not f.get('titulo')) and bd.get('titulo'):
            f['titulo'] = bd['titulo']
            f.pop('titulo_de_nombre', None)
        if bd.get('numero') and (not f.get('numero') or not f.get('codec')):
            f['numero'], f['disco'] = bd['numero'], bd.get('disco') or 1
        f['duracion'] = bd.get('duracion') or f.get('duracion')
    return f, bd


def _ediciones_de(busqueda):
    """Búsqueda manual: texto libre o enlace de Tidal (álbum o pista). → (ediciones, id de pista del enlace)."""
    c = tienda.cliente('tidal')
    m = _TIDAL_URL_RE.search(busqueda or '')
    if m:
        tipo, i = m.group(1).lower(), m.group(2)
        pista = None
        if tipo == 'track':
            t = c._get(f'tracks/{i}')
            pista, i = str(t['id']), str((t.get('album') or {}).get('id') or '')
            if not i:
                raise Fallo('Ese enlace de pista no trae su álbum')
        return [dict(c._album(c._get(f'albums/{i}')), como='enlace de Tidal')], pista
    return [dict(v, como='búsqueda manual') for v in c.buscar_albumes(busqueda, limite=25)], None


def _ediciones(artista, album, n_pistas, upc=None):
    c = tienda.cliente('tidal')
    cands = c.buscar_albumes(f'{artista} {album}', limite=25)
    if not any(v.get('n_pistas') == n_pistas and rescate._parecido(v.get('titulo'), album) >= 0.9 for v in cands):
        try:
            _n, disco = c.buscar_artista(artista)
            vistos = {v['id'] for v in cands}
            cands += [v for v in disco if v['id'] not in vistos]
        except Exception:  # noqa: BLE001
            pass
    upc = str(upc or '').lstrip('0') or None
    salida = []
    for v in cands:
        mismo_upc = bool(upc) and str(v.get('upc') or '').lstrip('0') == upc
        if mismo_upc or (rescate._parecido(v.get('titulo'), album) >= 0.85 and rescate._parecido(v.get('artista'), artista) >= 0.8):
            salida.append(dict(v, como='mismo código de barras' if mismo_upc else 'mismo artista y título'))
    salida.sort(key=lambda v: (v['como'] != 'mismo código de barras', abs((v.get('n_pistas') or 0) - (n_pistas or 0)),
                               not v.get('hires')))
    return salida


def plan(rutas, db=None, edicion_id=None, busqueda=None, elegir=None):
    """Qué se haría con cada pista dañada. `rutas`: archivos dañados (se resuelven los enlaces de una unión).
    `busqueda`: búsqueda manual en Tidal (texto o enlace); `elegir`: {ruta: id de pista de Tidal} elegidas a mano."""
    rutas = [os.path.realpath(r) for r in rutas if r]
    if not rutas:
        raise Fallo('No hay pistas para reparar')
    faltan = [r for r in rutas if not os.path.isfile(r)]
    if faltan:
        raise Fallo(f'No está el archivo {os.path.basename(faltan[0])}')
    elegir = {os.path.realpath(k): str(v) for k, v in (elegir or {}).items() if v}
    busqueda = (busqueda or '').strip() or None
    carpeta = analisis.carpeta_album(os.path.dirname(rutas[0]))
    n_pistas = len(tienda._audios(carpeta))
    artista, album, upc, fuentes, vecina = _datos_busqueda(rutas, carpeta, db)
    album_q = calidad_album(carpeta, db, excluir=set(rutas))
    base = {'carpeta': carpeta, 'artista': artista or '?', 'album': album or '?', 'n_pistas': n_pistas,
            'fuentes': fuentes, 'consulta': busqueda or f'{artista} {album}'.strip(), 'manual': bool(busqueda),
            'vecina': vecina,
            'calidad_album': etiqueta(*album_q) if album_q[1] else None, 'bits_album': album_q[0], 'sr_album': album_q[1]}
    pista_enlace = None
    try:
        tienda.preparar_tidal()                         # sesión PKCE renovada (la búsqueda también la usa)
        if busqueda:
            eds, pista_enlace = _ediciones_de(busqueda)
        elif artista and album:
            eds = _ediciones(artista, album, n_pistas, upc)
        else:
            eds = []
    except Fallo:
        raise
    except Exception as e:  # noqa: BLE001
        raise Fallo(f'No se pudo buscar en Tidal: {type(e).__name__}: {e}')
    if not eds:
        # Sin edición: la ventana ofrece la búsqueda manual en vez de un error.
        if busqueda:
            aviso = f'Tidal no encontró nada con «{busqueda}»'
        elif artista and album:
            aviso = f'Tidal no tiene «{artista} — {album}»: búscalo a mano'
        else:
            aviso = 'No se sabe el artista ni el álbum de la pista: búscalo a mano'
        return dict(base, edicion=None, ediciones=[], pistas_tidal=[], aviso=aviso,
                    pistas=[_item(r, carpeta, db, None, None, album_q) for r in rutas])
    ed = next((e for e in eds if e['id'] == str(edicion_id)), None) if edicion_id else eds[0]
    if not ed:
        raise Fallo('Esa edición ya no aparece en Tidal')
    pistas_t = tienda.cliente('tidal').pistas(ed['id'])
    if pista_enlace and len(rutas) == 1 and rutas[0] not in elegir and str(ed['id']) == str(eds[0]['id']):
        elegir[rutas[0]] = pista_enlace
    items = [_item(r, carpeta, db, pistas_t, elegir.get(r), album_q) for r in rutas]
    campos = ('id', 'titulo', 'artista', 'anio', 'n_pistas', 'hires', 'como', 'portada')
    return dict(base, aviso=None,
                edicion={k: ed.get(k) for k in campos + ('url',)},
                ediciones=[{k: e.get(k) for k in campos} for e in eds[:15]],
                pistas_tidal=[{k: p.get(k) for k in ('id', 'titulo', 'disco', 'numero', 'duracion')} for p in pistas_t],
                pistas=items)


def _item(r, carpeta, db, pistas_t, elegida, album_q):
    f, bd = _ficha(r, db)
    b, s = calidad_archivo(r)
    it = {'ruta': r, 'archivo': os.path.relpath(r, carpeta), 'titulo': f.get('titulo'), 'disco': f.get('disco'),
          'numero': f.get('numero'), 'duracion': f.get('duracion'), 'bits': b, 'sr': s,
          'calidad': etiqueta(b, s), 'reparable': True, 'motivo': None, 'pareja': None, 'manual': False,
          'dif_duracion': None, 'confirmar_duracion': False, 'bd': bd or None}
    if os.path.splitext(r)[1].lower() != '.flac':
        it.update(reparable=False, motivo=f'es {os.path.splitext(r)[1].lstrip(".").upper()}: Tidal entrega FLAC; '
                                          're-descarga el álbum completo')
        return it
    if pistas_t is None:
        it.update(reparable=False, motivo='falta elegir la edición en Tidal')
        return it
    if elegida:
        p = next((x for x in pistas_t if str(x['id']) == str(elegida)), None)
        it['manual'] = True
    else:
        p = rescate.emparejar(f, pistas_t)
    if not p:
        it.update(reparable=False, motivo='esa pista no está en la edición elegida' if elegida
                  else 'no hay una pista segura en esa edición: elígela')
        return it
    ref, dur = f.get('duracion'), p.get('duracion')
    if ref and dur:
        dif = float(dur) - float(ref)
        it['dif_duracion'] = round(dif, 1)
        if abs(dif) > MAX_DIF_CONFIRMABLE_S:
            it.update(reparable=False, motivo=f'dura {abs(dif):.0f} s {"más" if dif > 0 else "menos"} que la vieja: '
                                              'no es la misma pista')
        elif abs(dif) > rescate.TOLERANCIA_S:
            it['confirmar_duracion'] = True
    try:
        tb, ts, aq = calidad_tidal(p['id'])
    except Exception as e:  # noqa: BLE001
        tb, ts, aq = None, None, f'{type(e).__name__}'
    if aq and aq not in ('HI_RES_LOSSLESS', 'LOSSLESS'):
        it.update(reparable=False, motivo=f'Tidal la entrega con pérdida ({aq})')
    it['pareja'] = {'id': p['id'], 'titulo': p.get('titulo'), 'disco': p.get('disco'), 'numero': p.get('numero'),
                    'duracion': p.get('duracion'), 'bits': tb, 'sr': ts, 'calidad': etiqueta(tb, ts)}
    it['igual'] = (tb, ts) == tuple(album_q) if album_q[1] else (tb, ts) == (b, s)
    return it


# ─────────────────────────── reparar una pista ───────────────────────────

def _replaygain(ruta):
    """(ganancia dB, pico) de la pista según el filtro replaygain de ffmpeg, o (None, None)."""
    r = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-i', ruta, '-map', '0:a:0', '-af', 'replaygain',
                        '-f', 'null', '-'], capture_output=True, text=True, timeout=1800, errors='replace')
    g = re.search(r'track_gain\s*=\s*([+-]?[\d.]+)\s*dB', r.stderr or '')
    p = re.search(r'track_peak\s*=\s*([\d.]+)', r.stderr or '')
    return (float(g.group(1)) if g else None), (float(p.group(1)) if p else None)


def _tags_de_respaldo(nueva, vecina, bd):
    """Tags para la pista nueva cuando los de la vieja no se pueden leer: los del álbum (y la carátula) de una
    pista sana del mismo álbum; los de la pista, de la biblioteca de Orbyte y, si falta algo, de Tidal."""
    from mutagen.flac import FLAC
    n = FLAC(nueva)
    de_tidal = n.tags.as_dict() if n.tags is not None else {}
    tags, pics, fuentes = {}, [], []
    if vecina:
        try:
            v = FLAC(vecina)
            tags = {k: vals for k, vals in (v.tags.as_dict() if v.tags is not None else {}).items()
                    if k.lower() not in _TAGS_DE_PISTA}
            pics = list(v.pictures)
            fuentes.append('álbum y carátula de una pista sana')
        except Exception:  # noqa: BLE001
            tags, pics = {}, []
    if not tags:
        tags = {k: vals for k, vals in de_tidal.items() if k.lower() not in _TAGS_DE_PISTA}
        pics = list(n.pictures)
        fuentes.append('álbum de Tidal')
    bd = bd or {}
    pista = {'title': bd.get('titulo'), 'artist': _valido(bd.get('artista')) or None,
             'tracknumber': str(bd['numero']) if bd.get('numero') else None,
             'discnumber': str(bd['disco']) if bd.get('disco') else None, 'isrc': bd.get('isrc')}
    if any(pista.values()):
        fuentes.append('pista de la biblioteca de Orbyte')
    usados_tidal = False
    for k, v in pista.items():
        if v:
            tags[k] = [v]
        elif de_tidal.get(k):
            tags[k] = de_tidal[k]
            usados_tidal = True
    for k in ('lyrics', 'unsyncedlyrics'):
        if de_tidal.get(k):
            tags[k] = de_tidal[k]
    if usados_tidal:
        fuentes.append('pista de Tidal')
    return tags, pics, fuentes


def copiar_tags(vieja, nueva, sufijo=None, respaldo=None):
    """Todos los tags y la carátula de la vieja a la nueva; ReplayGain de pista recalculado; sufijo de calidad.
    Si la vieja no se deja leer (dañada desde el inicio), `respaldo` = {'vecina', 'bd'} arma los tags.
    Devuelve (título final, de dónde salió la metadata —None si fue de la vieja—)."""
    from mutagen.flac import FLAC
    tags, pics, fuentes, error = None, [], None, None
    try:
        v = FLAC(vieja)
        tags = v.tags.as_dict() if v.tags is not None else {}
        pics = list(v.pictures)
        if not tags.get('title'):
            error = 'la pista vieja no trae título en sus tags'
    except Exception as e:  # noqa: BLE001
        error = f'no se pudieron leer los tags de la pista vieja ({type(e).__name__})'
    if error:
        if not respaldo:
            raise Fallo(error + ': no se reemplaza')
        tags, pics, fuentes = _tags_de_respaldo(nueva, respaldo.get('vecina'), respaldo.get('bd'))
        if not tags.get('title'):
            raise Fallo(error + ' y no hay otra fuente para el título: no se reemplaza')
    n = FLAC(nueva)
    n.clear_pictures()
    if n.tags is not None:
        n.tags.clear()
    else:
        n.add_tags()
    for k, vals in tags.items():
        n[k] = vals
    for pic in pics:
        n.add_picture(pic)
    if fuentes is not None or any(k.lower().startswith('replaygain_track') for k in tags):
        g, p = _replaygain(nueva)
        if g is not None:
            n['replaygain_track_gain'] = [f'{g:+.2f} dB']
        if p is not None:
            n['replaygain_track_peak'] = [f'{p:.6f}']
    titulos = [SUFIJO_RE.sub('', t) for t in tags.get('title', [])]
    n['title'] = [f'{t} ({sufijo})' for t in titulos] if sufijo else titulos
    n.save()
    return n['title'][0], (' + '.join(fuentes) if fuentes else None)


class _FalloDescarga(Fallo):
    """Falla de la descarga en sí (corte, «File is corrupt!»): se reintenta."""


class _Sana(Exception):
    """La pista vieja resultó estar bien: no se reemplaza."""


def _vieja_confirmada(ruta, salud, reg):
    """Antes de reemplazar: ¿consta que la vieja está dañada? Si Salud ya lo confirmó con la huella y el archivo no
    cambió desde entonces, basta. Si no (revisada antes de la confirmación, o el archivo cambió), se confirma ahora."""
    if not salud:
        return
    with salud._con() as c:
        f = c.execute('SELECT album_id, tamano, mtime, errores, duracion FROM pistas WHERE ruta=?', (ruta,)).fetchone()
    st = os.stat(ruta)
    try:
        notas = json.loads(f['errores'] or '[]') if f else []
    except ValueError:
        notas = []
    igual = bool(f) and f['tamano'] == st.st_size and abs((f['mtime'] or 0) - st.st_mtime) < 1
    if igual and any(str(n).startswith('confirmado') for n in notas):
        return
    danada, nota = salud_mod.confirmar(ruta, salud_mod._prefijo())
    if danada is False:
        salud._guardar(ruta, f['album_id'] if f else None, st.st_size, st.st_mtime, 'ok', [nota], None,
                       f['duracion'] if f else None)
        raise _Sana(nota)
    reg(f'   · la vieja: {nota}')


def _bajar_una(it, tmp):
    pid = str(it['pareja']['id'])
    destino = tempfile.mkdtemp(prefix='intento-', dir=tmp)
    nuevo = rescate.bajar_pista('tidal', pid, destino)
    if not nuevo:
        raise _FalloDescarga('Tidal no entregó la pista (' + rescate.ultima_salida.get(pid, '') + ')')
    codec = (tienda.codec(nuevo) or '').lower()
    if codec != 'flac':
        raise Fallo(f'llegó en {codec.upper() or "?"}, no en FLAC')
    ok, errores, _pico, seg = analisis.decodificar(nuevo)
    if not ok:
        raise _FalloDescarga('la pista nueva también trae errores: ' + '; '.join(errores[:2]))
    if salud_mod.huella_flac(nuevo) is False:
        raise _FalloDescarga('la pista nueva no coincide con su propia huella MD5')
    return nuevo, seg


def _bajar_verificada(it, tmp, reg):
    """Baja la pista y la decodifica; si la descarga falla o llega dañada, reintenta (con pausa creciente)."""
    for intento in range(1, INTENTOS_DESCARGA + 1):
        try:
            return _bajar_una(it, tmp)
        except _FalloDescarga as e:
            if intento == INTENTOS_DESCARGA:
                raise Fallo(f'{e} · {INTENTOS_DESCARGA} intentos')
            espera = PAUSA_REINTENTO_S * intento
            reg(f'   … intento {intento} falló ({str(e)[:160]}); se reintenta en {espera} s')
            time.sleep(espera)


def reparar_pista(it, job, aj, reg, salud=None):
    """Baja, verifica, pasa la metadata y reemplaza. Devuelve el título final; Fallo si algo no cuadra."""
    ruta = it['ruta']
    if not os.path.isfile(ruta):
        raise Fallo('la pista vieja ya no está')
    _vieja_confirmada(ruta, salud, reg)
    tmp = tempfile.mkdtemp(prefix='reparar-', dir=os.path.dirname(os.path.abspath(salud.ruta)) if salud else None)
    try:
        nuevo, seg = _bajar_verificada(it, tmp, reg)
        ref = it.get('duracion') or (it.get('pareja') or {}).get('duracion')
        margen = MAX_DIF_CONFIRMABLE_S if it.get('acepto_duracion') else rescate.TOLERANCIA_S
        if ref and seg is not None and abs(seg - float(ref)) > margen:
            raise Fallo(f'dura {seg:.0f} s y la vieja {float(ref):.0f} s: no es la misma pista')
        b, s = calidad_archivo(nuevo)
        album_q = (job.get('bits_album'), job.get('sr_album'))
        distinta = bool(album_q[1]) and (b, s) != album_q
        if distinta and not it.get('acepto_calidad'):
            raise Fallo(f'llegó en {etiqueta(b, s)} y el álbum es {etiqueta(*album_q)}: no aceptaste otra calidad')
        titulo, fuente_meta = copiar_tags(ruta, nuevo, etiqueta(b, s) if distinta else None,
                                          {'vecina': job.get('vecina'), 'bd': it.get('bd')})
        if fuente_meta:
            reg(f'   · la vieja no se pudo leer: metadata de {fuente_meta}')
        # al lado de la vieja (mismo disco) y recién ahí reemplaza: la vieja se borra solo si todo lo anterior anduvo
        provisorio = ruta + '.reparando'
        shutil.copyfile(nuevo, provisorio)
        if os.path.getsize(provisorio) != os.path.getsize(nuevo):
            os.remove(provisorio)
            raise Fallo('la copia a la biblioteca quedó incompleta')
        os.replace(provisorio, ruta)
        reg(f'   ✓ {it["archivo"]}: reemplazada por la de Tidal ({etiqueta(b, s)})'
            f'{" · título: " + titulo if distinta or fuente_meta else ""}')
        _orbyte(ruta, aj, b, s, reg)
        if salud:
            st = os.stat(ruta)
            salud._guardar(ruta, None, st.st_size, st.st_mtime, 'ok', ['reparada con la pista de Tidal'], seg, ref)
        return titulo
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _orbyte(ruta, aj, bits, sr, reg):
    """Orbyte relee los datos técnicos de la pista (mismo ID: escuchas y favoritos intactos) y su LED."""
    db = aj.get('db_orbyte')
    if not db or not os.path.isfile(db):
        return
    con = sqlite3.connect(db, timeout=60)
    try:
        fila = con.execute('SELECT t.id, al.path FROM tracks t JOIN albums al ON al.id = t.album_id WHERE t.file_path=?',
                           (ruta,)).fetchone()
    finally:
        con.close()
    if not fila:
        return                                          # todavía no está en Orbyte (álbum en la Bandeja)
    from . import metadata
    try:
        metadata._correr('index', ['index_libraryV3.py', '--update', '--album', fila[1], '--db', db, '--root',
                                   aj.get('biblioteca') or '/mnt/musica', '--quiet'], reg, aj, metadata.LIMITE_S['index'])
    except Exception as e:  # noqa: BLE001
        reg(f'   ⚠ Orbyte no releyó el álbum ({e}); se verá al correr la metadata')
    con = sqlite3.connect(db, timeout=60)
    try:
        with con:                                       # el indexador no toca el LED (campo curado): FLAC PCM
            con.execute('UPDATE tracks SET led_color=? WHERE id=?', ('white' if (sr or 0) > 48000 else 'yellow', fila[0]))
    finally:
        con.close()


# ─────────────────────────── cola de trabajos ───────────────────────────

class Reparador:
    def __init__(self, salud, ajustes_fn, ocupado_tidal_fn, al_terminar=None, avisar_fn=None):
        self.salud, self.ajustes_fn, self.ocupado_tidal_fn = salud, ajustes_fn, ocupado_tidal_fn
        self.al_terminar, self.avisar_fn = al_terminar, avisar_fn
        self._ev = threading.Event()
        self.actual = None
        with salud._con() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS reparaciones(id INTEGER PRIMARY KEY AUTOINCREMENT, creado REAL,
                         terminado REAL, estado TEXT, titulo TEXT, carpeta TEXT, aid INTEGER, trabajo TEXT, registro TEXT)''')
            # lo que quedó «reparando» al apagarse vuelve a la cola (cada pista se rehace desde la descarga)
            c.execute("UPDATE reparaciones SET estado='en_cola' WHERE estado='reparando'")

    def encolar(self, p, elegidas, aid=None):
        """`p`: el plan; `elegidas`: {ruta: {'acepto_calidad', 'acepto_duracion'}} de las pistas que se reparan."""
        pistas = []
        for it in p['pistas']:
            if it['ruta'] in elegidas and it.get('reparable') and it.get('pareja'):
                e = elegidas[it['ruta']]
                if it.get('confirmar_duracion') and not e.get('acepto_duracion'):
                    continue                            # dura distinto y no se confirmó «Usar igual»
                pistas.append(dict(it, acepto_calidad=bool(e.get('acepto_calidad')),
                                   acepto_duracion=bool(it.get('confirmar_duracion') and e.get('acepto_duracion'))))
        if not pistas:
            raise Fallo('No hay pistas para reparar')
        trabajo = {'pistas': pistas, 'bits_album': p.get('bits_album'), 'sr_album': p.get('sr_album'),
                   'edicion': p.get('edicion'), 'vecina': p.get('vecina')}
        with self.salud._con() as c:
            cur = c.execute('INSERT INTO reparaciones(creado, estado, titulo, carpeta, aid, trabajo, registro) '
                            "VALUES(?, 'en_cola', ?, ?, ?, ?, '')",
                            (time.time(), f'{p["artista"]} — {p["album"]}', p['carpeta'], aid, json.dumps(trabajo)))
            jid = cur.lastrowid
        self._ev.set()
        return jid

    def lista(self, aid=None, limite=30):
        with self.salud._con() as c:
            q = 'SELECT id, creado, terminado, estado, titulo, carpeta, aid, trabajo, registro FROM reparaciones'
            filas = c.execute(q + (' WHERE aid=?' if aid else '') + ' ORDER BY id DESC LIMIT ?',
                              ((aid, limite) if aid else (limite,))).fetchall()
            activos = [json.loads(x['trabajo'] or '{}') for x in c.execute(
                "SELECT trabajo FROM reparaciones WHERE estado IN ('en_cola', 'reparando')")]
        en_curso = {x['ruta'] for t in activos for x in t.get('pistas', [])}
        out, vistas = [], set()
        for r in filas:
            t = json.loads(r['trabajo'] or '{}')
            # Pistas del álbum que siguen dañadas (y no están ya en cola): «Reparar las pendientes» sin volver a
            # revisar el álbum. Solo en el trabajo más reciente de cada carpeta.
            pend = []
            if r['estado'] not in ('en_cola', 'reparando') and r['carpeta'] and r['carpeta'] not in vistas:
                pend = [p for p in self.salud.problemas_en(r['carpeta']) if p not in en_curso]
            vistas.add(r['carpeta'])
            out.append({'id': r['id'], 'creado': r['creado'], 'terminado': r['terminado'], 'estado': r['estado'],
                        'titulo': r['titulo'], 'carpeta': r['carpeta'], 'aid': r['aid'],
                        'pistas': [x['archivo'] for x in t.get('pistas', [])], 'pendientes': pend,
                        'registro': r['registro'] or ''})
        return out

    def quitar(self, jid):
        with self.salud._con() as c:
            c.execute("DELETE FROM reparaciones WHERE id=? AND estado != 'reparando'", (jid,))

    def start(self):
        threading.Thread(target=self._bucle, daemon=True, name='reparar').start()

    def _bucle(self):
        while True:
            try:
                espera = self._uno()
            except Exception:  # noqa: BLE001
                log.exception('Reparar: error inesperado')
                espera = 60
            self._ev.wait(espera)
            self._ev.clear()

    def _uno(self):
        with self.salud._con() as c:
            r = c.execute("SELECT * FROM reparaciones WHERE estado='en_cola' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return 60
        if self.ocupado_tidal_fn():
            return 30                                   # una descarga usa la misma sesión de Tidal
        jid, trabajo = r['id'], json.loads(r['trabajo'] or '{}')
        self.actual = jid
        registro = []

        def reg(m):
            registro.append(m)
            with self.salud._con() as c:
                c.execute('UPDATE reparaciones SET registro=? WHERE id=?', ('\n'.join(registro), jid))

        with self.salud._con() as c:
            c.execute("UPDATE reparaciones SET estado='reparando', registro='' WHERE id=?", (jid,))
        aj = self.ajustes_fn()
        ed = trabajo.get('edicion') or {}
        reg(f'Reparar {len(trabajo["pistas"])} pista(s) con Tidal · edición «{ed.get("titulo")}»'
            f'{" (" + str(ed.get("anio")) + ")" if ed.get("anio") else ""} · {ed.get("como") or ""}')
        bien, mal, sanas = 0, 0, 0
        for i, it in enumerate(trabajo['pistas']):
            if i:
                time.sleep(PAUSA_S)
            reg(f'· {it["archivo"]} → Tidal {it["pareja"].get("disco") or 1}-{it["pareja"].get("numero")} '
                f'«{it["pareja"].get("titulo")}»')
            try:
                reparar_pista(it, trabajo, aj, reg, self.salud)
                bien += 1
            except _Sana as e:
                sanas += 1
                reg(f'   = no se reemplazó: la vieja está sana ({e})')
            except Fallo as e:
                mal += 1
                reg(f'   ✗ no se reemplazó: {e}')
            except Exception as e:  # noqa: BLE001
                mal += 1
                log.exception('Reparar %s', it['ruta'])
                reg(f'   ✗ error: {type(e).__name__}: {e} (la pista vieja quedó como estaba)')
        estado = 'hecho' if not mal else ('parcial' if bien else 'error')
        reg(f'Listo: {bien} reemplazada(s){f", {sanas} estaba(n) sana(s)" if sanas else ""}'
            f'{f", {mal} sin reemplazar" if mal else ""}')
        with self.salud._con() as c:
            c.execute('UPDATE reparaciones SET estado=?, terminado=? WHERE id=?', (estado, time.time(), jid))
        self.actual = None
        if self.avisar_fn:
            self.avisar_fn('ok' if not mal else 'revision', f'Reparar pistas: {r["titulo"]}',
                           f'{bien} reemplazada(s)' + (f', {sanas} sana(s)' if sanas else '')
                           + (f', {mal} sin reemplazar' if mal else ''), r['aid'])
        if self.al_terminar and r['aid'] and (bien or sanas):
            try:
                self.al_terminar(r['aid'])               # el álbum de la Bandeja se vuelve a revisar
            except Exception:  # noqa: BLE001
                log.exception('Reparar: no se pudo volver a revisar el álbum %s', r['aid'])
        return 1
