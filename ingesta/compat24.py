"""Copias 24 bit de los FLAC de 32 bits (pedido de Niko, 07-11-2026).

Los FLAC de 32 bits (FLAC 1.4, 2022; p.ej. Avantasia «The Metal Opera Pt.I HR», 32/192) no los abre
casi ningún decodificador: ni iPhone, ni Android, ni el navegador, ni libsndfile (Desktop). El original
NO se toca: se crea una copia FLAC 24 bit con la misma frecuencia (se conservan los 24 bits altos, lo que
se pierde queda bajo −144 dB) y el servidor la manda a quien no puede con el original. Desktop avisa que
lo sabe leer (?flac32=1, libFLAC) y recibe el original bit a bit.

- Copias en <biblioteca>/.orbyte-compat24/<xx>/<sha1 de la ruta>.flac (+ .json con origen, tamaño y fecha
  del original: si el original cambia, la copia deja de valer). Carpeta con punto: ningún escaneo la indexa.
- La crea la ingesta (hilo `Compat24`, de a una, con prioridad baja) y, si un cliente la pide antes, el
  servidor en el momento (crear(), con candado entre procesos: nunca dos ffmpeg para el mismo archivo).
- Lo usan ingesta/servicio.py (hilo + Salud) y app.py (_serve_audio).
"""
import fcntl
import hashlib
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

log = logging.getLogger('ingesta.compat24')

CARPETA = '.orbyte-compat24'
VERSION = 1                     # sube si cambia cómo se hace la copia (invalida las anteriores)
_BITS_COPIA = 24


# ---------------------------------------------------------------- detección
_cache_bits = {}


def _streaminfo(ruta):
    """(bits por muestra, muestras totales) del STREAMINFO de un FLAC, o None. Salta un ID3v2 inicial."""
    try:
        with open(ruta, 'rb') as f:
            cab = f.read(10)
            off = 0
            if cab[:3] == b'ID3' and len(cab) == 10:
                for b in cab[6:10]:
                    off = (off << 7) | (b & 0x7F)
                off += 10
            f.seek(off)
            cab = f.read(42)
    except OSError:
        return None
    if len(cab) < 42 or cab[:4] != b'fLaC' or (cab[4] & 0x7F) != 0:
        return None
    v = int.from_bytes(cab[18:26], 'big')     # 20 frecuencia · 3 canales-1 · 5 bits-1 · 36 muestras
    return ((v >> 36) & 0x1F) + 1, v & ((1 << 36) - 1)


def _bloque_y_frecuencia(ruta):
    """(tamaño máximo de bloque, frecuencia) del STREAMINFO, o (None, None)."""
    try:
        with open(ruta, 'rb') as f:
            cab = f.read(10)
            off = 0
            if cab[:3] == b'ID3' and len(cab) == 10:
                for b in cab[6:10]:
                    off = (off << 7) | (b & 0x7F)
                off += 10
            f.seek(off)
            cab = f.read(42)
        if len(cab) < 42 or cab[:4] != b'fLaC':
            return None, None
        return int.from_bytes(cab[10:12], 'big'), int.from_bytes(cab[18:21], 'big') >> 4
    except OSError:
        return None, None


def bits_flac(ruta):
    """Bits por muestra según el STREAMINFO del FLAC (None si no es FLAC). Con caché por tamaño y fecha."""
    try:
        st = os.stat(ruta)
    except OSError:
        return None
    clave = (ruta, st.st_size, st.st_mtime_ns)
    if clave not in _cache_bits:
        if len(_cache_bits) > 5000:
            _cache_bits.clear()
        si = _streaminfo(ruta)
        _cache_bits[clave] = si[0] if si else None
    return _cache_bits[clave]


def es_flac32(ruta):
    """True si es un FLAC de más de 24 bits (lo que no abren los clientes)."""
    if not str(ruta).lower().endswith('.flac'):
        return False
    b = bits_flac(ruta)
    return b is not None and b > _BITS_COPIA


# ---------------------------------------------------------------- rutas
def raiz_copias(biblioteca='/mnt/musica'):
    return os.path.join(biblioteca, CARPETA)


def _rutas(original, biblioteca):
    h = hashlib.sha1(os.path.normpath(original).encode('utf-8', 'surrogateescape')).hexdigest()
    base = os.path.join(raiz_copias(biblioteca), h[:2], h)
    # el candado va en el disco local (/tmp): la biblioteca es un recurso SMB y ahí flock no es confiable;
    # los dos procesos que crean copias (ingesta y Orbyte) corren en el mismo servidor
    return base + '.flac', base + '.json', os.path.join(tempfile.gettempdir(), f'orbyte-compat24-{h}.lock')


def _firma(original):
    st = os.stat(original)
    return {'origen': os.path.normpath(original), 'tam': st.st_size, 'mtime': st.st_mtime_ns, 'v': VERSION}


def copia_valida(original, biblioteca='/mnt/musica'):
    """Ruta de la copia 24 bit si existe y corresponde al original tal como está ahora; si no, None."""
    flac, meta, _ = _rutas(original, biblioteca)
    try:
        with open(meta, encoding='utf-8') as f:
            m = json.load(f)
        firma = _firma(original)
        if all(m.get(k) == firma[k] for k in firma) and os.path.isfile(flac) and os.path.getsize(flac) == m.get('tam_copia'):
            return flac
    except (OSError, ValueError):
        pass
    return None


def _original_danado(ruta, errores_ffmpeg, rate):
    """¿El original tiene frames que no se pueden leer? Lo decide `flac -t` (libFLAC, verifica CRC y MD5) si
    está instalado; si no, que ffmpeg haya avisado errores de lectura. Devuelve (sí/no, ' en el seg. X')."""
    if shutil.which('flac'):
        try:
            r = subprocess.run(['flac', '-t', ruta], capture_output=True, timeout=900)
        except (OSError, subprocess.TimeoutExpired):
            return bool(errores_ffmpeg), ''
        if r.returncode == 0:
            return False, ''
        m = re.search(r'after processing (\d+) samples', r.stderr.decode(errors='replace'))
        return True, (f' en el seg. {int(m.group(1)) / rate:.1f}' if m and rate else '')
    return bool(errores_ffmpeg), ''


def _leer_json(ruta):
    try:
        with open(ruta, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def detalle(original, biblioteca='/mnt/musica'):
    """(nota de la copia si la hay, error del último intento fallido si sigue vigente)."""
    flac, meta, _ = _rutas(original, biblioteca)
    m = _leer_json(meta) or {}
    fallo = fallo_vigente(original, biblioteca)
    return m.get('nota'), (fallo or {}).get('error')


def fallo_vigente(original, biblioteca='/mnt/musica'):
    """El último intento fallido, si el original no cambió desde entonces (no tiene sentido reintentar)."""
    f = _leer_json(_rutas(original, biblioteca)[0][:-5] + '.fallo.json')
    try:
        firma = _firma(original)
    except OSError:
        return None
    return f if f and all(f.get(k) == firma[k] for k in firma) else None


def _anotar_fallo(original, biblioteca, error):
    try:
        f = _firma(original)
        f.update(error=str(error)[:500], cuando=time.strftime('%Y-%m-%d %H:%M:%S'))
        ruta = _rutas(original, biblioteca)[0][:-5] + '.fallo.json'
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, 'w', encoding='utf-8') as fh:
            json.dump(f, fh, ensure_ascii=False)
    except OSError:
        pass


def crear(original, biblioteca='/mnt/musica', nice=0, timeout=1800):
    """Crea (o reusa) la copia 24 bit. Devuelve su ruta. Levanta RuntimeError si no se pudo."""
    hecha = copia_valida(original, biblioteca)
    if hecha:
        return hecha
    flac, meta, candado = _rutas(original, biblioteca)
    os.makedirs(os.path.dirname(flac), exist_ok=True)
    try:
        lk = open(candado, 'a')
    except OSError:
        lk = None                               # sin candado (otro usuario lo creó): se arma igual
    try:
        if lk:
            fcntl.flock(lk, fcntl.LOCK_EX)      # otro proceso la está creando: se espera y se reusa
        return _crear(original, biblioteca, flac, meta, nice, timeout)
    finally:
        if lk:
            fcntl.flock(lk, fcntl.LOCK_UN)
            lk.close()


def _crear(original, biblioteca, flac, meta, nice, timeout):
    hecha = copia_valida(original, biblioteca)
    if hecha:
        return hecha
    firma = _firma(original)
    tmp = f'{flac}.{os.getpid()}.tmp'
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', original,
           '-map', '0:a:0', '-map', '0:v?', '-c:v', 'copy', '-map_metadata', '0',
           '-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', str(_BITS_COPIA),
           '-compression_level', '5', '-f', 'flac', tmp]
    if nice:
        cmd = ['nice', '-n', str(nice)] + cmd
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _borrar(tmp)
        raise RuntimeError(f'ffmpeg tardó más de {timeout} s')
    if r.returncode != 0 or not os.path.isfile(tmp):
        _borrar(tmp)
        raise RuntimeError('ffmpeg: ' + (r.stderr.decode(errors='replace').strip()[-300:] or f'código {r.returncode}'))
    bits = bits_flac(tmp)
    mo, mc = _muestras(original), _muestras(tmp)
    errores = r.stderr.decode(errors='replace').strip()
    nota = None
    if bits == _BITS_COPIA and mo and mc and mo != mc:
        # Original dañado (visto en 6 de 79 pistas, 07-11-2026): un frame con el CRC malo a mitad de la
        # pista; `flac -t` aborta ahí y ffmpeg lo salta y sigue, así que a la copia le faltan esos 1-2
        # bloques (6-12 ms). La copia es lo más completo que se puede sacar del original: se acepta, con
        # nota, solo si ffmpeg avisó el error de lectura y lo que falta no pasa de unos pocos bloques.
        bloque, rate = _bloque_y_frecuencia(original)
        falta = mo - mc
        danado, donde = _original_danado(original, errores, rate)
        if danado and bloque and 0 < falta <= 4 * bloque:
            nota = (f'original dañado{donde}: no se pueden leer {falta} muestras '
                    f'({1000 * falta / (rate or 1):.0f} ms); la copia lleva todo lo demás')
            mo = mc
    if bits != _BITS_COPIA or not mo or mo != mc:
        _borrar(tmp)
        raise RuntimeError(f'la copia no quedó bien (bits {bits}, muestras {mc} de {mo})'
                           + (f' · ffmpeg: {errores[-200:]}' if errores else ''))
    os.replace(tmp, flac)
    firma.update(tam_copia=os.path.getsize(flac), bits=bits, muestras=mo, nota=nota,
                 creada=time.strftime('%Y-%m-%d %H:%M:%S'), segundos=round(time.monotonic() - t0, 1))
    with open(meta + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(firma, f, ensure_ascii=False)
    os.replace(meta + '.tmp', meta)
    _borrar(_rutas(original, biblioteca)[0][:-5] + '.fallo.json')
    log.info('Copia 24 bit lista (%.0f s)%s: %s', time.monotonic() - t0, f' — {nota}' if nota else '', original)
    return flac


def _muestras(ruta):
    si = _streaminfo(ruta)
    return si[1] if si else None


def _borrar(ruta):
    try:
        os.remove(ruta)
    except OSError:
        pass


# ---------------------------------------------------------------- biblioteca
def pistas_32(db, con_album=False):
    """[(id, ruta)] de las pistas FLAC de 32 bits según music.db ([(id, ruta, album_id, álbum, artista)]
    con con_album)."""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    try:
        filas = con.execute("SELECT t.id, t.file_path, t.album_id, a.name, COALESCE(t.album_artist, t.artist) "
                            "FROM tracks t LEFT JOIN albums a ON a.id = t.album_id "
                            "WHERE LOWER(t.codec)='flac' AND t.bit_depth > 24 ORDER BY t.file_path").fetchall()
    finally:
        con.close()
    filas = [(f[0], '/' + f[1].lstrip('/')) + tuple(f[2:]) for f in filas if f[1]]
    return filas if con_album else [f[:2] for f in filas]


def estado(db, biblioteca='/mnt/musica'):
    """Resumen para Salud: por álbum (carpeta), cuántas pistas de 32 bits tienen su copia."""
    albumes = {}
    listas = pendientes = fallidas = 0
    for _id, ruta, album_id, album, artista in pistas_32(db, con_album=True):
        carpeta = os.path.dirname(ruta)
        a = albumes.setdefault(carpeta, {'carpeta': carpeta, 'album_id': album_id, 'album': album or os.path.basename(carpeta),
                                         'artista': artista or '', 'pistas': 0, 'listas': 0, 'avisos': []})
        a['pistas'] += 1
        nota, error = detalle(ruta, biblioteca)
        if copia_valida(ruta, biblioteca):
            a['listas'] += 1
            listas += 1
            if nota:
                a['avisos'].append({'pista': os.path.basename(ruta), 'texto': nota, 'tipo': 'nota'})
        else:
            pendientes += 1
            if error:
                fallidas += 1
                a['avisos'].append({'pista': os.path.basename(ruta), 'texto': error, 'tipo': 'error'})
    return {'listas': listas, 'pendientes': pendientes, 'fallidas': fallidas,
            'albumes': sorted(albumes.values(), key=lambda a: a['carpeta'].lower())}


class Compat24:
    """Hilo de la ingesta: crea las copias que falten, de a una y con prioridad baja.
    Revisa al arrancar, cada `cada` segundos (lo que entró a la biblioteca) y cuando se le pide."""

    def __init__(self, ajustes, cada=900):
        self._ajustes = ajustes          # función -> dict (almacen.ajustes)
        self._cada = cada
        self._despertar = threading.Event()
        self.actual = None
        self.ultimo_error = None
        self.hechas = 0
        self.revisando = False
        self._forzar = False

    def start(self):
        threading.Thread(target=self._bucle, name='compat24', daemon=True).start()

    def ahora(self):
        """«Crear las que faltan ahora»: también reintenta las que fallaron."""
        self._forzar = True
        self._despertar.set()

    def _bucle(self):
        time.sleep(60)                    # que arranque todo lo demás primero
        while True:
            try:
                self._pasada()
            except Exception:  # noqa: BLE001
                log.exception('Compat24: pasada')
            self._despertar.wait(self._cada)
            self._despertar.clear()

    def _pasada(self):
        self.revisando = True
        try:
            self._pasada_()
        finally:
            self.revisando = False

    def _pasada_(self):
        aj = self._ajustes()
        db, bib = aj.get('db_orbyte'), aj.get('biblioteca') or '/mnt/musica'
        if not db or not os.path.isdir(bib):
            return
        forzar, self._forzar = self._forzar, False
        for _id, ruta in pistas_32(db):
            if not os.path.isfile(ruta) or copia_valida(ruta, bib) or not es_flac32(ruta):
                continue
            if not forzar and fallo_vigente(ruta, bib):
                continue                      # ya falló y el original no cambió: no se reintenta solo
            self.actual = ruta
            try:
                crear(ruta, bib, nice=15)
                self.hechas += 1
                self.ultimo_error = None
            except Exception as e:  # noqa: BLE001
                self.ultimo_error = f'{os.path.basename(ruta)}: {e}'
                _anotar_fallo(ruta, bib, e)
                log.warning('Compat24: %s', self.ultimo_error)
            finally:
                self.actual = None
            time.sleep(2)
        huerfanas(bib)


def huerfanas(biblioteca='/mnt/musica'):
    """Borra las copias cuyo original ya no existe (álbum borrado o movido). Devuelve cuántas."""
    raiz = raiz_copias(biblioteca)
    if not os.path.isdir(raiz) or not any(e for e in os.listdir(biblioteca) if not e.startswith('.')):
        return 0                              # biblioteca sin montar/vacía: no se toca nada
    n = 0
    for d, _sub, archivos in os.walk(raiz):
        for a in archivos:
            if not a.endswith('.json'):
                continue
            meta = os.path.join(d, a)
            try:
                with open(meta, encoding='utf-8') as f:
                    origen = json.load(f).get('origen')
            except (OSError, ValueError):
                continue
            if origen and not os.path.exists(origen):
                _borrar(meta[:-5] + '.flac')
                _borrar(meta)
                n += 1
                log.info('Compat24: copia huérfana borrada (ya no está %s)', origen)
    return n
