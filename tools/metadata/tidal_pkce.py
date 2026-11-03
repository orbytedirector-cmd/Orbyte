#!/usr/bin/env python3
"""
Sesión PKCE de Tidal para tidal-dl-ng.

Tidal limita a AAC 320 las sesiones iniciadas con «código de dispositivo» (la de «tidal-dl-ng login»); las PKCE
(inicio de sesión por navegador) reciben FLAC. tidal-dl-ng ya no ofrece el inicio PKCE y, al cargar su token, lo
trata siempre como de dispositivo: usa el access_token tal cual (sirve, la calidad la decide el token), pero si
tuviera que renovarlo lo haría con la app equivocada y fallaría.

Por eso la sesión PKCE «maestra» vive en ~/.config/orbyte/tidal_pkce.json (la crea
«tidal_calidad.py ALBUM --login-pkce») y, antes de cada descarga, este script la renueva y deja en el token.json
de tidal-dl-ng un access_token recién emitido: dura mucho más que una descarga, así que tidal-dl-ng nunca
necesita renovarlo.

  python3 tidal_pkce.py renovar      renueva la sesión PKCE y la escribe en el token.json de tidal-dl-ng
                                     (la primera vez respalda el token de dispositivo en token.json.dispositivo)
  python3 tidal_pkce.py restaurar    vuelve a dejar el token de dispositivo respaldado
  python3 tidal_pkce.py estado       qué sesión tiene hoy tidal-dl-ng

Se corre con el Python de tidal-dl-ng (el que tiene tidalapi). No imprime tokens.
"""
import base64
import calendar
import glob
import json
import os
import shutil
import sys
from pathlib import Path

PKCE = Path(os.path.expanduser('~/.config/orbyte/tidal_pkce.json'))


def token_tidal_dl_ng():
    for d in ('tidal_dl_ng-dev', 'tidal_dl_ng'):
        p = Path(os.path.expanduser(f'~/.config/{d}/token.json'))
        if p.is_file():
            return p
    hallados = glob.glob(os.path.expanduser('~/.config/tidal_dl_ng*/token.json'))
    return Path(hallados[0]) if hallados else None


def cid(jwt):
    try:
        cuerpo = jwt.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(cuerpo + '=' * (-len(cuerpo) % 4))).get('cid')
    except Exception:
        return None


def salida(**d):
    print(json.dumps(d, ensure_ascii=False))


def renovar():
    try:
        import tidalapi
    except ImportError:
        return salida(ok=False, error='Este Python no tiene tidalapi (usa el de tidal-dl-ng)')
    if not PKCE.is_file():
        return salida(ok=False, error='No hay sesión PKCE: créala con «tidal_calidad.py URL_ALBUM --login-pkce»')
    destino = token_tidal_dl_ng()
    if not destino:
        return salida(ok=False, error='No se encontró el token.json de tidal-dl-ng')
    with PKCE.open() as f:
        d = json.load(f)
    s = tidalapi.Session()
    s.is_pkce = True
    refresh = (d.get('refresh_token') or {}).get('data')
    try:
        if not refresh or not s.token_refresh(refresh):
            raise RuntimeError('Tidal no renovó la sesión')
        if not s.load_oauth_session(s.token_type, s.access_token, refresh, s.expiry_time, is_pkce=True):
            raise RuntimeError('Tidal no aceptó el token renovado')
    except Exception as e:  # noqa: BLE001
        return salida(ok=False, error=f'La sesión PKCE expiró o fue revocada ({type(e).__name__}); '
                                      'vuelve a crearla con «tidal_calidad.py URL_ALBUM --login-pkce»')
    s.refresh_token = refresh
    s.save_session_to_file(PKCE)
    os.chmod(PKCE, 0o600)

    respaldo = destino.with_name('token.json.dispositivo')
    with destino.open() as f:
        actual = json.load(f)
    if not respaldo.exists() and cid(actual.get('access_token') or '') != cid(s.access_token):
        shutil.copy2(destino, respaldo)
        os.chmod(respaldo, 0o600)
    vence = calendar.timegm(s.expiry_time.timetuple()) if s.expiry_time else None   # tidalapi: UTC sin zona
    nuevo = dict(actual)
    nuevo.update(token_type=s.token_type, access_token=s.access_token, refresh_token=refresh, expiry_time=vence)
    tmp = destino.with_suffix('.tmp')
    with tmp.open('w') as f:
        json.dump(nuevo, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, destino)
    return salida(ok=True, sesion='PKCE', vence=vence, pais=s.country_code)


def restaurar():
    destino = token_tidal_dl_ng()
    respaldo = destino.with_name('token.json.dispositivo') if destino else None
    if not respaldo or not respaldo.is_file():
        return salida(ok=False, error='No hay respaldo del token de dispositivo')
    shutil.copy2(respaldo, destino)
    return salida(ok=True, sesion='dispositivo')


def estado():
    destino = token_tidal_dl_ng()
    if not destino:
        return salida(ok=False, error='No se encontró el token.json de tidal-dl-ng')
    with destino.open() as f:
        t = json.load(f)
    c = cid(t.get('access_token') or '')
    pk = None
    if PKCE.is_file():
        with PKCE.open() as f:
            pk = cid(((json.load(f).get('access_token') or {}).get('data')) or '')
    return salida(ok=True, token=str(destino), cid=c,
                  sesion='PKCE' if pk and c == pk else 'dispositivo', hay_pkce=PKCE.is_file(),
                  respaldo=destino.with_name('token.json.dispositivo').is_file())


if __name__ == '__main__':
    orden = sys.argv[1] if len(sys.argv) > 1 else 'estado'
    {'renovar': renovar, 'restaurar': restaurar, 'estado': estado}.get(orden, estado)()
