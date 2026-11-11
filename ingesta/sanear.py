"""Salud · Sanear FLAC con cuadros dañados (pedido de Niko, 11-11-2026).

Muchos FLAC de la biblioteca traen uno o pocos cuadros dañados (CRC malo, o cuadros que faltan). El audio se lee
completo, pero en ese punto cada reproductor hace algo distinto: libFLAC pone silencio, ffmpeg salta el cuadro (la
pista se corre unos milisegundos) y algunos se traban. Sanear deja un FLAC limpio que ningún dispositivo traba y en
el que el daño pasa desapercibido:

  medir(ruta)   decodifica con libFLAC (ctypes) y devuelve dónde están los cuadros malos y cuántos ms se pierden.
  sanear(ruta)  decodifica a PCM crudo (cada cuadro en su posición exacta: los que faltan dejan su hueco), rellena
                cada tramo malo continuando la forma de onda de ambos lados (ver rellenar()), lo vuelve a codificar con los mismos
                bits, frecuencia y canales, le copia todos los tags y carátulas del original y lo verifica (decodifica
                limpio y con el mismo número de muestras). El original queda respaldado en «.orbyte-respaldo/» de la
                carpeta del álbum (las carpetas con punto no las ve el indexador) y recién ahí se reemplaza.

Solo para tramos cortos (SANEAR_MAX_MS en total): si se pierde más, conviene reemplazar la pista (Reparar con Tidal).
Se corre como proceso aparte con prioridad mínima (python -m ingesta.sanear medir|sanear RUTA) para no molestar.
"""
import ctypes
import ctypes.util
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from array import array

SANEAR_MAX_MS = 1000
RESPALDO = '.orbyte-respaldo'

# ───────────────────────────── libFLAC ─────────────────────────────

_READ_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte),
                            ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p)
_WRITE_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.POINTER(ctypes.POINTER(ctypes.c_int32)), ctypes.c_void_p)
_ERROR_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p)
_FIN, _ABORTADO = 4, 7
_CRC, _FALTA_CUADRO = 2, 6          # FRAME_CRC_MISMATCH; MISSING_FRAME (libFLAC ≥ 1.4.3 lo rellena con silencio)
_lib = None


def _cargar():
    global _lib
    if _lib is not None:
        return _lib
    cands = [ctypes.util.find_library('FLAC')] + sorted(glob.glob('/usr/lib/*/libFLAC.so*')) \
        + sorted(glob.glob('/usr/lib/libFLAC.so*')) + sorted(glob.glob('/usr/local/lib/libFLAC.so*'))
    for c in cands:
        if not c:
            continue
        try:
            lib = ctypes.CDLL(c)
            lib.FLAC__stream_decoder_new.restype = ctypes.c_void_p
            lib.FLAC__stream_decoder_init_stream.restype = ctypes.c_int
            lib.FLAC__stream_decoder_init_stream.argtypes = [
                ctypes.c_void_p, _READ_CB, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                _WRITE_CB, ctypes.c_void_p, _ERROR_CB, ctypes.c_void_p]
            for fn in ('process_until_end_of_stream', 'finish', 'get_state', 'set_md5_checking'):
                getattr(lib, 'FLAC__stream_decoder_' + fn).restype = ctypes.c_int
            lib.FLAC__stream_decoder_process_until_end_of_stream.argtypes = [ctypes.c_void_p]
            lib.FLAC__stream_decoder_finish.argtypes = [ctypes.c_void_p]
            lib.FLAC__stream_decoder_get_state.argtypes = [ctypes.c_void_p]
            lib.FLAC__stream_decoder_set_md5_checking.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.FLAC__stream_decoder_delete.restype = None
            lib.FLAC__stream_decoder_delete.argtypes = [ctypes.c_void_p]
            try:
                lib.version = ctypes.c_char_p.in_dll(lib, 'FLAC__VERSION_STRING').value.decode()
            except Exception:  # noqa: BLE001
                lib.version = '?'
            _lib = lib
            return lib
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError('no se encontró libFLAC en el servidor (paquete libflac)')


def streaminfo(ruta):
    """(frecuencia, canales, bits, muestras totales) del STREAMINFO; salta un ID3v2 inicial."""
    with open(ruta, 'rb') as f:
        cab = f.read(10)
        off = 0
        if cab[:3] == b'ID3':
            off = 10 + ((cab[6] & 0x7f) << 21 | (cab[7] & 0x7f) << 14 | (cab[8] & 0x7f) << 7 | (cab[9] & 0x7f))
        f.seek(off)
        b = f.read(42)
    if b[:4] != b'fLaC' or (b[4] & 0x7f) != 0:
        raise ValueError('no es un FLAC')
    v = int.from_bytes(b[8 + 10:8 + 18], 'big')
    return v >> 44, ((v >> 41) & 7) + 1, ((v >> 36) & 0x1f) + 1, v & ((1 << 36) - 1)


class _Decodificar:
    """Una pasada de libFLAC. Si `salida` (archivo abierto), escribe el PCM entrelazado (s16le/s24le/s32le, cada
    cuadro en su posición exacta). Anota los tramos malos: CRC malo, cuadros que faltan y final cortado."""

    def __init__(self, ruta, salida=None):
        self.ruta, self.salida = ruta, salida
        self.sr, self.canales, self.bits, self.total = streaminfo(ruta)
        self.ancho = {16: 2, 24: 3, 32: 4}.get(self.bits)
        if salida is not None and not self.ancho:
            raise ValueError(f'{self.bits} bits: solo se sanean FLAC de 16, 24 o 32 bits')
        self.malas = []
        self.siguiente = 0
        self._pendiente = None
        self._sospecha = False
        self.fatal = None
        self.avisos = {}

    def _leer(self, _d, buf, n, _c):
        try:
            datos = self._f.read(n[0])
        except Exception as e:  # noqa: BLE001
            self.fatal = str(e)
            n[0] = 0
            return 2
        n[0] = len(datos)
        if not datos:
            return 1
        ctypes.memmove(buf, datos, len(datos))
        return 0

    def _escribir(self, _d, frame, buffer, _c):
        try:
            h32 = ctypes.cast(frame, ctypes.POINTER(ctypes.c_uint32))
            bloque, canales = h32[0], h32[2]
            ini = ctypes.cast(frame, ctypes.POINTER(ctypes.c_uint64))[3]     # number.sample_number (byte 24)
            if ini > self.siguiente:
                self.malas.append([self.siguiente, ini, 'faltan cuadros'])
            if self._pendiente is not None:
                self.malas.append([ini, ini + bloque, self._pendiente])
                self._pendiente = None
            elif self._sospecha:
                # libFLAC ≥ 1.4.3 rellena con silencio los cuadros que no pudo leer (sin avisar cuáles): después de
                # un error, los cuadros en cero exacto son esos; el primero con audio cierra la sospecha
                cero = bytes(bloque * 4)
                if all(ctypes.string_at(buffer[c], bloque * 4) == cero for c in range(canales)):
                    self.malas.append([ini, ini + bloque, 'cuadro ilegible'])
                else:
                    self._sospecha = False
            self.siguiente = max(self.siguiente, ini + bloque)
            if self.salida is not None:
                b, ch = self.ancho, canales
                out = bytearray(bloque * ch * b)
                for c in range(ch):
                    crudo = ctypes.string_at(buffer[c], bloque * 4)        # int32 LE, justificado a la derecha
                    for k in range(b):
                        out[c * b + k::ch * b] = crudo[k::4]
                self.salida.seek(ini * ch * b)
                self.salida.write(out)
            return 0
        except Exception as e:  # noqa: BLE001
            self.fatal = f'{type(e).__name__}: {e}'
            return 1

    def _error(self, _d, status, _c):
        nombre = {0: 'se perdió la sincronía', 1: 'cabecera de cuadro dañada', 2: 'CRC de cuadro malo',
                  3: 'flujo no soportado', 6: 'falta un cuadro'}.get(status, f'error {status}')
        self.avisos[nombre] = self.avisos.get(nombre, 0) + 1
        if status in (0, 1, _CRC, _FALTA_CUADRO):
            self._sospecha = True
        if status in (_CRC, _FALTA_CUADRO):
            self._pendiente = 'CRC malo' if status == _CRC else 'falta un cuadro'

    def correr(self):
        lib = _cargar()
        cbs = (_READ_CB(self._leer), _WRITE_CB(self._escribir), _ERROR_CB(self._error))
        dec = lib.FLAC__stream_decoder_new()
        if not dec:
            raise MemoryError('libFLAC: no se pudo crear el decodificador')
        try:
            with open(self.ruta, 'rb') as self._f:
                if lib.FLAC__stream_decoder_init_stream(dec, cbs[0], None, None, None, None, cbs[1], None, cbs[2],
                                                        None) != 0:
                    raise RuntimeError('libFLAC no pudo abrir el archivo')
                lib.FLAC__stream_decoder_process_until_end_of_stream(dec)
                estado = lib.FLAC__stream_decoder_get_state(dec)
                if estado >= _ABORTADO and not self.fatal:
                    self.fatal = f'libFLAC se detuvo (estado {estado})'
        finally:
            lib.FLAC__stream_decoder_finish(dec)
            lib.FLAC__stream_decoder_delete(dec)
        if self.total and self.siguiente < self.total:
            self.malas.append([self.siguiente, self.total, 'final cortado'])
        # tramos juntos o encimados → uno
        malas = []
        for a, b, m in sorted(self.malas):
            if malas and a <= malas[-1][1]:
                malas[-1][1] = max(malas[-1][1], b)
                if m not in malas[-1][2]:
                    malas[-1][2] += f', {m}'
            else:
                malas.append([a, b, m])
        self.malas = malas
        return self


def _resumen(d):
    perdidas = sum(b - a for a, b, _m in d.malas)
    return {'sr': d.sr, 'canales': d.canales, 'bits': d.bits, 'muestras': d.total or d.siguiente,
            'ms': round(perdidas * 1000 / d.sr, 1) if d.sr else None, 'tramos': len(d.malas),
            'puntos': [{'en_s': round(a / d.sr, 3), 'ms': round((b - a) * 1000 / d.sr, 1), 'motivo': m}
                       for a, b, m in d.malas[:50]],
            'fatal': d.fatal, 'avisos': d.avisos,
            'saneable': bool(d.malas) and not d.fatal and bool(d.ancho)
                        and perdidas * 1000 / d.sr <= SANEAR_MAX_MS,
            # más de SANEAR_MAX_MS: se puede sanear igual si se pide (Niko decide; 11-11-2026)
            'forzable': bool(d.malas) and not d.fatal and bool(d.ancho)}


def medir(ruta):
    """→ {'ms' perdidos, 'tramos', 'puntos': [{en_s, ms, motivo}], 'saneable', 'fatal', ...}"""
    return _resumen(_Decodificar(ruta).correr())


# ───────────────────────────── relleno ─────────────────────────────

def _a_int32(datos, ancho):
    """bytes PCM (s16/s24/s32 LE) → array('i') justificado a la izquierda (rango int32 completo)."""
    if ancho == 4:
        return array('i', datos)
    n = len(datos) // ancho
    b4 = bytearray(n * 4)
    for k in range(ancho):
        b4[4 - ancho + k::4] = datos[k::ancho]
    return array('i', bytes(b4))


def _de_int32(arr, ancho):
    b4 = arr.tobytes()
    if ancho == 4:
        return b4
    n = len(arr)
    out = bytearray(n * ancho)
    for k in range(ancho):
        out[k::ancho] = b4[4 - ancho + k::4]
    return bytes(out)


def _periodo(m, t0, n, sentido, dec, pmin, pmax, w):
    """Mejor período P (muestras) para continuar la forma de onda: compara la ventana de `w` muestras junto al
    hueco con la misma ventana corrida P (hacia atrás si sentido=-1, hacia adelante si +1). → (P, correlación)."""
    def corr(p, paso):
        sxy = sxx = syy = 0.0
        for k in range(0, w, paso):
            if sentido < 0:
                u, v = m[t0 - w + k], m[t0 - w + k - p]
            else:
                u, v = m[t0 + k], m[t0 + k + p]
            sxy += u * v
            sxx += u * u
            syy += v * v
        return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0
    if n < w + pmax:
        pmax = n - w
    if pmax <= pmin:
        return None, 0.0
    mejor = max(((corr(p, dec), p) for p in range(pmin, pmax, dec)), default=(0.0, None))
    if mejor[1] is None:
        return None, 0.0
    fino = max((corr(p, 1), p) for p in range(max(pmin, mejor[1] - dec), min(pmax, mejor[1] + dec + 1)))
    return fino[1], fino[0]


def rellenar(f, total, canales, ancho, a, b, sr=44100):
    """Rellena las muestras [a, b) del PCM crudo `f`, como el ocultamiento de pérdidas de la telefonía digital:
    - por la izquierda, el último período de la forma de onda antes del hueco se repite hacia adelante (el período
      es el corrimiento de mejor correlación, entre 2,5 y 20 ms); por la derecha, el primero después del hueco se
      repite hacia atrás; las dos continuaciones se funden con un coseno a lo largo del hueco, así que el relleno
      empalma sin saltos con lo que suena antes y con lo que viene después;
    - si un lado no es periódico (correlación < 0,5: ruido, un golpe), ese lado usa su espejo (el audio al revés),
      que también empalma sin saltos;
    - al comienzo o al final del archivo, se funde con silencio."""
    L = b - a
    w = max(32, int(sr * 0.01))
    pmin, pmax = max(16, int(sr * 0.0025)), max(32, int(sr * 0.02))
    margen = max(L, w + pmax)
    lo, hi = max(0, a - margen), min(total, b + margen)
    fb = canales * ancho
    f.seek(lo * fb)
    x = _a_int32(f.read((hi - lo) * fb), ancho)
    q = 1 << (32 - 8 * ancho)                 # el paso de los bits bajos (se cuantiza al ancho real)
    n_pre, n_post = a - lo, hi - b
    m = [sum(x[k * canales + c] for c in range(canales)) / (canales * 2147483648.0) for k in range(hi - lo)]
    dec = max(1, sr // 22050)
    p_iz, c_iz = _periodo(m, a - lo, n_pre, -1, dec, pmin, pmax, w) if n_pre else (None, 0.0)
    p_de, c_de = _periodo(m, b - lo, n_post, +1, dec, pmin, pmax, w) if n_post else (None, 0.0)
    if c_iz < 0.5:
        p_iz = None
    if c_de < 0.5:
        p_de = None
    for i in range(L):
        wt = 0.5 - 0.5 * math.cos(math.pi * (i + 0.5) / L)
        if n_pre:
            k_iz = (a - p_iz + (i % p_iz)) if p_iz else (a - 1 - (i % n_pre))
        if n_post:
            k_de = (b + ((i - L) % p_de)) if p_de else (b + (L - 1 - i) % n_post)
        for c in range(canales):
            iz = x[(k_iz - lo) * canales + c] if n_pre else 0
            de = x[(k_de - lo) * canales + c] if n_post else 0
            if not n_pre:
                v = wt * de
            elif not n_post:
                v = (1 - wt) * iz
            else:
                v = (1 - wt) * iz + wt * de
            v = int(round(v / q)) * q
            x[(a - lo + i) * canales + c] = max(-2147483648, min(2147483647 - (q - 1), v))
    f.seek(a * fb)
    f.write(_de_int32(x[(a - lo) * canales:(b - lo) * canales], ancho))
    return {'periodo_ms': [round(p * 1000 / sr, 2) if p else None for p in (p_iz, p_de)]}


# ───────────────────────────── sanear ─────────────────────────────

def _codificar(raw, d, salida):
    fmt = {2: 's16le', 3: 's24le', 4: 's32le'}[d.ancho]
    if shutil.which('flac'):
        cmd = ['flac', '-s', '-8', '-f', '--force-raw-format', '--endian=little', '--sign=signed',
               f'--channels={d.canales}', f'--bps={d.bits}', f'--sample-rate={d.sr}', '-o', salida, raw]
    else:
        cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', fmt, '-ar', str(d.sr), '-ac', str(d.canales),
               '-i', raw, '-map_metadata', '-1', '-c:a', 'flac', '-compression_level', '8',
               '-sample_fmt', 's16' if d.bits == 16 else 's32']
        if d.bits == 24:
            cmd += ['-bits_per_raw_sample', '24']
        if d.bits == 32:
            cmd += ['-strict', 'experimental']
        cmd += ['-f', 'flac', salida]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.isfile(salida):
        raise RuntimeError(f'no se pudo codificar: {(r.stderr or "").strip()[-300:]}')


def _copiar_metadata(origen, destino):
    from mutagen.flac import FLAC
    o, n = FLAC(origen), FLAC(destino)
    if n.tags is None:
        n.add_tags()
    n.tags.clear()
    if o.tags:
        n.tags.extend(list(o.tags))
    n.clear_pictures()
    for p in o.pictures:
        n.add_picture(p)
    n.save()


def sanear(ruta, tmpdir=None, forzar=False):
    """Sanea `ruta` en su lugar (el original va a .orbyte-respaldo/). → dict con lo que se hizo.
    `forzar`: aunque se pierda más de SANEAR_MAX_MS."""
    med = _Decodificar(ruta)
    if not med.ancho:
        raise ValueError(f'{med.bits} bits: solo se sanean FLAC de 16, 24 o 32 bits')
    carpeta = os.path.dirname(ruta)
    tmp = tempfile.mkdtemp(prefix='orbyte-sanear-', dir=tmpdir)
    try:
        raw = os.path.join(tmp, 'pcm.raw')
        with open(raw, 'w+b') as f:
            d = _Decodificar(ruta, salida=f).correr()
            if d.fatal:
                raise RuntimeError(f'no se pudo decodificar: {d.fatal}')
            res = _resumen(d)
            if not d.malas:
                raise RuntimeError('no tiene cuadros dañados que sanear')
            if not res['saneable'] and not forzar:
                raise RuntimeError(f'pierde {res["ms"]} ms: es mucho para sanear, conviene reemplazarla')
            total = d.total or d.siguiente
            f.truncate(total * d.canales * d.ancho)        # el final cortado queda como hueco (se rellena)
            for a, b, _m in d.malas:
                rellenar(f, total, d.canales, d.ancho, a, b, d.sr)
        # se codifica junto al original (mismo disco: el reemplazo final es atómico)
        nuevo = os.path.join(carpeta, f'.sanear-{os.getpid()}.flac')
        try:
            _codificar(raw, d, nuevo)
            os.remove(raw)
            _copiar_metadata(ruta, nuevo)
            v = _Decodificar(nuevo).correr()
            if v.malas or v.fatal or v.total != total or (v.sr, v.canales, v.bits) != (d.sr, d.canales, d.bits):
                raise RuntimeError(f'la versión saneada no pasó la verificación ({v.fatal or v.malas[:2] or v.total})')
            resp_dir = os.path.join(carpeta, RESPALDO)
            os.makedirs(resp_dir, exist_ok=True)
            respaldo = os.path.join(resp_dir, os.path.basename(ruta))
            if os.path.exists(respaldo):
                base, ext = os.path.splitext(respaldo)
                k = 2
                while os.path.exists(f'{base} ({k}){ext}'):
                    k += 1
                respaldo = f'{base} ({k}){ext}'
            shutil.copy2(ruta, respaldo)
            if os.path.getsize(respaldo) != os.path.getsize(ruta):
                raise RuntimeError('el respaldo del original quedó incompleto')
            os.chmod(nuevo, os.stat(ruta).st_mode & 0o7777)
            os.replace(nuevo, ruta)
            nuevo = None
        finally:
            if nuevo and os.path.exists(nuevo):
                os.remove(nuevo)
        res.update(respaldo=respaldo, ok=True, forzada=forzar and not res['saneable'])
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    accion, ruta = sys.argv[1], sys.argv[2]
    try:
        if accion == 'medir':
            out = medir(ruta)
        elif accion == 'sanear':
            extra = sys.argv[3:]
            forzar = '--forzar' in extra
            extra = [x for x in extra if x != '--forzar']
            out = sanear(ruta, extra[0] if extra else None, forzar=forzar)
        else:
            raise SystemExit(f'acción desconocida: {accion}')
    except Exception as e:  # noqa: BLE001
        out = {'ok': False, 'error': f'{e}'}
    print(json.dumps(out, ensure_ascii=False))
