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

Golpes a distancia constante se informan como serie, con cuánto se parece el audio a sí mismo un paso después:
cerca de 1 es un TARTAMUDEO (el conversor o el reproductor repitió el mismo trozo de audio; la música original no
está ahí y no se puede recuperar del archivo, solo reemplazar).

La usa «Parchar» (ingesta/parche.py) para encontrar los tartamudeos de un álbum.

Uso (desde la carpeta del repo):
  python3 tools/clics.py ARCHIVO [--desde 25] [--hasta 40] [--umbral 12]
  python3 tools/clics.py CARPETA/*.flac          una línea por pista (álbum entero)
"""
import argparse
import json
import math
import os
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


TROZO_S = 30


def series(saltos, sr, tol=3, minimo=4):
    """Saltos de un canal a distancia constante (±tol muestras, entre 2 y 50 ms), al menos `minimo` seguidos.
    → [(inicio, fin, paso, n)] en muestras absolutas."""
    pos = sorted(saltos)
    out, usados = [], set()
    for a_i, a in enumerate(pos):
        if a in usados:
            continue
        for b in pos[a_i + 1:]:
            paso = b - a
            if paso > sr * 0.05:
                break
            if paso < sr * 0.002:
                continue
            cad, cur = [a, b], b
            while True:
                sig = [c for c in pos if abs(c - (cur + paso)) <= tol]
                if not sig:
                    break
                cur = sig[0]
                cad.append(cur)
            if len(cad) >= minimo:
                out.append((cad[0], cad[-1], round((cad[-1] - cad[0]) / (len(cad) - 1)), len(cad)))
                usados.update(cad)
                break
    return out


def repeticion(ruta, ini, fin, paso, sr, canales):
    """Cuánto se repite el audio a sí mismo `paso` muestras después dentro de la serie (la mejor ventana de dos
    pasos, con ±3 muestras de holgura: el largo del bucle puede ir cambiando). Cerca de 1 = el reproductor o el
    conversor repitió el mismo trozo una y otra vez (tartamudeo) y la música original no está."""
    a = max(0.0, (ini - paso) / sr)
    chs = leer(ruta, a, (fin + 2 * paso) / sr, canales)
    mejor = -1.0
    for x in chs:
        n = len(x)
        for s0 in range(0, max(1, n - 3 * paso - 3), max(1, paso // 2)):
            for dl in range(-3, 4):
                p = paso + dl
                if s0 + 2 * paso + p > n:
                    continue
                sxy = sxx = syy = 0.0
                for k in range(s0, s0 + 2 * paso, 3):
                    u, v = x[k], x[k + p]
                    sxy += float(u) * v
                    sxx += float(u) * u
                    syy += float(v) * v
                if sxx > 0 and syy > 0:
                    mejor = max(mejor, sxy / math.sqrt(sxx * syy))
    return mejor


def tartamudeos(ruta, umbral=6, minima=0.98, avance=None):
    """Tramos de la pista donde el audio se repite a sí mismo → [{desde, hasta, golpes, paso_ms, repeticion}]
    (segundos; con un período antes del primer golpe y uno después del último, más 20 ms de margen)."""
    sr, canales, _bits, dur = info(ruta)
    saltos = {c: [] for c in range(canales)}
    t = 0.0
    while t < dur:
        fin = min(dur, t + TROZO_S)
        chs = leer(ruta, t, min(dur, fin + 0.2), canales)
        base, largo = int(round(t * sr)), int(round((fin - t) * sr))
        for c, x in enumerate(chs):
            saltos[c] += [base + h[1] for h in analizar(x, sr, t, umbral) if h[0] == 'salto' and h[1] < largo]
        t = fin
        if avance:
            avance(fin / dur if dur else 1)
    sers = sorted((ini, fin_, paso, n) for c in saltos for ini, fin_, paso, n in series(saltos[c], sr))
    grupos = []
    for s in sers:
        if grupos and s[0] <= grupos[-1]['fin'] + s[2] * 2:
            g = grupos[-1]
            g['fin'] = max(g['fin'], s[1])
            g['golpes'] += s[3]
            g['series'].append(s)
        else:
            grupos.append({'ini': s[0], 'fin': s[1], 'paso': s[2], 'golpes': s[3], 'series': [s]})
    out = []
    for g in grupos:
        r = max(repeticion(ruta, s[0], s[1], s[2], sr, canales) for s in g['series'])
        if r < minima:
            continue
        out.append({'desde': round(max(0.0, (g['ini'] - g['paso']) / sr - 0.02), 3),
                    'hasta': round(min(dur, (g['fin'] + g['paso']) / sr + 0.02), 3),
                    'golpes': g['golpes'], 'paso_ms': round(g['paso'] * 1000 / sr, 2), 'repeticion': round(r, 4)})
    return out


def revisar(ruta, desde, hasta, umbral, resumen):
    sr, canales, bits, dur = info(ruta)
    hasta = hasta if hasta else dur
    todos, bits_vistos = [], 0
    t = desde
    while t < hasta:
        fin = min(hasta, t + TROZO_S)
        chs = leer(ruta, t, min(hasta, fin + 0.2), canales)
        bits_vistos = max(bits_vistos, bits_efectivos(chs))
        base = int(round(t * sr))
        largo = int(round((fin - t) * sr))
        for c, x in enumerate(chs):
            for h in analizar(x, sr, t, umbral):
                if h[1] < largo:
                    todos.append((base + h[1], c, (h[0], base + h[1], base + h[2]) + tuple(h[3:])))
        t = fin
    todos.sort()
    sers = []
    for c in range(canales):
        for ini, fin_, paso, n in series([h[1] for _, cc, h in todos if cc == c and h[0] == 'salto'], sr):
            sers.append((ini, fin_, paso, n, c))
    unidas = []
    for s in sorted(sers):
        r = repeticion(ruta, s[0], s[1], s[2], sr, canales)
        if unidas and s[0] <= unidas[-1][1] + s[2] * 2:
            u = unidas[-1]
            unidas[-1] = (u[0], max(u[1], s[1] + s[2] - u[2]), u[2], u[3] + s[3], u[4] | {s[4]}, max(u[5], r))
        else:
            unidas.append((s[0], s[1], s[2], s[3], {s[4]}, r))
    nombre_canal = (lambda c: ('izq', 'der')[c] if canales == 2 else str(c + 1))
    if resumen:
        txt = (f'{len(todos)} hallazgos' if todos else 'limpia')
        if unidas:
            txt += ' · ' + ' · '.join(
                f'{"TARTAMUDEO" if u[5] >= 0.98 else "serie"} {tiempo(0, u[0], sr)}–{tiempo(0, u[1] + u[2], sr)} '
                f'(cada {u[2] * 1000 / sr:.1f} ms, repetición {u[5]:.3f})'
                for u in unidas)
        print(f'{os.path.basename(ruta)}: {txt}', flush=True)
        return
    print(f'{ruta}')
    print(f'{sr} Hz · {canales} canales · {bits or "?"} bit declarados · '
          f'{bits_vistos} bit con datos en el tramo · tramo {tiempo(desde, 0, sr)} a {tiempo(hasta, 0, sr)}')
    if not todos:
        print('Sin clics, cortes ni recortes en el tramo.')
        return
    for _, c, (tipo, i, j, v, db, nivel) in todos[:200]:
        if tipo == 'salto':
            print(f'{tiempo(0, i, sr)}  {nombre_canal(c)}  salto  {v:5.1f}× lo normal · '
                  f'golpe {db:6.1f} dBFS · nivel alrededor {nivel:6.1f} dBFS')
        else:
            print(f'{tiempo(0, i, sr)}  {nombre_canal(c)}  {tipo}  {v} muestras ({v * 1000 / sr:.2f} ms)')
    if len(todos) > 200:
        print(f'… y {len(todos) - 200} más')
    juntos = {}
    for _, c, h in todos:
        juntos.setdefault(round(h[1] / (sr / 1000)), set()).add(c)
    ambos = sum(1 for s in juntos.values() if len(s) > 1)
    print(f'{len(todos)} hallazgos · {ambos} en los dos canales a la vez (±1 ms)')
    for u in unidas:
        r = u[5]
        print(f'Serie {tiempo(0, u[0], sr)}–{tiempo(0, u[1] + u[2], sr)}: {u[3]} golpes cada {u[2] * 1000 / sr:.2f} ms '
              f'({"/".join(nombre_canal(c) for c in sorted(u[4]))}) · repetición {r:.3f}'
              + (' → TARTAMUDEO: el mismo trozo repetido, la música original no está ahí' if r >= 0.98 else ''))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('archivos', nargs='+')
    ap.add_argument('--desde', type=float, default=0)
    ap.add_argument('--hasta', type=float, default=None)
    ap.add_argument('--umbral', type=float, default=12, help='veces sobre lo normal para contar un salto')
    ap.add_argument('--resumen', action='store_true', help='una línea por archivo (para revisar un álbum entero)')
    a = ap.parse_args()
    resumen = a.resumen or len(a.archivos) > 1
    for ruta in a.archivos:
        if not os.path.isfile(ruta) or ruta.lower().endswith(('.jpg', '.png', '.cue', '.log', '.txt', '.json')):
            continue
        try:
            revisar(ruta, a.desde, a.hasta, a.umbral, resumen)
        except subprocess.CalledProcessError:
            print(f'{os.path.basename(ruta)}: no se pudo leer', flush=True)


if __name__ == '__main__':
    main()
