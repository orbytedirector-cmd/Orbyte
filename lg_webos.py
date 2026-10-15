"""Ticket C-07 (pedido por Niko) — control mínimo de una tele LG webOS por la
red local, para abrir el "Modo TV" de Orbyte sin tocar el control.

Habla el mismo protocolo local que usa Home Assistant (websocket "SSAP" en el
puerto 3000, o 3001 con TLS en firmwares nuevos). Referencia del protocolo y
del manifiesto de permisos: aiowebostv (Apache-2.0), que es lo que usa HA.

Solo lo necesario:
- emparejar (la primera vez la tele muestra "¿Permitir conexión?"; después
  se usa la client-key guardada),
- encender por red (Wake-on-LAN),
- saber qué app está en primer plano (para volver a ella al terminar),
- abrir el navegador de la tele en una URL y cerrarlo,
- leer la MAC (para poder encenderla la próxima vez).

Dependencia: `websocket-client` (pip install websocket-client). Si falta,
`disponible()` devuelve False y el servidor lo explica en vez de romperse.
"""

import json
import socket
import ssl
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

try:
    import websocket  # websocket-client
    _WS_OK = True
except ImportError:  # pragma: no cover - depende del entorno del servidor
    websocket = None
    _WS_OK = False

WS_PORT = 3000
WSS_PORT = 3001
BROWSER_APP = 'com.webos.app.browser'

# Manifiesto sin firma (mismo que aiowebostv / Home Assistant).
_PERMISOS = [
    "APP_TO_APP", "CLOSE", "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK",
    "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_TEXT",
    "CONTROL_INPUT_TV", "CONTROL_MOUSE_AND_KEYBOARD", "CONTROL_POWER", "CONTROL_TV_SCREEN",
    "LAUNCH", "LAUNCH_WEBAPP", "READ_APP_STATUS", "READ_COUNTRY_INFO", "READ_CURRENT_CHANNEL",
    "READ_INPUT_DEVICE_LIST", "READ_INSTALLED_APPS", "READ_LGE_SDX", "READ_LGE_TV_INPUT_EVENTS",
    "READ_NETWORK_STATE", "READ_NOTIFICATIONS", "READ_POWER_STATE", "READ_RUNNING_APPS",
    "READ_SETTINGS", "READ_TV_CHANNEL_LIST", "READ_TV_CURRENT_TIME", "READ_UPDATE_INFO",
    "SEARCH", "TEST_OPEN", "TEST_PROTECTED", "TEST_SECURE", "UPDATE_FROM_REMOTE_APP",
    "WRITE_NOTIFICATION_ALERT", "WRITE_NOTIFICATION_TOAST", "WRITE_SETTINGS",
]


class WebOsError(Exception):
    """Error con un mensaje listo para mostrarle al usuario."""


class _Cortada(WebOsError):
    """La tele cerró el websocket."""


def _ssl_contexto():
    """La tele usa un certificado autofirmado y, en modelos 2020-2021, TLS y
    cifrados viejos que OpenSSL 3 rechaza por defecto."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (ValueError, AttributeError):
        pass
    try:
        ctx.set_ciphers('DEFAULT:@SECLEVEL=0')
    except ssl.SSLError:
        pass
    return ctx


def disponible():
    return _WS_OK


def puerto_abierto(ip, timeout=1.5):
    """¿La tele responde en el puerto de control? (encendida y en la red)."""
    for port in (WS_PORT, WSS_PORT):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def wake_on_lan(mac, ip=None):
    """Envía el "paquete mágico" para encender la tele por red."""
    limpio = ''.join(c for c in str(mac or '') if c.isalnum())
    if len(limpio) != 12:
        raise WebOsError('No tengo una MAC válida de la tele para encenderla por red.')
    paquete = b'\xff' * 6 + bytes.fromhex(limpio) * 16
    destinos = {'255.255.255.255'}
    if ip and ip.count('.') == 3:
        destinos.add(ip.rsplit('.', 1)[0] + '.255')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for d in destinos:
            for port in (9, 7):
                try:
                    s.sendto(paquete, (d, port))
                except OSError:
                    pass


_SSDP = ('239.255.255.250', 1900)
_ST_LG = 'urn:lge-com:service:webos-second-screen:1'


def _info_descripcion(location, timeout=2.5):
    """(nombre, modelo, udn) desde la descripción UPnP de la tele."""
    try:
        with urllib.request.urlopen(location, timeout=timeout) as r:
            root = ET.fromstring(r.read())
    except Exception:  # noqa: BLE001 - se usa lo que haya
        return None, None, None
    def txt(tag):
        for el in root.iter():
            if el.tag.rsplit('}', 1)[-1] == tag and el.text and el.text.strip():
                return el.text.strip()
        return None
    return txt('friendlyName'), txt('modelName'), txt('UDN')


def descubrir(timeout=3.0, log=None):
    """Busca teles LG webOS encendidas en la red local por SSDP. Pregunta por
    el servicio propio de LG ("webos-second-screen", el que usa Home
    Assistant), por DIAL y por todo (ssdp:all), y se queda con lo que se
    identifica como webOS/LG. Devuelve [{ip, nombre, modelo, udn}]."""
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:  # noqa: BLE001
                pass
    def _msg(st):
        return '\r\n'.join(['M-SEARCH * HTTP/1.1', f'HOST: {_SSDP[0]}:{_SSDP[1]}', 'MAN: "ssdp:discover"',
                             'MX: 2', f'ST: {st}', '', '']).encode()
    vistas, respondieron = {}, set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        for st in (_ST_LG, _ST_LG, 'urn:dial-multiscreen-org:service:dial:1', 'ssdp:all'):
            try:
                sock.sendto(_msg(st), _SSDP)
            except OSError as e:
                _log(f'Modo TV: búsqueda — no pude enviar SSDP ({e})')
        limite = time.time() + timeout
        while time.time() < limite:
            sock.settimeout(max(0.1, limite - time.time()))
            try:
                data, addr = sock.recvfrom(65507)
            except (socket.timeout, OSError):
                break
            respondieron.add(addr[0])
            cab = {}
            for linea in data.decode('utf-8', 'ignore').split('\r\n')[1:]:
                if ':' in linea:
                    k, v = linea.split(':', 1)
                    cab[k.strip().lower()] = v.strip()
            firma = ' '.join((cab.get('st', ''), cab.get('server', ''), cab.get('usn', ''))).lower()
            if 'lge-com' not in firma and 'webos' not in firma and ' lge ' not in f' {firma} ':
                continue
            # Preferir la LOCATION del servicio propio de LG (trae el nombre de la tele).
            if addr[0] not in vistas or 'lge-com' in cab.get('st', '').lower():
                vistas[addr[0]] = cab.get('location')
    finally:
        sock.close()
    _log(f'Modo TV: búsqueda SSDP — respondieron {len(respondieron)} equipos '
         f'({", ".join(sorted(respondieron)) or "ninguno"}); LG webOS: {", ".join(sorted(vistas)) or "ninguna"}')
    out = []
    def _uno(ip, loc):
        nombre, modelo, udn = _info_descripcion(loc) if loc else (None, None, None)
        out.append({'ip': ip, 'nombre': nombre or f'Tele LG ({ip})', 'modelo': modelo, 'udn': udn})
    hilos = [threading.Thread(target=_uno, args=(ip, loc), daemon=True) for ip, loc in vistas.items()]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(4)
    return sorted(out, key=lambda t: t['ip'])


def mac_desde_arp(ip):
    """MAC de `ip` según la tabla ARP del servidor (Linux), si la tele estuvo
    activa hace poco. None si no se sabe."""
    try:
        with open('/proc/net/arp', encoding='utf-8') as f:
            for linea in f.readlines()[1:]:
                partes = linea.split()
                if len(partes) >= 4 and partes[0] == ip and partes[3] != '00:00:00:00:00:00':
                    return partes[3]
    except OSError:
        pass
    return None


class WebOsTv:
    """Conexión de control a una tele. Usar como context manager:

        with WebOsTv(ip, client_key, on_prompt=fn) as tv:
            tv.abrir_url(url)
        nueva_key = tv.client_key
    """

    def __init__(self, ip, client_key=None, on_prompt=None, timeout=6, timeout_emparejar=60, log=None):
        self.ip = ip
        self.log = log
        self._avisado = False
        self.client_key = client_key or None
        self.on_prompt = on_prompt
        self.timeout = timeout
        self.timeout_emparejar = timeout_emparejar
        self._ws = None
        self._n = 0

    # -- conexión ----------------------------------------------------------
    def __enter__(self):
        self.conectar()
        return self

    def __exit__(self, *_a):
        self.cerrar()

    def _log(self, msg):
        if self.log:
            try:
                self.log(f'Modo TV [{self.ip}]: {msg}')
            except Exception:  # noqa: BLE001
                pass

    def conectar(self):
        """Conecta y se registra con la misma secuencia que Home Assistant
        (aiowebostv): "hello" -> info del sistema -> registro, en el mismo
        socket (los webOS nuevos cortan la conexión si uno se registra sin
        haber pedido antes la info del sistema). Prueba ws:3000 y wss:3001,
        con y sin cabecera Origin; si la tele corta, prueba la siguiente
        combinación con un socket nuevo. Cada paso queda en el log."""
        if not _WS_OK:
            raise WebOsError('Falta instalar websocket-client en el servidor (pip install websocket-client).')
        ws_url, wss_url = f'ws://{self.ip}:{WS_PORT}', f'wss://{self.ip}:{WSS_PORT}'
        intentos = ((ws_url, True), (wss_url, True), (ws_url, False), (wss_url, False))
        errores, sin_puerto = [], set()
        for url, sin_origin in intentos:
            if url in sin_puerto:
                continue
            self.cerrar()
            variante = f'{url} ({"sin" if sin_origin else "con"} Origin)'
            try:
                kw = {'timeout': self.timeout, 'suppress_origin': sin_origin}
                if url.startswith('wss'):
                    kw['sslopt'] = {'context': _ssl_contexto()}   # certificado autofirmado
                self._log(f'conectando a {variante}')
                self._ws = websocket.create_connection(url, **kw)
            except Exception as e:  # noqa: BLE001 - se prueba la siguiente combinación
                self._log(f'{variante} no conecta: {e.__class__.__name__}: {e}')
                errores.append(f'{url}: {e}')
                self._ws = None
                if isinstance(e, (ConnectionRefusedError, socket.timeout, TimeoutError, OSError)) \
                        and not isinstance(e, ssl.SSLError):
                    sin_puerto.add(url)
                continue
            try:
                self._saludar()
                self._log(f'{variante}: registrando ({"con" if self.client_key else "sin"} client-key)')
                self._registrar()
                self._log(f'registrado en {variante}')
                return
            except _Cortada as e:
                self._log(f'{variante} se cortó: {e}')
                errores.append(f'{variante}: {e}')
                if self._avisado:
                    # Ya salió «Permitir» en la tele: el corte vino de ahí (rechazo o cierre).
                    raise WebOsError(f'{e}. Vuelve a intentarlo y acepta «Permitir».') from None
                continue
        self.cerrar()
        raise WebOsError(f'La tele ({self.ip}) cortó la conexión en todos los intentos. '
                         'Revisa en la tele: Configuración › General › Dispositivos externos › '
                         '«LG Connect Apps» (o «Conectar app móvil») activado. Detalle: ' + ' | '.join(errores[-2:]))

    def _saludar(self):
        """"hello" e info del sistema antes del registro (como aiowebostv)."""
        self._enviar({'id': 'hello', 'type': 'hello', 'payload': {}})
        r = self._recibir(self.timeout)
        self._log(f'hello -> {str(r)[:300] if r is not None else "sin respuesta"}')
        self._enviar({'id': 'get_sys_info', 'type': 'request', 'uri': 'ssap://system/getSystemInfo', 'payload': {}})
        r = self._recibir(self.timeout)
        self._log(f'info del sistema -> {str(r)[:300] if r is not None else "sin respuesta"}')

    def _registrar(self):
        payload = {'forcePairing': False, 'pairingType': 'PROMPT',
                   'manifest': {'appVersion': '1.1', 'manifestVersion': 1, 'permissions': _PERMISOS}}
        if self.client_key:
            payload['client-key'] = self.client_key
        self._enviar({'type': 'register', 'id': 'register_0', 'payload': payload})
        limite = time.time() + self.timeout_emparejar
        self._avisado = avisado = False
        while time.time() < limite:
            r = self._recibir(max(1.0, limite - time.time()))
            if r is None:
                continue
            self._log(f'registro: recibido {str(r)[:200]}')
            if r.get('type') == 'response' and (r.get('payload') or {}).get('pairingType') == 'PROMPT':
                if not avisado and self.on_prompt:
                    self.on_prompt()
                self._avisado = avisado = True
                continue
            if r.get('type') == 'registered':
                self.client_key = (r.get('payload') or {}).get('client-key') or self.client_key
                return
            if r.get('type') == 'error':
                raise WebOsError('La tele rechazó la conexión: ' + str(r.get('error') or '')
                                 + ('. Vuelve a intentarlo y acepta «Permitir» en la tele.' if avisado else ''))
        raise WebOsError('No se aceptó la conexión en la tele a tiempo. Vuelve a intentarlo y acepta «Permitir».')

    def cerrar(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    # -- mensajes ----------------------------------------------------------
    def _enviar(self, msg):
        try:
            self._ws.send(json.dumps(msg))
        except Exception as e:  # noqa: BLE001
            raise _Cortada(f'Se cortó la conexión con la tele ({e.__class__.__name__}: {e})') from None

    def _recibir(self, timeout):
        self._ws.settimeout(timeout)
        try:
            data = self._ws.recv()
        except Exception as e:  # noqa: BLE001
            if isinstance(e, (socket.timeout, TimeoutError)) or 'Timeout' in e.__class__.__name__ \
                    or 'timed out' in str(e).lower():
                return None
            raise _Cortada(f'Se cortó la conexión con la tele ({e.__class__.__name__}: {e})')
        try:
            return json.loads(data)
        except (TypeError, ValueError):
            return None

    def pedir(self, uri, payload=None):
        """Envía una orden ssap://uri y devuelve el payload de la respuesta."""
        self._n += 1
        uid = f'orbyte_{self._n}'
        self._enviar({'id': uid, 'type': 'request', 'uri': f'ssap://{uri}', 'payload': payload or {}})
        limite = time.time() + self.timeout
        while time.time() < limite:
            r = self._recibir(max(0.5, limite - time.time()))
            if r is None or r.get('id') != uid:
                continue
            if r.get('type') == 'error':
                raise WebOsError(f'La tele respondió con error a {uri}: {r.get("error")}')
            return r.get('payload') or {}
        raise WebOsError(f'La tele no respondió a {uri}.')

    # -- acciones ----------------------------------------------------------
    def app_en_primer_plano(self):
        try:
            return (self.pedir('com.webos.applicationManager/getForegroundAppInfo') or {}).get('appId')
        except WebOsError:
            return None

    def mac(self):
        try:
            info = self.pedir('com.webos.service.connectionmanager/getinfo') or {}
        except WebOsError:
            return None
        for clave in ('wiredInfo', 'wifiInfo'):
            m = (info.get(clave) or {}).get('macAddress')
            if m and m != '00:00:00:00:00:00':
                return m
        return None

    def abrir_url(self, url):
        """Abre el navegador de la tele en `url`."""
        try:
            return self.pedir('system.launcher/launch', {'id': BROWSER_APP, 'target': url})
        except WebOsError:
            return self.pedir('system.launcher/open', {'target': url})

    def abrir_app(self, app_id):
        return self.pedir('system.launcher/launch', {'id': app_id})

    def cerrar_app(self, app_id):
        return self.pedir('system.launcher/close', {'id': app_id})
