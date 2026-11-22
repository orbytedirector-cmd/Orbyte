#!/usr/bin/env python3
"""Prueba de «Quitar clics» sobre una COPIA de la pista (no toca el original).

Lee ENTRADA, busca los clics entre --desde y --hasta (mismo criterio que tools/clics.py) y escribe en CARPETA dos
versiones para escuchar y comparar:

  «<nombre> [golpes].flac»  cada golpe se rellena por separado (su ancho medido + 0,1 ms a cada lado), solo en el
                            canal donde aparece; el resto del canal queda igual.
  «<nombre> [tramo].flac»   cada ráfaga (golpes a menos de 30 ms uno de otro) se rellena entera, de punta a punta,
                            en el canal donde aparece.

El relleno es el de Sanear (ingesta/sanear.py): continúa la forma de onda de cada lado y las funde. Fuera de lo
rellenado, el audio queda bit a bit igual. Copia tags y carátulas y verifica que cada versión decodifique limpia y
con el mismo número de muestras.

Uso:
  python3 tools/quitar_clics.py ENTRADA CARPETA --desde 32.5 --hasta 33.6 [--umbral 6]
"""
import argparse
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
from ingesta import sanear  # noqa: E402

MARGEN_S = 1.0          # audio de contexto a cada lado del tramo pedido
MAX_GOLPE_MS = 1.0      # ancho máximo de un golpe
EXTRA_MS = 0.1          # se rellena un poco más a cada lado del golpe medido
RAFAGA_MS = 30          # golpes más cerca que esto son la misma ráfaga
BORDE_TRAMO_MS = 2


def ancho_golpe(x, pi, sr):
    """[a, b) del golpe que tiene su pico de segunda diferencia en `pi`."""
    n = len(x)

    def d2(i):
        return abs(x[i] - 2 * x[i - 1] + x[i - 2]) if 2 <= i < n else 0
    lado = int(sr * 0.005)
    ref = sorted(d2(i) for i in range(max(2, pi - lado), min(n, pi + lado)))
    ref = ref[len(ref) // 2] or 1
    tope = int(sr * MAX_GOLPE_MS / 1000)
    a = pi
    while a > 2 and pi - a < tope and d2(a - 1) > 4 * ref:
        a -= 1
    b = pi
    while b < n - 1 and b - pi < tope and d2(b + 1) > 4 * ref:
        b += 1
    extra = max(1, int(sr * EXTRA_MS / 1000))
    # la segunda diferencia en i mira i-2..i: el golpe empieza 2 muestras antes
    return max(0, a - 2 - extra), min(n, b + 1 + extra)


def unir(tramos, junto=0):
    out = []
    for a, b in sorted(tramos):
        if out and a <= out[-1][1] + junto:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def rellenar_canal(m, a, b, sr, q):
    """Como sanear.rellenar, pero sobre un solo canal (lista de enteros justificados a la izquierda)."""
    L = b - a
    w = max(32, int(sr * 0.01))
    pmin, pmax = max(16, int(sr * 0.0025)), max(32, int(sr * 0.02))
    dec = max(1, sr // 22050)
    n_pre, n_post = a, len(m) - b
    f = [v / 2147483648.0 for v in m]
    p_iz, c_iz = sanear._periodo(f, a, n_pre, -1, dec, pmin, pmax, w) if n_pre else (None, 0.0)
    p_de, c_de = sanear._periodo(f, b, n_post, +1, dec, pmin, pmax, w) if n_post else (None, 0.0)
    if c_iz < 0.5:
        p_iz = None
    if c_de < 0.5:
        p_de = None
    nuevo = []
    for i in range(L):
        wt = 0.5 - 0.5 * math.cos(math.pi * (i + 0.5) / L)
        iz = de = 0
        if n_pre:
            iz = m[(a - p_iz + (i % p_iz)) if p_iz else (a - 1 - (i % n_pre))]
        if n_post:
            de = m[(b + ((i - L) % p_de)) if p_de else (b + (L - 1 - i) % n_post)]
        if not n_pre:
            v = wt * de
        elif not n_post:
            v = (1 - wt) * iz
        else:
            v = (1 - wt) * iz + wt * de
        v = int(round(v / q)) * q
        nuevo.append(max(-2147483648, min(2147483647 - (q - 1), v)))
    m[a:b] = nuevo
    return p_iz, p_de


def codificar(raw, d, salida):
    try:
        sanear._codificar(raw, d, salida)
        return
    except RuntimeError:
        if d.bits != 32 or not shutil.which('ffmpeg'):
            raise
    # flac < 1.4 no codifica 32 bit: ffmpeg
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 's32le', '-ar', str(d.sr), '-ac', str(d.canales),
           '-i', raw, '-map_metadata', '-1', '-c:a', 'flac', '-compression_level', '8', '-sample_fmt', 's32',
           '-strict', 'experimental', '-f', 'flac', salida]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f'no se pudo codificar: {r.stderr.strip()[-300:]}')


def ms(n, sr):
    return n * 1000 / sr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('entrada')
    ap.add_argument('carpeta')
    ap.add_argument('--desde', type=float, required=True)
    ap.add_argument('--hasta', type=float, required=True)
    ap.add_argument('--umbral', type=float, default=6)
    a = ap.parse_args()
    entrada = os.path.abspath(a.entrada)
    carpeta = os.path.abspath(a.carpeta)
    os.makedirs(carpeta, exist_ok=True)
    if os.path.realpath(carpeta) == os.path.realpath(os.path.dirname(entrada)):
        raise SystemExit('La carpeta de salida no puede ser la del archivo: elige otra.')
    nombre = os.path.splitext(os.path.basename(entrada))[0]

    tmp = tempfile.mkdtemp(prefix='.quitar-clics-', dir=carpeta)
    try:
        raw = os.path.join(tmp, 'pcm.raw')
        print('Decodificando…', flush=True)
        with open(raw, 'w+b') as f:
            d = sanear._Decodificar(entrada, salida=f).correr()
        if d.fatal or d.malas:
            raise SystemExit(f'El archivo tiene cuadros dañados ({d.fatal or d.malas[:2]}): primero Sanear.')
        sr, ch, ancho = d.sr, d.canales, d.ancho
        total = d.total or d.siguiente
        q = 1 << (32 - 8 * ancho)
        lo = max(0, int((a.desde - MARGEN_S) * sr))
        hi = min(total, int((a.hasta + MARGEN_S) * sr))
        ini, fin = int(a.desde * sr) - lo, int(a.hasta * sr) - lo
        fb = ch * ancho
        with open(raw, 'rb') as f:
            f.seek(lo * fb)
            x = sanear._a_int32(f.read((hi - lo) * fb), ancho)
        canales = [list(x[c::ch]) for c in range(ch)]
        print(f'{sr} Hz · {ch} canales · {d.bits} bit · {total / sr:.1f} s')

        golpes = []
        for c in range(ch):
            for h in clics.analizar(canales[c], sr, (lo / sr), a.umbral):
                if h[0] == 'salto' and ini <= h[1] < fin:
                    golpes.append((c, h[1]))
        if not golpes:
            raise SystemExit('No hay clics en ese tramo con ese umbral.')
        por_canal = {c: unir([ancho_golpe(canales[c], pi, sr) for cc, pi in golpes if cc == c])
                     for c in range(ch)}
        anchos = [ms(b - a_, sr) for t in por_canal.values() for a_, b in t]
        print(f'{len(golpes)} golpes · ancho rellenado {min(anchos):.2f} a {max(anchos):.2f} ms '
              f'(mediana {sorted(anchos)[len(anchos) // 2]:.2f} ms)')

        versiones = {'golpes': por_canal,
                     'tramo': {c: [[t[0][0] - int(sr * BORDE_TRAMO_MS / 1000), t[-1][1] + int(sr * BORDE_TRAMO_MS / 1000)]
                                   for t in _rafagas(por_canal[c], sr)] for c in range(ch)}}
        for etiqueta, tramos in versiones.items():
            nuevos = [list(v) for v in canales]
            for c in range(ch):
                # dos pasadas: en la segunda, el golpe siguiente ya está rellenado y no se copia al continuar
                for _ in range(2):
                    for ta, tb in tramos[c]:
                        rellenar_canal(nuevos[c], max(0, ta), min(len(nuevos[c]), tb), sr, q)
            cambiadas = sum(1 for c in range(ch) for u, v in zip(canales[c], nuevos[c]) if u != v)
            y = array('i', x)
            for c in range(ch):
                y[c::ch] = array('i', nuevos[c])
            with open(raw, 'r+b') as f:
                f.seek(lo * fb)
                f.write(sanear._de_int32(y, ancho))
            salida = os.path.join(carpeta, f'{nombre} [{etiqueta}].flac')
            codificar(raw, d, salida)
            sanear._copiar_metadata(entrada, salida)
            v = sanear._Decodificar(salida).correr()
            if v.malas or v.fatal or v.total != total:
                raise SystemExit(f'{etiqueta}: la versión no pasó la verificación')
            quedan = sum(1 for c in range(ch)
                         for h in clics.analizar(nuevos[c], sr, lo / sr, a.umbral)
                         if h[0] == 'salto' and ini <= h[1] < fin)
            rell = sum(b - a_ for c in range(ch) for a_, b in tramos[c])
            print(f'[{etiqueta}] {sum(len(t) for t in tramos.values())} tramos · {ms(rell, sr):.1f} ms rellenados '
                  f'(sumando canales) · {cambiadas} muestras cambiadas · quedan {quedan} saltos → {salida}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _rafagas(tramos, sr):
    junto = int(sr * RAFAGA_MS / 1000)
    grupos = []
    for t in tramos:
        if grupos and t[0] - grupos[-1][-1][1] <= junto:
            grupos[-1].append(t)
        else:
            grupos.append([t])
    return grupos


if __name__ == '__main__':
    main()
