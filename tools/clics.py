#!/usr/bin/env python3
"""Busca clics, cortes, saltos y tartamudeos en pistas (solo lectura). Ver ingesta/clics.py.

Uso (desde la carpeta del repo):
  python3 tools/clics.py ARCHIVO [--desde 25] [--hasta 40] [--umbral 12]
  python3 tools/clics.py CARPETA/*.flac          una línea por pista (álbum entero)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingesta.clics import main  # noqa: E402

if __name__ == '__main__':
    main()
