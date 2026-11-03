#!/usr/bin/env python3
"""
Sesión de Tidal fresca a partir de la de tidal-dl-ng, para las consultas de solo lectura de Orbyte
(letras en RichMeta, búsqueda en la Ingesta).

tidal-dl-ng guarda su token en ~/.config/tidal_dl_ng[-dev]/token.json, pero cuando lo renueva lo hace solo en
memoria: el access_token del archivo puede estar vencido aunque «tidal-dl-ng login» diga que todo está bien.
Este script hace lo mismo que tidal-dl-ng (tidalapi.load_oauth_session, que renueva con el refresh_token) y
devuelve el token vigente por stdout en JSON. No modifica token.json.

Se corre con el mismo Python de tidal-dl-ng (el de la primera línea de ~/.local/bin/tidal-dl-ng), que es el que
tiene tidalapi instalado.

  python3 tidal_sesion.py RUTA/token.json   →  {"access_token": …, "token_type": …, "country_code": …}
"""
import datetime
import json
import os
import sys
from pathlib import Path


def main():
    try:
        import tidalapi
    except ImportError:
        print(json.dumps({"error": "tidalapi no está instalado en este Python"}))
        return 2
    # Sesión PKCE de Orbyte (~/.config/orbyte/tidal_pkce.json, ver tidal_pkce.py): es la que recibe FLAC y la que
    # se renueva con la app correcta. Si existe, manda; si no, la de tidal-dl-ng.
    pkce = Path(os.path.expanduser("~/.config/orbyte/tidal_pkce.json"))
    if pkce.is_file():
        s = tidalapi.Session()
        try:
            if s.load_session_from_file(pkce):
                s.save_session_to_file(pkce)
                os.chmod(pkce, 0o600)
                print(json.dumps({"access_token": s.access_token, "token_type": s.token_type,
                                  "country_code": s.country_code, "pkce": True}))
                return 0
        except Exception:  # noqa: BLE001 — se intenta con la de tidal-dl-ng
            pass
    with open(sys.argv[1], encoding="utf-8") as f:
        d = json.load(f)
    exp = d.get("expiry_time")
    exp = datetime.datetime.fromtimestamp(exp) if isinstance(exp, (int, float)) else None
    errores = []
    for pkce in (False, True):
        s = tidalapi.Session()
        try:
            ok = s.load_oauth_session(d.get("token_type") or "Bearer", d["access_token"], d.get("refresh_token"),
                                      exp, is_pkce=pkce)
            if not ok and d.get("refresh_token"):          # token revocado (no «expired»): renovar a la fuerza
                if s.token_refresh(d["refresh_token"]):
                    ok = s.load_oauth_session(s.token_type, s.access_token, d.get("refresh_token"), None, is_pkce=pkce)
        except Exception as e:  # noqa: BLE001
            ok = False
            errores.append(f"{type(e).__name__}: {e}")
        if ok:
            print(json.dumps({"access_token": s.access_token, "token_type": s.token_type,
                              "country_code": s.country_code}))
            return 0
    print(json.dumps({"error": "; ".join(errores) or "Tidal no aceptó la sesión de tidal-dl-ng (inicia sesión de nuevo)"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
