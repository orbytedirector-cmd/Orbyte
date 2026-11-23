#!/usr/bin/env python3
"""Prueba de «parchar»: reemplaza un tramo dañado de una pista con el mismo tramo de OTRA copia (por ejemplo la de
Tidal), alineado a la muestra y convertido a la frecuencia y los bits de la pista. Escribe el resultado en otra
carpeta: no toca ni la pista ni la otra copia.

Cómo:
 1. Alinea las dos copias con el audio de antes y de después del tramo (3 s a cada lado), en tres pasadas: envolvente
    cada 10 ms (busca ±10 s), forma de onda a 8 kHz y forma de onda a la frecuencia de la pista. Mide el corrimiento
    antes y después por separado: si no coinciden, las copias no corren a la misma velocidad y no se parcha.
 2. Iguala el volumen de la otra copia por canal (mínimos cuadrados sobre el contexto) y detecta la polaridad.
 3. Copia el tramo de la otra copia (re-muestreada con soxr) y lo funde 5 ms a cada lado, fuera del tramo dañado.
    Fuera del tramo y de los fundidos, la pista queda bit a bit igual. Copia tags y carátulas y verifica.

Uso:
  python3 tools/parchar.py PISTA OTRA_COPIA CARPETA --desde 32.74 --hasta 33.19
"""
import argparse
import atexit
import math
import os
import shutil
import subprocess
import sys
import tempfile
from array import array

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
sys.path.insert(0, os.path.dirname(AQUI))

import clics  # noqa: E402
import quitar_clics  # noqa: E402
from ingesta import sanear  # noqa: E402

CONTEXTO_S = 3.0
FUNDIDO_MS = 5


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


def mejor_lag(tpl, ref, lags, quitar_media=False):
    """tpl: [(pos, valor)] de la pista; ref: lista de la otra copia. Lag l: tpl[pos] ~ ref[pos + l]."""
    if quitar_media:
        m = sum(v for _, v in tpl) / len(tpl)
        tpl = [(p, v - m) for p, v in tpl]
    mejor = (0.0, None)
    for l in lags:
        par = [(v, ref[p + l]) for p, v in tpl if 0 <= p + l < len(ref)]
        if len(par) < len(tpl) * 0.9:
            continue
        if quitar_media:
            mr = sum(b for _, b in par) / len(par)
            par = [(a, b - mr) for a, b in par]
        c = ncorr(par)
        if abs(c) > abs(mejor[0]):
            mejor = (c, l)
    return mejor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pista')
    ap.add_argument('otra')
    ap.add_argument('carpeta')
    ap.add_argument('--desde', type=float, required=True)
    ap.add_argument('--hasta', type=float, required=True)
    ap.add_argument('--buscar', type=float, default=10.0, help='segundos de corrimiento máximo entre las copias')
    a = ap.parse_args()
    pista, otra = os.path.abspath(a.pista), os.path.abspath(a.otra)
    carpeta = os.path.abspath(a.carpeta)
    os.makedirs(carpeta, exist_ok=True)
    if os.path.realpath(carpeta) == os.path.realpath(os.path.dirname(pista)):
        raise SystemExit('La carpeta de salida no puede ser la de la pista: elige otra.')
    nombre = os.path.splitext(os.path.basename(pista))[0]

    sr_p, ch, _b, dur = clics.info(pista)
    d0 = a.desde - CONTEXTO_S
    d1 = a.hasta + CONTEXTO_S
    if d0 < 0 or d1 > dur:
        raise SystemExit('Hacen falta 3 s de audio sano antes y después del tramo.')
    hueco = (a.desde, a.hasta)

    # ── 1) envolvente cada 10 ms a 8 kHz, mono ──
    print('Alineando…', flush=True)
    sr8, paso = 8000, 80
    p8 = pcm_f32(pista, d0, d1 - d0, sr8, 1, soxr=False)[0]
    o0 = d0 - a.buscar
    o8 = pcm_f32(otra, o0, (d1 - d0) + 2 * a.buscar, sr8, 1, soxr=False)[0]
    if len(o8) < len(p8):
        raise SystemExit('La otra copia es demasiado corta en ese punto.')
    off0 = int(round((max(0.0, o0) - d0) * sr8))   # índice en o8 de t = d0 (si no hubiera corrimiento) es -off0
    ep, eo = envolvente(p8, paso), envolvente(o8, paso)
    fuera = [(i, v) for i, v in enumerate(ep)
             if not (hueco[0] - 0.05 <= d0 + i * paso / sr8 <= hueco[1] + 0.05)]
    rango = int(a.buscar * sr8 / paso) + 2
    c1, l1 = mejor_lag(fuera, eo, range(-off0 // paso - rango, -off0 // paso + rango + 1), quitar_media=True)
    if l1 is None:
        raise SystemExit('No se pudo alinear (¿es la misma canción?).')
    # ── 2) forma de onda a 8 kHz, ±2 cuadros ──
    tpl8 = [(i, v) for i, v in enumerate(p8)
            if (hueco[0] - 1.0 <= d0 + i / sr8 < hueco[0] - 0.02) or (hueco[1] + 0.02 < d0 + i / sr8 <= hueco[1] + 1.0)]
    c2, l2 = mejor_lag(tpl8, o8, range(l1 * paso - 2 * paso, l1 * paso + 2 * paso + 1))
    if l2 is None:
        raise SystemExit('No se pudo alinear la forma de onda.')
    polaridad = -1.0 if c2 < 0 else 1.0
    corr_s = l2 / sr8 + (max(0.0, o0) - d0)            # tiempo en la otra copia = tiempo en la pista + corr_s
    print(f'  envolvente {c1:.3f} · onda 8 kHz {abs(c2):.3f} · la otra copia va {corr_s:+.4f} s'
          + (' · polaridad invertida' if polaridad < 0 else ''))

    # ── 3) a la frecuencia de la pista, antes y después por separado ──
    sr = sr_p
    v0 = a.desde - 0.3
    v1 = a.hasta + 0.3
    marg = 0.01
    ventana_o0 = v0 + corr_s - marg
    po = pcm_f32(otra, ventana_o0, (v1 - v0) + 2 * marg, sr, ch)
    # la pista, exacta (libFLAC, cada muestra en su lugar)
    tmp = tempfile.mkdtemp(prefix='.parchar-', dir=carpeta)
    atexit.register(shutil.rmtree, tmp, True)
    raw = os.path.join(tmp, 'pcm.raw')
    print('Decodificando la pista…', flush=True)
    with open(raw, 'w+b') as f:
        d = sanear._Decodificar(pista, salida=f).correr()
    if d.fatal or d.malas:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit('La pista tiene cuadros dañados: primero Sanear.')
    if d.sr != sr:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit('Frecuencia distinta entre ffprobe y libFLAC.')
    fb = ch * d.ancho
    vb, ve = int(round(v0 * sr)), int(round(v1 * sr))
    with open(raw, 'rb') as f:
        f.seek(vb * fb)
        xx = sanear._a_int32(f.read((ve - vb) * fb), d.ancho)
    pp = [[v / 2147483648.0 for v in xx[c::ch]] for c in range(ch)]
    base = int(round(marg * sr))                        # pp[i] ~ po[i + base] si el corrimiento fuera exacto
    lags = range(base - 4 * (sr // sr8), base + 4 * (sr // sr8) + 1)
    res = {}
    for lado, (t0, t1) in (('antes', (v0, a.desde - 0.02)), ('después', (a.hasta + 0.02, v1))):
        i0, i1 = int((t0 - v0) * sr), int((t1 - v0) * sr)
        tpl = [(i, sum(pp[c][i] for c in range(ch))) for i in range(i0, i1, 2)]
        suma = [sum(po[c][j] for c in range(ch)) for j in range(len(po[0]))]
        res[lado] = mejor_lag(tpl, suma, lags)
    (ca, la), (cd, ld) = res['antes'], res['después']
    print(f'  a {sr} Hz: antes {abs(ca):.4f} (lag {la}) · después {abs(cd):.4f} (lag {ld})')
    if la is None or ld is None or abs(la - ld) > 2:
        raise SystemExit('El corrimiento antes y después no coincide: las copias no corren igual. No se parcha.')
    lag = la

    # ── ganancia por canal ──
    gan = []
    for c in range(ch):
        sxy = syy = 0.0
        for t0, t1 in ((v0, a.desde - 0.02), (a.hasta + 0.02, v1)):
            for i in range(int((t0 - v0) * sr), int((t1 - v0) * sr)):
                y = polaridad * po[c][i + lag]
                sxy += pp[c][i] * y
                syy += y * y
        g = sxy / syy if syy else 1.0
        err = sxx = 0.0
        for t0, t1 in ((v0, a.desde - 0.02), (a.hasta + 0.02, v1)):
            for i in range(int((t0 - v0) * sr), int((t1 - v0) * sr)):
                e = pp[c][i] - g * polaridad * po[c][i + lag]
                err += e * e
                sxx += pp[c][i] * pp[c][i]
        gan.append(g)
        print(f'  canal {c + 1}: volumen ×{g:.4f} ({20 * math.log10(abs(g)):+.2f} dB) · diferencia que queda '
              f'{10 * math.log10(err / sxx + 1e-20):.1f} dB bajo la música')

    # ── parche sobre el PCM exacto de la pista ──
    try:
        total = d.total or d.siguiente
        ancho, q = d.ancho, 1 << (32 - 8 * d.ancho)
        F = int(sr * FUNDIDO_MS / 1000)
        h0, h1 = int(round(a.desde * sr)), int(round(a.hasta * sr))
        lo, hi = h0 - F, h1 + F
        with open(raw, 'r+b') as f:
            f.seek(lo * fb)
            x = sanear._a_int32(f.read((hi - lo) * fb), ancho)
            for k in range(hi - lo):
                n = lo + k
                if n < h0:
                    w = (k + 0.5) / F
                elif n >= h1:
                    w = 1.0 - (n - h1 + 0.5) / F
                else:
                    w = 1.0
                j = n - vb + lag
                for c in range(ch):
                    nuevo = gan[c] * polaridad * po[c][j] * 2147483648.0
                    v = (1 - w) * x[k * ch + c] + w * nuevo
                    v = int(round(v / q)) * q
                    x[k * ch + c] = max(-2147483648, min(2147483647 - (q - 1), v))
            f.seek(lo * fb)
            f.write(sanear._de_int32(x, ancho))
        salida = os.path.join(carpeta, f'{nombre} [parche].flac')
        quitar_clics.codificar(raw, d, salida)
        sanear._copiar_metadata(pista, salida)
        v = sanear._Decodificar(salida).correr()
        if v.malas or v.fatal or v.total != total:
            raise SystemExit('La versión parchada no pasó la verificación.')
        print(f'Parchado {a.desde:.3f}–{a.hasta:.3f} s ({(h1 - h0) / sr * 1000:.0f} ms + {FUNDIDO_MS} ms de fundido '
              f'a cada lado) → {salida}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
