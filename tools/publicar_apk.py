#!/usr/bin/env python3
"""Publica una versión nueva de la app Android para que los usuarios la
reciban desde Orbyte (aviso "Hay una versión nueva" al abrir la app).

Uso (desde la carpeta del servidor):
  python3 tools/publicar_apk.py /ruta/app-debug.apk "Qué trae esta versión"
  python3 tools/publicar_apk.py --estado        # qué versión está publicada

El número de versión se lee del propio APK (assets/orbyte_version.json, lo
genera el build). Se niega a publicar una versión que no sea más nueva que
la publicada (usa --forzar para saltarte esa revisión).
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import zipfile

APK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'apk')
FICHA = os.path.join(APK_DIR, 'android.json')
DESTINO = os.path.join(APK_DIR, 'orbyte.apk')


def leer_ficha():
    try:
        with open(FICHA, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description='Publicar la app Android en Orbyte')
    ap.add_argument('apk', nargs='?', help='APK a publicar (ej. app-debug.apk)')
    ap.add_argument('notas', nargs='?', default='', help='qué trae esta versión (se muestra en el aviso)')
    ap.add_argument('--estado', action='store_true', help='mostrar la versión publicada y salir')
    ap.add_argument('--forzar', action='store_true', help='publicar aunque no sea más nueva')
    a = ap.parse_args()

    actual = leer_ficha()
    if a.estado or not a.apk:
        if actual:
            print(f"Publicada: {actual['version_name']} (código {actual['version_code']}) "
                  f"el {actual['publicado_at']} — {actual['tamano'] / 1e6:.1f} MB")
            if actual.get('notas'):
                print(f"Notas: {actual['notas']}")
        else:
            print('Todavía no hay ninguna versión publicada.')
        return

    if not os.path.isfile(a.apk):
        sys.exit(f'No encuentro el APK: {a.apk}')
    try:
        with zipfile.ZipFile(a.apk) as z:
            ver = json.loads(z.read('assets/orbyte_version.json'))
    except KeyError:
        sys.exit('Ese APK no trae assets/orbyte_version.json: compílalo con la versión '
                 'del proyecto que incluye el actualizador (git pull y volver a compilar).')
    except (zipfile.BadZipFile, ValueError) as e:
        sys.exit(f'No pude leer el APK: {e}')

    codigo, nombre = int(ver['version_code']), str(ver['version_name'])
    if actual and codigo <= int(actual['version_code']) and not a.forzar:
        sys.exit(f"Ese APK es la versión {nombre} (código {codigo}) y ya está publicada la "
                 f"{actual['version_name']} (código {actual['version_code']}). Compila de nuevo "
                 f"después de un git pull, o usa --forzar.")

    os.makedirs(APK_DIR, exist_ok=True)
    tmp = DESTINO + '.tmp'
    shutil.copyfile(a.apk, tmp)
    sha = hashlib.sha256()
    with open(tmp, 'rb') as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b''):
            sha.update(bloque)
    ficha = {
        'version_code': codigo, 'version_name': nombre, 'notas': a.notas.strip(),
        'tamano': os.path.getsize(tmp), 'sha256': sha.hexdigest(),
        'publicado_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
    }
    os.replace(tmp, DESTINO)                       # primero el APK, después la ficha
    with open(FICHA + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(ficha, fh, ensure_ascii=False, indent=2)
    os.replace(FICHA + '.tmp', FICHA)
    print(f"Publicada la versión {nombre} (código {codigo}), {ficha['tamano'] / 1e6:.1f} MB.")
    print('Los usuarios verán el aviso la próxima vez que abran Orbyte en Android.')


if __name__ == '__main__':
    main()
