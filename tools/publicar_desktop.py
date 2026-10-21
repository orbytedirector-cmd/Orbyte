#!/usr/bin/env python3
"""Publica una versión nueva de Orbyte Desktop para que la app se actualice
sola (aviso "Hay una versión nueva" al abrir, o "Buscar actualización" en
Configuración).

Acepta el instalador de Windows y el AppImage de Linux, tal como salen de
las releases dev-windows / dev-linux:
  python3 tools/publicar_desktop.py ~/orbyte-artifact/Orbyte-Setup-1.0.0-dev.28.exe "Qué trae"
  python3 tools/publicar_desktop.py ~/orbyte-artifact/Orbyte-1.0.0-dev.25-x86_64.AppImage "Qué trae"
  python3 tools/publicar_desktop.py --estado

El sistema y la versión salen del nombre del archivo. Cada sistema lleva su
propia numeración (dev.N de Windows y de Linux son independientes). Se niega
a publicar una versión que no sea más nueva que la publicada (--forzar).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys

APK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'apk')
PLATAFORMAS = ('windows', 'linux')
_VERSION = re.compile(r'(\d+)\.(\d+)\.(\d+)(?:-dev\.(\d+))?')


def ficha_path(plataforma):
    return os.path.join(APK_DIR, f'desktop-{plataforma}.json')


def leer_ficha(plataforma):
    try:
        with open(ficha_path(plataforma), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def numero(version):
    """1.0.0-dev.27 -> [1, 0, 0, 27]; 1.2.0 (release) -> [1, 2, 0, 1000000] (va después de sus dev)."""
    m = _VERSION.search(version or '')
    if not m:
        return None
    a, b, c, dev = m.groups()
    return [int(a), int(b), int(c), int(dev) if dev else 1_000_000]


def detectar(nombre):
    n = os.path.basename(nombre)
    if n.lower().endswith('.exe') and 'setup' in n.lower():
        return 'windows', 'Orbyte-Setup.exe'
    if n.endswith('.AppImage'):
        return 'linux', 'Orbyte.AppImage'
    return None, None


def main():
    ap = argparse.ArgumentParser(description='Publicar Orbyte Desktop en el servidor')
    ap.add_argument('archivo', nargs='?', help='Orbyte-Setup-X.exe o Orbyte-X-x86_64.AppImage')
    ap.add_argument('notas', nargs='?', default='', help='qué trae esta versión (se muestra en el aviso)')
    ap.add_argument('--estado', action='store_true', help='mostrar lo publicado y salir')
    ap.add_argument('--forzar', action='store_true', help='publicar aunque no sea más nueva')
    a = ap.parse_args()

    if a.estado or not a.archivo:
        for p in PLATAFORMAS:
            f = leer_ficha(p)
            if f:
                print(f"{p.capitalize():8}: {f['version']} — publicada el {f['publicado_at']} — "
                      f"{f['tamano'] / 1e6:.1f} MB" + (f" — {f['notas']}" if f.get('notas') else ''))
            else:
                print(f"{p.capitalize():8}: nada publicado todavía")
        return

    if not os.path.isfile(a.archivo):
        sys.exit(f'No encuentro el archivo: {a.archivo}')
    plataforma, destino_nombre = detectar(a.archivo)
    if not plataforma:
        sys.exit('No reconozco el archivo: tiene que ser el instalador de Windows (Orbyte-Setup-….exe) '
                 'o el AppImage de Linux (Orbyte-…-x86_64.AppImage). El ZIP portable y el .tar.gz '
                 'todavía no se actualizan solos.')
    m = _VERSION.search(os.path.basename(a.archivo))
    if not m:
        sys.exit('No pude leer la versión en el nombre del archivo (se espera algo como 1.0.0-dev.27).')
    version = m.group(0)
    actual = leer_ficha(plataforma)
    if actual and numero(version) <= (actual.get('numero') or [0]) and not a.forzar:
        sys.exit(f"Ese archivo es la versión {version} y para {plataforma} ya está publicada la "
                 f"{actual['version']}. Baja la release más nueva, o usa --forzar.")

    os.makedirs(APK_DIR, exist_ok=True)
    destino = os.path.join(APK_DIR, destino_nombre)
    tmp = destino + '.tmp'
    shutil.copyfile(a.archivo, tmp)
    sha = hashlib.sha256()
    with open(tmp, 'rb') as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b''):
            sha.update(bloque)
    ficha = {
        'plataforma': plataforma, 'version': version, 'numero': numero(version),
        'archivo': destino_nombre, 'nombre_original': os.path.basename(a.archivo),
        'notas': a.notas.strip(), 'tamano': os.path.getsize(tmp), 'sha256': sha.hexdigest(),
        'publicado_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    os.replace(tmp, destino)                       # primero el archivo, después la ficha
    with open(ficha_path(plataforma) + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(ficha, fh, ensure_ascii=False, indent=2)
    os.replace(ficha_path(plataforma) + '.tmp', ficha_path(plataforma))
    print(f"Publicada Orbyte Desktop {version} para {plataforma} ({ficha['tamano'] / 1e6:.1f} MB).")
    print('Los equipos con esa versión anterior verán el aviso la próxima vez que abran Orbyte.')


if __name__ == '__main__':
    main()
