#!/usr/bin/env python3
"""
Diagnóstico: qué calidad le entrega Tidal a la sesión de tidal-dl-ng, comparada con lo que permite tu cuenta.

  python3 tidal_calidad.py URL_O_ID_DEL_ALBUM [RUTA/token.json]     sesión de tidal-dl-ng
  python3 tidal_calidad.py URL_O_ID_DEL_ALBUM --login-pkce           inicia sesión PKCE (navegador) y prueba
  python3 tidal_calidad.py URL_O_ID_DEL_ALBUM --pkce                 prueba con la sesión PKCE ya guardada

La sesión PKCE se guarda aparte, en ~/.config/orbyte/tidal_pkce.json (solo tu usuario puede leerla):
no toca la de tidal-dl-ng ni cambia cómo descarga.

Muestra (sin imprimir tokens):
  · cómo está iniciada la sesión (código de dispositivo o PKCE)
  · tu suscripción y la mejor calidad que Tidal dice que te corresponde
  · para la primera pista del álbum: la calidad pedida y la que Tidal entrega de verdad

Se corre con el mismo Python de tidal-dl-ng (el que tiene tidalapi). No modifica token.json.
"""
import base64
import datetime
import glob
import json
import os
import re
import sys
from pathlib import Path

PKCE = Path(os.path.expanduser('~/.config/orbyte/tidal_pkce.json'))


def token_por_defecto():
    for d in ('tidal_dl_ng-dev', 'tidal_dl_ng'):
        p = os.path.expanduser(f'~/.config/{d}/token.json')
        if os.path.isfile(p):
            return p
    return (glob.glob(os.path.expanduser('~/.config/tidal_dl_ng*/token.json')) or [None])[0]


def claims(jwt):
    try:
        cuerpo = jwt.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(cuerpo + '=' * (-len(cuerpo) % 4)))
    except Exception:
        return {}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    m = re.search(r'(\d+)\s*/?\s*$', sys.argv[1])
    if not m:
        print('No reconozco el álbum: pasa la URL de Tidal o el número del álbum')
        return 2
    album = m.group(1)
    extra = sys.argv[2:]
    try:
        import tidalapi
    except ImportError:
        print('Este Python no tiene tidalapi: usa el de tidal-dl-ng (primera línea de ~/.local/bin/tidal-dl-ng)')
        return 2
    print(f'tidalapi {getattr(tidalapi, "__version__", "?")}')
    s = None
    if '--login-pkce' in extra:
        s = tidalapi.Session()
        print('Abre el enlace de abajo en el navegador (computador o celular) e inicia sesión con tu cuenta de Tidal.')
        print('Terminarás en una página de error «Oops»: es lo esperado. Copia la dirección completa de esa página')
        print('(empieza con https://tidal.com/android/login/auth?code=…) y pégala aquí.')
        print()
        try:
            s.login_pkce()
        except Exception as e:  # noqa: BLE001
            print(f'No se pudo iniciar sesión: {type(e).__name__}: {e}')
            return 1
        PKCE.parent.mkdir(parents=True, exist_ok=True)
        PKCE.touch(mode=0o600, exist_ok=True)
        os.chmod(PKCE, 0o600)
        s.save_session_to_file(PKCE)
        print(f'Sesión PKCE guardada en {PKCE}')
    elif '--pkce' in extra:
        s = tidalapi.Session()
        if not PKCE.is_file() or not s.load_session_from_file(PKCE):
            print(f'No hay una sesión PKCE válida en {PKCE}: corre con --login-pkce')
            return 1
        s.save_session_to_file(PKCE)                    # por si se renovó el token
    else:
        ruta = extra[0] if extra else token_por_defecto()
        print(f'token: {ruta}')
        with open(ruta, encoding='utf-8') as f:
            d = json.load(f)
        exp = d.get('expiry_time')
        exp = datetime.datetime.fromtimestamp(exp) if isinstance(exp, (int, float)) else None
        for pkce in (False, True):
            s2 = tidalapi.Session()
            try:
                ok = s2.load_oauth_session(d.get('token_type') or 'Bearer', d['access_token'], d.get('refresh_token'),
                                           exp, is_pkce=pkce)
                if not ok and d.get('refresh_token') and s2.token_refresh(d['refresh_token']):
                    ok = s2.load_oauth_session(s2.token_type, s2.access_token, d.get('refresh_token'), None,
                                               is_pkce=pkce)
            except Exception as e:  # noqa: BLE001
                print(f'  sesión {"PKCE" if pkce else "dispositivo"}: {type(e).__name__}: {e}')
                ok = False
            if ok:
                s = s2
                break
    if not s:
        print('Tidal no aceptó la sesión: inicia sesión de nuevo con «tidal-dl-ng login»')
        return 1
    c = claims(s.access_token)
    print(f'Sesión: {"PKCE" if s.is_pkce else "código de dispositivo («tidal-dl-ng login»)"} · país {s.country_code}'
          f' · token con «cuk»: {"sí" if "cuk" in c else "no"} · id de app (cid): {c.get("cid", "?")}')

    def get(ruta, **params):
        try:
            r = s.request.request('GET', ruta, params=params)
        except Exception as e:  # noqa: BLE001 — tidalapi lanza en 4xx/5xx: se usa la respuesta igual
            r = getattr(e, 'response', None)
            if r is None:
                return 0, {'userMessage': f'{type(e).__name__}: {e}'}
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    try:
        st, sub = get(f'users/{s.user.id}/subscription')
        print(f'Cuenta: suscripción {(sub.get("subscription") or {}).get("type", "?")} · '
              f'mejor calidad de tu plan: {sub.get("highestSoundQuality", "?")} (HTTP {st})')
    except Exception as e:  # noqa: BLE001
        print(f'Cuenta: no se pudo leer la suscripción ({type(e).__name__})')

    st, al = get(f'albums/{album}')
    print(f'Álbum: {al.get("title", "?")} · catálogo: {al.get("audioQuality", "?")} '
          f'{(al.get("mediaMetadata") or {}).get("tags", "")} (HTTP {st})')
    st, pistas = get(f'albums/{album}/tracks', limit=1)
    items = pistas.get('items') or []
    if not items:
        print(f'Sin pistas (HTTP {st})')
        return 1
    t = items[0]
    print(f'Pista: {t.get("title")} · catálogo: {t.get("audioQuality")} {(t.get("mediaMetadata") or {}).get("tags", "")}')
    print(f'       https://tidal.com/browse/track/{t["id"]}')
    for q in ('HI_RES_LOSSLESS', 'LOSSLESS', 'HIGH'):
        st, p = get(f'tracks/{t["id"]}/playbackinfopostpaywall', audioquality=q, playbackmode='STREAM',
                    assetpresentation='FULL')
        if st != 200:
            print(f'  pedida {q:16} → HTTP {st} {p.get("userMessage") or p.get("subStatus") or ""}')
            continue
        print(f'  pedida {q:16} → entrega {p.get("audioQuality")} · {p.get("bitDepth") or "-"} bits · '
              f'{p.get("sampleRate") or "-"} Hz · {p.get("manifestMimeType")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
