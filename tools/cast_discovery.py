#!/usr/bin/env python3
"""
cast_discovery.py — Reconocimiento de dispositivos para "Transmitir a..."
═══════════════════════════════════════════════════════════════════════════
Script standalone (solo librería estándar de Python, nada que instalar) para
correr UNA VEZ a mano en el server y ver qué aparece en tu LAN antes de
construir el botón real de "🔊 Reproducir en…" en el reproductor.

Hace dos cosas:

  1. SSDP discovery (multicast, como hace cualquier app de streaming al
     buscar "dispositivos cercanos") — encuentra renderers UPnP/DLNA y
     reporta si exponen el servicio AVTransport (el que necesitamos para
     mandarles "reproducí este archivo" sin pérdida de calidad).

  2. Para cada IP encontrada, prueba además el endpoint HTTP de Yamaha
     Extended Control (YXC) — que usan TODOS tus MusicCast por igual
     (el receiver Y las bocinas standalone), en el puerto 80, sin importar
     qué haya anunciado por SSDP.

USO:
    python3 tools/cast_discovery.py
    python3 tools/cast_discovery.py --timeout 8    # si tu red es lenta/grande

No modifica nada, no requiere permisos especiales, no toca Orbyte ni
music.db — es puramente de diagnóstico.
"""

import argparse
import socket
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
# ssdp:all trae de todo (TVs, impresoras, routers…) — filtramos después por
# lo que realmente nos interesa (MediaRenderer / AVTransport). Se podría
# acotar el ST a "urn:schemas-upnp-org:device:MediaRenderer:1" para menos
# ruido, pero ssdp:all también agarra dispositivos MusicCast que a veces
# anuncian tipos propios de Yamaha en vez del genérico de UPnP.
SSDP_ST = 'ssdp:all'

YXC_PROBE_PATH = '/YamahaExtendedControl/v1/system/getDeviceInfo'


def ssdp_discover(timeout=5):
    """Manda un M-SEARCH multicast y junta las respuestas únicas por LOCATION."""
    msg = '\r\n'.join([
        'M-SEARCH * HTTP/1.1',
        f'HOST: {SSDP_ADDR}:{SSDP_PORT}',
        'MAN: "ssdp:discover"',
        f'MX: {min(timeout, 5)}',
        f'ST: {SSDP_ST}',
        '', '',
    ]).encode('utf-8')

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))

    locations = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(65507)
        except socket.timeout:
            break
        text = data.decode('utf-8', errors='ignore')
        for line in text.split('\r\n'):
            if line.lower().startswith('location:'):
                locations.add(line.split(':', 1)[1].strip())
    sock.close()
    return locations


def fetch_device_description(location, timeout=4):
    """Descarga y parsea el XML de descripción UPnP de un dispositivo,
    devolviendo (friendlyName, manufacturer, modelName, av_transport_url) —
    av_transport_url es None si el dispositivo no tiene ese servicio
    (o sea, no sirve para "reproducí este archivo")."""
    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            xml_bytes = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    ns = {'d': 'urn:schemas-upnp-org:device-1-0'}
    device = root.find('d:device', ns)
    if device is None:
        return None

    def text_of(tag, default='?'):
        el = device.find(f'd:{tag}', ns)
        return el.text.strip() if el is not None and el.text else default

    friendly_name = text_of('friendlyName')
    manufacturer  = text_of('manufacturer')
    model_name    = text_of('modelName')

    av_transport_url = None
    for service in device.findall('.//d:service', ns):
        service_type = (service.find('d:serviceType', ns).text or '') \
            if service.find('d:serviceType', ns) is not None else ''
        if 'AVTransport' in service_type:
            control_url = service.find('d:controlURL', ns)
            if control_url is not None and control_url.text:
                av_transport_url = urljoin(location, control_url.text.strip())
        # Ya que estamos parseando el service list, buscamos también
        # ConnectionManager (típico de renderers DLNA reales) solo para
        # confirmar el diagnóstico — no lo necesitamos para nada más.

    return friendly_name, manufacturer, model_name, av_transport_url


def probe_yxc(ip, timeout=2):
    """Prueba si esta IP responde al endpoint HTTP de Yamaha Extended
    Control — funciona igual para el receiver que para las bocinas
    MusicCast standalone, siempre en el puerto 80."""
    url = f'http://{ip}{YXC_PROBE_PATH}'
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode('utf-8', errors='ignore')
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--timeout', type=int, default=5, help='Segundos de espera para las respuestas SSDP (default: 5)')
    args = parser.parse_args()

    print(f'Buscando dispositivos en la red (SSDP, {args.timeout}s)…\n')
    locations = ssdp_discover(timeout=args.timeout)

    if not locations:
        print('No respondió nadie por SSDP. Puede ser que:')
        print('  - El router/AP separe clientes por VLAN o tenga "AP isolation" activado')
        print('    (esto también explicaría el problema de LAN con los repetidores Huawei)')
        print('  - El firewall del server esté bloqueando multicast UDP entrante')
        return

    print(f'{len(locations)} dispositivo(s) UPnP respondieron. Consultando cada uno…\n')

    seen_ips = set()
    found_any_renderer = False

    for location in sorted(locations):
        info = fetch_device_description(location)
        ip = urlparse(location).hostname
        if ip:
            seen_ips.add(ip)

        print(f'── {location}')
        if not info:
            print('   (no se pudo leer la descripción del dispositivo)\n')
            continue

        friendly_name, manufacturer, model_name, av_transport_url = info
        print(f'   Nombre:        {friendly_name}')
        print(f'   Fabricante:    {manufacturer}')
        print(f'   Modelo:        {model_name}')
        if av_transport_url:
            found_any_renderer = True
            print(f'   AVTransport:   SÍ ✅  ({av_transport_url})')
            print('   -> Este dispositivo sirve para "Reproducir en…" por UPnP/DLNA sin pérdida de calidad.')
        else:
            print('   AVTransport:   no (este dispositivo no es un renderer de audio)')
        print()

    if seen_ips:
        print('Probando Yamaha Extended Control (YXC) en cada IP encontrada…\n')
        for ip in sorted(seen_ips):
            yxc = probe_yxc(ip)
            if yxc:
                print(f'── {ip}')
                print(f'   YXC (MusicCast): SÍ ✅')
                print(f'   {yxc[:300]}{"…" if len(yxc) > 300 else ""}\n')

    if not found_any_renderer:
        print('Ningún dispositivo anunció el servicio AVTransport por SSDP.')
        print('No es necesariamente un callejón sin salida: algunos MusicCast solo')
        print('anuncian AVTransport cuando están ENCENDIDOS y con la entrada de red')
        print('activa (no en standby) — probá con el receiver prendido en una entrada')
        print('de red/streaming antes de descartar esta vía.')


if __name__ == '__main__':
    main()
