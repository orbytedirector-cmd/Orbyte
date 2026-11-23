"""Parchar (23-11-2026): reemplaza un tramo dañado de una pista con el mismo tramo de OTRA copia (Tidal u otro
archivo), alineado a la muestra y convertido a la frecuencia y los bits de la pista.

Caso que lo originó: Avantasia, *The Metal Opera* 32/192, «Sign of the Cross» 0:32.76–0:33.17 y «Serpents in
Paradise» 0:54.04–0:54.31: TARTAMUDEOS (el mismo trozo de 13–22 ms repetido una y otra vez; la música original no
está en el archivo). Ningún relleno sirve: se reemplaza el tramo con el de otra copia.

  detectar(ruta)                       tramos donde el audio se repite a sí mismo (ingesta/clics.py)
  parchar(pista, otra, desde, hasta, salida)
      1. Alinea las dos copias con 3 s de audio sano antes y después del tramo: envolvente cada 10 ms (±10 s; se
         prueban sus 6 mejores picos, porque la música se repite por compases), forma de onda a 8 kHz y a la
         frecuencia de la pista, antes y después por separado y a fracción de muestra.
      2. Si el daño corrió el tiempo (la pista quedó con unos ms de más o de menos), se sigue la alineación de antes
         hasta la mitad del tramo y la de después desde ahí (fundido de 10 ms).
      3. Iguala el volumen por canal (misma energía que la pista alrededor) y la polaridad; re-muestrea con soxr.
      4. Reemplaza el tramo y lo funde 5 ms a cada lado, fuera del tramo. Fuera del tramo y de los fundidos, cada
         muestra queda idéntica. Mismos bits, frecuencia y canales; tags y carátulas copiados; se verifica.
  vista_previa(ruta, desde, hasta, salida)   20 s alrededor del tramo en FLAC 24 bit (lo que abre el navegador)

Corre como proceso aparte con prioridad mínima (python -m ingesta.parche detectar|parchar|vista ...).
"""
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import logging
from array import array

from . import clics, sanear

log = logging.getLogger('ingesta.parche')

CONTEXTO_S = 3.0
FUNDIDO_MS = 5
MAX_TRAMO_S = 2.0
MINIMO_PARECIDO = 0.3        # forma de onda hasta 4 kHz: otra masterización da 0,5–0,7; otra grabación, < 0,2
AVISO_PARECIDO = 0.7
VISTA_S = 20


class Fallo(Exception):
    pass


def pcm_f32(ruta, desde, dur, sr, canales, soxr=True):
    """Audio de `ruta` desde `desde` (s), re-muestreado a `sr`, como listas float por canal."""
    desde = max(0.0, desde)
    base = ['ffmpeg', '-v', 'error', '-nostdin', '-ss', f'{desde:.6f}', '-t', f'{dur:.6f}', '-i', ruta,
            '-map', '0:a:0', '-ac', str(canales)]
    for usar in ((True, False) if soxr else (False,)):
        af = ['-af', f'aresample={sr}:resampler=soxr:precision=28'] if usar else ['-ar', str(sr)]
        r = subprocess.run(base + af + ['-f', 'f32le', '-'], capture_output=True)
        if r.returncode == 0:
            break
    else:
        raise RuntimeError(f'ffmpeg no pudo leer {ruta}: {r.stderr.decode(errors="replace")[-300:]}')
    a = array('f')
    a.frombytes(r.stdout[:len(r.stdout) - len(r.stdout) % (4 * canales)])
    if sys.byteorder != 'little':
        a.byteswap()
    return [list(a[c::canales]) for c in range(canales)]


def envolvente(x, paso):
    return [math.sqrt(sum(v * v for v in x[i:i + paso]) / paso) for i in range(0, len(x) - paso + 1, paso)]


def ncorr(u_pairs):
    sxy = sxx = syy = 0.0
    for u, v in u_pairs:
        sxy += u * v
        sxx += u * u
        syy += v * v
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def mejor_lag(tpl, ref, lags, quitar_media=False, todos=False, signo=None):
    """tpl: [(pos, valor)] de la pista; ref: lista de la otra copia. Lag l: tpl[pos] ~ ref[pos + l].
    → (correlación, lag) del mejor; con todos=True, la lista [(correlación, lag)] de cada lag.
    Sin `signo` gana la mayor en valor absoluto; con signo (+1/−1, la polaridad ya decidida) gana la mayor con ese
    signo: un bajo de 80 Hz da un pico invertido a medio período (6 ms) que no debe ganar."""
    if quitar_media:
        m = sum(v for _, v in tpl) / len(tpl)
        tpl = [(p, v - m) for p, v in tpl]
    mejor = (0.0, None)
    lista = []
    for l in lags:
        par = [(v, ref[p + l]) for p, v in tpl if 0 <= p + l < len(ref)]
        if len(par) < len(tpl) * 0.9:
            continue
        if quitar_media:
            mr = sum(b for _, b in par) / len(par)
            par = [(a, b - mr) for a, b in par]
        c = ncorr(par)
        lista.append((c, l))
        if (signo * c > signo * mejor[0] or mejor[1] is None) if signo else abs(c) > abs(mejor[0]):
            mejor = (c, l)
    return lista if todos else mejor


def codificar(raw, d, salida):
    """PCM crudo → FLAC con los mismos bits, frecuencia y canales (flac < 1.4 no codifica 32 bit: ffmpeg)."""
    try:
        sanear._codificar(raw, d, salida)
        return
    except RuntimeError:
        if d.bits != 32 or not shutil.which('ffmpeg'):
            raise
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 's32le', '-ar', str(d.sr), '-ac', str(d.canales),
           '-i', raw, '-map_metadata', '-1', '-c:a', 'flac', '-compression_level', '8', '-sample_fmt', 's32',
           '-strict', 'experimental', '-f', 'flac', salida]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'no se pudo codificar: {r.stderr.strip()[-300:]}')


def parchar(pista, otra, desde, hasta, salida, buscar=10.0, tmpdir=None):
    """Escribe en `salida` la pista con [desde, hasta] (s) tomado de `otra`. → informe {lineas, parecido,
    corrimiento_ms, volumen_db, aviso, ...}. Lanza Fallo si no se puede hacer bien."""
    if not pista.lower().endswith('.flac'):
        raise Fallo('Solo se parchan FLAC')
    if not 0 < hasta - desde <= MAX_TRAMO_S:
        raise Fallo(f'El tramo debe durar entre 0 y {MAX_TRAMO_S:g} s')
    if os.path.realpath(os.path.dirname(salida)) == os.path.realpath(os.path.dirname(pista)):
        raise Fallo('La salida no puede quedar en la carpeta de la pista')
    informe = {'lineas': [], 'volumen_db': [], 'aviso': None}
    tmp = tempfile.mkdtemp(prefix='parche-', dir=tmpdir)
    try:
        return _parchar(pista, otra, desde, hasta, salida, buscar, tmp, informe)
    except Fallo as e:
        e.lineas = informe['lineas']
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _parchar(pista, otra, desde, hasta, salida, buscar, tmp, informe):
    sr_p, ch, _b, dur = clics.info(pista)
    d0 = desde - CONTEXTO_S
    d1 = hasta + CONTEXTO_S
    if d0 < 0 or d1 > dur:
        raise Fallo('Hacen falta 3 s de audio sano antes y después del tramo.')
    hueco = (desde, hasta)

    # ── 1) envolvente cada 10 ms a 8 kHz, mono ──
    sr8, paso = 8000, 80
    p8 = pcm_f32(pista, d0, d1 - d0, sr8, 1, soxr=False)[0]
    o0 = d0 - buscar
    o8 = pcm_f32(otra, o0, (d1 - d0) + 2 * buscar, sr8, 1, soxr=False)[0]
    if len(o8) < len(p8):
        raise Fallo('La otra copia es demasiado corta en ese punto.')
    off0 = int(round((max(0.0, o0) - d0) * sr8))   # índice en o8 de t = d0 (si no hubiera corrimiento) es -off0
    ep, eo = envolvente(p8, paso), envolvente(o8, paso)
    fuera = [(i, v) for i, v in enumerate(ep)
             if not (hueco[0] - 0.05 <= d0 + i * paso / sr8 <= hueco[1] + 0.05)]
    rango = int(buscar * sr8 / paso) + 2
    lista = mejor_lag(fuera, eo, range(-off0 // paso - rango, -off0 // paso + rango + 1), quitar_media=True,
                      todos=True)
    if not lista:
        raise Fallo('No se pudo alinear (¿es la misma canción?).')
    # la música se repite por compases: la envolvente puede confundirse en un pulso. Se prueban sus 6 mejores picos
    # con la forma de onda (antes y después del tramo juntos, cada 2 muestras) y gana el que mejor calza.
    picos = [(c, l) for k, (c, l) in enumerate(lista)
             if c > 0 and all(c >= lista[j][0] for j in range(max(0, k - 3), min(len(lista), k + 4)))]
    picos = sorted(picos, reverse=True)[:6]
    tpl_ambos = [(i, v) for i, v in enumerate(p8) if i % 2 == 0 and (
        hueco[0] - 1.0 <= d0 + i / sr8 < hueco[0] - 0.02 or hueco[1] + 0.02 < d0 + i / sr8 <= hueco[1] + 1.0)]
    cand = []
    for c, l in picos:
        cw, lw = mejor_lag(tpl_ambos, o8, range(l * paso - 3 * paso, l * paso + 3 * paso + 1))
        if lw is not None:
            cand.append((abs(cw), c, lw, cw))
    if not cand:
        raise Fallo('No se pudo alinear (¿es la misma canción?).')
    _a, c1, l1w, cw = max(cand)
    # la polaridad es de la grabación entera: se decide una vez, con antes y después juntos
    polaridad = -1.0 if cw < 0 else 1.0
    # ── 2) forma de onda a 8 kHz, antes y después por separado (±3 cuadros) ──
    lados = {'antes': (hueco[0] - 1.0, hueco[0] - 0.02), 'después': (hueco[1] + 0.02, hueco[1] + 1.0)}
    l8 = {}
    for lado, (t0, t1) in lados.items():
        tpl8 = [(i, v) for i, v in enumerate(p8) if t0 <= d0 + i / sr8 < t1]
        l8[lado] = mejor_lag(tpl8, o8, range(l1w - paso, l1w + paso + 1), signo=polaridad)
        if l8[lado][1] is None:
            raise Fallo('No se pudo alinear la forma de onda.')
    corr_s = {lado: l / sr8 + (max(0.0, o0) - d0) for lado, (_c, l) in l8.items()}   # t_otra = t_pista + corr_s
    informe['lineas'].append(f'envolvente {c1:.3f} · onda 8 kHz antes {abs(l8["antes"][0]):.3f} / después {abs(l8["después"][0]):.3f}'
          f' · la otra copia va {corr_s["antes"]:+.4f} s' + (' · polaridad invertida' if polaridad < 0 else ''))

    # ── 3) a la frecuencia de la pista, antes y después por separado ──
    sr = sr_p
    v0 = desde - 0.3
    v1 = hasta + 0.3
    marg = 0.1
    cmin = min(corr_s.values())
    ventana_o0 = v0 + cmin - marg
    po = pcm_f32(otra, ventana_o0, (v1 - v0) + abs(corr_s['después'] - corr_s['antes']) + 2 * marg, sr, ch)
    # la pista, exacta (libFLAC, cada muestra en su lugar)
    raw = os.path.join(tmp, 'pcm.raw')
    with open(raw, 'w+b') as f:
        d = sanear._Decodificar(pista, salida=f).correr()
    if d.fatal or d.malas:
        raise Fallo('La pista tiene cuadros dañados: primero Sanear.')
    if d.sr != sr:
        raise Fallo('Frecuencia distinta entre ffprobe y libFLAC.')
    fb = ch * d.ancho
    vb, ve = int(round(v0 * sr)), int(round(v1 * sr))
    with open(raw, 'rb') as f:
        f.seek(vb * fb)
        xx = sanear._a_int32(f.read((ve - vb) * fb), d.ancho)
    pp = [[v / 2147483648.0 for v in xx[c::ch]] for c in range(ch)]
    suma = [sum(po[c][j] for c in range(ch)) for j in range(len(po[0]))]
    paso8 = sr // sr8
    res = {}
    for lado, (t0, t1) in (('antes', (v0, desde - 0.02)), ('después', (hasta + 0.02, v1))):
        base = int(round((corr_s[lado] - cmin + marg) * sr))      # pp[i] ~ po[i + base]
        i0, i1 = int((t0 - v0) * sr), int((t1 - v0) * sr)
        tpl = [(i, sum(pp[c][i] for c in range(ch))) for i in range(i0, i1, 2)]
        c, l = mejor_lag(tpl, suma, range(base - 4 * paso8, base + 4 * paso8 + 1), signo=polaridad)
        if l is None:
            raise Fallo('No se pudo alinear a la frecuencia de la pista.')
        # fracción de muestra: parábola por la correlación en l-1, l, l+1
        cm, _ = mejor_lag(tpl, suma, [l - 1])
        cp, _ = mejor_lag(tpl, suma, [l + 1])
        c_, cm, cp = polaridad * c, polaridad * cm, polaridad * cp
        den = cm - 2 * c_ + cp
        frac = max(-0.5, min(0.5, 0.5 * (cm - cp) / den)) if den else 0.0
        res[lado] = (c, l + frac)
    (ca, la), (cd, ld) = res['antes'], res['después']
    corrido = ld - la
    informe['lineas'].append(f'a {sr} Hz: antes {polaridad * ca:+.4f} · después {polaridad * cd:+.4f}'
          + (f' · la pista tiene {abs(corrido) / sr * 1000:.2f} ms {"de más" if corrido < 0 else "de menos"} en el '
             f'tramo (el daño corrió el tiempo; se empalma a la mitad)' if abs(corrido) > 1 else ''))
    if abs(corrido) > 0.05 * sr:
        raise Fallo('El corrimiento antes y después difiere en más de 50 ms: no se parcha.')
    if abs(corrido) <= 1:
        la = ld = (la + ld) / 2

    def otra_en(c, j):
        """Muestra de la otra copia en la posición fraccionaria j (interpolación lineal)."""
        k = int(math.floor(j))
        t = j - k
        return po[c][k] * (1 - t) + po[c][k + 1] * t

    def ctx():
        for (t0, t1), lag in (((v0, desde - 0.02), la), ((hasta + 0.02, v1), ld)):
            for i in range(int((t0 - v0) * sr), int((t1 - v0) * sr)):
                yield i, lag

    # ── volumen por canal: igual energía que la pista en el contexto ──
    gan = []
    peor = 1.0
    for c in range(ch):
        sxy = syy = sxx = 0.0
        for i, lag in ctx():
            y = polaridad * otra_en(c, i + lag)
            sxy += pp[c][i] * y
            syy += y * y
            sxx += pp[c][i] * pp[c][i]
        g = math.sqrt(sxx / syy) if syy else 1.0
        cor = sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0
        peor = min(peor, cor)
        err = sum((pp[c][i] - g * polaridad * otra_en(c, i + lag)) ** 2 for i, lag in ctx())
        gan.append(g)
        informe['volumen_db'].append(round(20 * math.log10(abs(g)), 2))
        informe['lineas'].append(f'canal {c + 1}: volumen ×{g:.4f} ({20 * math.log10(abs(g)):+.2f} dB) · parecido a toda frecuencia {cor:.3f} · '
              f'diferencia {10 * math.log10(err / sxx + 1e-20):.1f} dB bajo la música')
    # «parecido»: la forma de onda hasta 4 kHz (a 8 kHz), con la polaridad decidida, el peor de antes y después.
    # A toda frecuencia dos masterizaciones distintas se parecen mucho menos (agudos, fase del EQ) aunque sea la
    # misma grabación; eso queda en las líneas de cada canal.
    parecido = min(polaridad * l8['antes'][0], polaridad * l8['después'][0])
    informe.update(parecido=round(parecido, 3), parecido_completo=round(peor, 3),
                   corrimiento_ms=round(corrido / sr * 1000, 2))
    if parecido < MINIMO_PARECIDO:
        raise Fallo(f'Las copias casi no se parecen (parecido {parecido:.2f}): no es la misma grabación, no se parcha.')
    if parecido < AVISO_PARECIDO:
        informe['aviso'] = 'Las copias se parecen poco (otra masterización o mezcla): escucha bien antes de aplicar.'

    # ── parche sobre el PCM exacto de la pista ──
    total = d.total or d.siguiente
    ancho, q = d.ancho, 1 << (32 - 8 * d.ancho)
    F = int(sr * FUNDIDO_MS / 1000)
    h0, h1 = int(round(desde * sr)), int(round(hasta * sr))
    lo, hi = h0 - F, h1 + F
    with open(raw, 'r+b') as f:
        f.seek(lo * fb)
        x = sanear._a_int32(f.read((hi - lo) * fb), ancho)
        mitad, X = (h0 + h1) // 2, int(sr * 0.01)
        for k in range(hi - lo):
            n = lo + k
            if n < h0:
                w = (k + 0.5) / F
            elif n >= h1:
                w = 1.0 - (n - h1 + 0.5) / F
            else:
                w = 1.0
            # antes de la mitad se sigue la alineación de antes; después, la de después (fundido de 10 ms)
            u = min(1.0, max(0.0, (n - (mitad - X // 2) + 0.5) / X))
            ja, jd = n - vb + la, n - vb + ld
            for c in range(ch):
                dn = (1 - u) * otra_en(c, ja) + u * otra_en(c, jd)
                nuevo = gan[c] * polaridad * dn * 2147483648.0
                v = (1 - w) * x[k * ch + c] + w * nuevo
                v = int(round(v / q)) * q
                x[k * ch + c] = max(-2147483648, min(2147483647 - (q - 1), v))
        f.seek(lo * fb)
        f.write(sanear._de_int32(x, ancho))
    codificar(raw, d, salida)
    sanear._copiar_metadata(pista, salida)
    v = sanear._Decodificar(salida).correr()
    if v.malas or v.fatal or v.total != total:
        raise Fallo('La versión parchada no pasó la verificación.')
    informe.update(ok=True, desde=desde, hasta=hasta, ms=round((h1 - h0) / sr * 1000), fundido_ms=FUNDIDO_MS,
                   sr=sr, bits=d.bits, canales=ch)
    return informe


def vista_previa(ruta, desde, hasta, salida):
    """VISTA_S segundos centrados en el tramo, en FLAC 24 bit (hasta 96 kHz): el navegador no abre FLAC de 32 bit."""
    _sr, _ch, _b, dur = clics.info(ruta)
    centro = (desde + hasta) / 2
    ini = max(0.0, min(centro - VISTA_S / 2, dur - VISTA_S))
    sr = min(_sr, 96000)
    r = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{ini:.3f}', '-t', str(VISTA_S), '-i', ruta,
                        '-map', '0:a:0', '-map_metadata', '-1', '-af', f'aresample={sr}:resampler=soxr',
                        '-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', '24', salida],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Fallo(f'No se pudo crear la vista previa: {r.stderr.strip()[-200:]}')
    return {'inicio': round(ini, 3), 'tramo_en': round(desde - ini, 3), 'tramo_hasta': round(hasta - ini, 3)}


# ───────────────────────────── trabajos en segundo plano ─────────────────────────────

def _prefijo():
    p = []
    if shutil.which('ionice'):
        p += ['ionice', '-c', '3']
    if shutil.which('nice'):
        p += ['nice', '-n', '19']
    return p


def _isrc(ruta):
    try:
        from mutagen.flac import FLAC
        t = FLAC(ruta).tags or {}
        v = (t.get('isrc') or t.get('ISRC') or [''])[0]
        return v.strip().upper() or None
    except Exception:  # noqa: BLE001
        return None


class Parchador:
    """Un hilo, un trabajo a la vez y con prioridad mínima: buscar tartamudeos de pistas (a pedido), preparar un
    parche (bajar la otra copia de Tidal si hace falta, parchar en una carpeta aparte, vistas previas) y aplicarlo
    (el original a .orbyte-respaldo/ y la parchada en su lugar). Lo pesado corre como proceso aparte."""

    ESTADOS_VIVOS = ('en_cola', 'bajando', 'parchando', 'aplicando')

    def __init__(self, datos, repo, ajustes):
        self.dir = os.path.join(datos, 'parches')
        self.db = os.path.join(datos, 'parches.db')
        self.repo, self.ajustes = repo, ajustes
        self._cola = []                 # [(accion, clave)] · detectar: ruta · preparar/aplicar: token
        self._lock = threading.Lock()
        self._ev = threading.Event()
        self.actual = None
        self.avance = None              # 0..1 de la pista que se está revisando
        self.al_aplicar = None          # f(ruta, respaldo, fila) tras reemplazar la pista
        os.makedirs(self.dir, exist_ok=True)
        with self._con() as c:
            c.execute('CREATE TABLE IF NOT EXISTS revisiones (ruta TEXT PRIMARY KEY, tamano INTEGER, mtime REAL, '
                      'tramos TEXT, error TEXT, cuando REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS preparados (token TEXT PRIMARY KEY, ruta TEXT, desde REAL, '
                      'hasta REAL, fuente TEXT, otra TEXT, estado TEXT, informe TEXT, error TEXT, tamano INTEGER, '
                      'mtime REAL, cuando REAL)')
            # lo que quedó a medias al reiniciar no sigue solo
            c.execute("UPDATE preparados SET estado='error', error='Se interrumpió (la ingesta se reinició)' "
                      "WHERE estado IN ('en_cola', 'bajando', 'parchando', 'aplicando')")
        self._purgar()
        threading.Thread(target=self._bucle, name='parchador', daemon=True).start()

    def _con(self):
        c = sqlite3.connect(self.db, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _purgar(self, horas=48):
        """Borra los parches preparados que nadie aplicó (y sus archivos) después de `horas`."""
        limite = time.time() - horas * 3600
        with self._con() as c:
            viejos = [r['token'] for r in c.execute(
                "SELECT token FROM preparados WHERE cuando < ? AND estado NOT IN ('en_cola', 'bajando', 'parchando', "
                "'aplicando')", (limite,))]
            c.executemany('DELETE FROM preparados WHERE token=?', [(t,) for t in viejos])
        vivos = set()
        with self._con() as c:
            vivos = {r['token'] for r in c.execute('SELECT token FROM preparados')}
        for n in os.listdir(self.dir):
            if n not in vivos:
                shutil.rmtree(os.path.join(self.dir, n), ignore_errors=True)

    # ── encargos ──
    def detectar(self, rutas):
        with self._lock:
            ya = set(self._cola) | ({self.actual} if self.actual else set())
            for r in rutas:
                if ('detectar', r) not in ya:
                    self._cola.append(('detectar', r))
        self._ev.set()

    def preparar(self, ruta, desde, hasta, fuente, otra=None):
        desde, hasta = round(float(desde), 3), round(float(hasta), 3)
        if not 0 < hasta - desde <= MAX_TRAMO_S:
            raise Fallo(f'El tramo debe durar entre 0 y {MAX_TRAMO_S:g} s')
        if fuente not in ('tidal', 'archivo') or (fuente == 'archivo' and not otra):
            raise Fallo('Falta la otra copia')
        token = os.urandom(6).hex()
        with self._con() as c:
            c.execute('INSERT INTO preparados (token, ruta, desde, hasta, fuente, otra, estado, cuando) '
                      "VALUES (?,?,?,?,?,?, 'en_cola', ?)", (token, ruta, desde, hasta, fuente, otra, time.time()))
        with self._lock:
            pos = sum(1 for a, _ in self._cola if a != 'detectar')
            self._cola.insert(pos, ('preparar', token))
        self._ev.set()
        return token

    def aplicar(self, token):
        f = self.fila(token)
        if not f or f['estado'] != 'listo':
            raise Fallo('Ese parche no está listo para aplicar')
        self._poner_estado(token, 'aplicando')
        with self._lock:
            self._cola.insert(0, ('aplicar', token))
        self._ev.set()

    def descartar(self, token):
        f = self.fila(token)
        if not f:
            return
        if f['estado'] in ('bajando', 'parchando', 'aplicando'):
            raise Fallo('Ese parche se está trabajando: espera a que termine')
        with self._lock:
            self._cola = [x for x in self._cola if x[1] != token]
        with self._con() as c:
            c.execute('DELETE FROM preparados WHERE token=?', (token,))
        shutil.rmtree(os.path.join(self.dir, token), ignore_errors=True)

    def fila(self, token):
        with self._con() as c:
            r = c.execute('SELECT * FROM preparados WHERE token=?', (token,)).fetchone()
        if not r:
            return None
        f = dict(r)
        f['informe'] = json.loads(f['informe'] or 'null')
        return f

    def archivo(self, token, cual):
        """Vista previa ('original' | 'parchada') de un parche preparado."""
        if cual not in ('original', 'parchada') or not token.isalnum():
            return None
        p = os.path.join(self.dir, token, f'vista-{cual}.flac')
        return p if os.path.isfile(p) else None

    def _poner_estado(self, token, estado, **kv):
        campos = dict(kv, estado=estado)
        if 'informe' in campos and not isinstance(campos['informe'], str):
            campos['informe'] = json.dumps(campos['informe'], ensure_ascii=False)
        with self._con() as c:
            c.execute(f'UPDATE preparados SET {", ".join(f"{k}=?" for k in campos)} WHERE token=?',
                      list(campos.values()) + [token])

    def estado(self, rutas):
        """Lo que toca a estas pistas: revisiones vigentes, cola y parches preparados."""
        rutas = list(rutas)
        nuestras = set(rutas)
        revs = {}
        with self._con() as c:
            for k in range(0, len(rutas), 400):
                parte = rutas[k:k + 400]
                for r in c.execute(f'SELECT * FROM revisiones WHERE ruta IN ({",".join("?" * len(parte))})', parte):
                    try:
                        st = os.stat(r['ruta'])
                    except OSError:
                        continue
                    if st.st_size != r['tamano'] or abs(st.st_mtime - r['mtime']) > 1:
                        continue                       # el archivo cambió: hay que volver a revisarlo
                    revs[r['ruta']] = {'tramos': json.loads(r['tramos'] or '[]'), 'error': r['error'],
                                       'cuando': int(r['cuando'] or 0)}
            preps = []
            for k in range(0, len(rutas), 400):
                parte = rutas[k:k + 400]
                preps += [dict(r) for r in c.execute(
                    f'SELECT * FROM preparados WHERE ruta IN ({",".join("?" * len(parte))}) ORDER BY cuando',
                    parte)]
        for p in preps:
            p['informe'] = json.loads(p['informe'] or 'null')
            p['vistas'] = bool(self.archivo(p['token'], 'original'))
        with self._lock:
            cola = [r for a, r in self._cola if a == 'detectar' and r in nuestras]
            actual = self.actual
        act = None
        if actual and actual[0] == 'detectar' and actual[1] in nuestras:
            act = {'ruta': actual[1], 'avance': self.avance}
        return {'revisiones': revs, 'en_cola': cola, 'revisando': act, 'preparados': preps}

    # ── bucle ──
    def _correr(self, args, avance=None, timeout=3600):
        cmd = _prefijo() + [sys.executable, '-m', 'ingesta.parche', *args]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.repo,
                             errors='replace')
        ultima, t0 = '', time.time()
        for linea in p.stdout:
            linea = linea.strip()
            if linea.startswith('AVANCE ') and avance:
                try:
                    avance(float(linea.split()[1]))
                except ValueError:
                    pass
            elif linea:
                ultima = linea
            if time.time() - t0 > timeout:
                p.kill()
                break
        err = p.stderr.read()
        p.wait()
        try:
            return json.loads(ultima)
        except ValueError:
            return {'ok': False, 'error': (err or 'sin respuesta').strip()[-300:]}

    def _bucle(self):
        while True:
            self._ev.wait(60)
            with self._lock:
                if not self._cola:
                    self._ev.clear()
                    continue
                self.actual = self._cola.pop(0)
            accion, clave = self.actual
            try:
                if accion == 'detectar':
                    self._detectar(clave)
                elif accion == 'preparar':
                    self._preparar(clave)
                elif accion == 'aplicar':
                    self._aplicar(clave)
            except Fallo as e:
                if accion != 'detectar':
                    self._poner_estado(clave, 'error', error=str(e))
            except Exception as e:  # noqa: BLE001
                log.exception('Parchar: %s %s', accion, clave)
                if accion != 'detectar':
                    self._poner_estado(clave, 'error', error=f'{type(e).__name__}: {e}')
            finally:
                with self._lock:
                    self.actual = None
                    self.avance = None
                time.sleep(0.5)

    def _detectar(self, ruta):
        if not os.path.isfile(ruta):
            return
        st = os.stat(ruta)
        self.avance = 0.0
        r = self._correr(['detectar', ruta], avance=lambda x: setattr(self, 'avance', x), timeout=7200)
        with self._con() as c:
            c.execute('INSERT OR REPLACE INTO revisiones (ruta, tamano, mtime, tramos, error, cuando) '
                      'VALUES (?,?,?,?,?,?)', (ruta, st.st_size, st.st_mtime,
                                               json.dumps(r.get('tramos') or [], ensure_ascii=False),
                                               None if r.get('ok') else r.get('error'), time.time()))
        if r.get('tramos'):
            log.info('Parchar: %s: %d tartamudeo(s) %s', ruta, len(r['tramos']),
                     ', '.join(f'{t["desde"]}–{t["hasta"]}' for t in r['tramos']))

    def _bajar_tidal(self, ruta, carpeta):
        """La misma pista en Tidal (por ISRC; si no, por artista y título) que dure lo mismo → (archivo, pista)."""
        from . import reparar, tienda
        db = (self.ajustes() or {}).get('db_orbyte')
        cands = []
        isrc = _isrc(ruta)
        if isrc:
            try:
                _q, cands = reparar.buscar_pista(ruta, isrc, db, limite=5)
            except Exception as e:  # noqa: BLE001
                log.warning('Parchar: búsqueda por ISRC %s: %s', isrc, e)
        p = next((c for c in cands if c['calza']), None)
        if not p:
            _q, cands = reparar.buscar_pista(ruta, None, db, limite=8)
            p = next((c for c in cands if c['calza'] and c['parecido'] >= 0.85), None)
        if not p:
            raise Fallo('No encontré esta pista en Tidal con la misma duración y título: usa «Otra copia»')
        tienda.preparar_tidal()
        casa = os.path.join(carpeta, 'tidal')
        os.makedirs(casa, exist_ok=True)
        rc, salida = tienda._bajar_tidal_a(p['url'], os.path.join(casa, 'bajada'), casa)
        archivos = tienda._audios(os.path.join(casa, 'bajada'))
        if rc != 0 or not archivos:
            ultima = next((x for x in reversed(salida.splitlines()) if x.strip()), f'código {rc}')
            raise Fallo(f'No se pudo bajar la pista de Tidal: {ultima}')
        cod = tienda.codec(archivos[0])
        if cod in tienda.CODECS_CON_PERDIDA:
            raise Fallo(f'Tidal la entregó con pérdida ({cod}): no sirve para parchar')
        return archivos[0], {'titulo': p.get('titulo'), 'album': p.get('album'), 'artista': p.get('artista'),
                             'hires': p.get('hires'), 'url': p.get('url'), 'isrc': p.get('isrc'),
                             'por': 'ISRC' if isrc and p.get('isrc') == isrc else 'título y duración'}

    def _preparar(self, token):
        f = self.fila(token)
        if not f:
            return
        ruta = f['ruta']
        if not os.path.isfile(ruta):
            raise Fallo('La pista ya no está')
        st = os.stat(ruta)
        carpeta = os.path.join(self.dir, token)
        os.makedirs(carpeta, exist_ok=True)
        fuente = None
        if f['fuente'] == 'tidal':
            self._poner_estado(token, 'bajando')
            otra, fuente = self._bajar_tidal(ruta, carpeta)
        else:
            otra = f['otra']
            if not otra or not os.path.isfile(otra):
                raise Fallo('La otra copia ya no está')
            fuente = {'archivo': os.path.basename(otra)}
        self._poner_estado(token, 'parchando')
        r = self._correr(['preparar', ruta, otra, str(f['desde']), str(f['hasta']), carpeta])
        if f['fuente'] == 'tidal':
            shutil.rmtree(os.path.join(carpeta, 'tidal'), ignore_errors=True)
        if not r.get('ok'):
            raise Fallo(r.get('error') or 'No se pudo parchar')
        r['fuente'] = fuente
        self._poner_estado(token, 'listo', informe=r, error=None, tamano=st.st_size, mtime=st.st_mtime)

    def _aplicar(self, token):
        f = self.fila(token)
        if not f:
            return
        ruta = f['ruta']
        parchada = os.path.join(self.dir, token, 'parchada.flac')
        if not os.path.isfile(parchada):
            raise Fallo('El archivo parchado ya no está: vuelve a prepararlo')
        st = os.stat(ruta)
        if st.st_size != f['tamano'] or abs(st.st_mtime - f['mtime']) > 1:
            raise Fallo('La pista cambió desde que se preparó el parche: vuelve a prepararlo')
        carpeta = os.path.dirname(ruta)
        resp_dir = os.path.join(carpeta, sanear.RESPALDO)
        os.makedirs(resp_dir, exist_ok=True)
        respaldo = os.path.join(resp_dir, os.path.basename(ruta))
        if os.path.exists(respaldo):
            base, ext = os.path.splitext(respaldo)
            k = 2
            while os.path.exists(f'{base} ({k}){ext}'):
                k += 1
            respaldo = f'{base} ({k}){ext}'
        shutil.copy2(ruta, respaldo)
        if os.path.getsize(respaldo) != st.st_size:
            raise Fallo('El respaldo del original quedó incompleto: no se reemplazó nada')
        tmp = os.path.join(carpeta, f'.parche-{token}.flac')        # oculto: nunca es una pista (#227)
        try:
            shutil.copyfile(parchada, tmp)
            if os.path.getsize(tmp) != os.path.getsize(parchada):
                raise Fallo('La copia del archivo parchado quedó incompleta: no se reemplazó nada')
            os.chmod(tmp, st.st_mode & 0o7777)
            os.replace(tmp, ruta)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        inf = f['informe'] or {}
        inf['respaldo'] = respaldo
        self._poner_estado(token, 'aplicado', informe=inf, error=None)
        shutil.rmtree(os.path.join(self.dir, token), ignore_errors=True)
        log.info('Parchar: %s %s–%s aplicado (original en %s)', ruta, f['desde'], f['hasta'], respaldo)
        with self._con() as c:
            c.execute('DELETE FROM revisiones WHERE ruta=?', (ruta,))
        if self.al_aplicar:
            try:
                self.al_aplicar(ruta, respaldo, dict(f, informe=inf))
            except Exception:  # noqa: BLE001
                log.exception('Parchar: aviso tras aplicar')
        self.detectar([ruta])                      # se confirma que el tartamudeo ya no está


# ───────────────────────────── proceso aparte ─────────────────────────────

def _cli(args):
    accion = args[0]
    if accion == 'detectar':
        tramos = clics.tartamudeos(args[1], avance=lambda x: print(f'AVANCE {x:.3f}', flush=True))
        return {'ok': True, 'tramos': tramos}
    if accion == 'preparar':
        pista, otra, desde, hasta, carpeta = args[1], args[2], float(args[3]), float(args[4]), args[5]
        salida = os.path.join(carpeta, 'parchada.flac')
        inf = parchar(pista, otra, desde, hasta, salida, tmpdir=carpeta)
        inf['vista'] = vista_previa(pista, desde, hasta, os.path.join(carpeta, 'vista-original.flac'))
        vista_previa(salida, desde, hasta, os.path.join(carpeta, 'vista-parchada.flac'))
        return inf
    raise SystemExit(f'acción desconocida: {accion}')


if __name__ == '__main__':
    try:
        out = _cli(sys.argv[1:])
    except Fallo as e:
        out = {'ok': False, 'error': str(e)}
    except Exception as e:  # noqa: BLE001
        out = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
    print(json.dumps(out, ensure_ascii=False), flush=True)
