"""
Orbyte Ingesta — pistas que llegan con pérdida en una descarga pedida sin pérdida.

Orden (por pista):
  1. reintento en el mismo servicio (solo Tidal: sesión PKCE recién renovada, historial aparte)
  2. la misma pista del mismo álbum en el otro servicio (Tidal ↔ Qobuz): álbum por código de barras o por
     artista + título; pista por disco + número, título y duración (±2 s)
  3. si nada resulta: queda para que decidas en Descargas → convertir() a FLAC o MP3 320 con «(Lossy)» al final
     del título. No se busca subir la calidad: solo que todo el álbum quede en el mismo contenedor.
"""
import difflib
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import analisis
from . import tienda

TOLERANCIA_S = 2.0
TOLERANCIA_REL = 0.003     # 0,3 % de la duración: en una pista larga, 2 s fijos son demasiado poco


def tolerancia(duracion):
    """Diferencia de duración aceptable entre la pista vieja y la nueva: 2 s, o el 0,3 % si es más (08-11-2026:
    «Illumination Theory» de Dream Theater, 22 min, 1338 s la vieja y 1336 s la de Tidal, se rechazaba)."""
    try:
        return max(TOLERANCIA_S, TOLERANCIA_REL * float(duracion or 0))
    except (TypeError, ValueError):
        return TOLERANCIA_S


# ─────────────────────────── qué hay en la carpeta ───────────────────────────

def _num(v):
    try:
        return int(str(v or '').split('/')[0].strip() or 0) or None
    except ValueError:
        return None


def _sondeo(ruta):
    """(codec, frecuencia, duración) según ffprobe; un archivo dañado o incompleto da frecuencia/duración vacías."""
    import json
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                            'stream=codec_name,sample_rate:format=duration', '-of', 'json', ruta],
                           capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or '{}')
        st = (d.get('streams') or [{}])[0]
        dur = float((d.get('format') or {}).get('duration') or 0)
        return (st.get('codec_name') or '').lower() or None, int(st.get('sample_rate') or 0), dur
    except Exception:  # noqa: BLE001
        return None, 0, 0.0


def ficha(ruta):
    """{'ruta', 'codec', 'titulo', 'numero', 'disco', 'duracion', 'danada'} de un archivo de audio."""
    codec, sr, dur = _sondeo(ruta)
    info = {'ruta': ruta, 'codec': codec, 'titulo': None, 'numero': None, 'disco': 1, 'duracion': None,
            'danada': not codec or not sr or dur < 1}
    try:
        import mutagen
        f = mutagen.File(ruta, easy=True)
        if f is not None:
            t = f.tags or {}
            info['titulo'] = (t.get('title') or [None])[0]
            info['numero'] = _num((t.get('tracknumber') or [None])[0])
            info['disco'] = _num((t.get('discnumber') or [None])[0]) or 1
            info['duracion'] = getattr(f.info, 'length', None)
    except Exception:  # noqa: BLE001
        pass
    if not info['numero']:                                    # «03 - Título.m4a»
        base = os.path.basename(ruta)
        cab = base.split(' ', 1)[0].replace('.', '')
        if '-' in cab and cab.split('-')[0].isdigit() and cab.split('-')[1].isdigit():
            info['disco'], info['numero'] = int(cab.split('-')[0]), int(cab.split('-')[1])
        elif cab.isdigit():
            info['numero'] = int(cab)
    if not info['titulo']:                                    # «19. Avantasia - Avantasia (Live).flac» → «Avantasia (Live)»
        base = re.sub(r'^\s*\d+(?:-\d+)?\s*[.\-_)]?\s*', '', os.path.splitext(os.path.basename(ruta))[0])
        info['titulo'] = base.split(' - ', 1)[1] if ' - ' in base else base
        info['titulo_de_nombre'] = True
    if info['disco'] == 1:                                    # «Disc 2/01. …», «CD 2/…»
        m = re.search(r'(?:disc|disco|cd)\s*0*(\d+)\s*$', os.path.basename(os.path.dirname(ruta)), re.I)
        if m:
            info['disco'] = int(m.group(1))
    return info


def con_perdida(carpetas):
    """Pistas con pérdida o dañadas/incompletas (las dos se intentan recuperar)."""
    salida = []
    for c in carpetas:
        for a in sorted(tienda._audios(c)):
            f = ficha(a)
            if f['danada'] or f['codec'] in tienda.CODECS_CON_PERDIDA:
                salida.append(f)
    return salida


def _motivo(f):
    return 'dañada o incompleta' if f.get('danada') else (f.get('codec') or '?').upper()


# ─────────────────────────── emparejar con el catálogo ───────────────────────────

def _parecido(a, b):
    a, b = analisis._norm(a or ''), analisis._norm(b or '')
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _dur_ok(f, p):
    return not f.get('duracion') or not p.get('duracion') or abs(float(f['duracion']) - float(p['duracion'])) <= tolerancia(f['duracion'])


def emparejar(f, pistas):
    misma = [p for p in pistas if (p.get('disco') or 1) == (f.get('disco') or 1) and p.get('numero') == f.get('numero')]
    for p in misma:
        if _dur_ok(f, p) and (_parecido(f.get('titulo'), p.get('titulo')) >= 0.5 or not f.get('titulo')):
            return p
    candidatos = [p for p in pistas if _dur_ok(f, p) and _parecido(f.get('titulo'), p.get('titulo')) >= 0.85]
    if candidatos:
        return max(candidatos, key=lambda p: _parecido(f.get('titulo'), p.get('titulo')))
    # numeración corrida entre discos («19.» = disco 2, pista 6 en el otro servicio): por posición absoluta
    orden = sorted(pistas, key=lambda p: ((p.get('disco') or 1), p.get('numero') or 0))
    n = f.get('numero') or 0
    if (f.get('disco') or 1) == 1 and 0 < n <= len(orden):
        p = orden[n - 1]
        if _dur_ok(f, p) and (_parecido(f.get('titulo'), p.get('titulo')) >= 0.6 or not f.get('titulo')):
            return p
    return None


def _upc(servicio, album_id):
    try:
        c = tienda.cliente(servicio)
        d = c._get(f'albums/{album_id}') if servicio == 'tidal' else c._get('album/get', album_id=album_id)
        return str(d.get('upc') or '').lstrip('0') or None
    except Exception:  # noqa: BLE001
        return None


def album_en_otro(origen, album_id, artista, album, n_pistas, avisar=None):
    """Ediciones del mismo álbum en el otro servicio, la más parecida primero: [(servicio, id, cómo, n_pistas)]."""
    otro = 'qobuz' if origen == 'tidal' else 'tidal'
    upc = _upc(origen, album_id)
    try:
        cands = tienda.cliente(otro).buscar_albumes(f'{artista} {album}', limite=25)
    except Exception as e:  # noqa: BLE001
        if avisar:
            avisar(f'No se pudo buscar en {otro.capitalize()}: {type(e).__name__}: {e}')
        return []
    if n_pistas and not any(v.get('n_pistas') == n_pistas and _parecido(v.get('titulo'), album) >= 0.9 for v in cands):
        # la búsqueda por texto no siempre trae todas las ediciones (Ghostlights de 24 pistas): la discografía sí
        try:
            _n, disco = tienda.cliente(otro).buscar_artista(artista)
            vistos = {v['id'] for v in cands}
            cands += [v for v in disco if v['id'] not in vistos]
        except Exception as e:  # noqa: BLE001
            if avisar:
                avisar(f'No se pudo leer la discografía en {otro.capitalize()}: {type(e).__name__}: {e}')
    salida = []
    if upc:
        salida += [(otro, v['id'], 'mismo código de barras', v.get('n_pistas')) for v in cands
                   if str(v.get('upc') or '').lstrip('0') == upc]
    mismos = [v for v in cands if _parecido(v.get('titulo'), album) >= 0.9 and _parecido(v.get('artista'), artista) >= 0.8
              and v['id'] not in {x[1] for x in salida}]
    # primero la edición con la misma cantidad de pistas (la deluxe de 24 antes que la normal de 13)
    mismos.sort(key=lambda v: (abs((v.get('n_pistas') or 0) - (n_pistas or 0)), -(v.get('bits') or 0)))
    salida += [(otro, v['id'], 'mismo artista y título', v.get('n_pistas')) for v in mismos]
    return salida


# ─────────────────────────── bajar una sola pista ───────────────────────────

ultima_salida = {}          # pista_id → últimas líneas de la herramienta (para el registro si no bajó nada)


def bajar_pista(servicio, pista_id, tmp):
    """Archivo descargado (en tmp) o None."""
    os.makedirs(os.path.join(tmp, 'audio'), exist_ok=True)
    if servicio == 'tidal':
        tienda.preparar_tidal()
        rc, sal = tienda._bajar_tidal_a(f'https://tidal.com/browse/track/{pista_id}', os.path.join(tmp, 'audio'),
                                        os.path.join(tmp, 'casa'), letras=True)
    else:
        rc, sal = tienda._bajar_qobuz_a(f'https://open.qobuz.com/track/{pista_id}', os.path.join(tmp, 'audio'))
    audios = tienda._audios(os.path.join(tmp, 'audio'))
    lineas = [l.strip() for l in (sal or '').splitlines() if l.strip() and '━' not in l]
    ultima_salida[str(pista_id)] = f'código {rc}: ' + ' | '.join(lineas[-4:]) if lineas else f'código {rc}, sin salida'
    return audios[0] if audios else None


def _reemplazar(f, nuevo):
    destino = os.path.splitext(f['ruta'])[0] + os.path.splitext(nuevo)[1].lower()
    shutil.move(nuevo, destino + '.tmp')
    os.remove(f['ruta'])
    os.replace(destino + '.tmp', destino)
    return destino


def rescatar(d, carpetas, base_tmp, avisar):
    """
    Intenta conseguir sin pérdida cada pista con pérdida de la descarga `d`.
    avisar(texto) va al registro. Devuelve (pendientes, notas): las que siguen con pérdida y lo que se hizo.
    """
    pendientes = con_perdida(carpetas)
    if not pendientes:
        return [], []
    notas = []
    avisar(f'{len(pendientes)} pista(s) llegaron con pérdida o dañadas: '
           + ', '.join(f'{os.path.basename(f["ruta"])} ({_motivo(f)})' for f in pendientes[:6])
           + (' …' if len(pendientes) > 6 else ''))
    origen = d['servicio']
    pistas_origen = None
    try:
        pistas_origen = tienda.cliente(origen).pistas(d['item_id'])
    except Exception as e:  # noqa: BLE001
        avisar(f'No se pudo leer el álbum en {origen.capitalize()}: {e}')

    def intentar(f, servicio, pista, etiqueta):
        tmp = tempfile.mkdtemp(prefix='rescate-', dir=base_tmp)
        try:
            nuevo = bajar_pista(servicio, pista['id'], tmp)
            c, sr_n, dur_n = _sondeo(nuevo) if nuevo else (None, 0, 0.0)
            if nuevo and c and sr_n and dur_n >= 1 and c not in tienda.CODECS_CON_PERDIDA:
                final = _reemplazar(f, nuevo)
                avisar(f'  ✓ {os.path.basename(final)}: {etiqueta} ({c.upper()})')
                notas.append({'archivo': os.path.basename(final), 'como': etiqueta})
                return True
            avisar(f'  · {os.path.basename(f["ruta"])}: {servicio.capitalize()} no la entregó bien '
                   f'({("llegó " + c.upper()) if c and sr_n else ("llegó dañada" if nuevo else "no se descargó")})')
            if not nuevo:
                avisar(f'      {servicio}: {ultima_salida.get(str(pista["id"]), "sin salida")[:400]}')
        except Exception as e:  # noqa: BLE001
            avisar(f'  · {os.path.basename(f["ruta"])}: {etiqueta} falló ({type(e).__name__}: {e})')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return False

    # 1) reintento en el mismo servicio: Tidal (sesión PKCE renovada) para todas; Qobuz solo para las dañadas
    #    (si Qobuz entregó con pérdida, volver a pedirla da lo mismo)
    reintentar = [f for f in pendientes if origen == 'tidal' or f.get('danada')]
    if reintentar and pistas_origen:
        avisar(f'Reintentando en {origen.capitalize()}' + (' con la sesión renovada…' if origen == 'tidal' else '…'))
        for f in reintentar:
            p = emparejar(f, pistas_origen)
            if not p:
                avisar(f'  · {os.path.basename(f["ruta"])}: no se encontró la pista en el álbum de {origen.capitalize()}')
                continue
            if intentar(f, origen, p, f'reintento en {origen.capitalize()}'):
                pendientes.remove(f)
            time.sleep(2)

    # 2) la misma pista en el otro servicio
    if pendientes:
        ediciones = album_en_otro(origen, d['item_id'], d.get('artista') or '', d.get('album') or '', d.get('n_pistas'),
                                  avisar)
        otro_nombre = 'Qobuz' if origen == 'tidal' else 'Tidal'
        if not ediciones:
            avisar(f'El álbum no está en {otro_nombre}')
        for servicio, alb, como, n in ediciones[:3]:
            if not pendientes:
                break
            avisar(f'Buscando en {servicio.capitalize()} (álbum {alb}, {n or "?"} pistas, {como})…')
            try:
                pistas_otro = tienda.cliente(servicio).pistas(alb)
            except Exception as e:  # noqa: BLE001
                avisar(f'No se pudo leer el álbum en {servicio.capitalize()}: {e}')
                continue
            for f in list(pendientes):
                p = emparejar(f, pistas_otro)
                if not p:
                    avisar(f'  · {os.path.basename(f["ruta"])}: no está en esta edición de {servicio.capitalize()}')
                    continue
                if intentar(f, servicio, p, f'traída de {servicio.capitalize()} (puede ser otra masterización)'):
                    pendientes.remove(f)
                time.sleep(2)
    return pendientes, notas


# ─────────────────────────── convertir (decisión tuya) ───────────────────────────

def convertir(ruta, formato):
    """AAC u otro con pérdida → FLAC 16 bits o MP3 320, con «(Lossy)» al final del título y del nombre."""
    base, _ext = os.path.splitext(ruta)
    ext = '.flac' if formato == 'flac' else '.mp3'
    destino = (base if base.endswith(' (Lossy)') else base + ' (Lossy)') + ext
    cmd = ['ffmpeg', '-v', 'error', '-y', '-i', ruta, '-map', '0:a:0', '-map_metadata', '0']
    cmd += ['-c:a', 'flac', '-sample_fmt', 's16'] if formato == 'flac' else ['-c:a', 'libmp3lame', '-b:a', '320k',
                                                                            '-id3v2_version', '3']
    r = subprocess.run(cmd + [destino + '.tmp' + ext], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or 'ffmpeg falló').strip().splitlines()[-1])
    os.replace(destino + '.tmp' + ext, destino)
    try:
        import mutagen
        f = mutagen.File(destino, easy=True)
        if f is not None:
            if f.tags is None:
                f.add_tags()
            titulo = (f.tags.get('title') or [os.path.basename(base).split(' - ', 1)[-1]])[0]
            if not titulo.endswith('(Lossy)'):
                f.tags['title'] = f'{titulo} (Lossy)'
            f.save()
    except Exception:  # noqa: BLE001
        pass
    os.remove(ruta)
    return destino
