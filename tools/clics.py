#!/usr/bin/env python3
"""Busca clics, cortes y saltos en un tramo de una pista (solo lectura).

La revisión de Salud solo ve cuadros que el decodificador no puede leer. Un clic que ya venía grabado
en el audio (archivo sano para libFLAC) no lo ve. Esta herramienta mira las muestras:

- salto: la segunda diferencia de la onda (x[n] - 2·x[n-1] + x[n-2]) se dispara muy por encima de lo
  normal en ese bloque de 5 ms. La música, sobre todo a 96/192 kHz, cambia suave entre muestras.
- silencio: tramo de ceros exactos (≥ 1 ms) en medio de audio con señal.
- repetidas: la misma muestra repetida (≥ 1 ms) con señal alrededor (pegado / relleno).
- recorte: muestras seguidas en el máximo digital.

También dice los bits que de verdad trae el archivo (un FLAC de 32 bit con los 8 de abajo en cero es
un 24 bit rellenado).

Uso:
  python3 tools/clics.py ARCHIVO [--desde 25] [--hasta 40] [--umbral 12]
"""
import argparse
import json
import math
import subprocess
import sys
from array import array

BLOQUE_MS = 5


def info(ruta):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                          'stream=sample_rate,channels,bits_per_raw_sample,sample_fmt:format=duration',
                          '-of', 'json', ruta], capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    s = j['streams'][0]
    return int(s['sample_rate']), int(s['channels']), int(s.get('bits_per_raw_sample') or 0), \
        float(j['format'].get('duration') or 0)


def leer(ruta, desde, hasta, canales):
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-ss', str(desde), '-t', str(hasta - desde), '-i', ruta,
           '-map', '0:a:0', '-f', 's32le', '-acodec', 'pcm_s32le', '-']
    datos = subprocess.run(cmd, capture_output=True, check=True).stdout
    a = array('i')
    a.frombytes(datos[:len(datos) - len(datos) % (4 * canales)])
    if sys.byteorder != 'little':
        a.byteswap()
    return [a[c::canales] for c in range(canales)]


def bits_efectivos(chs):
    o = 0
    for x in chs:
        for v in x:
            o |= v
    if o == 0:
        return 0
    cola = (o & -o).bit_length() - 1
    return 32 - cola


def tiempo(desde, i, sr):
    t = desde + i / sr
    m = int(t // 60)
    return f'{m}:{t - 60 * m:06.3f}'


def analizar(x, sr, desde, umbral):
    n = len(x)
    blk = max(64, int(sr * BLOQUE_MS / 1000))
    MAX = 2 ** 31 - 1
    hallazgos = []
    # saltos: pico de la segunda diferencia frente a su rms en el bloque (y en los vecinos)
    rms = []
    picos = []
    for b in range(0, n - 2, blk):
        s = 0.0
        pk = 0
        pi = b
        fin = min(n, b + blk)
        for i in range(max(2, b), fin):
            d = x[i] - 2 * x[i - 1] + x[i - 2]
            ad = d if d >= 0 else -d
            s += float(d) * d
            if ad > pk:
                pk, pi = ad, i
        cnt = max(1, fin - max(2, b))
        rms.append(math.sqrt(s / cnt))
        picos.append((pk, pi))
    for k, (pk, pi) in enumerate(picos):
        vecinos = [rms[j] for j in (k - 2, k - 1, k + 1, k + 2) if 0 <= j < len(rms)]
        ref = sorted(vecinos)[len(vecinos) // 2] if vecinos else rms[k]
        ref = max(ref, rms[k] * 0.5, 2 ** 31 * 1e-6)
        r = pk / ref
        if r >= umbral:
            nivel = max(abs(x[j]) for j in range(max(0, pi - 50), min(n, pi + 50))) or 1
            hallazgos.append(('salto', pi, pi, r, 20 * math.log10(pk / 2 ** 31 + 1e-12),
                              20 * math.log10(nivel / 2 ** 31 + 1e-12)))
    # silencios, repetidas y recorte
    minimo = max(2, int(sr / 1000))
    i = 1
    while i < n:
        if x[i] == x[i - 1]:
            j = i
            while j < n and x[j] == x[i - 1]:
                j += 1
            largo = j - i + 1
            v = x[i - 1]
            if abs(v) >= MAX - 1 and largo >= 3:
                hallazgos.append(('recorte', i - 1, j - 1, largo, 0, 0))
            elif largo >= minimo and 0 < i - 1 and j < n:
                antes = max(abs(x[k]) for k in range(max(0, i - 1 - blk), i - 1)) if i > 1 else 0
                despues = max(abs(x[k]) for k in range(j, min(n, j + blk))) if j < n else 0
                if min(antes, despues) > 2 ** 31 * 10 ** (-60 / 20):
                    hallazgos.append(('silencio' if v == 0 else 'repetidas', i - 1, j - 1, largo, 0, 0))
            i = j + 1
        else:
            i += 1
    return hallazgos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('archivo')
    ap.add_argument('--desde', type=float, default=0)
    ap.add_argument('--hasta', type=float, default=None)
    ap.add_argument('--umbral', type=float, default=12, help='veces sobre lo normal para contar un salto')
    a = ap.parse_args()
    sr, canales, bits, dur = info(a.archivo)
    hasta = a.hasta if a.hasta else dur
    if hasta - a.desde > 60:
        print('Tramo largo: en Python puro tarda ~1 s por cada 2 s de audio a 192 kHz.', file=sys.stderr)
    chs = leer(a.archivo, a.desde, hasta, canales)
    print(f'{a.archivo}')
    print(f'{sr} Hz · {canales} canales · {bits or "?"} bit declarados · '
          f'{bits_efectivos(chs)} bit con datos en el tramo · tramo {tiempo(a.desde, 0, sr)} a {tiempo(hasta, 0, sr)}')
    todos = []
    for c, x in enumerate(chs):
        for h in analizar(x, sr, a.desde, a.umbral):
            todos.append((h[1], c, h))
    if not todos:
        print('Sin clics, cortes ni recortes en el tramo.')
        return
    todos.sort()
    for _, c, (tipo, i, j, v, db, nivel) in todos[:200]:
        canal = ('izq', 'der')[c] if canales == 2 else str(c + 1)
        if tipo == 'salto':
            print(f'{tiempo(a.desde, i, sr)}  {canal}  salto  {v:5.1f}× lo normal · '
                  f'golpe {db:6.1f} dBFS · nivel alrededor {nivel:6.1f} dBFS')
        else:
            ms = v * 1000 / sr
            print(f'{tiempo(a.desde, i, sr)}  {canal}  {tipo}  {v} muestras ({ms:.2f} ms)')
    if len(todos) > 200:
        print(f'… y {len(todos) - 200} más')
    juntos = {}
    for _, c, h in todos:
        juntos.setdefault(round(h[1] / (sr / 1000)), set()).add(c)
    ambos = sum(1 for s in juntos.values() if len(s) > 1)
    print(f'{len(todos)} hallazgos · {ambos} en los dos canales a la vez (±1 ms)')


if __name__ == '__main__':
    main()
