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
import time

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
        """Conecta y se registra. Prueba primero wss:3001 (los firmwares LG
        actualizados cierran el ws:3000 sin cifrar apenas se conecta) y, si la
        conexión se corta antes de terminar el registro, prueba el otro puerto
        con un socket nuevo."""
        if not _WS_OK:
            raise WebOsError('Falta instalar websocket-client en el servidor (pip install websocket-client).')
        errores = []
        for url, sslopt in ((f'wss://{self.ip}:{WSS_PORT}', {'context': _ssl_contexto()}),
                            (f'ws://{self.ip}:{WS_PORT}', None)):
            self.cerrar()
            try:
                kw = {'timeout': self.timeout}
                if sslopt:
                    kw['sslopt'] = sslopt   # la tele usa un certificado autofirmado
                self._log(f'conectando a {url}')
                self._ws = websocket.create_connection(url, **kw)
            except Exception as e:  # noqa: BLE001 - se prueba el otro puerto
                self._log(f'{url} no conecta: {e.__class__.__name__}: {e}')
                errores.append(f'{url}: {e}')
                self._ws = None
                continue
            self._log(f'conectado a {url}; registrando ({"con" if self.client_key else "sin"} client-key)')
            try:
                self._registrar()
                self._log(f'registrado en {url}')
                return
            except _Cortada as e:
                self._log(f'{url} se cortó durante el registro: {e}')
                errores.append(f'{url}: {e}')
                if self._avisado:
                    # Ya salió «Permitir» en la tele: el corte vino de ahí (rechazo o cierre).
                    raise WebOsError(f'{e}. Vuelve a intentarlo y acepta «Permitir».') from None
                continue
        self.cerrar()
        raise WebOsError('No pude conectarme a la tele (' + self.ip + '). ' + ' | '.join(errores)
                         + '. Revisa en la tele: Configuración › General › Dispositivos externos ›'
                           ' «LG Connect Apps» / «Encender con móvil» activados.')

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
