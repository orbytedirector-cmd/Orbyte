"""MQA: detección en el audio, LED que corresponde y tags.

Tidal ya no marca los MQA en los tags, pero el audio sigue trayéndolo: el DAC lo reconoce (LED verde, azul o
magenta) y Orbyte no, porque el indexador decide por los tags. Aquí se busca la firma MQA en el audio mismo, como
hace el DAC, y se escriben los tags para que el indexador llegue al mismo LED (y no lo pierda al reindexar).

Detección (mismo método que MQA_identifier): el flujo MQA va escondido en un bit bajo de (izquierdo XOR derecho),
el bit 16 contando desde arriba (el bit 0 de un PCM de 16 bits, el bit 8 de uno de 24) o uno o dos más arriba.
Se decodifica el inicio de la pista a enteros de 32 bits alineados arriba (así el bit 16 es el mismo para 16 y
24 bits) y se busca la palabra de sincronía 0xbe0498c88 (36 bits). Detrás vienen la frecuencia original
(4 bits, de la muestra +3 a la +6) y la procedencia (5 bits, de la +29 a la +33): > 8 = MQA Studio.

LED (igual que index_libraryV3.get_led_color): MQA Studio → azul; otro MQA → verde; sin MQA: amarillo hasta 48 kHz,
blanco arriba (DSD aparte). Verificado contra el iFi ZEN DAC V2 (06-11-2026): el azul/verde lo decide la procedencia
de la firma, no la frecuencia original (un MQA 48 → 96 kHz se ve verde, y los «magenta» por frecuencia eran Studio).
El magenta (MQB) no se deduce del audio: se marca a mano cuando el DAC lo muestra, y queda coherente en todo:
tag MQAENCODER=«MQB» → el indexador lo lee como MQA tipo «mqb» → LED magenta, texto «MQB».
"""
import os
import re
import subprocess

SINCRONIA = format(0xbe0498c88, '036b')
SEGUNDOS = 20                       # inicio de la pista que se analiza
# byte 2 de cada muestra s32le = bits 16..23; el MQA va en el bit 16, 17 o 18 → bits 0, 1, 2 de ese byte
_TABLAS = [bytes(48 + ((i >> k) & 1) for i in range(256)) for k in range(3)]

LED_NOMBRE = {'yellow': 'CD', 'white': 'HR', 'green': 'MQA', 'blue': 'MQA Studio', 'magenta': 'MQB',
              'cyan': 'DSD', 'red': 'DSD256', 'silver': 'Lossy'}
# Tags que escribe Orbyte (el indexador marca MQA si hay MQAENCODER u ORIGINALSAMPLERATE, y Studio si algún valor
# dice «MQA Studio»).
TAG_TIPO, TAG_SR = 'MQAENCODER', 'ORIGINALSAMPLERATE'
_TAGS_MQA = ('mqaencoder', 'originalsamplerate', 'mqaoriginalsamplerate', 'mqastudio')


def _frecuencia_original(c):
    """4 bits: el bit bajo elige la base (0 → 44,1 kHz; 1 → 48 kHz); los 3 altos, invertidos, la potencia de 2."""
    base = 48000 if c & 1 else 44100
    mult = 1 << (((c >> 3) & 1) | (((c >> 2) & 1) << 1) | (((c >> 1) & 1) << 2))
    return base * mult * (2 if mult > 16 else 1)


def buscar_en_pcm(datos):
    """`datos`: PCM s32le estéreo intercalado. → {'mqa', 'studio', 'sr_original', 'bit'} o {'mqa': False}."""
    izq, der = datos[2::8], datos[6::8]                  # byte de los bits 16..23 de cada canal
    n = min(len(izq), len(der))
    if n < 64:
        return {'mqa': False}
    x = (int.from_bytes(izq[:n], 'big') ^ int.from_bytes(der[:n], 'big')).to_bytes(n, 'big')
    mejor = None
    for k, tabla in enumerate(_TABLAS):
        bits = x.translate(tabla)                        # b'0101…': un carácter por muestra
        j = bits.find(SINCRONIA.encode())
        if j < 0:
            continue
        i = j + 35                                       # muestra donde se completa la sincronía
        if i + 34 > len(bits):
            continue
        if mejor is None or i < mejor[0]:
            mejor = (i, k, bits)
    if not mejor:
        return {'mqa': False}
    i, k, bits = mejor
    orsf = int(bits[i + 3:i + 7], 2)
    procedencia = int(bits[i + 29:i + 34], 2)
    return {'mqa': True, 'studio': procedencia > 8, 'sr_original': _frecuencia_original(orsf), 'bit': 16 + k,
            'cod_procedencia': procedencia, 'cod_frecuencia': orsf}


def detectar(ruta, segundos=SEGUNDOS, prefijo=None):
    """Busca MQA en el inicio de la pista. → dict de buscar_en_pcm, o {'mqa': False, 'error': …}."""
    cmd = list(prefijo or []) + ['ffmpeg', '-nostdin', '-v', 'error'] + (['-threads', '1'] if prefijo else []) + \
        ['-i', ruta, '-map', '0:a:0', '-t', str(segundos), '-f', 's32le', '-acodec', 'pcm_s32le', 'pipe:1']
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return {'mqa': False, 'error': f'{type(e).__name__}: {e}'}
    if r.returncode != 0 and not r.stdout:
        return {'mqa': False, 'error': (r.stderr.decode('utf-8', 'replace').strip().splitlines() or ['ffmpeg falló'])[-1]}
    canales = _canales(ruta)
    if canales and canales != 2:
        return {'mqa': False, 'nota': f'{canales} canales (MQA es estéreo)'}
    return buscar_en_pcm(r.stdout)


def _canales(ruta):
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries', 'stream=channels',
                            '-of', 'default=nw=1:nk=1', ruta], capture_output=True, text=True, timeout=30)
        return int((r.stdout or '0').strip() or 0)
    except Exception:  # noqa: BLE001
        return None


def led(is_mqa, studio, sr, sr_original, is_dsd=False, dsd_rate=None, mqb=False):
    """El LED que pone el indexador (index_libraryV3.get_led_color)."""
    if is_mqa and mqb:
        return 'magenta'
    if is_mqa and studio:
        return 'blue'
    if is_mqa:
        return 'green'
    if is_dsd:
        return {'DSD64': 'cyan', 'DSD128': 'cyan', 'DSD256': 'red', 'DSD512': 'magenta'}.get(dsd_rate, 'cyan')
    return 'yellow' if (sr or 0) <= 48000 else 'white'


def texto(d, sr=None):
    """«MQA Studio · original 96 kHz» / «No es MQA»."""
    if not d or not d.get('mqa'):
        return 'No es MQA'
    t = 'MQB' if d.get('mqb') else 'MQA Studio' if d.get('studio') else 'MQA'
    o = d.get('sr_original')
    return t + (f' · original {o / 1000:g} kHz'.replace('.', ',') if o else '')


# ── tags ──

# Las URLs no dicen nada del audio (un tag URL de la tienda con «…MQA…»): mismo criterio que
# index_libraryV3.extract_metadata (_TAGS_URL).
_TAGS_URL = {'url', 'www', 'website', 'purl', 'source', 'wwwaudiofile', 'wwwaudiosource',
             'wwwcommercialinfo', 'wwwpublisher', 'wwwartist'}


def _es_url(k, v):
    return str(k).lower() in _TAGS_URL or '://' in str(v)


def tags_mqa(ruta):
    """Lo que dicen hoy los tags (mismo criterio que index_libraryV3): {'mqa', 'studio', 'sr_original', 'otros'}.
    `otros`: tags que no escribe Orbyte pero hacen que el indexador lo vea como MQA (p. ej. un comentario «MQA»)."""
    from mutagen import File
    try:
        f = File(ruta)
    except Exception:  # noqa: BLE001
        return None
    if f is None or f.tags is None:
        return {'mqa': False, 'studio': False, 'mqb': False, 'sr_original': None, 'otros': []}
    tags = {}
    for k, v in f.tags.items():
        k = str(k).lower()
        vals = v if isinstance(v, list) else [v]
        tags[k] = ' '.join(str(x) for x in vals)
    encoder = tags.get('encoder', '').lower()
    orig = tags.get('originalsamplerate') or tags.get('mqaoriginalsamplerate')
    valores = ' '.join(v for k, v in tags.items() if not _es_url(k, v)).lower()
    es = bool('mqaencode' in encoder or 'mqaencoder' in tags or orig or 'mqa' in valores
              or 'tidal masters' in valores or 'mqa' in os.path.basename(ruta).lower())
    otros = [k for k, v in tags.items() if k not in _TAGS_MQA and k != 'encoder' and not _es_url(k, v)
             and ('mqa' in v.lower() or 'tidal masters' in v.lower())]
    try:
        orig = int(re.sub(r'\D', '', str(orig))) if orig else None
    except ValueError:
        orig = None
    mqb = es and 'mqb' in valores
    return {'mqa': es, 'studio': es and not mqb and ('studio' in encoder or 'mqa studio' in valores), 'mqb': mqb,
            'sr_original': orig,
            'otros': otros, 'en_nombre': 'mqa' in os.path.basename(ruta).lower()}


def escribir(ruta, es_mqa, studio=False, sr_original=None, mqb=False):
    """Deja los tags MQA de un FLAC como se pide (el audio no se toca). Devuelve una lista de avisos."""
    from mutagen.flac import FLAC
    f = FLAC(ruta)
    if f.tags is None:
        f.add_tags()
    avisos = []
    for k in list(f.tags.keys()):
        if k.lower() in _TAGS_MQA:
            del f.tags[k]
    enc = (f.get('encoder') or [''])[0]
    if 'mqa' in enc.lower():
        del f['encoder']                             # un ENCODER «MQAEncode…» marcaría MQA aunque se quite
    if es_mqa:
        f[TAG_TIPO] = ['MQB' if mqb else 'MQA Studio' if studio else 'MQA']
        if sr_original:
            f[TAG_SR] = [str(int(sr_original))]
    else:
        for k in list(f.tags.keys()):
            vals = f.tags[k]
            if any(_es_url(k, v) for v in vals):
                continue                             # una URL con «MQA» no cuenta (ver _TAGS_URL)
            if any('mqa' in str(v).lower() or 'tidal masters' in str(v).lower() for v in vals):
                if k.lower() in ('comment', 'description', 'encoded_by', 'encodedby', 'encodersettings'):
                    del f.tags[k]
                else:
                    avisos.append(f'el tag {k} dice «MQA»: el indexador lo seguiría viendo como MQA')
        if 'mqa' in os.path.basename(ruta).lower():
            avisos.append('el nombre del archivo dice «MQA»: el indexador lo seguiría viendo como MQA')
    f.save()
    return avisos
