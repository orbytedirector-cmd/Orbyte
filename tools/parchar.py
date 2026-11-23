#!/usr/bin/env python3
"""Parchar un tramo de una pista con otra copia, fuera de la biblioteca (ver ingesta/parche.py; en la ingesta es
«Tartamudeos» en la ficha del álbum). Escribe «<nombre> [parche].flac» en CARPETA: no toca ninguna de las dos.

Uso (desde la carpeta del repo):
  python3 tools/parchar.py PISTA OTRA_COPIA CARPETA --desde 32.74 --hasta 33.19
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingesta import parche  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pista')
    ap.add_argument('otra')
    ap.add_argument('carpeta')
    ap.add_argument('--desde', type=float, required=True)
    ap.add_argument('--hasta', type=float, required=True)
    ap.add_argument('--buscar', type=float, default=10.0, help='segundos de corrimiento máximo entre las copias')
    a = ap.parse_args()
    os.makedirs(a.carpeta, exist_ok=True)
    nombre = os.path.splitext(os.path.basename(a.pista))[0]
    salida = os.path.join(os.path.abspath(a.carpeta), f'{nombre} [parche].flac')
    try:
        inf = parche.parchar(os.path.abspath(a.pista), os.path.abspath(a.otra), a.desde, a.hasta, salida,
                             buscar=a.buscar, tmpdir=os.path.abspath(a.carpeta))
    except parche.Fallo as e:
        raise SystemExit(str(e))
    for linea in inf['lineas']:
        print(f'  {linea}')
    if inf.get('aviso'):
        print(f'  Ojo: {inf["aviso"]}')
    print(f'Parchado {a.desde:.3f}–{a.hasta:.3f} s ({inf["ms"]} ms + {inf["fundido_ms"]} ms de fundido a cada lado)'
          f' → {salida}')


if __name__ == '__main__':
    main()
