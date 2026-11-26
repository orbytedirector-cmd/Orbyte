#!/usr/bin/env python3
"""Informe de los géneros de la biblioteca (solo lectura): cómo está escrito cada uno, dónde y cuántas veces, y
cómo quedaría con la regla de Niko (25-11-2026): cada palabra con mayúscula inicial y las palabras separadas por un
espacio, sin «-», «_» ni otros caracteres («Nu Metal», «Classic Rock», «Post Punk», «Hip Hop»).

Fuentes (music.db):
  tracks.genre                               el tag GENRE de cada archivo (puede traer varios separados por «;»)
  track_meta.genre_primary / genre_secondary   el enriquecimiento (RichMeta) por pista
  album_meta.genre_primary / genre_secondary   lo mismo resumido por álbum

Secciones:
  1. Duplicados: el mismo género escrito de varias formas (sin contar mayúsculas, tildes, «-», «_», espacios ni
     «&» = «and»), con la forma propuesta.
  2. Formato: escrito de una sola forma, pero no cumple la regla.
  3. Para revisar a mano: varios géneros en un mismo valor («/», «,», «;»), muy largos o con números.
  4. Siglas y palabras cortas que la regla trataría distinto (R&B, AOR, «and», «'n'»…).

Uso (desde la carpeta del repo):
  python3 tools/generos.py [--db music.db] [--salida generos.txt]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict

SIGLAS = {'aor', 'edm', 'idm', 'ebm', 'nwobhm', 'mpb', 'ost', 'uk', 'us', 'usa', 'dj', 'r&b', 'rnb', 'nwoahm',
          'ccm', 'lo', 'fi'}
SIGLAS_FORMA = {'r&b': 'R&B', 'rnb': 'R&B'}
SEPARADORES = re.compile(r'[\s\-_‐‑–—/\\.]+')
VARIOS = re.compile(r'[/,;|]')


def clave(g):
    """Misma clave = mismo género: sin tildes, minúsculas, «&» = «and», solo letras y números."""
    s = unicodedata.normalize('NFKD', g)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch)).lower().replace('&', ' and ')
    s = re.sub(r"\b'n'\b|\bn'\b|'n\b", ' and ', s)
    return re.sub(r'[^0-9a-z]+', '', s)


def regla(g):
    """La forma según la regla: cada palabra con mayúscula inicial, separadas por un espacio."""
    palabras = [p for p in SEPARADORES.split(g.strip()) if p]
    out = []
    for p in palabras:
        b = p.lower()
        if b in SIGLAS_FORMA:
            out.append(SIGLAS_FORMA[b])
        elif b in SIGLAS and len(b) > 2:
            out.append(b.upper())
        else:
            out.append(b[:1].upper() + b[1:])
    return ' '.join(out)


def partes(valor):
    if valor is None:
        return []
    if isinstance(valor, str) and valor.strip().startswith('['):
        try:
            lista = json.loads(valor)
            return [str(x).strip() for x in lista if str(x).strip()]
        except ValueError:
            pass
    return [p.strip() for p in str(valor).replace('\x00', ';').split(';') if p.strip()]


def columnas(con, tabla):
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info({tabla})')}
    except sqlite3.Error:
        return set()


def main():
    ap = argparse.ArgumentParser()
    aqui = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument('--db', default=os.path.join(aqui, 'music.db'))
    ap.add_argument('--salida')
    a = ap.parse_args()
    con = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True)

    usos = defaultdict(Counter)        # forma escrita -> {fuente: veces}
    fuentes = []
    if 'genre' in columnas(con, 'tracks'):
        fuentes.append(('tag GENRE (pistas)', 'SELECT genre FROM tracks WHERE genre IS NOT NULL AND genre != ""'))
    for tabla, quien in (('track_meta', 'pistas'), ('album_meta', 'álbumes')):
        cols = columnas(con, tabla)
        if 'genre_primary' in cols:
            fuentes.append((f'{tabla} principal ({quien})',
                            f'SELECT genre_primary FROM {tabla} WHERE genre_primary IS NOT NULL'))
        if 'genre_secondary' in cols:
            fuentes.append((f'{tabla} secundarios ({quien})',
                            f'SELECT genre_secondary FROM {tabla} WHERE genre_secondary IS NOT NULL'))
    for nombre, sql in fuentes:
        for (v,) in con.execute(sql):
            for p in partes(v):
                usos[p][nombre] += 1

    grupos = defaultdict(dict)
    for forma, c in usos.items():
        grupos[clave(forma) or forma.lower()][forma] = c
    total = lambda c: sum(c.values())

    lineas = []
    p = lineas.append
    p(f'Géneros distintos tal como están escritos: {len(usos)} · géneros reales (agrupados): {len(grupos)}')
    p('Fuentes: ' + ', '.join(n for n, _ in fuentes))
    p('')

    dup, formato, revisar, siglas = [], [], [], []
    for k, formas in grupos.items():
        mas_usada = max(formas, key=lambda f: total(formas[f]))
        propuesta = regla(mas_usada)
        n = sum(total(c) for c in formas.values())
        raras = [f for f in formas if VARIOS.search(f) or len(f) > 32 or re.search(r'\d', f)]
        if raras:
            revisar.append((n, propuesta, formas))
        if len(formas) > 1:
            dup.append((n, propuesta, formas))
        elif propuesta != mas_usada:
            formato.append((n, propuesta, formas))
        if any(w.lower() in SIGLAS or w.lower() in ('and', "'n'", 'n', 'y', 'of', 'the', 'de', 'en')
               for f in formas for w in SEPARADORES.split(f)):
            siglas.append((n, propuesta, formas))

    def bloque(titulo, filas):
        p(f'== {titulo}: {len(filas)} ==')
        for n, propuesta, formas in sorted(filas, key=lambda x: (-x[0], x[1])):
            p(f'{propuesta}   ({n} usos)')
            for f, c in sorted(formas.items(), key=lambda x: -total(x[1])):
                donde = ', '.join(f'{fu} {v}' for fu, v in c.most_common())
                p(f'    «{f}»  {donde}')
        p('')

    bloque('1. Duplicados (mismo género, varias formas)', dup)
    bloque('2. Formato (una forma, no cumple la regla)', formato)
    bloque('3. Para revisar a mano (varios géneros juntos, largos o con números)', revisar)
    bloque('4. Siglas y palabras cortas (R&B, AOR, «and», «de»…)', siglas)
    ok = sorted(regla(max(f, key=lambda x: total(f[x]))) for k, f in grupos.items()
                if len(f) == 1 and regla(next(iter(f))) == next(iter(f)))
    p(f'== Ya cumplen la regla: {len(ok)} ==')
    p(', '.join(ok))

    texto = '\n'.join(lineas) + '\n'
    if a.salida:
        with open(a.salida, 'w', encoding='utf-8') as fh:
            fh.write(texto)
        print(f'{len(usos)} formas, {len(grupos)} géneros · {len(dup)} duplicados, {len(formato)} de formato, '
              f'{len(revisar)} para revisar → {a.salida}')
    else:
        sys.stdout.write(texto)


if __name__ == '__main__':
    main()
