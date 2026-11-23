#!/usr/bin/env python3
"""Prueba de «parchar»: reemplaza un tramo dañado de una pista con el mismo tramo de OTRA copia (por ejemplo la de
Tidal), alineado a la muestra y convertido a la frecuencia y los bits de la pista. Escribe el resultado en otra
carpeta: no toca ni la pista ni la otra copia.

Cómo:
 1. Alinea las dos copias con el audio de antes y de después del tramo (3 s a cada lado), en tres pasadas: envolvente
    cada 10 ms (busca ±10 s), forma de onda a 8 kHz y forma de onda a la frecuencia de la pista. Mide el corrimiento
    antes y después por separado.
 2. Iguala el volumen de la otra copia por canal (misma energía que la pista en el contexto) y la polaridad.
 3. Copia el tramo de la otra copia (re-muestreada con soxr) y lo funde 5 ms a cada lado, fuera del tramo dañado.
    Si el daño corrió el tiempo (la pista quedó con unos ms de más o de menos), se sigue la alineación de antes hasta
    la mitad del tramo y la de después desde ahí, con un fundido de 10 ms.
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


def mejor_lag(tpl, ref, lags, quitar_media=False, todos=False):
    """tpl: [(pos, valor)] de la pista; ref: lista de la otra copia. Lag l: tpl[pos] ~ ref[pos + l].
    → (correlación, lag) del mejor; con todos=True, la lista [(correlación, lag)] de cada lag."""
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
        if abs(c) > abs(mejor[0]):
            mejor = (c, l)
    return lista if todos else mejor


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
    lista = mejor_lag(fuera, eo, range(-off0 // paso - rango, -off0 // paso + rango + 1), quitar_media=True,
                      todos=True)
    if not lista:
        raise SystemExit('No se pudo alinear (¿es la misma canción?).')
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
            cand.append((abs(cw), c, lw))
    if not cand:
        raise SystemExit('No se pudo alinear (¿es la misma canción?).')
    _cw, c1, l1w = max(cand)
    # ── 2) forma de onda a 8 kHz, antes y después por separado (±3 cuadros) ──
    lados = {'antes': (hueco[0] - 1.0, hueco[0] - 0.02), 'después': (hueco[1] + 0.02, hueco[1] + 1.0)}
    l8 = {}
    for lado, (t0, t1) in lados.items():
        tpl8 = [(i, v) for i, v in enumerate(p8) if t0 <= d0 + i / sr8 < t1]
        l8[lado] = mejor_lag(tpl8, o8, range(l1w - paso, l1w + paso + 1))
        if l8[lado][1] is None:
            raise SystemExit('No se pudo alinear la forma de onda.')
    c2 = max((c for c, _ in l8.values()), key=abs)
    polaridad = -1.0 if c2 < 0 else 1.0
    corr_s = {lado: l / sr8 + (max(0.0, o0) - d0) for lado, (_c, l) in l8.items()}   # t_otra = t_pista + corr_s
    print(f'  envolvente {c1:.3f} · onda 8 kHz antes {abs(l8["antes"][0]):.3f} / después {abs(l8["después"][0]):.3f}'
          f' · la otra copia va {corr_s["antes"]:+.4f} s' + (' · polaridad invertida' if polaridad < 0 else ''))

    # ── 3) a la frecuencia de la pista, antes y después por separado ──
    sr = sr_p
    v0 = a.desde - 0.3
    v1 = a.hasta + 0.3
    marg = 0.1
    cmin = min(corr_s.values())
    ventana_o0 = v0 + cmin - marg
    po = pcm_f32(otra, ventana_o0, (v1 - v0) + abs(corr_s['después'] - corr_s['antes']) + 2 * marg, sr, ch)
    # la pista, exacta (libFLAC, cada muestra en su lugar)
    tmp = tempfile.mkdtemp(prefix='.parchar-', dir=carpeta)
    atexit.register(shutil.rmtree, tmp, True)
    raw = os.path.join(tmp, 'pcm.raw')
    print('Decodificando la pista…', flush=True)
    with open(raw, 'w+b') as f:
        d = sanear._Decodificar(pista, salida=f).correr()
    if d.fatal or d.malas:
        raise SystemExit('La pista tiene cuadros dañados: primero Sanear.')
    if d.sr != sr:
        raise SystemExit('Frecuencia distinta entre ffprobe y libFLAC.')
    fb = ch * d.ancho
    vb, ve = int(round(v0 * sr)), int(round(v1 * sr))
    with open(raw, 'rb') as f:
        f.seek(vb * fb)
        xx = sanear._a_int32(f.read((ve - vb) * fb), d.ancho)
    pp = [[v / 2147483648.0 for v in xx[c::ch]] for c in range(ch)]
    suma = [sum(po[c][j] for c in range(ch)) for j in range(len(po[0]))]
    paso8 = sr // sr8
    res = {}
    for lado, (t0, t1) in (('antes', (v0, a.desde - 0.02)), ('después', (a.hasta + 0.02, v1))):
        base = int(round((corr_s[lado] - cmin + marg) * sr))      # pp[i] ~ po[i + base]
        i0, i1 = int((t0 - v0) * sr), int((t1 - v0) * sr)
        tpl = [(i, sum(pp[c][i] for c in range(ch))) for i in range(i0, i1, 2)]
        c, l = mejor_lag(tpl, suma, range(base - 4 * paso8, base + 4 * paso8 + 1))
        if l is None:
            raise SystemExit('No se pudo alinear a la frecuencia de la pista.')
        # fracción de muestra: parábola por la correlación en l-1, l, l+1
        cm, _ = mejor_lag(tpl, suma, [l - 1])
        cp, _ = mejor_lag(tpl, suma, [l + 1])
        den = cm - 2 * c + cp
        frac = max(-0.5, min(0.5, 0.5 * (cm - cp) / den)) if den else 0.0
        res[lado] = (c, l + frac)
    (ca, la), (cd, ld) = res['antes'], res['después']
    corrido = ld - la
    print(f'  a {sr} Hz: antes {abs(ca):.4f} · después {abs(cd):.4f}'
          + (f' · la pista tiene {abs(corrido) / sr * 1000:.2f} ms {"de más" if corrido < 0 else "de menos"} en el '
             f'tramo (el daño corrió el tiempo; se empalma a la mitad)' if abs(corrido) > 1 else ''))
    if abs(corrido) > 0.05 * sr:
        raise SystemExit('El corrimiento antes y después difiere en más de 50 ms: no se parcha.')
    if abs(corrido) <= 1:
        la = ld = (la + ld) / 2

    def otra_en(c, j):
        """Muestra de la otra copia en la posición fraccionaria j (interpolación lineal)."""
        k = int(math.floor(j))
        t = j - k
        return po[c][k] * (1 - t) + po[c][k + 1] * t

    def ctx():
        for (t0, t1), lag in (((v0, a.desde - 0.02), la), ((a.hasta + 0.02, v1), ld)):
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
        print(f'  canal {c + 1}: volumen ×{g:.4f} ({20 * math.log10(abs(g)):+.2f} dB) · parecido {cor:.3f} · '
              f'diferencia {10 * math.log10(err / sxx + 1e-20):.1f} dB bajo la música')
    if peor < 0.9:
        print('  Ojo: las copias se parecen poco (otra masterización o mezcla); el parche puede notarse.')

    # ── parche sobre el PCM exacto de la pista ──
    total = d.total or d.siguiente
    ancho, q = d.ancho, 1 << (32 - 8 * d.ancho)
    F = int(sr * FUNDIDO_MS / 1000)
    h0, h1 = int(round(a.desde * sr)), int(round(a.hasta * sr))
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
    salida = os.path.join(carpeta, f'{nombre} [parche].flac')
    quitar_clics.codificar(raw, d, salida)
    sanear._copiar_metadata(pista, salida)
    v = sanear._Decodificar(salida).correr()
    if v.malas or v.fatal or v.total != total:
        raise SystemExit('La versión parchada no pasó la verificación.')
    print(f'Parchado {a.desde:.3f}–{a.hasta:.3f} s ({(h1 - h0) / sr * 1000:.0f} ms + {FUNDIDO_MS} ms de fundido '
          f'a cada lado) → {salida}')


if __name__ == '__main__':
    main()
