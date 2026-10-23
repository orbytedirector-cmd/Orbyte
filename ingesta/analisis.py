"""
Orbyte Ingesta — detección y revisión de álbumes (etapa 1: solo lectura).

Nada de este módulo modifica, mueve ni borra archivos: lee la carpeta de
entrada, interroga cada archivo con ffprobe/mutagen, decodifica cada pista
completa con ffmpeg (archivos dañados + pico) y arma un reporte con lo que
haría falta para dejar el álbum en la biblioteca.
"""
import difflib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import edicion as edicion_mod

try:
    import mutagen
except ImportError:          # el servicio funciona igual, con menos tags
    mutagen = None
try:
    from PIL import Image
except ImportError:
    Image = None

VERSION_REPORTE = 1

AUDIO_EXT = {'.flac', '.wav', '.aif', '.aiff', '.aifc', '.dsf', '.dff', '.wv', '.ape',
             '.m4a', '.mp4', '.alac', '.mp3', '.ogg', '.oga', '.opus', '.aac', '.wma',
             '.ac3', '.eac3', '.dts', '.mka', '.tta', '.mpc'}
IMAGEN_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'}
IGNORAR = {'.ds_store', 'thumbs.db', 'desktop.ini'}

LOSSY_CODECS = {'mp3', 'aac', 'vorbis', 'opus', 'wmav1', 'wmav2', 'mp2', 'ac3', 'eac3',
                'dts', 'musepack7', 'musepack8'}
A_FLAC = {'wavpack', 'ape', 'alac', 'tta'}       # sin pérdida, pero no FLAC
CODEC_LABEL = {'flac': 'FLAC', 'wavpack': 'WavPack', 'ape': "Monkey's Audio", 'alac': 'ALAC',
               'mp3': 'MP3', 'aac': 'AAC', 'vorbis': 'OGG', 'opus': 'Opus', 'wmav2': 'WMA',
               'wmav1': 'WMA', 'ac3': 'AC-3', 'eac3': 'E-AC-3 (Dolby)', 'dts': 'DTS',
               'truehd': 'TrueHD', 'tta': 'TTA', 'mp2': 'MP2',
               'dsd_lsbf_planar': 'DSD', 'dsd_msbf_planar': 'DSD', 'dsd_lsbf': 'DSD',
               'dsd_msbf': 'DSD'}
DSD_RATES = {2822400: 'DSD64', 5644800: 'DSD128', 11289600: 'DSD256', 22579200: 'DSD512',
             352800: 'DSD64', 705600: 'DSD128', 1411200: 'DSD256'}
STD_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)

# Subcarpetas de disco: «CD 1», «CD01», «Disc 2», «Disco 1», «Vol. 1 - Disc 2», «DVD 1»…
DISCO_RE = re.compile(r'(?:^|[\s\-_.\[(])(?:cd|dis[ck]|disco|dvd|sacd)\s*[-_.]?\s*(\d{1,2})(?!\d)', re.I)
VOLUMEN_RE = re.compile(r'^(?:vol(?:ume|umen)?\.?)\s*(\d{1,2})$', re.I)

# Prioridad para desempatar el tag de calidad cuando la mayoría empata
PRIORIDAD_TAG = ['MQA', 'DSD', 'Lossy', 'HR', 'CD']
RANGO_TAG = {'Lossy': 0, 'CD': 1, 'HR': 2, 'MQA': 2, 'DSD': 3}

NIVELES = ('error', 'decision', 'correccion', 'aviso')


# ─────────────────────────── utilidades ───────────────────────────

def sin_acentos(s):
    return ''.join(c for c in unicodedata.normalize('NFKD', s or '') if not unicodedata.combining(c))


def nombre_seguro(s):
    """Mismo criterio que la biblioteca actual: caracteres no válidos en Windows → «_»."""
    s = re.sub(r'[:?*"<>|\\/]', '_', (s or '').strip())
    s = re.sub(r'\s+', ' ', s).strip().rstrip('.').strip()
    return s or '_'


def letra_de(artista):
    s = sin_acentos(artista or '').strip()
    if not s:
        return '#'
    c = s[0].upper()
    return c if 'A' <= c <= 'Z' else '#'


def kbps_nominal(bps, codec):
    if not bps:
        return None
    k = bps / 1000.0
    if codec == 'mp3' and k > 320:
        return 320
    best = min(STD_KBPS, key=lambda v: abs(v - k))
    return best if abs(best - k) / best <= 0.06 else int(round(k))


def _num(v):
    """«3/12» → 3 ; «03» → 3 ; basura → None."""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
        if isinstance(v, tuple):          # MP4 trkn = [(3, 12)]
            v = v[0]
    m = re.match(r'\s*(\d+)', str(v))
    return int(m.group(1)) if m else None


def _anio(v):
    m = re.search(r'(1[89]\d\d|20\d\d)', str(v or ''))
    return m.group(1) if m else None


def _texto(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return None
    s = str(v).replace('\x00', ' ').strip()
    return s or None


def dividir_por_disco(nombre):
    """Número de disco de una subcarpeta, o None si no parece de disco."""
    m = DISCO_RE.search(nombre)
    if m:
        return int(m.group(1))
    m = VOLUMEN_RE.match(nombre.strip())
    return int(m.group(1)) if m else None


# ─────────────────────────── detección ───────────────────────────

def detectar_albumes(entrada):
    """
    Recorre la carpeta de entrada y devuelve las unidades «álbum»:
      - una carpeta con audio;
      - una carpeta cuyas subcarpetas son discos (CD 1, Disc 2…) = un álbum de varios discos;
      - Artista/Álbum también funciona (cada álbum es una unidad).
    Devuelve (unidades, sueltos) — sueltos = archivos de audio directo en la raíz.
    """
    entrada = os.path.abspath(entrada)
    hojas = {}
    sueltos = []
    for raiz, dirs, archivos in os.walk(entrada):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not d.startswith('@'))
        audio = [a for a in archivos if Path(a).suffix.lower() in AUDIO_EXT and not a.startswith('._')]
        if not audio:
            continue
        if os.path.abspath(raiz) == entrada:
            sueltos = sorted(audio)
            continue
        hojas[raiz] = audio

    unidades = {}
    for hoja in hojas:
        padre = os.path.dirname(hoja)
        unidad = hoja
        if dividir_por_disco(os.path.basename(hoja)) is not None and os.path.abspath(padre) != entrada:
            unidad = padre
        unidades.setdefault(unidad, []).append(hoja)

    resultado = []
    for unidad, carpetas in sorted(unidades.items()):
        resultado.append({'ruta': unidad, 'rel': os.path.relpath(unidad, entrada),
                          'carpetas': sorted(carpetas), 'firma': firma(unidad)})
    return resultado, sueltos


def firma(carpeta):
    """(n.º de archivos, bytes, mtime más reciente) de todo lo que hay bajo la carpeta."""
    n = tam = 0
    ult = 0.0
    for raiz, dirs, archivos in os.walk(carpeta):
        for a in archivos:
            try:
                st = os.stat(os.path.join(raiz, a))
            except OSError:
                continue
            n += 1
            tam += st.st_size
            ult = max(ult, st.st_mtime)
    return [n, tam, round(ult, 1)]


# ─────────────────────────── lectura de archivos ───────────────────────────

def ffprobe(ruta):
    try:
        r = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
                            '-show_format', '-show_streams', ruta],
                           capture_output=True, text=True, timeout=120)
        return json.loads(r.stdout or '{}')
    except Exception:
        return {}


_ID3 = {'TIT2': 'titulo', 'TPE1': 'artista', 'TPE2': 'artista_album', 'TALB': 'album',
        'TDRC': 'anio', 'TYER': 'anio', 'TDOR': 'anio_original', 'TRCK': 'pista', 'TPOS': 'disco',
        'TCON': 'genero'}
_MP4 = {'\xa9nam': 'titulo', '\xa9ART': 'artista', 'aART': 'artista_album', '\xa9alb': 'album',
        '\xa9day': 'anio', 'trkn': 'pista', 'disk': 'disco', '\xa9gen': 'genero'}
_VORBIS = {'title': 'titulo', 'artist': 'artista', 'albumartist': 'artista_album',
           'album artist': 'artista_album', 'album_artist': 'artista_album', 'album': 'album',
           'date': 'anio', 'year': 'anio', 'originaldate': 'anio_original',
           'tracknumber': 'pista', 'track': 'pista', 'discnumber': 'disco', 'disc': 'disco',
           'genre': 'genero', 'totaldiscs': 'total_discos', 'disctotal': 'total_discos'}


def tags_mutagen(ruta):
    """Tags normalizados + dict crudo (claves en minúscula) leídos con mutagen."""
    norm, crudo = {}, {}
    if mutagen is None:
        return norm, crudo
    try:
        f = mutagen.File(ruta)
    except Exception:
        return norm, crudo
    if f is None or f.tags is None:
        return norm, crudo
    try:
        items = list(f.tags.items())
    except Exception:
        return norm, crudo
    for k, v in items:
        clave = str(k)
        if clave.startswith('APIC') or clave.startswith('covr') or clave.lower().startswith('cover art') \
                or clave.lower() == 'metadata_block_picture':
            continue
        if hasattr(v, 'text'):            # frame ID3
            val = v.text
        else:
            val = v
        texto = _texto(val) if not (isinstance(val, list) and val and isinstance(val[0], tuple)) else val
        base = clave.split(':', 1)
        if base[0] == 'TXXX' and len(base) > 1:
            crudo[base[1].lower()] = _texto(val)
            continue
        crudo[clave.lower()] = texto if not isinstance(texto, list) else str(texto)
        destino = _ID3.get(clave[:4]) if clave[:4] in _ID3 and len(clave) <= 4 else None
        destino = destino or _MP4.get(clave) or _VORBIS.get(clave.lower())
        if destino and destino not in norm:
            if destino in ('pista', 'disco'):
                norm[destino] = _num(val)
                if destino == 'disco':
                    m = re.match(r'\s*\d+\s*/\s*(\d+)', str(_texto(val) or ''))
                    if m:
                        norm['total_discos'] = int(m.group(1))
                    if isinstance(val, list) and val and isinstance(val[0], tuple) and len(val[0]) > 1:
                        norm['total_discos'] = val[0][1] or None
            elif destino in ('anio', 'anio_original'):
                norm[destino] = _anio(_texto(val))
            elif destino == 'total_discos':
                norm[destino] = _num(val)
            else:
                norm[destino] = _texto(val)
    return norm, crudo


def tags_ffprobe(info):
    t = {}
    for fuente in [info.get('format', {}).get('tags') or {}] + \
                  [s.get('tags') or {} for s in info.get('streams', []) if s.get('codec_type') == 'audio']:
        for k, v in fuente.items():
            t.setdefault(k.lower(), v)
    norm = {}
    for k, v in t.items():
        destino = _VORBIS.get(k) or {'album_artist': 'artista_album', 'track': 'pista', 'disc': 'disco'}.get(k)
        if destino and destino not in norm:
            if destino in ('pista', 'disco', 'total_discos'):
                norm[destino] = _num(v)
            elif destino in ('anio', 'anio_original'):
                norm[destino] = _anio(v)
            else:
                norm[destino] = _texto(v)
    return norm, t


def decodificar(ruta):
    """Decodifica la pista completa: (ok, errores[], pico_dB, segundos_decodificados)."""
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-loglevel', 'level+info',
           '-i', ruta, '-map', '0:a:0', '-af',
           'astats=measure_perchannel=none:measure_overall=Peak_level', '-f', 'null', '-']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, errors='replace')
    except subprocess.TimeoutExpired:
        return False, ['la decodificación no terminó en 30 minutos'], None, None
    except Exception as e:
        return False, [str(e)], None, None
    errores, pico, segundos = [], None, None
    for linea in r.stderr.splitlines():
        if '[error]' in linea or '[fatal]' in linea:
            limpio = re.sub(r'^.*?\[(?:error|fatal)\]\s*', '', linea).strip()
            if limpio and limpio not in errores:
                errores.append(limpio)
        t = re.search(r'\btime=(\d+):(\d+):(\d+(?:\.\d+)?)', linea)
        if t:
            segundos = int(t.group(1)) * 3600 + int(t.group(2)) * 60 + float(t.group(3))
        m = re.search(r'Peak level dB:\s*(-?[\d.]+|-?inf)', linea)
        if m:
            try:
                pico = float(m.group(1))
            except ValueError:
                pico = None
    ok = r.returncode == 0 and not errores
    if r.returncode != 0 and not errores:
        errores.append(f'ffmpeg terminó con código {r.returncode}')
    return ok, errores[:4], pico, segundos


def id3_en_flac(ruta):
    """Tags ID3 pegados a un FLAC (no estándar): (al inicio, al final)."""
    try:
        with open(ruta, 'rb') as f:
            inicio = f.read(3) == b'ID3'
            f.seek(0, os.SEEK_END)
            if f.tell() < 128:
                return inicio, False
            f.seek(-128, os.SEEK_END)
            final = f.read(3) == b'TAG'
        return inicio, final
    except OSError:
        return False, False


def leer_cue(ruta):
    raw = Path(ruta).read_bytes()
    texto = None
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            texto = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    album = {'title': '', 'performer': '', 'date': '', 'genre': ''}
    pistas, archivo, actual = [], None, None

    def valor(linea):
        m = re.match(r'\s*\S+\s+(?:\S+\s+)?"(.*)"\s*$', linea)
        if m:
            return m.group(1)
        return linea.split(None, 2)[-1].strip().strip('"')

    for linea in (texto or '').splitlines():
        l = linea.strip()
        if not l:
            continue
        up = l.upper()
        if up.startswith('FILE '):
            m = re.match(r'FILE\s+"(.*)"\s+\S+$', l, re.I) or re.match(r'FILE\s+(\S+)\s+\S+$', l, re.I)
            archivo = m.group(1) if m else None
        elif up.startswith('TRACK '):
            try:
                n = int(l.split()[1])
            except (IndexError, ValueError):
                n = len(pistas) + 1
            actual = {'num': n, 'file': archivo, 'title': '', 'performer': '', 'start': None}
            pistas.append(actual)
        elif up.startswith('INDEX 01') and actual is not None:
            try:
                mm, ss, ff = (int(x) for x in l.split()[2].split(':'))
                actual['start'] = mm * 60 + ss + ff / 75.0
            except (IndexError, ValueError):
                pass
        elif up.startswith('TITLE '):
            (actual if actual is not None else album)['title'] = valor(l)
        elif up.startswith('PERFORMER '):
            (actual if actual is not None else album)['performer'] = valor(l)
        elif up.startswith('REM DATE '):
            album['date'] = valor(l)
        elif up.startswith('REM GENRE '):
            album['genre'] = valor(l)
    return album, [p for p in pistas if p['start'] is not None and p['file']]


# ─────────────────────────── análisis de una pista ───────────────────────────

def analizar_pista(ruta, base):
    info = ffprobe(ruta)
    stream = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), None)
    fmt = info.get('format', {})
    ext = Path(ruta).suffix.lower()
    p = {'archivo': os.path.relpath(ruta, base), 'ext': ext.lstrip('.'),
         'tamano': os.path.getsize(ruta), 'mtime': round(os.path.getmtime(ruta), 1)}
    if not stream:
        p.update({'legible': False, 'codec': None, 'codec_label': ext.lstrip('.').upper(),
                  'duracion': 0, 'tags': {}, 'clase': None})
        return p

    codec = (stream.get('codec_name') or '').lower()
    sr = int(stream.get('sample_rate') or 0)
    bits = int(stream.get('bits_per_raw_sample') or stream.get('bits_per_sample') or 0)
    sample_fmt = stream.get('sample_fmt') or ''
    es_float = sample_fmt.startswith(('flt', 'dbl')) and codec not in LOSSY_CODECS \
        and not codec.startswith('dsd') or codec.startswith('pcm_f')
    es_dsd = ext in ('.dsf', '.dff') or codec.startswith('dsd')
    lossy = codec in LOSSY_CODECS
    canales = int(stream.get('channels') or 0)
    try:
        duracion = float(stream.get('duration') or fmt.get('duration') or 0)
    except ValueError:
        duracion = 0.0
    br = stream.get('bit_rate') or (fmt.get('bit_rate') if not lossy else None) or fmt.get('bit_rate')
    try:
        br = int(float(br)) if br else None
    except ValueError:
        br = None
    portada = any(s.get('codec_type') == 'video' and (s.get('disposition') or {}).get('attached_pic')
                  for s in info.get('streams', []))
    # Algunas versiones no marcan attached_pic en FLAC: un stream mjpeg/png de 1 frame también cuenta
    portada = portada or any(s.get('codec_type') == 'video' and s.get('codec_name') in ('mjpeg', 'png')
                             for s in info.get('streams', []))

    ft, crudo_ff = tags_ffprobe(info)
    mt, crudo_mu = tags_mutagen(ruta)
    tags = dict(ft)
    for k, v in mt.items():                 # mutagen manda (ID3 dañados que ffprobe no lee)
        if v not in (None, ''):
            tags[k] = v
    if not tags.get('artista') and tags.get('artista_album'):
        tags['artista'] = tags['artista_album']
    crudo = dict(crudo_ff)
    crudo.update({k: v for k, v in crudo_mu.items() if v})

    # ── MQA: mismo criterio que index_libraryV3 ──
    encoder = str(crudo.get('encoder') or '').lower()
    orig_sr = crudo.get('originalsamplerate') or crudo.get('mqaoriginalsamplerate')
    valores = ' '.join(str(v) for v in crudo.values()).lower()
    es_mqa = bool('mqaencode' in encoder or 'mqaencoder' in crudo or orig_sr
                  or 'mqa' in valores or 'tidal masters' in valores
                  or 'mqa' in Path(ruta).name.lower())
    mqa_tipo = None
    if es_mqa:
        mqa_tipo = 'studio' if ('studio' in encoder or 'mqa studio' in valores) else 'mqa'

    if es_dsd:
        clase = 'DSD'
    elif es_mqa:
        clase = 'MQA'
    elif lossy:
        clase = 'Lossy'
    elif sr > 48000:
        clase = 'HR'
    else:
        clase = 'CD'

    if es_dsd:
        etiqueta = DSD_RATES.get(sr) or 'DSD'
    elif lossy:
        k = kbps_nominal(br, codec)
        etiqueta = f"{CODEC_LABEL.get(codec, codec.upper())} · {k} kbps" if k else CODEC_LABEL.get(codec, codec.upper())
    else:
        prof = '32f' if es_float else (str(bits) if bits else '?')
        etiqueta = f"{CODEC_LABEL.get(codec, codec.upper().replace('PCM_', 'PCM '))} {prof}/{sr / 1000:g}"

    p.update({'legible': True, 'codec': codec, 'codec_label': CODEC_LABEL.get(codec, codec.upper()),
              'formato': etiqueta, 'sr': sr, 'bits': bits, 'float': bool(es_float), 'dsd': es_dsd,
              'lossy': lossy, 'canales': canales, 'duracion': round(duracion, 2),
              'kbps': kbps_nominal(br, codec) if lossy else (round(br / 1000) if br else None),
              'mqa': es_mqa, 'mqa_tipo': mqa_tipo, 'mqa_sr_original': orig_sr,
              'clase': clase, 'portada_incrustada': portada, 'tags': tags,
              'muestras': _muestras(stream, sr),
              'edicion': _edicion(crudo)})
    if codec == 'flac':
        ini, fin = id3_en_flac(ruta)
        if ini or fin:
            p['id3'] = [x for x, v in (('inicio', ini), ('final', fin)) if v]
    return p


_CLAVES_EDICION = {
    'mbid': ('musicbrainz_albumid', 'musicbrainz album id'),
    'mb_grupo': ('musicbrainz_releasegroupid', 'musicbrainz release group id'),
    'sello': ('label', 'organization', 'publisher', 'tpub'),
    'catalogo': ('catalognumber', 'catalog #', 'catalog', 'labelno'),
    'barcode': ('barcode', 'upc', 'ean'),
    'pais': ('releasecountry', 'musicbrainz album release country'),
    'anio_original': ('originaldate', 'originalyear', 'original year', 'tdor', 'tory'),
    'fecha_edicion': ('releasedate', 'date', 'tdrc', 'year'),
}


def _edicion(crudo):
    """Datos de la edición si vienen en los tags (Picard, tiendas, rips de EAC…)."""
    limpio = {}
    for k, v in crudo.items():
        k = re.sub(r'^----:com\.apple\.itunes:', '', str(k).lower()).strip()
        if v not in (None, ''):
            limpio.setdefault(k, v)
    d = {}
    for campo, claves in _CLAVES_EDICION.items():
        for c in claves:
            if limpio.get(c):
                d[campo] = str(limpio[c]).strip()[:80]
                break
    for c in ('anio_original', 'fecha_edicion'):
        if d.get(c):
            d[c] = _anio(d[c]) or d[c]
    return {k: v for k, v in d.items() if v}


def _muestras(stream, sr):
    """Largo exacto en muestras (sirve para calcular el TOC de un rip de CD)."""
    try:
        if stream.get('time_base') == f'1/{sr}' and stream.get('duration_ts'):
            return int(stream['duration_ts'])
    except (TypeError, ValueError):
        pass
    return None


# ─────────────────────────── carátulas ───────────────────────────

def _puntaje_imagen(nombre):
    n = Path(nombre).stem.lower()
    if n in ('cover', 'front', 'folder'):
        return 0
    if re.search(r'\b(cover|front|folder)\b', n) and not re.search(r'back|inlay|inside|tray|booklet', n):
        return 1
    if re.search(r'back|inlay|inside|tray|booklet|matrix|label|obi|scan|spine', n):
        return 9
    return 5


def _info_imagen(ruta):
    d = {'archivo': ruta, 'tamano': os.path.getsize(ruta)}
    if Image is not None:
        try:
            with Image.open(ruta) as im:
                d['ancho'], d['alto'] = im.size
                d['formato'] = (im.format or '').upper()
        except Exception:
            d['ilegible'] = True
    with open(ruta, 'rb') as f:
        d['md5'] = hashlib.md5(f.read()).hexdigest()
    return d


CARPETAS_ARTE = re.compile(r'^(covers?|art(work)?s?|scans?|images?|imagenes|booklet|fotos?|pics?|caratulas?|carátulas?)$', re.I)


def _candidatos_caratula(ruta, imagenes, limite=40):
    """Todas las imágenes del álbum (raíz, discos y subcarpetas de arte), de más a menos probable."""
    lista = []
    for img in imagenes:
        rel = os.path.relpath(img, ruta)
        partes = rel.split(os.sep)
        nombre = Path(partes[-1]).stem.lower()
        puntaje = _puntaje_imagen(partes[-1])
        if len(partes) > 1 and any(CARPETAS_ARTE.match(x) for x in partes[:-1]):
            if puntaje == 5 and re.fullmatch(r'0*1|00|a', nombre):
                puntaje = 2               # «Covers/01.jpg»: suele ser la portada
            elif puntaje == 5:
                puntaje = 6
        lista.append((puntaje, len(partes), rel.lower(), img, rel))
    lista.sort()
    salida = []
    for puntaje, _, _, img, rel in lista[:limite]:
        d = _info_imagen(img)
        d['archivo'] = rel
        d['puntaje'] = puntaje
        salida.append(d)
    return salida


def mejor_imagen(carpeta):
    try:
        imgs = [a for a in os.listdir(carpeta)
                if Path(a).suffix.lower() in IMAGEN_EXT and not a.startswith('.')
                and os.path.isfile(os.path.join(carpeta, a))]
    except OSError:
        return None
    if not imgs:
        return None
    imgs.sort(key=lambda a: (_puntaje_imagen(a), a.lower()))
    if _puntaje_imagen(imgs[0]) >= 9:
        return None                       # solo hay contraportadas/escaneos
    return os.path.join(carpeta, imgs[0])


# ─────────────────────────── duplicados (music.db de Orbyte) ───────────────────────────

_CACHE_BIB = {'clave': None, 'datos': None}


def _norm(s):
    s = sin_acentos(s or '').lower()
    s = s.replace('&', ' and ')
    s = re.sub(r'^the\s+', '', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _norm_album(s):
    s = re.sub(r'\[[^\]]*\]', ' ', s or '')
    s = re.sub(r'\((?:[^)]*(?:remaster|deluxe|edition|edición|version|versión|expanded|anniversary|bonus|\d{4})[^)]*)\)',
               ' ', s, flags=re.I)
    s = re.sub(r'\s*[-–]\s*(?:cd|disc|disco)\s*\d+\s*$', ' ', s, flags=re.I)
    return _norm(s)


def _tag_existente(row):
    if row['dsd']:
        return 'DSD'
    if row['mqa'] and row['mqa'] * 2 >= row['n']:
        return 'MQA'
    if row['lossy'] * 2 > row['n']:
        return 'Lossy'
    return 'HR' if (row['sr'] or 0) > 48000 else 'CD'


def biblioteca(db, raiz='/mnt/musica'):
    """Álbumes de la biblioteca agrupados por carpeta de álbum (se juntan las «CD 01/CD 02»)."""
    raiz = os.path.abspath(raiz)
    try:
        clave = (db, os.path.getmtime(db), raiz)
    except OSError:
        return None
    if _CACHE_BIB['clave'] == clave:
        return _CACHE_BIB['datos']
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    lossy_sql = ("(COALESCE(t.compression_mode,'')='Lossy' OR LOWER(COALESCE(t.codec,'')) IN ("
                 + ','.join(f"'{c}'" for c in sorted(LOSSY_CODECS)) + "))")
    filas = con.execute(f'''
        SELECT al.id, al.name, al.year, al.path, al.primary_format, ar.name AS artista,
               COUNT(t.id) AS n, MAX(t.sample_rate) AS sr,
               SUM(CASE WHEN t.is_dsd=1 THEN 1 ELSE 0 END) AS dsd,
               SUM(CASE WHEN t.is_mqa=1 THEN 1 ELSE 0 END) AS mqa,
               SUM(CASE WHEN {lossy_sql} THEN 1 ELSE 0 END) AS lossy
        FROM albums al JOIN artists ar ON ar.id = al.artist_id
        LEFT JOIN tracks t ON t.album_id = al.id
        GROUP BY al.id''').fetchall()
    con.close()
    grupos = {}
    artistas = {}
    for f in filas:
        ruta = f['path'] or ''
        if dividir_por_disco(os.path.basename(ruta)) is not None:
            ruta = os.path.dirname(ruta)
        g = grupos.setdefault(ruta, {'ruta': ruta, 'album': f['name'], 'anio': f['year'],
                                     'artista': f['artista'], 'formato': f['primary_format'],
                                     'pistas': 0, 'tags': Counter(), 'ids': []})
        g['pistas'] += f['n'] or 0
        g['tags'][_tag_existente(f)] += f['n'] or 1
        g['ids'].append(f['id'])
        # /mnt/musica/<Letra>/<Artista>/… → (Letra, Artista)
        rel = os.path.relpath(ruta, raiz).split(os.sep) if ruta.startswith(raiz + os.sep) else []
        if len(rel) >= 3 and len(rel[0]) == 1:
            artistas.setdefault(_norm(f['artista']), Counter())[(rel[0], rel[1])] += 1
    for g in grupos.values():
        g['tag'] = g['tags'].most_common(1)[0][0] if g['tags'] else None
        g['tags'] = dict(g['tags'])
        g['n_artista'] = _norm(g['artista'])
        g['n_album'] = _norm_album(g['album'])
    datos = {'grupos': list(grupos.values()), 'artistas': artistas}
    _CACHE_BIB.update(clave=clave, datos=datos)
    return datos


def _parecido(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def buscar_duplicados(bib, artista, album):
    if not bib:
        return []
    na, nal = _norm(artista), _norm_album(album)
    encontrados = []
    for g in bib['grupos']:
        sa = _parecido(na, g['n_artista'])
        if sa < 0.88:
            continue
        sal = _parecido(nal, g['n_album'])
        if sal < 0.86:
            continue
        encontrados.append({'ruta': g['ruta'], 'album': g['album'], 'anio': g['anio'],
                            'artista': g['artista'], 'formato': g['formato'], 'tag': g['tag'],
                            'pistas': g['pistas'], 'similitud': round(min(sa, sal), 2)})
    encontrados.sort(key=lambda d: -d['similitud'])
    return encontrados[:5]


def carpeta_artista_existente(bib, artista):
    """(letra, carpeta) que ya usa la biblioteca para ese artista, si existe."""
    if not bib:
        return None
    c = bib['artistas'].get(_norm(artista))
    if c:
        return c.most_common(1)[0][0]
    return None


# ─────────────────────────── análisis del álbum ───────────────────────────

def _mayoria(valores):
    c = Counter(v for v in valores if v not in (None, ''))
    return c.most_common(1)[0][0] if c else None


def _desde_nombre_archivo(nombre):
    """«1-03 - Título.flac» / «03. Título.flac» / «03 Título» → (disco, pista, título)."""
    stem = Path(nombre).stem
    m = re.match(r'^\s*(\d)[-.](\d{1,3})[\s._-]+(.*)$', stem)
    if m:
        return int(m.group(1)), int(m.group(2)), m.group(3).strip(' -._') or None
    m = re.match(r'^\s*(\d{1,3})[\s._-]+(.*)$', stem)
    if m:
        return None, int(m.group(1)), m.group(2).strip(' -._') or None
    return None, None, stem


def _desde_carpeta(nombre):
    """«Artista - Álbum (2007) [tag]» / «2007 - Álbum» → (artista, álbum, año)."""
    anio = _anio(nombre)
    s = re.sub(r'\[[^\]]*\]', '', nombre)
    s = re.sub(r'\(\s*(1[89]\d\d|20\d\d)\s*\)', '', s)
    s = re.sub(r'^\s*(1[89]\d\d|20\d\d)\s*[-.]\s*', '', s).strip(' -')
    artista = None
    if ' - ' in s:
        artista, s = s.split(' - ', 1)
    return (artista or '').strip() or None, s.strip() or None, anio


VINILO_RE = re.compile(r'(?<![a-z])(lp|vinyl|vinilo|vinyl[- ]?rip|needledrop)(?![a-z])', re.I)
CATALOGO_RE = re.compile(r'(?<![A-Z0-9])([A-Z]{2,6}[- ]?[A-Z]{0,2}\d{3,7}[A-Z]?)(?![A-Z0-9])')


def _datos_para_edicion(ruta, pistas, legibles, info_cues, otros, artista, album, origen,
                        edicion, n_discos, discos_n, anios_carpeta, anio_tags, anio_orig, cue0):
    """Lo que identificar() necesita: duraciones en orden, TOC, catálogos, años y tipo de formato."""
    duraciones, por_disco = [], {}
    for d in discos_n:
        lista = []
        for p in sorted([p for p in pistas if p.get('disco') == d and p.get('legible')],
                        key=lambda p: (p.get('numero') or 999, p['archivo'])):
            if p.get('imagen_cue'):
                cue = next((c for c in info_cues for r in c['referencias'] if r['archivo'] == p['archivo']), None)
                if cue:
                    ref = next(r for r in cue['referencias'] if r['archivo'] == p['archivo'])
                    ini = [cp['inicio'] for cp in cue['pistas'] if cp['archivo_cue'] == ref['archivo_cue']]
                    fin = ini[1:] + [p.get('duracion') or 0]
                    lista += [round(b - a, 1) for a, b in zip(ini, fin)]
                    por_disco.setdefault(d, []).append(('cue', p, ini))
                    continue
            lista.append(round(p.get('duracion') or 0, 1))
            por_disco.setdefault(d, []).append(('pista', p, None))
        duraciones.append(lista)

    toc = None
    if n_discos == 1:
        for o in otros:
            if o.lower().endswith('.log'):
                try:
                    with open(os.path.join(ruta, o), 'rb') as f:
                        crudo = f.read(400000)
                    texto = crudo.decode('utf-16') if crudo[:2] in (b'\xff\xfe', b'\xfe\xff') else crudo.decode('utf-8', 'replace')
                    toc = edicion_mod.toc_desde_log_str(texto)
                except Exception:
                    toc = None
                if toc:
                    break
        elementos = por_disco.get(discos_n[0], []) if discos_n else []
        if not toc and len(elementos) == 1 and elementos[0][0] == 'cue':
            p = elementos[0][1]
            toc = edicion_mod.toc_desde_cue(elementos[0][2], p.get('muestras'), p.get('sr'))
        if not toc and elementos and all(e[0] == 'pista' and e[1].get('sr') == 44100 and not e[1].get('lossy')
                                         for e in elementos):
            toc = edicion_mod.toc_desde_pistas([e[1].get('muestras') for e in elementos])

    nombres = os.path.basename(ruta) + ' ' + os.path.basename(os.path.dirname(ruta))
    catalogos = []
    if edicion.get('catalogo'):
        catalogos.append(edicion['catalogo'])
    for c in CATALOGO_RE.findall(nombres):
        if not re.fullmatch(r'(CD|LP|HR|DSD|MQA|FLAC|WEB|SACD)\d*', c.replace(' ', '').replace('-', '')) \
                and not re.fullmatch(r'[A-Z]+[- ]?(19|20)\d\d', c):
            catalogos.append(c)
    barcodes = [re.sub(r'\D', '', edicion['barcode'])] if edicion.get('barcode') else []
    barcodes = [b for b in barcodes if len(b) in (12, 13)]

    anios = {a for a in [anio_tags, anio_orig, cue0.get('anio')] + list(anios_carpeta) if a}
    if any(p.get('dsd') for p in legibles):
        formato = 'sacd'
    elif VINILO_RE.search(nombres):
        formato = 'vinilo'
    elif any(p.get('lossy') for p in legibles) or any((p.get('sr') or 0) > 48000 for p in legibles):
        formato = 'digital'
    else:
        formato = 'cd'
    return {'artista': artista, 'album': album, 'duraciones': duraciones, 'toc': toc,
            'catalogos': list(dict.fromkeys(catalogos)), 'barcodes': barcodes, 'anios': anios,
            'formato': formato, 'mbid': edicion.get('mbid'),
            'tags_utiles': origen.get('artista') in ('tags', 'cue') and origen.get('album') in ('tags', 'cue'),
            'rutas_audio': [os.path.join(ruta, p['archivo']) for p in legibles[:3]]}


def analizar_album(unidad, ajustes, progreso=None, trabajadores=3, cliente_mb=None, eleccion=None, previo=None):
    """
    unidad: dict de detectar_albumes() (ruta, carpetas).
    ajustes: {'biblioteca': '/mnt/musica', 'db_orbyte': '.../music.db'}
    Devuelve el reporte (dict serializable a JSON).
    """
    t0 = time.time()
    ruta = unidad['ruta']
    problemas = []

    def problema(nivel, codigo, texto, pistas=None):
        problemas.append({'nivel': nivel, 'codigo': codigo, 'texto': texto, 'pistas': pistas or []})

    # ── inventario de archivos ──
    audio, imagenes, cues, otros = [], [], [], []
    for raiz, dirs, archivos in os.walk(ruta):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.'))
        for a in sorted(archivos):
            if a.startswith('._') or a.lower() in IGNORAR:
                continue
            full = os.path.join(raiz, a)
            ext = Path(a).suffix.lower()
            if ext in AUDIO_EXT:
                audio.append(full)
            elif ext in IMAGEN_EXT:
                imagenes.append(full)
            elif ext == '.cue':
                cues.append(full)
            else:
                otros.append(os.path.relpath(full, ruta))

    # ── discos ──
    carpetas_audio = sorted({os.path.dirname(a) for a in audio})
    multi = len(carpetas_audio) > 1 or any(c != ruta for c in carpetas_audio)
    disco_de_carpeta = {}
    for c in carpetas_audio:
        if c == ruta:
            disco_de_carpeta[c] = None
        else:
            disco_de_carpeta[c] = dividir_por_disco(os.path.basename(c))

    # ── lectura técnica + tags (rápido) ──
    total = len(audio)
    pistas = []
    for i, a in enumerate(audio, 1):
        if progreso:
            progreso(f'Leyendo {i}/{total}: {os.path.basename(a)}')
        p = analizar_pista(a, ruta)
        p['carpeta'] = os.path.relpath(os.path.dirname(a), ruta)
        if p['carpeta'] == '.':
            p['carpeta'] = ''
        pistas.append(p)

    # ── CUE ──
    info_cues = []
    imagenes_cue = {}           # archivo de audio (rel) → n.º de pistas del cue
    por_nombre = {}
    for p in pistas:
        por_nombre.setdefault((p['carpeta'], p['archivo'].split('/')[-1].lower()), p)
        por_nombre.setdefault((p['carpeta'], Path(p['archivo']).stem.lower()), p)
    for cue in cues:
        carpeta_rel = os.path.relpath(os.path.dirname(cue), ruta)
        carpeta_rel = '' if carpeta_rel == '.' else carpeta_rel
        try:
            meta, cpistas = leer_cue(cue)
        except Exception as e:
            problema('aviso', 'cue_ilegible', f'No se pudo leer {os.path.relpath(cue, ruta)}: {e}')
            continue
        refs = []
        for fname in sorted({cp['file'] for cp in cpistas}):
            base = os.path.basename(fname.replace('\\', '/'))
            p = por_nombre.get((carpeta_rel, base.lower())) or por_nombre.get((carpeta_rel, Path(base).stem.lower()))
            n = sum(1 for cp in cpistas if cp['file'] == fname)
            refs.append({'archivo_cue': base, 'archivo': p['archivo'] if p else None, 'pistas': n,
                         'nombre_distinto': bool(p and p['archivo'].split('/')[-1] != base)})
        info_cues.append({'archivo': os.path.relpath(cue, ruta), 'titulo': meta.get('title'),
                          'interprete': meta.get('performer'), 'anio': _anio(meta.get('date')),
                          'genero': meta.get('genre') or None, 'n_pistas': len(cpistas),
                          'referencias': refs,
                          'pistas': [{'num': cp['num'], 'titulo': cp['title'],
                                      'interprete': cp['performer'], 'inicio': round(cp['start'], 2),
                                      'archivo_cue': os.path.basename(cp['file'].replace('\\', '/'))}
                                     for cp in cpistas]})
        faltan = [r['archivo_cue'] for r in refs if not r['archivo']]
        if faltan:
            problema('error', 'cue_sin_audio',
                     f'El cue {os.path.basename(cue)} apunta a archivos que no están: {", ".join(faltan)}')
        for r in refs:
            if r['archivo'] and r['pistas'] > 1:
                imagenes_cue[r['archivo']] = r['pistas']

    for p in pistas:
        if p['archivo'] in imagenes_cue:
            p['imagen_cue'] = imagenes_cue[p['archivo']]

    # ── decodificación completa (en paralelo) ──
    hechas = [0]

    anteriores = {}
    for d in (previo or {}).get('discos') or []:
        for t in d.get('pistas') or []:
            if t.get('decodificado_s') is not None:
                anteriores[(t['archivo'], t['tamano'], t.get('mtime'))] = t

    def _dec(p):
        ant = anteriores.get((p['archivo'], p['tamano'], p.get('mtime')))
        if ant:          # mismo archivo y mismo tamaño que en la revisión anterior: no se decodifica de nuevo
            hechas[0] += 1
            ok_ant = ant.get('decodifica') and not ant.get('error_por_id3')
            return ok_ant, ant.get('errores') or [], ant.get('pico_db'), ant.get('decodificado_s')
        res = decodificar(os.path.join(ruta, p['archivo']))
        hechas[0] += 1
        if progreso:
            progreso(f'Decodificando {hechas[0]}/{total}')
        return res

    legibles = [p for p in pistas if p.get('legible')]
    with ThreadPoolExecutor(max_workers=max(1, trabajadores)) as ex:
        for p, (ok, errores, pico, segundos) in zip(legibles, ex.map(_dec, legibles)):
            p['errores'] = errores
            p['pico_db'] = None if pico is None else round(pico, 2)
            p['decodificado_s'] = None if segundos is None else round(segundos, 2)
            completo = segundos is not None and p.get('duracion') and segundos >= p['duracion'] - 0.5
            if not ok and completo and p.get('id3'):
                # El audio salió entero: el error es el tag ID3 pegado al FLAC (flac -t dice lo mismo)
                ok = True
                p['error_por_id3'] = True
            elif not ok and completo:
                p['error_completo'] = True
            p['decodifica'] = ok

    # ── problemas por pista ──
    ilegibles = [p['archivo'] for p in pistas if not p.get('legible')]
    if ilegibles:
        problema('error', 'ilegible', 'Archivos que ffprobe no reconoce como audio', ilegibles)
    danados = [p['archivo'] for p in legibles if not p.get('decodifica') and not p.get('error_completo')]
    if danados:
        problema('error', 'danado', 'Archivos con errores al decodificar (dañados o incompletos)', danados)
    raros = [p['archivo'] for p in legibles if p.get('error_completo')]
    if raros:
        problema('decision', 'errores_menores',
                 'Errores al decodificar, aunque el audio se leyó completo: escúchalos antes de aceptarlos', raros)
    con_id3 = [p['archivo'] for p in legibles if p.get('id3')]
    if con_id3:
        problema('correccion', 'id3_en_flac',
                 'Tags ID3 pegados al FLAC (no estándar; hacen fallar a algunos decodificadores): '
                 'se quitarán dejando los tags FLAC. El audio está completo.', con_id3)

    convertir = [p['archivo'] for p in legibles if p['codec'] in A_FLAC
                 or (p['codec'] or '').startswith('pcm_') and not p['dsd']]
    if convertir:
        tipos = sorted({p['codec_label'] if not (p['codec'] or '').startswith('pcm_') else 'WAV/AIFF'
                        for p in legibles if p['archivo'] in convertir})
        problema('correccion', 'convertir', f'Se convertirán a FLAC ({", ".join(tipos)})', convertir)
    flotantes = [p for p in legibles if p.get('float')]
    if flotantes:
        picos = [p['pico_db'] for p in flotantes if p.get('pico_db') is not None]
        pmax = max(picos) if picos else None
        txt = 'Audio en coma flotante: se pasará a FLAC 24 bits'
        if pmax is not None and pmax > -0.1:
            txt += f' con ajuste de ganancia de todo el álbum (pico {pmax:+.2f} dBFS)'
        problema('correccion', 'float_24', txt, [p['archivo'] for p in flotantes])
    if imagenes_cue:
        n = sum(imagenes_cue.values())
        problema('correccion', 'dividir_cue',
                 f'Imagen + cue: se dividirá en {n} pistas', sorted(imagenes_cue))

    multicanal = [p['archivo'] for p in legibles if p['canales'] > 2 or p['codec'] in ('eac3', 'ac3', 'dts', 'truehd')]
    if multicanal:
        problema('decision', 'multicanal',
                 'Audio multicanal / Dolby (Atmos, AC-3, DTS): Orbyte y el DAC trabajan en estéreo', multicanal)
    lossy = [p for p in legibles if p['lossy'] and p['archivo'] not in multicanal]
    bajos = [p['archivo'] for p in lossy if p.get('kbps') and p['kbps'] < 192]
    if bajos:
        problema('decision', 'lossy_bajo', 'Con pérdida a menos de 192 kbps', bajos)
    aac = [p['archivo'] for p in lossy if p['codec'] == 'aac' and p['archivo'] not in bajos]
    if aac:
        problema('aviso', 'aac', 'AAC: se conserva como Lossy (confirma que no exista en FLAC)', aac)

    clases = Counter(p['clase'] for p in legibles if p.get('clase'))
    if len(clases) > 1:
        problema('aviso', 'mezcla', 'El álbum mezcla calidades: ' +
                 ', '.join(f'{k} ({v})' for k, v in clases.most_common()))
    srs = Counter(p['sr'] for p in legibles if not p['dsd'] and not p['lossy'])
    if len(srs) > 1:
        problema('aviso', 'mezcla_sr', 'Frecuencias distintas entre pistas: ' +
                 ', '.join(f'{k / 1000:g} kHz ({v})' for k, v in srs.most_common()))
    mqa_carpeta = 'mqa' in os.path.basename(ruta).lower()
    if mqa_carpeta and not clases.get('MQA'):
        problema('aviso', 'mqa_sin_tags',
                 'La carpeta dice MQA pero los archivos no lo indican: Orbyte no lo marcaría como MQA')

    # ── tags del álbum ──
    tag = lambda k: [p['tags'].get(k) for p in legibles]
    artista_album = _mayoria(tag('artista_album'))
    artistas = Counter(v for v in tag('artista') if v)
    artista = artista_album or (artistas.most_common(1)[0][0] if artistas else None)
    album = _mayoria(tag('album'))
    anio = _mayoria(tag('anio')) or _mayoria(tag('anio_original'))
    genero = _mayoria(tag('genero'))
    car_art, car_alb, car_anio = _desde_carpeta(os.path.basename(ruta))
    cue0 = info_cues[0] if info_cues else {}

    origen = {'artista': 'tags', 'album': 'tags', 'anio': 'tags'}
    if not artista and cue0.get('interprete'):
        artista, origen['artista'] = cue0['interprete'], 'cue'
    if not album and cue0.get('titulo'):
        album, origen['album'] = cue0['titulo'], 'cue'
    if not anio and cue0.get('anio'):
        anio, origen['anio'] = cue0['anio'], 'cue'
    if not artista and car_art:
        artista, origen['artista'] = car_art, 'carpeta'
    if not album and car_alb:
        album, origen['album'] = car_alb, 'carpeta'
    if not anio and car_anio:
        anio, origen['anio'] = car_anio, 'carpeta'

    if not artista:
        problema('decision', 'sin_artista', 'Falta el artista (ni tags, ni cue, ni nombre de carpeta)')
        artista = 'Artista desconocido'
    elif origen['artista'] == 'carpeta':
        problema('decision', 'artista_inferido', f'Artista tomado del nombre de la carpeta: «{artista}» — confírmalo')
    elif origen['artista'] == 'cue':
        problema('aviso', 'artista_cue', f'Artista tomado del cue: «{artista}»')
    if not album:
        problema('decision', 'sin_album', 'Falta el nombre del álbum')
        album = os.path.basename(ruta)
    elif origen['album'] == 'carpeta':
        problema('decision', 'album_inferido', f'Álbum tomado del nombre de la carpeta: «{album}» — confírmalo')
    elif origen['album'] == 'cue':
        problema('aviso', 'album_cue', f'Álbum tomado del cue: «{album}»')
    # «Artista - Álbum» metido en el tag del álbum
    pref = re.match(r'^\s*(.+?)\s+[-–]\s+(.+)$', album or '')
    if pref and _norm(pref.group(1)) == _norm(artista):
        problema('correccion', 'album_con_artista',
                 f'El nombre del álbum trae el artista adelante: se usará «{pref.group(2).strip()}»')
        album = pref.group(2).strip()
    anio_tags = _mayoria(tag('anio'))
    anio_orig = _mayoria(tag('anio_original'))
    anios_carpeta = sorted(set(re.findall(r'(?<!\d)(1[89]\d\d|20\d\d)(?!\d)', os.path.basename(ruta))))
    fuentes = []
    if anio_tags:
        fuentes.append(('tags', anio_tags))
    if anio_orig and anio_orig != anio_tags:
        fuentes.append(('año original (tags)', anio_orig))
    if cue0.get('anio'):
        fuentes.append(('cue', cue0['anio']))
    if anios_carpeta:
        fuentes.append(('carpeta', ' / '.join(anios_carpeta)))
    if not anio:
        problema('decision', 'sin_anio', 'Falta el año del álbum')
    else:
        distintos = {anio_tags, cue0.get('anio')} - {None}
        choca = len(distintos) > 1 or (anios_carpeta and anio not in anios_carpeta
                                       and not (anio_orig and anio_orig in anios_carpeta))
        if choca:
            problema('decision', 'anio_conflicto',
                     'El año no coincide entre fuentes (' + ', '.join(f'{k}: {v}' for k, v in fuentes) +
                     f') — se propone {anio}; puede ser una reedición o remaster')
        elif origen['anio'] != 'tags':
            problema('aviso', 'anio_inferido', f'Año tomado del {origen["anio"]}: {anio}')
        if anio_orig and anio_tags and anio_orig != anio_tags:
            problema('aviso', 'reedicion', f'Reedición: año original {anio_orig}, esta edición {anio_tags}')
    if not artista_album and len(artistas) > 3:
        problema('decision', 'varios_artistas',
                 f'{len(artistas)} artistas distintos y sin «artista del álbum»: ¿recopilatorio (Various Artists)?')
    albumes_distintos = Counter(v for v in tag('album') if v)
    if len(albumes_distintos) > 1:
        problema('aviso', 'album_inconsistente', 'Las pistas no tienen el mismo nombre de álbum: ' +
                 ' / '.join(f'«{k}» ({v})' for k, v in albumes_distintos.most_common(4)))
    sin_artista_pista = [p['archivo'] for p in legibles if not p['tags'].get('artista')]
    if sin_artista_pista and len(sin_artista_pista) < len(legibles):
        problema('correccion', 'artista_pista', 'Pistas sin artista: se completará con el del álbum', sin_artista_pista)
    desconocidos = [p['archivo'] for p in legibles
                    if re.search(r'unknown|desconocid', str(p['tags'].get('artista') or ''), re.I)]
    if desconocidos:
        problema('decision', 'unknown_artist', 'Pistas con «Unknown Artist» en los tags', desconocidos)

    # ── discos y numeración ──
    for p in pistas:
        d_tag = p.get('tags', {}).get('disco')
        d_car = disco_de_carpeta.get(os.path.join(ruta, p['carpeta']) if p['carpeta'] else ruta)
        p['disco'] = d_car or d_tag or 1
        if d_car and d_tag and d_car != d_tag:
            p.setdefault('notas', []).append(f'tag de disco {d_tag}, carpeta {d_car}')
    discos_n = sorted({p['disco'] for p in pistas})
    n_discos = len(discos_n)
    if n_discos == 1 and multi and carpetas_audio != [ruta]:
        pass
    carpetas_sin_disco = [os.path.relpath(c, ruta) for c, d in disco_de_carpeta.items() if c != ruta and d is None]
    if carpetas_sin_disco:
        problema('decision', 'subcarpetas', 'Subcarpetas que no parecen discos: ' + ', '.join(carpetas_sin_disco))

    sin_num, sin_titulo = [], []
    for d in discos_n:
        del_disco = [p for p in pistas if p['disco'] == d and p.get('legible') and not p.get('imagen_cue')]
        usados = Counter()
        for p in del_disco:
            n = p['tags'].get('pista')
            dn, nn, tn = _desde_nombre_archivo(p['archivo'].split('/')[-1])
            if not n and nn:
                n = nn
                sin_num.append(p['archivo'])
            p['numero'] = n
            titulo = p['tags'].get('titulo')
            if not titulo:
                titulo = tn
                sin_titulo.append(p['archivo'])
            p['titulo'] = titulo
            if n:
                usados[n] += 1
        repetidos = [n for n, c in usados.items() if c > 1]
        if repetidos:
            problema('decision', 'pista_repetida',
                     f'Disco {d}: números de pista repetidos ({", ".join(map(str, sorted(repetidos)))})')
        if usados and not repetidos:
            esperado = set(range(1, max(usados) + 1))
            faltan = sorted(esperado - set(usados))
            if faltan and len(faltan) <= 10:
                problema('aviso', 'pista_faltante',
                         f'Disco {d}: faltan las pistas {", ".join(map(str, faltan))}')
        if any(p.get('numero') is None for p in del_disco):
            problema('decision', 'sin_numero',
                     f'Disco {d}: pistas sin número (ni en tags ni en el nombre)',
                     [p['archivo'] for p in del_disco if p.get('numero') is None])
    if sin_num:
        problema('correccion', 'numero_de_nombre', 'Número de pista tomado del nombre de archivo', sin_num)
    if sin_titulo:
        problema('correccion', 'titulo_de_nombre', 'Título tomado del nombre de archivo', sin_titulo)

    # ── calidad del álbum (mayoría de pistas; en empate manda la prioridad) ──
    clases_eff = Counter()
    for p in legibles:
        clases_eff[p['clase']] += imagenes_cue.get(p['archivo'], 1)
    if clases_eff:
        maximo = max(clases_eff.values())
        tag_calidad = next(t for t in PRIORIDAD_TAG if clases_eff.get(t) == maximo)
    else:
        tag_calidad = None
    formatos = Counter(p.get('formato') for p in legibles)
    resumen_formato = formatos.most_common(1)[0][0] if formatos else None

    # ── carátula ──
    car = {'tipo': 'ninguna'}
    principal = mejor_imagen(ruta)
    por_disco = []
    for c in carpetas_audio:
        if c == ruta:
            continue
        img = mejor_imagen(c)
        if img:
            por_disco.append({'disco': disco_de_carpeta.get(c) or 0, **_info_imagen(img)})
    if principal:
        car = {'tipo': 'archivo', **_info_imagen(principal)}
    elif por_disco:
        primero = sorted(por_disco, key=lambda x: x['disco'])[0]
        car = {'tipo': 'archivo', **{k: v for k, v in primero.items() if k != 'disco'}}
    else:
        con_portada = next((p for p in legibles if p.get('portada_incrustada')), None)
        if con_portada:
            car = {'tipo': 'incrustada', 'pista': con_portada['archivo']}
    candidatos = _candidatos_caratula(ruta, imagenes)
    if car['tipo'] == 'ninguna':
        utiles = [c for c in candidatos if c['puntaje'] <= 2]
        if utiles:
            elegido = utiles[0]
            car = {'tipo': 'archivo', **_info_imagen(os.path.join(ruta, elegido['archivo']))}
    con_incrustada = next((p['archivo'] for p in legibles if p.get('portada_incrustada')), None)
    if car.get('archivo'):
        car['archivo'] = os.path.relpath(car['archivo'], ruta)
    for d in por_disco:
        d['archivo'] = os.path.relpath(d['archivo'], ruta)
    distintas = len({d['md5'] for d in por_disco} | ({car['md5']} if car.get('md5') else set())) > 1
    car['por_disco'] = por_disco if distintas else []
    car['candidatos'] = candidatos
    car['incrustada_en'] = con_incrustada
    n_opciones = len({c['md5'] for c in candidatos if c['puntaje'] < 9}) + (1 if car['tipo'] == 'incrustada' else 0)

    if car['tipo'] == 'ninguna':
        if candidatos:
            problema('decision', 'sin_caratula',
                     f'Ninguna imagen parece la portada: elige entre las {len(candidatos)} de la carpeta o búscala en línea')
        else:
            problema('decision', 'sin_caratula', 'Sin carátula: habrá que buscarla en línea')
    elif car['tipo'] == 'incrustada':
        problema('correccion', 'caratula_extraer', 'Carátula solo incrustada: se guardará como Cover.jpg')
    else:
        if car.get('ilegible'):
            problema('decision', 'caratula_ilegible', f'La imagen {car["archivo"]} no se puede abrir')
        elif car['archivo'] != 'Cover.jpg' or car.get('formato') not in (None, 'JPEG'):
            problema('correccion', 'caratula_renombrar', f'Carátula «{car["archivo"]}» → Cover.jpg')
        lado = min(car.get('ancho') or 0, car.get('alto') or 0)
        if lado and lado < 500:
            problema('aviso', 'caratula_chica', f'Carátula pequeña ({car["ancho"]}×{car["alto"]} px)')
    if car['tipo'] != 'ninguna' and n_opciones > 1:
        problema('aviso', 'caratula_opciones',
                 f'Hay {n_opciones} imágenes que podrían ser la carátula: se propone la marcada; podrás elegir otra')
    if distintas:
        problema('correccion', 'caratula_por_disco',
                 'Cada disco trae su portada: se incrustará en sus pistas y se guardará como «Cover CD2.jpg»…')

    # ── edición (si los tags la traen) ──
    edicion = {}
    for campo in _CLAVES_EDICION:
        v = _mayoria([(p.get('edicion') or {}).get(campo) for p in legibles])
        if v:
            edicion[campo] = v

    # ── edición exacta (MusicBrainz) ──
    ed_mb = None
    if cliente_mb is not None:
        datos_ed = _datos_para_edicion(ruta, pistas, legibles, info_cues, otros, artista, album, origen,
                                       edicion, n_discos, discos_n, anios_carpeta, anio_tags, anio_orig, cue0)
        try:
            ed_mb = edicion_mod.identificar(cliente_mb, datos_ed, eleccion=eleccion, progreso=progreso)
        except Exception as e:
            ed_mb = {'estado': 'error', 'error': f'{type(e).__name__}: {e}', 'candidatas': []}
        ed_mb['datos'] = {'duraciones': datos_ed['duraciones'], 'toc': datos_ed.get('toc'),
                          'formato': datos_ed.get('formato'), 'catalogos': datos_ed.get('catalogos')}
        el = ed_mb.get('elegida')
        if el and el.get('anio'):
            anterior = anio
            anio = el['anio']
            problemas[:] = [x for x in problemas if x['codigo'] not in ('anio_conflicto', 'sin_anio', 'anio_inferido')]
            txt = f'Edición identificada ({ed_mb["metodo"]}): {el["anio"]}'
            txt += f' · {el["sello"]}' if el.get('sello') else ''
            txt += f' {el["catalogo"]}' if el.get('catalogo') else ''
            txt += f' · {el["pais"]}' if el.get('pais') else ''
            if el.get('anio_original') and el['anio_original'] != el['anio']:
                txt += f' — original {el["anio_original"]}'
            if anterior and anterior != anio:
                txt += f' (los tags decían {anterior})'
            if ed_mb['estado'] == 'anio':
                txt += '. Hay varias ediciones casi iguales del mismo año: se eligió la más parecida'
            problema('aviso', 'edicion', txt)
            if el.get('portada'):
                car.setdefault('candidatos', []).append({'archivo': None, 'mb': el['id'], 'puntaje': 3,
                                                         'origen': 'Cover Art Archive'})
                if car['tipo'] == 'ninguna':
                    car.update(tipo='caa', mb=el['id'])
                    problemas[:] = [x for x in problemas if x['codigo'] != 'sin_caratula']
                    problema('correccion', 'caratula_caa',
                             'Sin carátula en la carpeta: se usará la portada de la edición (Cover Art Archive)')
        elif ed_mb['estado'] == 'dudosa':
            problemas[:] = [x for x in problemas if x['codigo'] != 'anio_conflicto']
            problema('decision', 'elegir_edicion',
                     f'No hay una edición clara en MusicBrainz: elige entre {len(ed_mb["candidatas"])} candidatas')
        elif ed_mb['estado'] == 'sin_red':
            problema('aviso', 'mb_sin_red', 'No se pudo consultar MusicBrainz (sin conexión): se usó el año de los tags')
        elif ed_mb['estado'] == 'error':
            problema('aviso', 'mb_error', f'Falló la consulta a MusicBrainz: {ed_mb.get("error")}')
        else:
            problema('aviso', 'mb_sin_resultado', 'MusicBrainz no encontró esta edición: se usó el año de los tags')

    # ── destino propuesto ──
    bib = None
    db = ajustes.get('db_orbyte')
    if db and os.path.exists(db):
        try:
            bib = biblioteca(db, ajustes.get('biblioteca') or '/mnt/musica')
        except Exception as e:
            problema('aviso', 'db_orbyte', f'No se pudo leer la base de Orbyte para buscar duplicados: {e}')
    else:
        problema('aviso', 'db_orbyte', 'No se encontró music.db: no se revisaron duplicados')
    existente = carpeta_artista_existente(bib, artista)
    if existente:
        letra, carpeta_artista = existente
    else:
        letra, carpeta_artista = letra_de(artista), nombre_seguro(artista)
    nombre_album = nombre_seguro(f'{album} ({anio}) [{tag_calidad}]' if anio else f'{album} [{tag_calidad}]')
    raiz_bib = ajustes.get('biblioteca') or '/mnt/musica'
    ruta_destino = os.path.join(raiz_bib, letra, carpeta_artista, nombre_album)
    destino = {'letra': letra, 'artista': carpeta_artista, 'album': nombre_album,
               'ruta': ruta_destino, 'artista_existe': bool(existente),
               'ruta_existe': os.path.exists(ruta_destino)}
    if destino['ruta_existe']:
        problema('decision', 'destino_existe', f'Ya existe la carpeta de destino {ruta_destino}')

    # nombres de archivo propuestos
    def nombre_pista(d, n, titulo, ext):
        num = f'{n:02d}' if n else '00'
        pref = f'{d}-{num}' if n_discos > 1 else num
        return nombre_seguro(f'{pref} - {titulo or "Pista"}') + '.' + ext

    for p in pistas:
        if not p.get('legible') or p.get('imagen_cue'):
            continue
        ext = 'flac' if (p['codec'] in A_FLAC or (p['codec'] or '').startswith('pcm_') or p.get('float')) \
            and not p['dsd'] else p['ext'].lower()
        if ext == 'm4a' and p['codec'] == 'alac':
            ext = 'flac'
        p['nombre_destino'] = nombre_pista(p['disco'], p.get('numero'), p.get('titulo'), ext)
    for c in info_cues:
        disco_c = None
        carpeta_c = os.path.dirname(c['archivo'])
        disco_c = disco_de_carpeta.get(os.path.join(ruta, carpeta_c) if carpeta_c else ruta) or 1
        for cp in c['pistas']:
            cp['nombre_destino'] = nombre_pista(disco_c, cp['num'], cp['titulo'], 'flac')

    # ── duplicados ──
    duplicados = buscar_duplicados(bib, artista, album)
    if duplicados:
        d0 = duplicados[0]
        comp = ''
        if d0.get('tag') and tag_calidad:
            dif = RANGO_TAG.get(tag_calidad, 0) - RANGO_TAG.get(d0['tag'], 0)
            comp = ' — el nuevo es de MEJOR calidad' if dif > 0 else (
                ' — el nuevo es de PEOR calidad' if dif < 0 else ' — misma calidad')
        problema('decision', 'duplicado',
                 f'Ya está en la biblioteca: {d0["artista"]} — {d0["album"]} [{d0["tag"] or "?"}]{comp}')

    # ── estado ──
    niveles = Counter(p['nivel'] for p in problemas)
    if niveles.get('error'):
        estado = 'error'
    elif niveles.get('decision'):
        estado = 'requiere_revision'
    else:
        estado = 'revisado'

    n_pistas_final = sum(imagenes_cue.get(p['archivo'], 1) for p in legibles)
    discos = []
    for d in discos_n:
        discos.append({'numero': d, 'pistas': sorted(
            [p for p in pistas if p['disco'] == d],
            key=lambda p: (p.get('numero') or 999, p['archivo']))})

    orden = {n: i for i, n in enumerate(NIVELES)}
    problemas.sort(key=lambda x: orden.get(x['nivel'], 9))
    return {
        'version': VERSION_REPORTE,
        'analizado': time.time(),
        'segundos': round(time.time() - t0, 1),
        'carpeta': ruta,
        'nombre_carpeta': os.path.basename(ruta),
        'artista': artista, 'artista_album': artista_album, 'album': album, 'anio': anio,
        'genero': genero, 'origen_tags': origen,
        'calidad': {'tag': tag_calidad, 'conteo': dict(clases_eff), 'formato': resumen_formato,
                    'mqa': bool(clases.get('MQA'))},
        'n_discos': n_discos, 'n_pistas': n_pistas_final, 'n_archivos_audio': len(pistas),
        'duracion': round(sum(p.get('duracion') or 0 for p in legibles), 1),
        'tamano': sum(p['tamano'] for p in pistas),
        'discos': discos, 'cues': info_cues, 'caratula': car, 'otros_archivos': otros, 'edicion': edicion,
        'edicion_mb': ed_mb,
        'destino': destino, 'duplicados': duplicados,
        'problemas': problemas, 'conteo_problemas': dict(niveles), 'estado': estado,
    }


if __name__ == '__main__':      # prueba manual: python -m ingesta.analisis CARPETA [music.db]
    import sys
    unidades, sueltos = detectar_albumes(sys.argv[1])
    for u in unidades:
        rep = analizar_album(u, {'biblioteca': '/mnt/musica',
                                 'db_orbyte': sys.argv[2] if len(sys.argv) > 2 else None},
                             progreso=lambda m: print('  ', m, end='\r'))
        print(f"\n{u['rel']}: {rep['estado']} → {rep['destino']['ruta']}")
        for pr in rep['problemas']:
            print(f"   [{pr['nivel']}] {pr['texto']} {pr['pistas'][:3] if pr['pistas'] else ''}")
