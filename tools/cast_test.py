#!/usr/bin/env python3
"""
cast_test.py — Prueba real de "Reproducir en…" contra un renderer UPnP
═══════════════════════════════════════════════════════════════════════════
Corre DESPUÉS de tools/cast_discovery.py, usando la AVTransport URL que ese
script haya encontrado. Toma una pista real de tu base (por ID), le genera
un link firmado de corta duración (/cast-audio/<id>?token=…) y le manda al
renderer el mismo par de comandos SOAP que usaría cualquier app de
streaming: "acá está el archivo" (SetAVTransportURI) y "reproducí" (Play).

IMPORTANTE — este script se ejecuta EN el server (importa app.py directo
para reusar la lógica real de tokens/DB, no la reinventa), pero el
renderer necesita poder alcanzar al server por la URL que le pasás en
--server-host: si el server está en el 3er piso y tiene varias IPs/interfaces,
usá la IP LAN real (la misma que usás para entrar a Orbyte desde el celu),
NUNCA localhost/127.0.0.1 — el receiver no es la misma máquina.

USO:
    python3 tools/cast_test.py \\
        --control-url http://192.168.100.24:49154/AVTransport/ctrl \\
        --track-id 123 \\
        --server-host 192.168.100.50:5001

Para encontrar un --track-id rápido para probar, desde la carpeta del repo:
    python3 -c "
import sqlite3
c = sqlite3.connect('music.db')
c.row_factory = sqlite3.Row
r = c.execute('SELECT id, title, artist, file_path FROM tracks LIMIT 5').fetchall()
for x in r: print(dict(x))
"
(ajustá 'music.db' si tu DB_PATH es otro — revisá app.py si no estás seguro)
"""

import argparse
import os
import sys
import mimetypes
import urllib.request
import urllib.error
from xml.sax.saxutils import escape as xml_escape

AVT_NS = 'urn:schemas-upnp-org:service:AVTransport:1'

# DLNA/UPnP protocolInfo por extensión — best-effort. Los renderers en
# general son tolerantes con esto (usan más la extensión/Content-Type real
# del archivo al pedirlo), pero declarar bien el mimetype ayuda a que el
# renderer no rechace la pista de entrada. El caso DSD es el menos
# estandarizado — si el RX-A880 lo rechaza, es el primer lugar para mirar.
_MIME_BY_EXT = {
    '.flac': 'audio/flac',
    '.mp3':  'audio/mpeg',
    '.wav':  'audio/wav',
    '.aiff': 'audio/aiff',
    '.aif':  'audio/aiff',
    '.m4a':  'audio/mp4',
    '.alac': 'audio/mp4',
    '.dsf':  'audio/x-dsd',
    '.dff':  'audio/x-dsd',
}


def guess_protocol_info(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    mime = _MIME_BY_EXT.get(ext) or mimetypes.guess_type(file_path)[0] or 'application/octet-stream'
    return f'http-get:*:{mime}:*', mime


def build_didl(track_id, title, artist, protocol_info, media_url, file_size=None):
    size_attr = f' size="{file_size}"' if file_size else ''
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="{track_id}" parentID="0" restricted="1">'
        f'<dc:title>{xml_escape(title or "Pista")}</dc:title>'
        f'<dc:creator>{xml_escape(artist or "")}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="{xml_escape(protocol_info)}"{size_attr}>{xml_escape(media_url)}</res>'
        '</item></DIDL-Lite>'
    )


def soap_call(control_url, action, body_xml, timeout=8):
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_xml}</s:Body></s:Envelope>'
    ).encode('utf-8')

    req = urllib.request.Request(control_url, data=envelope, method='POST')
    req.add_header('Content-Type', 'text/xml; charset="utf-8"')
    req.add_header('SOAPACTION', f'"{AVT_NS}#{action}"')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='ignore')
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)


def set_av_transport_uri(control_url, media_url, didl):
    body = (
        f'<u:SetAVTransportURI xmlns:u="{AVT_NS}">'
        '<InstanceID>0</InstanceID>'
        f'<CurrentURI>{xml_escape(media_url)}</CurrentURI>'
        f'<CurrentURIMetaData>{xml_escape(didl)}</CurrentURIMetaData>'
        '</u:SetAVTransportURI>'
    )
    return soap_call(control_url, 'SetAVTransportURI', body)


def play(control_url):
    body = f'<u:Play xmlns:u="{AVT_NS}"><InstanceID>0</InstanceID><Speed>1</Speed></u:Play>'
    return soap_call(control_url, 'Play', body)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--control-url', required=True, help='AVTransport control URL (de cast_discovery.py)')
    parser.add_argument('--track-id', required=True, type=int, help='ID de la pista en tracks.id')
    parser.add_argument('--server-host', required=True,
                         help='IP LAN:puerto de ESTE server, tal cual la vería el renderer (ej: 192.168.100.50:5001). NUNCA localhost.')
    args = parser.parse_args()

    if args.server_host.startswith('127.') or 'localhost' in args.server_host:
        print('⚠️  --server-host no puede ser localhost/127.0.0.1 — el receiver es OTRA máquina en la red '
              'y necesita la IP LAN real de este server para poder descargar el archivo.')
        sys.exit(1)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import app as orbyte_app  # reusa DB_PATH, clean_db_path y el firmador de tokens reales

    conn = orbyte_app.get_db_connection()
    try:
        track = conn.execute(
            'SELECT id, title, artist, file_path FROM tracks WHERE id=?', (args.track_id,)
        ).fetchone()
    finally:
        conn.close()

    if not track:
        print(f'No existe ninguna pista con id={args.track_id}.')
        sys.exit(1)

    file_path = orbyte_app.clean_db_path(track['file_path'])
    if not os.path.isfile(file_path):
        print(f'La pista existe en la base pero el archivo no está en disco: {file_path}')
        sys.exit(1)

    token = orbyte_app.cast_token_for_track(track['id'])
    media_url = f'http://{args.server_host}/cast-audio/{track["id"]}?token={token}'
    protocol_info, mime = guess_protocol_info(file_path)
    file_size = os.path.getsize(file_path)
    didl = build_didl(track['id'], track['title'], track['artist'], protocol_info, media_url, file_size)

    print(f'Pista:          {track["artist"]} — {track["title"]}')
    print(f'Archivo:        {file_path}  ({file_size/1024/1024:.1f} MB, {mime})')
    print(f'URL firmada:    {media_url}')
    print(f'                (probála sola en el navegador si querés confirmar que baja bien el audio)')
    print(f'Control URL:    {args.control_url}')
    print()

    print('→ SetAVTransportURI…')
    status, body = set_av_transport_uri(args.control_url, media_url, didl)
    print(f'  HTTP {status}')
    if status != 200:
        print(f'  Respuesta: {body[:800]}')
        print('\n❌ El renderer rechazó la pista antes de intentar reproducirla. Motivos típicos:')
        print('   - No le gustó el protocolInfo/metadata del DIDL (más común con DSD)')
        print('   - No puede alcanzar --server-host (probá pegar la URL firmada en un navegador aparte)')
        print('   - El token venció (tienen 10 minutos) — volvé a correr el script')
        sys.exit(1)

    print('→ Play…')
    status, body = play(args.control_url)
    print(f'  HTTP {status}')
    if status == 200:
        print('\n✅ Comando aceptado — debería estar sonando. Confirmame si escuchás algo y con qué calidad/formato lo muestra el receiver.')
    else:
        print(f'  Respuesta: {body[:800]}')
        print('\n⚠️  SetAVTransportURI se aceptó pero Play falló — probá play manual desde el propio receiver/app MusicCast.')


if __name__ == '__main__':
    main()
