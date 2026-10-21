#!/usr/bin/env python3
"""Publica una versión nueva de la app iPhone para instalarla con SideStore
desde Orbyte (aviso "Hay una versión nueva" al abrir la app, o el enlace).

Uso (desde la carpeta del servidor):
  python3 tools/publicar_ipa.py ~/Descargas/Orbyte.ipa "Qué trae esta versión"
  python3 tools/publicar_ipa.py --estado     # versión publicada + enlaces para SideStore

La versión se lee del propio IPA (Info.plist: CFBundleShortVersionString y
CFBundleVersion). Se niega a publicar un build que no sea más nuevo que el
publicado (usa --forzar para saltarte esa revisión). No toca el IPA: SideStore
lo firma en cada teléfono, igual que cuando lo instalas a mano.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import zipfile
from urllib.parse import quote

APK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'apk')
FICHA = os.path.join(APK_DIR, 'ios.json')
DESTINO = os.path.join(APK_DIR, 'orbyte.ipa')
PUERTO = 5001


def leer_ficha():
    try:
        with open(FICHA, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def numero(build):
    try:
        return int(str(build).split('.')[0])
    except ValueError:
        return 0


def ips_del_servidor():
    """IP de la red local y de Tailscale, para armar los enlaces de prueba."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.168.0.1', 9))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        ts = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True, timeout=5).stdout.split()
        ips += [ip for ip in ts if ip and ip not in ips]
    except (OSError, subprocess.SubprocessError):
        pass
    return ips


def mostrar(ficha):
    print(f"Publicada: {ficha['version']} (build {ficha['build']}) el {ficha['publicado_at']} — "
          f"{ficha['tamano'] / 1e6:.1f} MB")
    if ficha.get('notas'):
        print(f"Notas: {ficha['notas']}")
    print('\nEnlaces para instalar con SideStore (abrir en Safari del iPhone):')
    for ip in ips_del_servidor():
        url = f"http://{ip}:{PUERTO}/descargas/orbyte.ipa?t={ficha['enlace_token']}"
        print(f"  {'Tailscale' if ip.startswith('100.') else 'Casa    '}: sidestore://install?url={quote(url, safe='')}")


def main():
    ap = argparse.ArgumentParser(description='Publicar la app iPhone en Orbyte (SideStore)')
    ap.add_argument('ipa', nargs='?', help='IPA a publicar (ej. Orbyte.ipa de ipa-main)')
    ap.add_argument('notas', nargs='?', default='', help='qué trae esta versión (se muestra en el aviso)')
    ap.add_argument('--estado', action='store_true', help='mostrar la versión publicada y los enlaces')
    ap.add_argument('--forzar', action='store_true', help='publicar aunque no sea más nuevo')
    a = ap.parse_args()

    actual = leer_ficha()
    if a.estado or not a.ipa:
        if actual:
            mostrar(actual)
        else:
            print('Todavía no hay ninguna versión de iPhone publicada.')
        return

    if not os.path.isfile(a.ipa):
        sys.exit(f'No encuentro el IPA: {a.ipa}')
    try:
        with zipfile.ZipFile(a.ipa) as z:
            nombres = [n for n in z.namelist() if re.fullmatch(r'Payload/[^/]+\.app/Info\.plist', n)]
            if not nombres:
                sys.exit('Ese archivo no parece un IPA (no trae Payload/*.app/Info.plist).')
            plist = plistlib.loads(z.read(nombres[0]))
    except zipfile.BadZipFile as e:
        sys.exit(f'No pude leer el IPA: {e}')

    version = str(plist.get('CFBundleShortVersionString') or '?')
    build = str(plist.get('CFBundleVersion') or '0')
    if actual and numero(build) <= numero(actual['build']) and not a.forzar:
        sys.exit(f"Ese IPA es la versión {version} (build {build}) y ya está publicada la "
                 f"{actual['version']} (build {actual['build']}). Baja el ipa-main más nuevo, o usa --forzar.")

    os.makedirs(APK_DIR, exist_ok=True)
    tmp = DESTINO + '.tmp'
    shutil.copyfile(a.ipa, tmp)
    sha = hashlib.sha256()
    with open(tmp, 'rb') as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b''):
            sha.update(bloque)
    ficha = {
        'version': version, 'build': build, 'bundle_id': plist.get('CFBundleIdentifier'),
        'notas': a.notas.strip(), 'tamano': os.path.getsize(tmp), 'sha256': sha.hexdigest(),
        'publicado_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        'enlace_token': secrets.token_urlsafe(24),
    }
    os.replace(tmp, DESTINO)                       # primero el IPA, después la ficha
    with open(FICHA + '.tmp', 'w', encoding='utf-8') as fh:
        json.dump(ficha, fh, ensure_ascii=False, indent=2)
    os.replace(FICHA + '.tmp', FICHA)
    print(f"Publicada la versión {version} (build {build}), {ficha['tamano'] / 1e6:.1f} MB.\n")
    mostrar(ficha)


if __name__ == '__main__':
    main()
