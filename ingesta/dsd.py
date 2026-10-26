"""
Orbyte Ingesta — DSD sin pasar nunca por PCM.

  info(ruta)                     formato de un .dsf / .dff (frecuencia, canales, bytes por canal, DST o no)
  a_dsf(fuente, salida, b0, b1)  copia el flujo DSD (completo o el tramo [b0, b1) en bytes por canal) a un .dsf:
                                 sirve para pasar DFF → DSF y para cortar una imagen DSD + cue en pistas
  md5(ruta, b0, b1)              huella del flujo DSD de cada canal (para verificar que la copia es idéntica)

Un byte = 8 muestras DSD por canal. Los cortes de un cue (cuadros de 1/75 s) caen siempre en un byte exacto
(DSD64: 4704 bytes por cuadro), así que cortar no cambia ni una muestra.
El DSD dentro de este módulo se maneja como en DSF: canales separados, bit menos significativo primero.
"""
import hashlib
import os
import struct

BLOQUE = 4096                         # bytes por canal en cada bloque de un DSF
PASO = BLOQUE * 32                    # bytes por canal que se leen de una vez (~128 KB)
INVERTIR = bytes(int(f'{i:08b}'[::-1], 2) for i in range(256))
SILENCIO = bytes([0x69]).translate(INVERTIR)

# orden de canales de DSF según el tipo (1 mono … 7 = 5.1)
TIPOS_DSF = {1: ['C'], 2: ['FL', 'FR'], 3: ['FL', 'FR', 'C'], 4: ['FL', 'FR', 'BL', 'BR'],
             5: ['FL', 'FR', 'C', 'LFE'], 6: ['FL', 'FR', 'C', 'BL', 'BR'], 7: ['FL', 'FR', 'C', 'LFE', 'BL', 'BR']}
IDS_DFF = {b'SLFT': 'FL', b'SRGT': 'FR', b'MLFT': 'FL', b'MRGT': 'FR', b'LS  ': 'BL', b'RS  ': 'BR',
           b'C   ': 'C', b'LFE ': 'LFE'}


class ErrorDSD(Exception):
    pass


# ─────────────────────────── lectura ───────────────────────────

def _info_dsf(f, ruta):
    cab = f.read(28)
    if len(cab) < 28 or cab[:4] != b'DSD ':
        raise ErrorDSD(f'{os.path.basename(ruta)}: no es un DSF válido')
    fmt = f.read(52)
    if fmt[:4] != b'fmt ':
        raise ErrorDSD(f'{os.path.basename(ruta)}: DSF sin bloque fmt')
    _, version, formato, tipo, canales, fs, bps, muestras, bloque, _ = struct.unpack('<QIIIIIIQII', fmt[4:52])
    if formato != 0:
        raise ErrorDSD(f'{os.path.basename(ruta)}: DSF con formato {formato} no soportado')
    datos = f.read(12)
    if datos[:4] != b'data':
        raise ErrorDSD(f'{os.path.basename(ruta)}: DSF sin bloque de datos')
    bl = bloque or BLOQUE
    declarado = struct.unpack('<Q', datos[4:12])[0] - 12
    esperado = -(-((muestras + 7) // 8) // bl) * bl * canales
    return {'tipo_archivo': 'dsf', 'fs': fs, 'canales': canales, 'tipo_canales': tipo, 'muestras': muestras,
            'bytes_canal': (muestras + 7) // 8, 'bloque': bl, 'inicio': 28 + 52 + 12,
            'msb': bps == 8, 'dst': False,
            # bloques de datos de más (o de menos) respecto del conteo de muestras: ffmpeg avisa «Invalid data»
            'irregular': declarado != esperado, 'bloques_de_mas': (declarado - esperado) // (bl * canales or 1)}


def _chunks(f, fin):
    while f.tell() + 12 <= fin:
        cid = f.read(4)
        tam = struct.unpack('>Q', f.read(8))[0]
        pos = f.tell()
        yield cid, pos, tam
        f.seek(pos + tam + (tam & 1))


def _id3_al_inicio(f):
    """Largo de un ID3 pegado al inicio del archivo (lo hacía Ingesta antes con los DFF), o 0."""
    f.seek(0)
    cab = f.read(10)
    f.seek(0)
    if len(cab) < 10 or cab[:3] != b'ID3':
        return 0
    tam = (cab[6] << 21) | (cab[7] << 14) | (cab[8] << 7) | cab[9]
    return 10 + tam + (10 if cab[5] & 0x10 else 0)


def _info_dff(f, ruta):
    base = _id3_al_inicio(f)
    f.seek(base)
    cab = f.read(16)
    if len(cab) < 16 or cab[:4] != b'FRM8' or cab[12:16] != b'DSD ':
        raise ErrorDSD(f'{os.path.basename(ruta)}: no es un DFF válido')
    fin = base + 12 + struct.unpack('>Q', cab[4:12])[0]
    d = {'tipo_archivo': 'dff', 'msb': True, 'dst': False, 'id3_al_inicio': base}
    ids = []
    for cid, pos, tam in _chunks(f, fin):
        if cid == b'PROP':
            f.seek(pos)
            if f.read(4) != b'SND ':
                continue
            for sid, spos, stam in _chunks(f, pos + tam):
                f.seek(spos)
                if sid == b'FS  ':
                    d['fs'] = struct.unpack('>I', f.read(4))[0]
                elif sid == b'CHNL':
                    n = struct.unpack('>H', f.read(2))[0]
                    ids = [f.read(4) for _ in range(n)]
                    d['canales'] = n
                elif sid == b'CMPR':
                    d['dst'] = f.read(4) != b'DSD '
        elif cid in (b'DSD ', b'DST '):
            d['inicio'], d['largo'] = pos, tam
            d['dst'] = d['dst'] or cid == b'DST '
    if 'inicio' not in d or 'fs' not in d or not d.get('canales'):
        raise ErrorDSD(f'{os.path.basename(ruta)}: DFF incompleto')
    roles = [IDS_DFF.get(i) for i in ids]
    d['orden'] = list(range(d['canales']))
    d['tipo_canales'] = {1: 1, 2: 2}.get(d['canales'])
    for tipo, orden in TIPOS_DSF.items():           # multicanal: reordenar al orden de DSF
        if len(orden) == d['canales'] and sorted(orden) == sorted(r for r in roles if r):
            d['tipo_canales'] = tipo
            d['orden'] = [roles.index(r) for r in orden]
            break
    if not d['tipo_canales']:
        raise ErrorDSD(f'{os.path.basename(ruta)}: distribución de canales DFF no soportada '
                       f'({", ".join(i.decode("latin-1").strip() for i in ids)})')
    if not d['dst']:
        d['bytes_canal'] = d['largo'] // d['canales']
        d['muestras'] = d['bytes_canal'] * 8
    return d


def info(ruta):
    with open(ruta, 'rb') as f:                     # por el contenido, no por la extensión
        es_dsf = f.read(4) == b'DSD '
        f.seek(0)
        return _info_dsf(f, ruta) if es_dsf else _info_dff(f, ruta)


def _leer(ruta, d, b0, b1):
    """Genera, por tramos, una lista con los bytes de cada canal (LSB primero) del rango [b0, b1)."""
    with open(ruta, 'rb') as f:
        if d['tipo_archivo'] == 'dsf':
            bl, n = d['bloque'], d['canales']
            pos = b0
            while pos < b1:
                g, off = divmod(pos, bl)
                hasta = min(b1, (g + 1) * bl)
                f.seek(d['inicio'] + g * bl * n)
                grupo = f.read(bl * n)
                if len(grupo) < bl * n:
                    raise ErrorDSD(f'{os.path.basename(ruta)}: termina antes de lo que declara')
                partes = [grupo[c * bl + off: c * bl + off + (hasta - pos)] for c in range(n)]
                if d['msb']:
                    partes = [p.translate(INVERTIR) for p in partes]
                yield partes
                pos = hasta
        else:
            n = d['canales']
            f.seek(d['inicio'] + b0 * n)
            pos = b0
            while pos < b1:
                k = min(PASO, b1 - pos)
                crudo = f.read(k * n)
                if len(crudo) < k * n:
                    raise ErrorDSD(f'{os.path.basename(ruta)}: termina antes de lo que declara')
                yield [crudo[c::n].translate(INVERTIR) for c in d['orden']]
                pos += k


# ─────────────────────────── escritura ───────────────────────────

def a_dsf(fuente, salida, b0=0, b1=None, muestras=None):
    """Escribe en `salida` el tramo [b0, b1) (bytes por canal) del DSD de `fuente`. Devuelve los bytes por canal."""
    d = info(fuente)
    if d['dst']:
        raise ErrorDSD(f'{os.path.basename(fuente)}: DFF comprimido con DST: no se puede pasar a DSF sin descomprimir')
    b1 = d['bytes_canal'] if b1 is None else min(b1, d['bytes_canal'])
    if not 0 <= b0 < b1:
        raise ErrorDSD(f'{os.path.basename(fuente)}: tramo vacío ({b0}–{b1})')
    n, total = d['canales'], b1 - b0
    if muestras is None:
        muestras = d['muestras'] - b0 * 8 if b1 == d['bytes_canal'] else total * 8
    bloques = (total + BLOQUE - 1) // BLOQUE
    datos = bloques * BLOQUE * n
    with open(salida, 'wb') as o:
        o.write(b'DSD ' + struct.pack('<QQQ', 28, 28 + 52 + 12 + datos, 0))
        o.write(b'fmt ' + struct.pack('<QIIIIIIQII', 52, 1, 0, d['tipo_canales'], n, d['fs'], 1, muestras, BLOQUE, 0))
        o.write(b'data' + struct.pack('<Q', 12 + datos))
        pend = [bytearray() for _ in range(n)]
        for partes in _leer(fuente, d, b0, b1):
            for c in range(n):
                pend[c] += partes[c]
            while len(pend[0]) >= BLOQUE:
                o.write(b''.join(bytes(pend[c][:BLOQUE]) for c in range(n)))
                for c in range(n):
                    del pend[c][:BLOQUE]
        if pend[0]:
            # relleno del último bloque con silencio DSD (0x69, aquí con LSB primero = 0x96): si un
            # reproductor no respeta el conteo de muestras, se oye silencio y no un golpe
            o.write(b''.join(bytes(pend[c]) + SILENCIO * (BLOQUE - len(pend[c])) for c in range(n)))
    return total


def md5(ruta, b0=0, b1=None):
    """Huella del DSD de cada canal en [b0, b1), igual sea DSF o DFF."""
    d = info(ruta)
    if d['dst']:
        return None
    b1 = d['bytes_canal'] if b1 is None else min(b1, d['bytes_canal'])
    hs = [hashlib.md5() for _ in range(d['canales'])]
    for partes in _leer(ruta, d, b0, b1):
        for h, p in zip(hs, partes):
            h.update(p)
    return [h.hexdigest() for h in hs]


def byte_de(segundos, fs):
    """Posición (bytes por canal) de un instante; los cuadros de cue caen siempre en un byte exacto."""
    return int(round(segundos * fs / 8))
