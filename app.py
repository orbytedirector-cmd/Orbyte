import os
import re
import json
import math
import gzip
import random
import tempfile
import hashlib
import hmac
import socket
import struct
import signal
import atexit
import logging
import uuid
from logging.handlers import RotatingFileHandler
import urllib.request
import urllib.error
import http.client
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape
from urllib.parse import urljoin, urlparse
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context, session, redirect, url_for, g
# itsdangerous ya viene con Flask (lo usa internamente para firmar la cookie
# de sesión) — no es una dependencia nueva. La usamos para los tokens de
# /cast-audio: de corta duración y atados a una pista puntual, para que un
# dispositivo UPnP externo (que no puede loguearse) pueda pedir el archivo
# sin dejar la librería abierta al público.
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from genre_similarity import genre_similarity as _genre_similarity_fn
import ipaddress
import lg_webos  # Ticket C-07 — control de la tele LG webOS (Modo TV)
import ai_playlist  # Ticket AI-01 — agente de IA (intent parser + filter mapper), módulo aislado
import ai_playlist_pagination  # Ticket AI-27 — paginación ("Expandir"), módulo aislado
import behavior_engine  # Ticket AI-02 — motor de comportamiento (colección), módulo aislado
import estadisticas     # estadísticas de uso para el administrador (solo lectura)
import estadisticas_ia  # «Resumen IA» de esas estadísticas
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
# Ticket 19, Feature 4 — adjuntos (captura de pantalla + log) en el mail
# de reporte de errores. base64 para decodificar la captura que manda el
# cliente; MIMEBase/encoders para el patrón estándar de adjunto genérico
# (funciona igual para la imagen que para el .txt del log, no hace falta
# MIMEImage aparte).
import base64
from email.mime.base import MIMEBase
from email import encoders
import sqlite3
import mimetypes
import subprocess
import threading
import shutil
try:
    import requests as req_lib
except ImportError:
    req_lib = None

# Playlist colaborativa: genera el QR de invitación como PNG. Es una
# dependencia NUEVA (no está en start.sh) — se instala con:
#   pip install qrcode[pil]
# Igual que con requests/mpd más arriba, su ausencia no rompe el resto de la
# app: solo /admin/colaborativa/qr.png devuelve un error explicando qué falta.
try:
    import qrcode
    from io import BytesIO
    _QRCODE_AVAILABLE = True
except ImportError:
    _QRCODE_AVAILABLE = False

# Some systems' mimetypes DB doesn't know .flac — register it explicitly so
# /stream-dsd's cached output always gets served with the right Content-Type.
mimetypes.add_type('audio/flac', '.flac')

try:
    from mpd import MPDClient as _MPDClient
    _MPD_AVAILABLE = True
except ImportError:
    _MPD_AVAILABLE = False

# ── Favorites (Ticket 10 §3.3) ──────────────────────────────────────────────
# Reemplaza la vieja tabla `favorites` (global, sin user_id, solo pistas — ver
# AGENTS.md / Ticket 10 §2.2). El PO confirmó que no hace falta migrar datos
# viejos (se dropea la tabla directo, ver lazy migration en get_db_connection).
# A diferencia del viejo _favorites_set global, esto ya no puede vivir en un
# cache en memoria de proceso — es por-usuario, así que se resuelve por
# request contra user_item_favorites.

def _user_fav_track_ids(conn, user_id):
    """Set de track_id favoritos del usuario logueado, para las vistas HTML
    (artist.html/album.html/favorites.html) que pintan el corazón por fila."""
    if not user_id:
        return set()
    rows = conn.execute(
        "SELECT item_id FROM user_item_favorites WHERE user_id=? AND item_type='track'",
        (user_id,)
    ).fetchall()
    return {r[0] for r in rows}

# ── Nationality / Language → ISO 3166-1 alpha-2 country code ─────────────────
# Used to build flagcdn.com image URLs — works on all OS/browser combos,
# unlike Unicode flag emoji which require Noto Color Emoji on Linux.
COUNTRY_ISO = {
    'United States':'us','United Kingdom':'gb','England':'gb','Scotland':'gb',
    'Wales':'gb','Northern Ireland':'gb','Pontypridd':'gb','Brighton':'gb','London':'gb',
    'Germany':'de','Saarbrücken':'de','Dortmund':'de',
    'France':'fr','Italy':'it','Savona':'it',
    'Spain':'es','Madrid':'es','Asturias':'es',
    'Sweden':'se','Arvika Municipality':'se',
    'Norway':'no','Finland':'fi','Denmark':'dk','Copenhagen':'dk',
    'Netherlands':'nl','Australia':'au','Canada':'ca',
    'Brazil':'br','Argentina':'ar','Mexico':'mx','Chile':'cl',
    'Colombia':'co','Venezuela':'ve',
    'Japan':'jp','South Korea':'kr','Ireland':'ie','Portugal':'pt',
    'Greece':'gr','Poland':'pl','Switzerland':'ch','Iceland':'is',
    'New Zealand':'nz','South Africa':'za','Jamaica':'jm','Cuba':'cu',
    'Dominican Republic':'do','Puerto Rico':'pr','Philippines':'ph',
    'Kazakhstan':'kz','Bali':'id','Indonesia':'id',
    'Boston':'us','Miami':'us','New York':'us','San Francisco':'us',
    'Seattle':'us','Texas':'us','Washington':'us','Raleigh':'us',
    'Arlington':'us','Volcano':'us',
}

# Language code → ISO country code (language → representative country)
LANG_ISO = {
    'en':'gb','es':'es','de':'de','fr':'fr','pt':'pt','it':'it',
    'ja':'jp','ko':'kr','nl':'nl','ru':'ru','sv':'se','no':'no',
    'fi':'fi','da':'dk','pl':'pl','zh':'cn','ar':'sa','tr':'tr',
    'cs':'cz','hu':'hu','ro':'ro','uk':'ua','el':'gr','he':'il',
}

# Language code → full name (Spanish)
LANG_LABELS = {
    'en':'Inglés','es':'Español','de':'Alemán','fr':'Francés','pt':'Portugués',
    'it':'Italiano','ja':'Japonés','ko':'Coreano','nl':'Holandés','ru':'Ruso',
    'sv':'Sueco','no':'Noruego','fi':'Finlandés','da':'Danés','pl':'Polaco',
    'zh':'Chino','ar':'Árabe','tr':'Turco','cs':'Checo','hu':'Húngaro',
    'ro':'Rumano','uk':'Ucraniano','el':'Griego','he':'Hebreo',
}

def _flag_img(iso, label='', size='20x15'):
    """Return a flagcdn.com <img> tag. Empty string when iso is unknown."""
    if not iso:
        return ''
    return (f'<img src="https://flagcdn.com/{size}/{iso}.png" '
            f'width="{size.split("x")[0]}" height="{size.split("x")[1]}" '
            f'alt="{label}" title="{label}" '
            f'style="border-radius:2px;vertical-align:middle;object-fit:cover">')

def nationality_flag(nat):
    """Return an <img> flag tag for a nationality/country name. Empty when not found."""
    if not nat or nat in ('Unknown', ''):
        return ''
    iso = COUNTRY_ISO.get(nat)
    return _flag_img(iso, nat) if iso else ''

def lang_flag(code):
    """Return an <img> flag tag for a 2-letter language ISO 639-1 code."""
    if not code:
        return ''
    iso = LANG_ISO.get(code.lower())
    return _flag_img(iso, code.upper(), '20x15') if iso else ''

def lang_label(code):
    """Return the full Spanish name for a 2-letter language ISO 639-1 code."""
    if not code:
        return ''
    return LANG_LABELS.get(code.lower(), code.upper())


app = Flask(__name__)
CORS(app)

# ── Ticket 23: logging estructurado a archivo ────────────────────────────────
# Origen: cortes intermitentes de reproducción reportados por el PO, sin
# forma de diagnosticarlos porque el server ahora corre como servicio
# systemd sin consola a la vista (antes, `python app.py` interactivo
# mostraba todo por stdout — eso se perdió al pasar a servicio).
#
# Se configura el logger RAÍZ (logging.getLogger(), sin nombre) en vez de
# uno nuevo con nombre propio: app.logger (el que ya usan ~30 call sites
# en todo este archivo — warning/info/error en stream, report-issue,
# admin, etc.) propaga hacia el raíz por default. Configurando el raíz,
# TODO ese logging ya existente empieza a caer también al archivo nuevo
# sin tocar un solo call site — y cualquier logging nuevo que se agregue
# de acá en adelante, en cualquier parte del proyecto, cae solo.
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')

class _JSONLFormatter(logging.Formatter):
    """Un objeto JSON por línea (JSONL) — parseable con jq/scripts, no
    solo para leer a ojo. Campos estructurados extra (request_id,
    track_id, etc.) se agregan pasando `extra={...}` al logger; los que
    no aplican a una línea puntual simplemente no aparecen en ese objeto.
    """
    def format(self, record):
        payload = {
            'ts':     datetime.fromtimestamp(record.created, tz=timezone.utc)
                      .isoformat(timespec='milliseconds'),
            'level':  record.levelname,
            'logger': record.name,
            'msg':    record.getMessage(),
        }
        for key in (
            'request_id', 'method', 'path', 'status', 'duration_ms',
            'remote_addr', 'user_id', 'track_id',
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload['exc'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)

def _configure_logging():
    """Política de limpieza: RotatingFileHandler con maxBytes=10MB x
    backupCount=20 — tope duro de ~200MB en disco total, el archivo más
    viejo se borra solo apenas se supera ese tope. No hace falta cron ni
    intervención manual — el propio logging se autolimita.

    Nivel INFO por default (configurable con la variable de entorno
    ORBYTE_LOG_LEVEL sin tocar código, ej. DEBUG para una sesión de
    diagnóstico puntual sin dejarlo así permanentemente — a DEBUG cae
    bastante más volumen, ver el filtro en _log_request_end).
    """
    os.makedirs(_LOG_DIR, exist_ok=True)
    level = getattr(logging, os.environ.get('ORBYTE_LOG_LEVEL', 'INFO').upper(), logging.INFO)

    file_handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, 'orbyte.log'),
        maxBytes=10 * 1024 * 1024, backupCount=20, encoding='utf-8'
    )
    file_handler.setFormatter(_JSONLFormatter())
    file_handler.setLevel(level)

    # Consola también — journald (systemd) sigue capturando esto por su
    # cuenta como venía haciendo, sin pisarle nada; el archivo es la
    # fuente principal para "rescatar como archivo", esto es un espejo
    # legible a ojo si Niko alguna vez hace `journalctl -u orbyte -f`.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    )
    console_handler.setLevel(level)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # El logger 'werkzeug' (access log del server de desarrollo, y
    # potencialmente errores de transporte a nivel de socket —
    # BrokenPipeError/ConnectionResetError cuando un cliente se
    # desconecta a mitad de una respuesta) también propaga al raíz por
    # default. Se sube a WARNING (no INFO) para no duplicar el access
    # log de cada request — eso ya lo cubre _log_request_end() de forma
    # más rica (status, duración, usuario) — pero SIN silenciarlo del
    # todo: si Werkzeug loguea un error de transporte a WARNING/ERROR
    # (razonable esperar que sí, no confirmado 100% sin poder
    # reproducirlo en vivo), va a terminar en el archivo igual.
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Ticket 23: con debug=True (hoy solo con ORBYTE_DEBUG=1, ver app.run),
    # Flask por default RE-LANZA cualquier excepción no manejada para
    # que el debugger interactivo de Werkzeug la muestre en el
    # navegador, en vez de pasarla por @app.errorhandler — confirmado
    # contra la documentación oficial de Flask ("Exceptions are
    # re-raised... if TESTING or DEBUG is enabled"). Sin este override,
    # _log_unhandled_exception() de abajo nunca se dispararía con la
    # configuración actual.
    #
    # Trade-off consciente, no un efecto secundario no buscado: esto
    # también apaga el debugger interactivo en el navegador para
    # excepciones no manejadas. Corriendo como servicio desatendido,
    # tener el log en archivo importa más que ese debugger — que además,
    # accesible por cualquiera en la LAN/Tailscale, es en los hechos
    # una superficie de ejecución de código remota si algo lo dispara.
    # Si alguna vez hace falta el debugger interactivo para una sesión
    # de trabajo puntual, es una sola línea para comentar acá.
    app.config['PROPAGATE_EXCEPTIONS'] = False

    return file_handler

_configure_logging()

_request_logger = logging.getLogger('orbyte.request')
_error_logger    = logging.getLogger('orbyte.error')

@app.before_request
def _log_request_start():
    g.request_id = uuid.uuid4().hex[:8]
    g.request_start = time.monotonic()

@app.after_request
def _log_request_end(response):
    # El logging nunca debe poder romper una response real — cualquier
    # excepción acá se traga silenciosamente antes que tirar abajo un
    # request que por lo demás fue exitoso.
    try:
        duration_ms = (
            int((time.monotonic() - g.request_start) * 1000)
            if hasattr(g, 'request_start') else None
        )
        api_user = getattr(g, 'api_user', None)
        user_id = api_user['id'] if api_user else session.get('user_id')

        # No todo a INFO: /api/v1/auth* y /api/v1/stream* siempre (son
        # justo lo que este ticket necesita poder cruzar contra el log
        # del cliente), cualquier 4xx/5xx siempre (son señal, no ruido),
        # el resto queda en DEBUG — no aparece en el archivo con el
        # nivel INFO default, pero está ahí con ORBYTE_LOG_LEVEL=DEBUG
        # para una sesión de diagnóstico puntual sin tocar código.
        always_log_prefixes = ('/api/v1/auth', '/api/v1/stream')
        should_log_info = (
            response.status_code >= 400
            or request.path.startswith(always_log_prefixes)
        )
        level = logging.INFO if should_log_info else logging.DEBUG
        _request_logger.log(
            level, f"{request.method} {request.path} -> {response.status_code}",
            extra={
                'request_id':  getattr(g, 'request_id', None),
                'method':      request.method,
                'path':        request.path,
                'status':      response.status_code,
                'duration_ms': duration_ms,
                'remote_addr': request.remote_addr,
                'user_id':     user_id,
            }
        )
    except Exception:
        pass
    return response

@app.errorhandler(Exception)
def _log_unhandled_exception(e):
    """Catch-all — se registra DESPUÉS de cualquier @app.errorhandler más
    específico ya existente (ej. el de 404 de abajo), así que Flask llama
    a ese primero por ser más específico; esto solo entra para lo que
    nadie más maneja.

    HTTPException (abort(400), abort(403), los 404 de abajo, etc.) es
    tráfico normal — se devuelve tal cual (`return e`, patrón estándar de
    Flask para catch-alls que necesitan distinguir esto de un 500 real) y
    NO se loguea acá con traceback: ya queda cubierto por
    _log_request_end() de arriba (status >= 400 → INFO), sin duplicar ni
    ensuciar el log con un traceback para algo esperado. Solo una
    excepción de Python de verdad no manejada llega hasta acá abajo.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    _error_logger.error(
        f"Excepción no manejada en {request.method} {request.path}: {e}",
        exc_info=True,
        extra={
            'request_id':  getattr(g, 'request_id', None),
            'method':      request.method,
            'path':        request.path,
            'remote_addr': request.remote_addr,
        }
    )
    return jsonify({'error': 'internal_error'}), 500

# ── Diagnóstico: 404 sin ruta ─────────────────────────────────────────────
# Agregado para poder depurar el cliente nativo (iOS) sin consola de Xcode
# conectada: el access log default de Werkzeug ya muestra el path+query de
# cada request, pero queda perdido entre el resto del tráfico. Este
# warning puntual es mucho más fácil de encontrar/grepear. Para paths bajo
# /api/ devuelve JSON en vez del HTML default de Flask — no cambia nada
# para el resto de la web (se re-lanza la excepción tal cual).
@app.errorhandler(404)
def _handle_404(e):
    app.logger.warning(
        f"404 sin ruta: {request.method} {request.full_path} "
        f"UA={request.headers.get('User-Agent', '?')}"
    )
    if request.path.startswith('/api/'):
        return jsonify({'error': 'not_found', 'path': request.path}), 404
    return e

# ── Auth: secret key + session cookie config ─────────────────────────────────
# El correo del administrador (dueño de la plataforma). Cualquier signup con
# este correo queda auto-aprobado y con permisos de admin.
ADMIN_EMAIL = 'orbytedirector@gmail.com'

def _load_or_create_secret_key():
    """Usa SECRET_KEY de entorno si está definida (recomendado en producción).
    Si no, genera una clave y la persiste en .secret_key junto a app.py para
    que las sesiones sobrevivan reinicios del servidor sin configuración manual."""
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.secret_key')
    try:
        if os.path.isfile(key_path):
            with open(key_path, 'r') as f:
                existing = f.read().strip()
                if existing:
                    return existing
        new_key = secrets.token_hex(32)
        with open(key_path, 'w') as f:
            f.write(new_key)
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
        return new_key
    except Exception:
        # Último recurso: clave en memoria (las sesiones no sobreviven un reinicio)
        return secrets.token_hex(32)

app.secret_key = _load_or_create_secret_key()
if not os.environ.get('SECRET_KEY'):
    app.logger.warning(
        'SECRET_KEY no está definida como variable de entorno — usando una clave '
        'generada y guardada en .secret_key. Para producción, exporta SECRET_KEY '
        'con un valor fijo (ej: python3 -c "import secrets;print(secrets.token_hex(32))").'
    )

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# Solo marcar la cookie como Secure si Orbyte se sirve por HTTPS (ej. detrás de
# un reverse proxy TLS o Tailscale Serve). Por defecto queda en False para que
# el login funcione accediendo por Tailscale vía http://100.x.x.x sin TLS.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '0') == '1'

# ── Auth: notificaciones por correo (signup pendiente / aprobado / rechazado) ─
# Todas estas variables son opcionales: si SMTP_HOST no está definida, el envío
# de correos se omite silenciosamente (con un warning en el log) y el flujo de
# signup/aprobación/rechazo sigue funcionando igual — nunca rompe la app.
SMTP_HOST       = os.environ.get('SMTP_HOST', '')
SMTP_PORT       = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER       = os.environ.get('SMTP_USER', '')
SMTP_PASSWORD   = os.environ.get('SMTP_PASSWORD', '')
SMTP_FROM_EMAIL = os.environ.get('SMTP_FROM_EMAIL', SMTP_USER)
SMTP_FROM_NAME  = os.environ.get('SMTP_FROM_NAME', 'Orbyte')
SMTP_USE_TLS    = os.environ.get('SMTP_USE_TLS', '1') == '1'

def _send_email(to_addr, subject, html_body, attachments=None):
    """Envía un correo HTML, opcionalmente con adjuntos. Nunca lanza
    excepción — si falla, solo lo deja registrado en el log para no
    interrumpir signup/aprobación/rechazo (ni el reporte de errores,
    Ticket 19 §4).

    `attachments`, si viene, es una lista de tuplas
    (filename: str, content: bytes, mime_type: str) — Ticket 19 §4, para
    la captura de pantalla y el log de /api/v1/report-issue. Con
    adjuntos, el mensaje pasa a ser MIMEMultipart('mixed') con un
    MIMEMultipart('alternative') anidado adentro (el HTML) más un
    MIMEBase por adjunto — estructura estándar para HTML + adjuntos.
    Sin adjuntos (el caso de siempre: mails de aprobación/rechazo), el
    mensaje queda idéntico al de antes de este cambio —
    MIMEMultipart('alternative') solo, mismo comportamiento exacto.
    """
    if not SMTP_HOST:
        app.logger.warning(
            f'SMTP no configurado — se omite el envío de "{subject}" a {to_addr}. '
            f'Definí SMTP_HOST / SMTP_USER / SMTP_PASSWORD (y opcionalmente '
            f'SMTP_PORT, SMTP_FROM_EMAIL, SMTP_FROM_NAME, SMTP_USE_TLS) como '
            f'variables de entorno para activar las notificaciones por correo.'
        )
        return False
    try:
        if attachments:
            msg = MIMEMultipart('mixed')
            msg['Subject'] = subject
            msg['From'] = f'{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>'
            msg['To'] = to_addr
            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(html_body, 'html', 'utf-8'))
            msg.attach(alt)
            for filename, content, mime_type in attachments:
                maintype, _, subtype = (mime_type or 'application/octet-stream').partition('/')
                part = MIMEBase(maintype or 'application', subtype or 'octet-stream')
                part.set_payload(content)
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                msg.attach(part)
        else:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f'{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>'
            msg['To'] = to_addr
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))
        # Ticket 25: 20s en vez de 10 — el log adjunto ahora puede ser el
        # archivo completo (comprimido) en vez de un recorte de 200KB,
        # y el servidor SMTP de Gmail a veces tarda más que eso bajo
        # carga. Sigue acotado (nunca cuelga indefinidamente).
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM_EMAIL, [to_addr], msg.as_string())
        return True
    except Exception as e:
        app.logger.warning(f'No se pudo enviar el correo "{subject}" a {to_addr}: {e}')
        return False

def _email_wrapper(title, message_html):
    """Envoltorio HTML con el lenguaje visual de Orbyte (negro + dorado) para
    todos los correos transaccionales. Usa colores sólidos (sin gradientes de
    texto) porque la mayoría de los clientes de correo no soportan
    background-clip:text de forma confiable."""
    return f'''<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#000000;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#000000;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="100%" style="max-width:480px;background:#0d0d0d;border:1px solid rgba(200,146,10,0.35);border-radius:16px;overflow:hidden;">
        <tr><td style="padding:32px 32px 8px;text-align:center;">
          <div style="font-size:24px;font-weight:900;letter-spacing:3px;color:#F5C518;">ORBYTE</div>
        </td></tr>
        <tr><td style="padding:8px 32px 0;">
          <h1 style="color:#EFEFEF;font-size:19px;margin:16px 0 8px;font-family:Arial,Helvetica,sans-serif;">{title}</h1>
        </td></tr>
        <tr><td style="padding:0 32px 32px;color:#AAAAAA;font-size:14px;line-height:1.6;font-family:Arial,Helvetica,sans-serif;">
          {message_html}
        </td></tr>
        <tr><td style="padding:16px 32px;border-top:1px solid #1C1C1C;text-align:center;">
          <span style="color:#444444;font-size:11px;font-family:Arial,Helvetica,sans-serif;">Orbyte &mdash; tu biblioteca de música Hi-Res</span>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>'''

def _send_signup_pending_email(to_addr):
    _send_email(
        to_addr, 'Tu cuenta en Orbyte está en revisión',
        _email_wrapper('Cuenta creada — pendiente de aprobación', f'''
            <p>Hola,</p>
            <p>Tu cuenta <strong style="color:#EFEFEF;">{to_addr}</strong> se creó correctamente en Orbyte.</p>
            <p>Un administrador tiene que revisarla antes de que puedas ingresar. Te vamos a avisar
            a este mismo correo apenas se apruebe o se rechace.</p>
        ''')
    )

def _send_account_approved_email(to_addr):
    login_url = url_for('login', _external=True)
    _send_email(
        to_addr, '¡Tu cuenta en Orbyte fue aprobada!',
        _email_wrapper('Cuenta aprobada ✓', f'''
            <p>Buenas noticias — tu cuenta <strong style="color:#EFEFEF;">{to_addr}</strong> fue aprobada.</p>
            <p>Ya podés iniciar sesión y empezar a escuchar.</p>
            <p style="text-align:center;margin:28px 0 8px;">
              <a href="{login_url}" style="background:#C8920A;color:#000000;text-decoration:none;
                 padding:12px 28px;border-radius:8px;font-weight:700;display:inline-block;
                 font-family:Arial,Helvetica,sans-serif;">Iniciar sesión</a>
            </p>
        ''')
    )

def _send_account_rejected_email(to_addr):
    _send_email(
        to_addr, 'Tu solicitud de cuenta en Orbyte',
        _email_wrapper('Solicitud no aprobada', f'''
            <p>Tu solicitud de cuenta <strong style="color:#EFEFEF;">{to_addr}</strong> no fue aprobada
            por el administrador.</p>
            <p>Si creés que se trata de un error, respondé este correo para contactar al administrador.</p>
        ''')
    )

# ── Diamond SVG helper ────────────────────────────────────────────────────────
_DIAMOND_SVG_TMPL = (
    '<svg width="{w}" height="{h}" viewBox="0 0 20 22" fill="none" '
    'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="10" y1="1" x2="10" y2="3.5"/>'
    '<line x1="7" y1="1.8" x2="8.5" y2="3.5"/>'
    '<line x1="13" y1="1.8" x2="11.5" y2="3.5"/>'
    '<path d="M3 8 L7 4.5 L13 4.5 L17 8"/>'
    '<line x1="3" y1="8" x2="17" y2="8"/>'
    '<path d="M3 8 L10 19 L17 8"/>'
    '<line x1="7" y1="4.5" x2="10" y2="8"/>'
    '<line x1="13" y1="4.5" x2="10" y2="8"/>'
    '</svg>'
)
_DIAMOND_SIZES = {'sm': ('11','13'), 'md': ('13','15'), 'lg': ('16','18'), 'np': ('18','21')}

def diamond_svg(led_color, size='sm'):
    """Return a colored SVG diamond indicator for the given led_color tier."""
    color = (led_color or 'yellow').lower()
    w, h  = _DIAMOND_SIZES.get(size, ('11', '13'))
    svg   = _DIAMOND_SVG_TMPL.format(w=w, h=h)
    return f'<span class="led-diamond-wrap led-d-{color}">{svg}</span>'

# Register helpers in Jinja globals (must be after app is created)
app.jinja_env.globals['nationality_flag'] = nationality_flag
app.jinja_env.globals['lang_flag']        = lang_flag
app.jinja_env.globals['lang_label']       = lang_label
app.jinja_env.globals['diamond_svg']      = diamond_svg

# Make MOOD_LABELS available in all templates
@app.context_processor
def inject_globals():
    # ADV_* keys power the "Búsqueda Avanzada" filter capsules (see the
    # QUALITY_OPTIONS / *_BUCKETS constants defined near the advanced-search
    # routes below). Referencing them here is safe even though they are
    # defined later in the file: Flask only calls this function per-request,
    # long after the whole module has finished loading.
    current_user = None
    if session.get('user_id'):
        current_user = {'email': session.get('user_email', ''), 'is_admin': bool(session.get('is_admin'))}
        # El avatar no vive en la sesión (cambia más seguido que el resto y
        # no vale la pena mantenerlo sincronizado ahí) — una consulta chica
        # a users, aceptable en una app de este tamaño.
        conn = get_db_connection()
        try:
            row = conn.execute('SELECT avatar FROM users WHERE id=?', (session['user_id'],)).fetchone()
            current_user['avatar'] = row['avatar'] if row else None
        finally:
            conn.close()
    # Playlist colaborativa: un invitado nunca tiene current_user (no pasó
    # por /login) — collab_guest es su equivalente acotado, usado por
    # base.html para ocultar reproductor/favoritos/panel admin y mostrar el
    # nombre + cupo en su lugar.
    collab_guest = None
    if session.get('is_collab_guest'):
        collab_guest = {'name': session.get('collab_name', 'Invitado'),
                         'avatar': session.get('collab_avatar')
                                    or {'type': 'initials',
                                        'text': _collab_initials(session.get('collab_name', 'Invitado'))}}
    return {'MOOD_LABELS': MOOD_LABELS, 'LED_LABELS': LED_LABELS,
            'ADV_QUALITY_OPTIONS': QUALITY_OPTIONS,
            'ADV_POP_BUCKETS': POP_BUCKETS,
            'ADV_ENERGY_BUCKETS': ENERGY_BUCKETS,
            'ADV_BAIL_BUCKETS': BAIL_BUCKETS,
            'current_user': current_user,
            'collab_guest': collab_guest}

MUSIC_ROOT = "/mnt/musica/"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music.db")
# Avatares de invitado para la playlist colaborativa (ver ticket "playlist
# colaborativa" — selección de avatar). Viven en static/avatares/<categoria>/.
AVATAR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "avatares")

# ── LED color definitions (iFi Zen DAC V2) ───────────────────────────────────
# Source of truth: tracks.led_color field in the DB. Never recompute.
LED_LABELS = {
    'yellow':  'PCM 44.1/48 kHz',
    'white':   'PCM 88.2/96/176.4/192/352.8/384 kHz',
    'cyan':    'DSD 64/128',
    'red':     'DSD 256',
    'green':   'MQA',
    'blue':    'MQA Studio',
    'magenta': 'Original Sample Rate (MQB)',
    # 'silver' NO es un LED del DAC: el iFi recibe el MP3 ya decodificado a
    # PCM 44.1/48 y enciende amarillo (tracks.led_color sigue 'yellow', que
    # es la verdad del hardware). Es el color del DISTINTIVO de calidad para
    # archivos con pérdida — se deriva de codec/compression_mode, nunca se
    # guarda en led_color. Ver _es_con_perdida()/_lossy_sql().
    'silver':  'Lossy (MP3 · AAC)',
}
LED_ORDER = ['yellow', 'white', 'cyan', 'red', 'green', 'blue', 'magenta', 'silver']

# ── Calidad "Con Pérdida" ───────────────────────────────────────────────────
LOSSY_CODECS = ('mp3', 'aac', 'vorbis', 'opus', 'wmav1', 'wmav2', 'mp2')
_LOSSY_EXTS  = ('.mp3', '.ogg', '.opus', '.wma')   # .m4a puede ser ALAC: solo por codec
_LOSSY_CODEC_LABEL = {'vorbis': 'OGG', 'wmav1': 'WMA', 'wmav2': 'WMA'}
_STD_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
LOSSY_COLOR = 'silver'
LOSSY_QUALITY = 'LOSSY'


def _lossy_sql(alias='t'):
    """Fragmento SQL booleano: la pista es con pérdida. alias='' para
    subconsultas sin alias (SELECT ... FROM tracks WHERE album_id=...)."""
    p = f'{alias}.' if alias else ''
    codecs = ','.join(f"'{c}'" for c in LOSSY_CODECS)
    return (f"(COALESCE({p}compression_mode,'')='Lossy' "
            f"OR LOWER(COALESCE({p}codec,'')) IN ({codecs}))")


def _quality_color_sql(alias='t'):
    """Color de distintivo en SQL: 'silver' si es con pérdida, si no el
    led_color de la BD (mismo valor que devuelve _fmt_format)."""
    p = f'{alias}.' if alias else ''
    return f"(CASE WHEN {_lossy_sql(alias)} THEN 'silver' ELSE {p}led_color END)"


def _es_con_perdida(track):
    cm = (track.get('compression_mode') or '')
    if cm == 'Lossy':
        return True
    codec = (track.get('codec') or '').lower()
    if codec in LOSSY_CODECS:
        return True
    if not codec and not cm:
        ext = os.path.splitext(track.get('file_path') or '')[1].lower()
        return ext in _LOSSY_EXTS
    return False


def _kbps_nominal(track):
    """kbps 'de etiqueta' (320, 256, 192...). tracks.bitrate viene del
    contenedor (incluye la carátula embebida: un MP3 320 CBR figura como
    321-343k), así que se ajusta al valor estándar más cercano; un VBR que
    no calza queda con su promedio redondeado."""
    bps = track.get('bitrate') or 0
    if not bps:
        return None
    k = bps / 1000.0
    if (track.get('codec') or '').lower() == 'mp3' and k > 320:
        return 320
    best = min(_STD_KBPS, key=lambda v: abs(v - k))
    if abs(best - k) / best <= 0.06:
        return best
    return int(round(k))


def _lossy_label(track):
    codec = (track.get('codec') or '').lower()
    if not codec:
        codec = os.path.splitext(track.get('file_path') or '')[1].lower().lstrip('.')
    name = _LOSSY_CODEC_LABEL.get(codec, (codec or 'MP3').upper())
    kbps = _kbps_nominal(track)
    return f'{name} · {kbps} kbps' if kbps else name

# Mood display labels — maps raw DB value → friendly UI label
MOOD_LABELS = {
    'Humorístico': 'De Buen Humor',
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_db_path(path):
    if not path:
        return path
    return path.strip("'\"")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Fix (19/08, pregunta del PO sobre uso simultáneo con cuentas
    # distintas): sin esto, SQLite queda en su modo de journal por
    # default (rollback journal) — permite un solo escritor a la vez, y
    # con el timeout default de sqlite3 (5s) dos escrituras que
    # coincidan entre dos cuentas distintas (ej: heartbeat de una +
    # aprobar/favorito de la otra, al mismo tiempo) pueden terminar en
    # "database is locked" — un 500 real, aunque de baja probabilidad
    # con el uso actual. WAL permite lectores concurrentes con un solo
    # escritor sin bloquearse entre sí (el caso común), y el
    # busy_timeout más generoso le da margen a las pocas escrituras que
    # sí compiten para esperar en cola en vez de fallar en el acto.
    # `journal_mode=WAL` es una propiedad persistente del archivo de la
    # base (no hace falta en cada conexión para que "quede" activada),
    # pero correrlo acá es inofensivo si ya está en WAL (no-op) y
    # garantiza que quede activada sin depender de un paso manual
    # aparte.
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    # Ticket 14: funciones SQL custom para el scoring de Infinite.
    # genre_similarity: envuelve el modulo genre_similarity/ (taxonomia +
    # excepciones) — devuelve 0.0 si algun genero viene NULL en vez de
    # tirar excepcion (puede pasar, no todos los tracks tienen
    # genre_secondary). No se registra "una sola vez" a nivel modulo
    # porque cada request abre su propia conexion (mismo patron que el
    # resto de esta funcion) — registrar un callback Python es barato
    # (no hace I/O), así que hacerlo en cada conexion no es un problema
    # de performance real.
    def _genre_similarity_sql(g1, g2):
        if not g1 or not g2:
            return 0.0
        return _genre_similarity_fn(g1, g2)
    conn.create_function("genre_similarity", 2, _genre_similarity_sql)

    def _popularity_score_sql(listeners, playcount):
        """0.0-1.0, escala logaritmica -- lastfm_listeners/playcount son
        muy dispersos (de 0 a millones), una escala lineal quedaria
        dominada por outliers. Usa el mayor de los dos (playcount suele
        ser ~10x listeners, se escala para poder compararlos). Cap en 1M
        "equivalente a listeners" como techo de "muy popular" — evita
        tener que hacer un MAX() de toda la tabla en cada consulta."""
        value = max(listeners or 0, (playcount or 0) / 10.0)
        if value <= 0:
            return 0.0
        return min(1.0, math.log(value + 1) / math.log(1_000_001))
    conn.create_function("popularity_score", 2, _popularity_score_sql)
    # Lazy migration: add website_url column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE artists ADD COLUMN website_url TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists
    # Lazy migration: expression index for the "vista de pistas" de-dupe
    # (_track_dedupe_condition, ver más abajo). Sin este índice, el
    # NOT EXISTS correlacionado que colapsa duplicados hace un scan
    # completo de tracks por cada fila candidata (porque LOWER(TRIM(...))
    # no puede usar un índice normal) — con una biblioteca grande esto
    # tarda minutos. Con el índice, SQLite resuelve el match Título+Artista
    # con una búsqueda indexada en vez de una comparación fila por fila.
    # CREATE INDEX IF NOT EXISTS es prácticamente gratis una vez creado
    # (solo una consulta al catálogo), así que es seguro dejarlo acá.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_dedupe_norm "
        "ON tracks (LOWER(TRIM(title)), LOWER(TRIM(artist)))"
    )
    # Lazy migration: tabla de usuarios (login/sesiones). CREATE TABLE IF NOT
    # EXISTS es prácticamente gratis una vez creada la tabla.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            is_approved   INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            approved_at   TEXT,
            last_seen     TEXT,
            last_device   TEXT,
            last_ip       TEXT
        )
    ''')
    # Lazy migration: perfil extendido (Ticket 05, Lote D) — avatar (reusa
    # los mismos assets de static/avatares/ que ya usa la playlist
    # colaborativa), bio libre, géneros favoritos como JSON (mismo patrón
    # que similar_artists_json/similar_tracks_json ya existentes).
    try:
        conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN bio TEXT")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN favorite_genres_json TEXT")
        conn.commit()
    except Exception:
        pass
    # Lazy migration: Ticket 26 — apartado de Configuración. Una sola
    # columna JSON con TODAS las preferencias adentro (mismo patrón que
    # favorite_genres_json arriba) en vez de una columna por setting, para
    # no necesitar otra migración cada vez que se agrega una preferencia
    # nueva — ver DEFAULT_USER_SETTINGS / _settings_payload más abajo.
    try:
        conn.execute("ALTER TABLE users ADD COLUMN settings_json TEXT")
        conn.commit()
    except Exception:
        pass
    # Lazy migration: Ticket 19, Feature 5 — nickname opcional que
    # reemplaza al email en el Header del cliente nativo cuando está
    # seteado. NULL/vacío = se sigue mostrando el email (fallback resuelto
    # del lado del cliente, acá solo se persiste el valor tal cual).
    try:
        conn.execute("ALTER TABLE users ADD COLUMN nickname TEXT")
        conn.commit()
    except Exception:
        pass
    # Lazy migration: bandas favoritas del usuario (máximo 5, reforzado en
    # el endpoint, no acá). FK a artists real — a diferencia de los géneros,
    # una banda favorita sí necesita referenciar un id real del catálogo
    # para poder mostrarla con datos reales (portada, nombre correcto).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_favorite_artists (
            user_id     INTEGER NOT NULL,
            artist_id   INTEGER NOT NULL,
            added_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, artist_id)
        )
    ''')
    # Lazy migration: Playlist colaborativa (QR + invitados + cola). Igual
    # que arriba, CREATE TABLE IF NOT EXISTS es prácticamente gratis una vez
    # creadas. Solo puede haber UNA sesión activa a la vez (is_active=1) —
    # crear una nueva desactiva cualquier otra (ver admin_collab_crear).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS collab_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            token         TEXT UNIQUE NOT NULL,
            created_by    INTEGER NOT NULL,
            max_tracks    INTEGER NOT NULL DEFAULT 20,
            window_hours  REAL NOT NULL DEFAULT 2,
            is_active     INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at     TEXT
        )
    ''')
    # Un participante = un dispositivo/conexión distinguible: no hay
    # usuario/contraseña de invitado compartido, cada escaneo del QR crea su
    # propia fila acá y su propia sesión Flask firmada — la fila (no el
    # User-Agent, que es igual para dos iPhones) ES el identificador de
    # dispositivo que pide el ticket.
    # Un participante = un dispositivo/conexión distinguible. device_key
    # identifica el DISPOSITIVO físico (no la cookie de sesión, que se puede
    # perder o descartar a propósito) para que reescanear el QR — con o sin
    # sesión activa — vuelva a mapear a la MISMA fila y no reinicie el cupo
    # de pistas (ver _collab_device_key).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS collab_participants (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    INTEGER NOT NULL,
            device_key    TEXT NOT NULL,
            name          TEXT NOT NULL,
            joined_at     TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen     TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS collab_queue_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id      INTEGER NOT NULL,
            participant_id  INTEGER NOT NULL,
            track_id        INTEGER NOT NULL,
            added_at        TEXT NOT NULL DEFAULT (datetime('now')),
            dispatched      INTEGER NOT NULL DEFAULT 0
        )
    ''')
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_participant_device "
        "ON collab_participants (session_id, device_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_queue_session_disp "
        "ON collab_queue_items (session_id, dispatched)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_collab_queue_participant_added "
        "ON collab_queue_items (participant_id, added_at)"
    )
    # Lazy migration: "delegado" de playlist colaborativa (ticket: el admin
    # designa a UN participante que pueda pedir la actualización de la cola
    # sin que el admin tenga que tocar su celular — pensado para manejar y
    # no distraerse). can_pull vive en el participante (a quién se lo
    # delegaron); pull_requested_at/by viven en la sesión (el pedido en sí,
    # uno a la vez — ver _collab_set_delegate y /api/collab/solicitar-pull).
    try:
        conn.execute("ALTER TABLE collab_participants ADD COLUMN can_pull INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # Columna ya existe
    try:
        conn.execute("ALTER TABLE collab_sessions ADD COLUMN pull_requested_at TEXT")
        conn.commit()
    except Exception:
        pass  # Columna ya existe
    try:
        conn.execute("ALTER TABLE collab_sessions ADD COLUMN pull_requested_by INTEGER")
        conn.commit()
    except Exception:
        pass  # Columna ya existe
    # Lazy migration: avatar de invitado (ticket "playlist colaborativa —
    # avatar en el player del admin"). Solo se guarda la REFERENCIA a un
    # archivo que ya vive en static/avatares/ (categoria + nombre de
    # archivo) — nunca la imagen. Ambas NULL significa "no eligió avatar":
    # el admin arma el círculo de iniciales a partir de participants.name,
    # que ya se guardaba de todos modos. Mismo criterio de "vive mientras
    # la sesión colaborativa viva" que el resto de esta tabla.
    try:
        conn.execute("ALTER TABLE collab_participants ADD COLUMN avatar_category TEXT")
        conn.commit()
    except Exception:
        pass  # Columna ya existe
    try:
        conn.execute("ALTER TABLE collab_participants ADD COLUMN avatar_file TEXT")
        conn.commit()
    except Exception:
        pass  # Columna ya existe
    # Lazy migration: Ticket C-05 (Modo Colaborativo para usuarios con la
    # app). code = código corto de la sesión (para entrar sin escanear el
    # QR); user_id = participante con cuenta (nulo para invitados web);
    # cupo_desde/bloqueado_hasta = nuevo cupo fijo con bloqueo; activo = el
    # usuario con cuenta puede salir del modo y volver a entrar.
    for _sql in ("ALTER TABLE collab_sessions ADD COLUMN code TEXT",
                 "ALTER TABLE collab_participants ADD COLUMN user_id INTEGER",
                 "ALTER TABLE collab_participants ADD COLUMN cupo_desde TEXT",
                 "ALTER TABLE collab_participants ADD COLUMN bloqueado_hasta TEXT",
                 "ALTER TABLE collab_participants ADD COLUMN activo INTEGER DEFAULT 1"):
        try:
            conn.execute(_sql)
            conn.commit()
        except Exception:
            pass  # Columna ya existe
    # Lazy migration: Ticket C-06 (distribución de la reproducción).
    # auto_carga = la app del anfitrión pide sola la siguiente pista;
    # distribucion = 'turnos' | 'llegada' | 'menos'; inf_* = control de las
    # tandas del modo infinito (cuándo propuso por última vez y cuántas
    # pistas de usuarios había entonces). En las pistas: dispatched_at
    # (cuándo se entregó al reproductor), played_at (cuándo empezó a sonar)
    # y rescate (tanda de rescate del modo infinito: no cuenta en su cupo).
    for _sql in ("ALTER TABLE collab_sessions ADD COLUMN auto_carga INTEGER DEFAULT 1",
                 "ALTER TABLE collab_sessions ADD COLUMN distribucion TEXT DEFAULT 'turnos'",
                 "ALTER TABLE collab_sessions ADD COLUMN inf_ultima_at TEXT",
                 "ALTER TABLE collab_sessions ADD COLUMN inf_usuarios_n INTEGER",
                 "ALTER TABLE collab_queue_items ADD COLUMN dispatched_at TEXT",
                 "ALTER TABLE collab_queue_items ADD COLUMN played_at TEXT",
                 "ALTER TABLE collab_queue_items ADD COLUMN rescate INTEGER DEFAULT 0"):
        try:
            conn.execute(_sql)
            conn.commit()
        except Exception:
            pass  # Columna ya existe
    # Ticket C-10 (pedido por Niko): Orbyte social, etapa 1 — amigos con
    # amistad mutua, silenciados y centro de notificaciones. Las amistades se
    # guardan una vez por par (usuario_a < usuario_b).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS amistades (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_a     INTEGER NOT NULL,
            usuario_b     INTEGER NOT NULL,
            solicitante   INTEGER NOT NULL,
            estado        TEXT NOT NULL DEFAULT 'pendiente',
            creada_at     TEXT NOT NULL,
            aceptada_at   TEXT,
            UNIQUE (usuario_a, usuario_b)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS amigos_silenciados (
            usuario_id    INTEGER NOT NULL,
            amigo_id      INTEGER NOT NULL,
            PRIMARY KEY (usuario_id, amigo_id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notificaciones (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario_id    INTEGER NOT NULL,
            tipo          TEXT NOT NULL,
            actor_id      INTEGER,
            texto         TEXT NOT NULL,
            datos_json    TEXT,
            fuera         INTEGER NOT NULL DEFAULT 1,
            leida_at      TEXT,
            creada_at     TEXT NOT NULL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_notificaciones_usuario ON notificaciones (usuario_id, id)')
    try:
        conn.execute("ALTER TABLE users ADD COLUMN amigo_codigo TEXT")   # QR de la cuenta (C-10)
        conn.commit()
    except Exception:
        pass
    # Ticket C-10 etapa 2: playlists compartidas con amigos. permiso 'ver' |
    # 'editar' por persona; playlist_tracks.added_by = quién agregó la pista
    # (para mostrar su avatar en una playlist compartida).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS playlist_compartidas (
            playlist_id   INTEGER NOT NULL,
            usuario_id    INTEGER NOT NULL,
            permiso       TEXT NOT NULL DEFAULT 'ver',
            compartida_at TEXT NOT NULL,
            PRIMARY KEY (playlist_id, usuario_id)
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_playlist_compartidas_usuario ON playlist_compartidas (usuario_id)')
    # Ticket C-10 etapa 3: actividad entre amigos — likes, toques, respect,
    # recomendaciones (y quién las escuchó), escuchas de playlists ajenas y
    # comentarios en playlists. dia = fecha local del servidor (límites diarios).
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS social_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, de_id INTEGER NOT NULL, para_id INTEGER NOT NULL,
            tipo TEXT NOT NULL, ref_id INTEGER NOT NULL, dia TEXT NOT NULL, creada_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_social_likes_para ON social_likes (para_id, tipo);
        CREATE INDEX IF NOT EXISTS idx_social_likes_ref ON social_likes (tipo, ref_id);
        CREATE TABLE IF NOT EXISTS social_toques (
            id INTEGER PRIMARY KEY AUTOINCREMENT, de_id INTEGER NOT NULL, para_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL, dia TEXT NOT NULL, creada_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_social_toques_para ON social_toques (para_id);
        CREATE TABLE IF NOT EXISTS social_respects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, de_id INTEGER NOT NULL, para_id INTEGER NOT NULL,
            dia TEXT NOT NULL, creada_at TEXT NOT NULL, UNIQUE (de_id, dia));
        CREATE INDEX IF NOT EXISTS idx_social_respects_para ON social_respects (para_id);
        CREATE TABLE IF NOT EXISTS social_recomendaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT, de_id INTEGER NOT NULL, track_id INTEGER NOT NULL,
            dia TEXT NOT NULL, creada_at TEXT NOT NULL, UNIQUE (de_id, track_id, dia));
        CREATE TABLE IF NOT EXISTS social_recomendacion_escuchas (
            recomendacion_id INTEGER NOT NULL, usuario_id INTEGER NOT NULL, creada_at TEXT NOT NULL,
            PRIMARY KEY (recomendacion_id, usuario_id));
        CREATE TABLE IF NOT EXISTS playlist_escuchas (
            id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER NOT NULL, usuario_id INTEGER NOT NULL,
            creada_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_playlist_escuchas_pl ON playlist_escuchas (playlist_id);
        CREATE TABLE IF NOT EXISTS playlist_comentarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT, playlist_id INTEGER NOT NULL, usuario_id INTEGER NOT NULL,
            texto TEXT NOT NULL, creada_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_playlist_comentarios_pl ON playlist_comentarios (playlist_id, id);
    ''')
    _dsc_migrar(conn)   # Ticket C-11 (Descubrir)
    _integ.migrar(conn)  # Ticket 42b (clave de Alexa)
    for _sql in ("ALTER TABLE playlist_tracks ADD COLUMN added_by INTEGER",
                 "ALTER TABLE playlists ADD COLUMN copia_de INTEGER"):
        try:
            conn.execute(_sql)
            conn.commit()
        except Exception:
            pass  # Columna ya existe
    # Ticket C-07 (Modo TV): teles LG webOS donde Orbyte muestra lo que suena.
    # client_key = autorización que da la tele al aceptar «Permitir» (una vez);
    # clave = parte secreta del link de solo lectura de la página /tv/<clave>;
    # prev_app = app que tenía la tele antes, para volver a ella al quitar.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tv_pantallas (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ip          TEXT UNIQUE NOT NULL,
            nombre      TEXT,
            mac         TEXT,
            client_key  TEXT,
            clave       TEXT NOT NULL,
            prev_app    TEXT,
            activa      INTEGER NOT NULL DEFAULT 0,
            admin_id    INTEGER,
            usada_at    TEXT
        )
    ''')
    # Ticket C-07c: elegir entre varias teles — nombre propio (alias) y el
    # identificador UPnP (udn) para reconocerla aunque cambie de IP.
    for _sql in ("ALTER TABLE tv_pantallas ADD COLUMN alias TEXT",
                 "ALTER TABLE tv_pantallas ADD COLUMN udn TEXT",
                 # Ticket C-08: tipo 'lg' | 'app' (Orbyte TV en Android TV),
                 # modo de pantalla elegido la última vez ('normal' | 'oled').
                 "ALTER TABLE tv_pantallas ADD COLUMN tipo TEXT DEFAULT 'lg'",
                 "ALTER TABLE tv_pantallas ADD COLUMN modo TEXT DEFAULT 'normal'",
                 "ALTER TABLE tv_pantallas ADD COLUMN app_secret TEXT",
                 "ALTER TABLE tv_pantallas ADD COLUMN app_modelo TEXT",
                 "ALTER TABLE tv_pantallas ADD COLUMN dispositivo TEXT"):
        try:
            conn.execute(_sql)
            conn.commit()
        except Exception:
            pass  # Columna ya existe
    # Lazy migration: "Reproducir en…" — dispositivos UPnP/DLNA (receiver,
    # bocinas MusicCast, etc.) que el admin descubrió y curó a mano. No
    # volvemos a escanear la red en cada request: el escaneo SSDP es lento
    # (varios segundos) y ruidoso (agarra TVs, el router…), así que se hace
    # solo cuando el admin aprieta "Buscar dispositivos" y acá queda
    # guardado el resultado ya filtrado (solo lo que tiene AVTransport).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS cast_targets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            control_url   TEXT NOT NULL UNIQUE,
            ip            TEXT,
            manufacturer  TEXT,
            model_name    TEXT,
            discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_used_at  TEXT
        )
    ''')
    # Lazy migration: el volumen se controla con OTRO servicio UPnP
    # (RenderingControl, no AVTransport) — se agrega la columna para las
    # bases que ya tenían cast_targets de antes de esto.
    try:
        conn.execute("ALTER TABLE cast_targets ADD COLUMN rendering_control_url TEXT")
        conn.commit()
    except Exception:
        pass  # La columna ya existe
    # Lazy migration (Ticket 10 §2.2/§3.3): se reemplaza la vieja `favorites`
    # (global, sin user_id, solo pistas) por `user_item_favorites` (per-user,
    # tres tipos). El PO confirmó que no hace falta migrar los datos viejos —
    # se dropea directo. DROP TABLE IF EXISTS es tan barato como el CREATE
    # TABLE IF NOT EXISTS de acá arriba una vez que la tabla ya no existe, así
    # que es seguro dejarlo acá corriendo en cada conexión, mismo criterio.
    conn.execute("DROP TABLE IF EXISTS favorites")
    # Nombre `user_item_favorites` a propósito, para no confundirlo con
    # `user_favorite_artists` (el showcase de perfil, tope 5 — Ticket 05
    # Lote D) al leer el código de corrido. Ilimitado, tres tipos.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_item_favorites (
            user_id     INTEGER NOT NULL,
            item_type   TEXT NOT NULL CHECK (item_type IN ('artist', 'album', 'track')),
            item_id     INTEGER NOT NULL,
            added_at    TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, item_type, item_id)
        )
    ''')
    # Lazy migration (Ticket 10 §3.1): Playlists. user_id desde el día uno
    # (a diferencia de la vieja `favorites`). `position` explícito en
    # playlist_tracks deja la puerta abierta a reordenar (§3.2, reorder
    # endpoint) sin reescribir toda la tabla.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS playlists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')
    # Lazy migration: Ticket 20, Ítem 3 — orden de Playlists por Última
    # Reproducción / Más Reproducidas. Ninguna de las dos se podía derivar
    # de columnas existentes: updated_at cambia cuando se agrega/saca una
    # pista (ver api_v1_playlist_add_track/remove_track), NO cuando se
    # reproduce — hacía falta un punto de datos propio, actualizado desde
    # el nuevo endpoint mark-played (ver más abajo).
    try:
        conn.execute("ALTER TABLE playlists ADD COLUMN last_played_at TEXT")
        conn.commit()
    except Exception:
        pass  # Columna ya existe
    try:
        conn.execute("ALTER TABLE playlists ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except Exception:
        pass  # Columna ya existe
    conn.execute('''
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id  INTEGER NOT NULL,
            track_id     INTEGER NOT NULL,
            position     INTEGER NOT NULL,
            added_at     TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (playlist_id, track_id)
        )
    ''')
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_playlist_tracks_playlist "
        "ON playlist_tracks (playlist_id, position)"
    )
    # Lazy migration: Ticket AI-01 (Etapas 3+4 del epic "Agente de IA") — log
    # de cada petición al agente (texto crudo, intención parseada, filtros
    # aplicados, si se usó fallback). Insumo directo para iterar prompts más
    # adelante (Etapa 8) — ver AI_AGENT_MASTER_PLAN.md §10. Tabla nueva,
    # aislada, no reemplaza ni modifica ninguna tabla existente.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS ai_playlist_requests (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id               INTEGER NOT NULL,
            raw_query             TEXT NOT NULL,
            status                TEXT NOT NULL,
            parsed_intent_json    TEXT,
            filters_applied_json  TEXT,
            used_fallback         INTEGER NOT NULL DEFAULT 0,
            confidence            REAL,
            provider              TEXT,
            track_count           INTEGER,
            created_at            TEXT NOT NULL DEFAULT (datetime('now'))
        )
    ''')
    # Lazy migration: Ticket AI-02 (Etapa 1 del epic "Agente de IA", mitad
    # backend) — motor de comportamiento (colección). Tres tablas nuevas y
    # aisladas, todas con UNIQUE(user_id, client_*_id) para que reintentos
    # de red del cliente nunca dupliquen ni corrompan el conteo (ver
    # AI_AGENT_MASTER_PLAN.md §8 y behavior_engine.py).
    conn.execute('''
        CREATE TABLE IF NOT EXISTS listening_events (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id                  INTEGER NOT NULL,
            client_event_id          TEXT NOT NULL,
            track_id                 INTEGER NOT NULL,
            duration_played_seconds  INTEGER NOT NULL DEFAULT 0,
            completed                INTEGER NOT NULL DEFAULT 0,
            source                   TEXT NOT NULL DEFAULT 'manual',
            created_at               TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, client_event_id)
        )
    ''')
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listening_events_user "
        "ON listening_events (user_id, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listening_events_track "
        "ON listening_events (track_id)"
    )
    conn.execute('''
        CREATE TABLE IF NOT EXISTS search_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            client_event_id  TEXT NOT NULL,
            query_text       TEXT NOT NULL,
            result_count     INTEGER,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, client_event_id)
        )
    ''')
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_history_user "
        "ON search_history (user_id, created_at)"
    )
    conn.execute('''
        CREATE TABLE IF NOT EXISTS session_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL,
            client_session_id   TEXT NOT NULL,
            device              TEXT,
            started_at          TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at            TEXT,
            UNIQUE(user_id, client_session_id)
        )
    ''')
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_log_user "
        "ON session_log (user_id, started_at)"
    )
    # Lazy migration: Ticket AI-05 (Etapa 7) — columnas de feedback sobre
    # una playlist ya generada por el agente de IA. Mismo patrón
    # try/except ya usado en este archivo (ver "website_url" más arriba)
    # para agregar columnas a una tabla que ya existe (ai_playlist_requests,
    # Ticket AI-01) sin romper nada de lo que ya guarda.
    for ddl in (
        "ALTER TABLE ai_playlist_requests ADD COLUMN rating INTEGER",
        "ALTER TABLE ai_playlist_requests ADD COLUMN feedback_comment TEXT",
        "ALTER TABLE ai_playlist_requests ADD COLUMN saved_playlist_id INTEGER",
        "ALTER TABLE ai_playlist_requests ADD COLUMN feedback_at TEXT",
        # Ticket AI-25 (paginación "Expandir", primera versión) — quedó
        # sin usarse, reemplazada por las dos columnas de AI-27 de abajo
        # (ver ai_playlist_pagination.py). No se borra — SQLite no borra
        # columnas fácilmente y no hace daño dejarla.
        "ALTER TABLE ai_playlist_requests ADD COLUMN delivered_track_ids_json TEXT",
        # Ticket AI-27 (pedido por Niko: paginación sin re-consultar la
        # base con exclusiones) — el pool completo que devolvió
        # generate_playlist para esta petición (no solo lo mostrado) y
        # cuántos de esos ya se entregaron. Ver ai_playlist_pagination.py,
        # módulo nuevo y exclusivo para esto.
        "ALTER TABLE ai_playlist_requests ADD COLUMN candidate_pool_json TEXT",
        "ALTER TABLE ai_playlist_requests ADD COLUMN pool_served_count INTEGER DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
            conn.commit()
        except Exception:
            pass  # columna ya existe
    conn.commit()
    return conn

# ── Auth: helpers, decorators, device parsing ─────────────────────────────────

# Cuánto tiempo sin heartbeat antes de considerar a un usuario "desconectado"
# en el panel admin. El JS del cliente manda un heartbeat cada ~25s (ver
# base.html), así que 2 minutos da margen de sobra para pestañas en segundo
# plano / iOS Safari throttling sin ser tan largo como para mostrar gente
# "en línea" mucho después de haber cerrado la app.
ONLINE_WINDOW_MINUTES = 2

def _utcnow_iso():
    """Igual que datetime.utcnow().isoformat() (mismo formato, sin offset) pero
    sin el DeprecationWarning de Python 3.12+. Se mantiene el formato exacto
    para no romper las comparaciones de string ya guardadas en last_seen/etc."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

def get_current_user():
    """Devuelve el dict del usuario logueado (o None) a partir de la sesión."""
    uid = session.get('user_id')
    if not uid:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def login_required(view):
    """Exige sesión iniciada. Para uso en rutas específicas — el grueso de la
    app ya queda protegido por el before_request _require_login más abajo."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        return view(*args, **kwargs)
    return wrapped

def admin_required(view):
    """Exige sesión iniciada Y permisos de administrador."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('user_id'):
            return redirect(url_for('login', next=request.path))
        if not session.get('is_admin'):
            return "No autorizado", 403
        return view(*args, **kwargs)
    return wrapped

def _parse_device(user_agent):
    """Traduce un User-Agent a una etiqueta legible: 'iPhone · Safari', etc."""
    if not user_agent:
        return 'Desconocido'
    ua = user_agent.lower()
    if   'ipad' in ua:                 platform = 'iPad'
    elif 'iphone' in ua:               platform = 'iPhone'
    elif 'android' in ua:              platform = 'Android'
    elif 'macintosh' in ua or 'mac os' in ua: platform = 'Mac'
    elif 'windows' in ua:              platform = 'Windows'
    elif 'linux' in ua:                platform = 'Linux'
    else:                              platform = 'Desconocido'

    if   'crios' in ua:                browser = 'Chrome'
    elif 'fxios' in ua:                browser = 'Firefox'
    elif 'edg/' in ua:                 browser = 'Edge'
    elif 'chrome/' in ua:              browser = 'Chrome'
    elif 'firefox/' in ua:             browser = 'Firefox'
    elif 'safari' in ua and 'chrome' not in ua: browser = 'Safari'
    else:                              browser = ''

    return f'{platform} · {browser}' if browser else platform

def _touch_user_activity(user_id):
    """Actualiza last_seen/last_device/last_ip del usuario — llamado en login
    y en cada heartbeat del cliente."""
    conn = get_db_connection()
    try:
        device = _parse_device(request.headers.get('User-Agent', ''))
        ip = (request.headers.get('X-Forwarded-For', request.remote_addr) or '').split(',')[0].strip()
        conn.execute(
            'UPDATE users SET last_seen=?, last_device=?, last_ip=? WHERE id=?',
            (_utcnow_iso(), device, ip, user_id)
        )
        conn.commit()
    finally:
        conn.close()

# Playlist colaborativa: endpoints vedados a una sesión de invitado. Invitado
# = "navega y añade a la cola colaborativa" (ver ticket) — nada de admin,
# favoritos (son de una cuenta real), ni acceso directo a audio/stream (no
# tiene reproductor). Todo lo demás (home, artist, album, track, search,
# browse, búsqueda avanzada y sus /api/ de lectura) queda accesible tal cual
# ya está, sin tocar esos templates.
# Ticket C-03: paginas que SI ve un invitado (el resto redirige a /colab/app).
_COLLAB_GUEST_PAGES = {'collab_guest_app', 'collab_join', 'collab_leave', 'collab_codigo', 'cover_file', 'cover',
                       'cast_cover', 'static', 'service_worker'}

_COLLAB_GUEST_BLOCKED_ENDPOINTS = {
    'admin_dashboard', 'admin_users', 'admin_users_estado', 'admin_approve_user',
    'admin_reject_user', 'admin_revoke_user',
    'admin_collab', 'admin_collab_crear', 'admin_collab_finalizar', 'admin_collab_qr',
    'admin_collab_permiso',
    'api_admin_collab_estado', 'api_admin_collab_pull',
    'admin_estadisticas', 'admin_estadisticas_datos', 'admin_estadisticas_usuario', 'admin_estadisticas_resumen_ia',
    'api_web_behavior_listen', 'api_web_behavior_session_start', 'api_web_behavior_session_end',
    'api_favorites_rebuild_cache', 'favorites_page',
    'audio_file', 'stream_dsd', 'api_heartbeat',
    # "Reproducir en…" — un invitado NUNCA debe poder redirigir el audio del
    # anfitrión a otro dispositivo de la casa.
    'api_admin_cast_discover', 'api_admin_cast_targets', 'api_admin_cast_target_delete', 'api_admin_cast_play',
    'api_admin_cast_transport', 'api_admin_cast_seek', 'api_admin_cast_volume',
}

@app.before_request
def _require_login():
    """Protege toda la plataforma: sin sesión válida, redirige a /login (o
    devuelve 401 JSON para rutas /api/ para no romper el JS del cliente)."""
    ep = request.endpoint
    # 'cover': mismo criterio que 'cast_cover' (ya exento arriba) — una
    # carátula de álbum no es información sensible, y el cliente nativo no
    # maneja cookies de sesión persistentes entre lanzamientos de la app.
    if ep in ('login', 'signup', 'static', 'service_worker', 'collab_join', 'collab_codigo', 'cast_audio', 'cast_cover', 'cover_file',
              'tv_pantalla', 'tv_estado', 'tv_qr',
              'api_v1_tvapp_hola', 'api_v1_tvapp_orden', 'api_v1_tvapp_respuesta',
              'descargas_orbyte_ipa') or request.path.startswith('/static/'):
        # descargas_orbyte_ipa: la baja SideStore, sin sesión; se valida con
        # el enlace_token de apk/ios.json (cambia en cada publicación).
        # tv_*: Ticket C-07 — página de solo lectura para la tele; se valida
        # con su propia clave y solo desde la red de la casa (_tv_fila_por_clave).
        return
    # /api/v1/*: API del cliente nativo (Orbyte-iOS). Usa token Bearer propio
    # en vez de la cookie de sesión — se protege endpoint por endpoint con
    # @api_login_required, nunca por este gate de cookie/sesión.
    if request.path.startswith('/api/v1/'):
        return
    # Sesión de invitado de playlist colaborativa: autenticada pero acotada
    # a un subconjunto de la app — nunca pasa por el chequeo de user_id/
    # is_approved de abajo, que es exclusivo de cuentas reales.
    if session.get('is_collab_guest'):
        if ep in _COLLAB_GUEST_BLOCKED_ENDPOINTS:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'not_authorized_guest'}), 403
            return "No autorizado para invitados de la playlist colaborativa", 403
        # Ticket C-03: el invitado ya no navega la web antigua (se rompía en
        # el celular): cualquier PAGINA lo lleva a la web de invitados.
        if (not request.path.startswith('/api/') and ep not in _COLLAB_GUEST_PAGES
                and request.method == 'GET'):
            return redirect(url_for('collab_guest_app'))
        return
    if not session.get('user_id'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'not_authenticated'}), 401
        return redirect(url_for('login', next=request.path))
    if not session.get('is_approved'):
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'account_not_approved'}), 403
        return redirect(url_for('login', pending='1'))

# ── Auth: rutas de signup / login / logout / heartbeat / admin ───────────────

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if session.get('user_id'):
        return redirect(url_for('home'))
    error = None
    if request.method == 'POST':
        email     = (request.form.get('email') or '').strip().lower()
        password  = request.form.get('password') or ''
        password2 = request.form.get('password2') or ''
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            error = 'Correo inválido.'
        elif len(password) < 8:
            error = 'La contraseña debe tener al menos 8 caracteres.'
        elif password != password2:
            error = 'Las contraseñas no coinciden.'
        else:
            conn = get_db_connection()
            try:
                if conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
                    error = 'Ya existe una cuenta con ese correo.'
                else:
                    is_admin_email = (email == ADMIN_EMAIL)
                    now = _utcnow_iso()
                    conn.execute(
                        'INSERT INTO users (email, password_hash, is_admin, is_approved, created_at, approved_at) '
                        'VALUES (?, ?, ?, ?, ?, ?)',
                        (email, generate_password_hash(password),
                         1 if is_admin_email else 0, 1 if is_admin_email else 0,
                         now, now if is_admin_email else None)
                    )
                    conn.commit()
                    if not is_admin_email:
                        _send_signup_pending_email(email)
                    return redirect(url_for('login', created=('admin' if is_admin_email else 'pending')))
            finally:
                conn.close()
    return render_template('signup.html', error=error)

def _change_user_password(user_id, current_password, new_password):
    """Valida y aplica un cambio de contraseña. Devuelve None si OK, o un
    código de error — compartido entre la ruta web (/account) y la API
    nativa (/api/v1/auth/change-password) para no duplicar la validación."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT password_hash FROM users WHERE id=?', (user_id,)).fetchone()
        if not row or not check_password_hash(row['password_hash'], current_password):
            return 'wrong_current_password'
        if len(new_password) < 8:
            return 'weak_password'
        conn.execute('UPDATE users SET password_hash=? WHERE id=?',
                     (generate_password_hash(new_password), user_id))
        conn.commit()
        return None
    finally:
        conn.close()

@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    error = None
    success = None
    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password     = request.form.get('new_password', '')
        new_password2    = request.form.get('new_password2', '')
        if new_password != new_password2:
            error = 'Las contraseñas nuevas no coinciden.'
        else:
            err = _change_user_password(session['user_id'], current_password, new_password)
            if err == 'wrong_current_password':
                error = 'La contraseña actual es incorrecta.'
            elif err == 'weak_password':
                error = 'La nueva contraseña debe tener al menos 8 caracteres.'
            else:
                success = 'Contraseña actualizada correctamente.'
    # current_user (inyectado por el context processor) solo trae email/
    # is_admin — para "Miembro desde"/"Última conexión" hace falta el
    # registro completo, que get_current_user() ya sabe traer.
    account_details = get_current_user()
    return render_template('account.html', error=error, success=success, account_details=account_details)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('user_id'):
        return redirect(url_for('home'))
    error = None
    if request.method == 'POST':
        email    = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        next_url = request.form.get('next') or ''
        conn = get_db_connection()
        try:
            row = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            user = dict(row) if row else None
        finally:
            conn.close()
        if not user or not check_password_hash(user['password_hash'], password):
            error = 'Correo o contraseña incorrectos.'
        elif not user['is_approved']:
            error = 'Tu cuenta aún no ha sido aprobada por el administrador.'
        else:
            session.clear()
            # 'Recordarme': tildado por defecto en el HTML — si se desmarca,
            # la sesión no es permanente y expira al cerrar el navegador en
            # vez de durar PERMANENT_SESSION_LIFETIME (30 días).
            session.permanent       = (request.form.get('remember') == 'on')
            session['user_id']     = user['id']
            session['user_email']  = user['email']
            session['is_admin']    = bool(user['is_admin'])
            session['is_approved'] = True
            _touch_user_activity(user['id'])
            if not next_url.startswith('/'):
                next_url = url_for('home')
            return redirect(next_url)
    return render_template('login.html', error=error,
                           created=request.args.get('created'),
                           pending_msg=request.args.get('pending'),
                           next=request.args.get('next', ''))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── API v1: autenticación del cliente nativo (Orbyte-iOS) ────────────────────
# Cookie de sesión no sirve para un cliente nativo (no hay cookie jar
# persistente entre lanzamientos de la app), así que emitimos un token
# firmado con itsdangerous — mismo mecanismo que ya usa /cast-audio, sin
# agregar ninguna dependencia nueva. El cliente lo guarda en el Keychain y lo
# manda como 'Authorization: Bearer <token>' en cada request a /api/v1/*.
_api_token_signer = URLSafeTimedSerializer(app.secret_key, salt='api-auth-v1')
_API_TOKEN_MAX_AGE = 60 * 60 * 24 * 30  # 30 días

# Ticket B-01 (pedido por Niko): presencia de las APPS (iOS/Android/Desktop).
# Antes, para un cliente con token Bearer la actividad solo se registraba en
# login/logout; las apps guardan la sesión y no vuelven a hacer login, así que
# "Última conexión" / "Último dispositivo" quedaban viejos (Desktop mostraba
# el Android de días atrás). El heartbeat de la web (/api/heartbeat) es solo
# por cookie. Regla de Niko: registrar enseguida si CAMBIA el dispositivo (o
# la IP); con el mismo dispositivo, solo refrescar la hora, sin saturar.
# Siempre UPDATE de la fila del usuario (nunca filas nuevas). Sin lecturas
# extra: api_login_required ya trae la fila completa del usuario.
# 60 s < ONLINE_WINDOW_MINUTES (2 min): el "en línea" del panel sigue exacto.
_API_PRESENCE_REFRESH_SECONDS = 60


def _api_presence_touch(user):
    try:
        device = _parse_device(request.headers.get('User-Agent', ''))
        ip = (request.headers.get('X-Forwarded-For', request.remote_addr) or '').split(',')[0].strip()
        changed = device != (user.get('last_device') or '') or ip != (user.get('last_ip') or '')
        stale = True
        last_seen = user.get('last_seen')
        if last_seen:
            try:
                seen = datetime.fromisoformat(str(last_seen).replace('Z', '+00:00'))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                stale = (datetime.now(timezone.utc) - seen).total_seconds() >= _API_PRESENCE_REFRESH_SECONDS
            except ValueError:
                stale = True
        if changed or stale:
            _touch_user_activity(user['id'])
    except Exception:
        # La presencia nunca puede romper un request de la API.
        app.logger.exception('B-01: no se pudo registrar la presencia')


# ── Ticket 42b (pedido por Niko): clave propia para la skill de Alexa ─────────
# La skill usaba una clave de sesión de 30 días y dejó de responder al vencer.
# La clave de Alexa (`alx_…`, ver integraciones.py) no vence, se revoca con
# ./clave_alexa.sh y solo abre lo que usa Alexa. Si Alexa llega con una clave
# rechazada se avisa a los admins en la campana (como mucho cada 6 h).
ALEXA_ENDPOINTS = {'api_v1_ai_playlist', 'api_v1_ai_playlist_expand', 'api_v1_ai_playlist_feedback', 'api_v1_me',
                   'api_v1_alexa_stream', 'api_v1_alexa_track'}
_alexa_aviso_at = [0.0]
_alexa_uso_at = {}


def _alexa_avisar_rechazo(motivo):
    if time.time() - _alexa_aviso_at[0] < 6 * 3600:
        return
    _alexa_aviso_at[0] = time.time()
    app.logger.warning(f'[alexa] clave rechazada: {motivo}')
    try:
        conn = get_db_connection()
        try:
            for a in conn.execute('SELECT id FROM users WHERE is_admin=1').fetchall():
                _notificar(conn, a['id'], 'sistema',
                           f'Alexa no pudo entrar a Orbyte ({motivo}). En el servidor: ./clave_alexa.sh estado',
                           datos={'origen': 'alexa'})
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - el aviso nunca rompe el pedido
        app.logger.exception('[alexa] no se pudo avisar el rechazo')


def _alexa_usuario_por_clave(clave):
    """(usuario, None) si la clave de Alexa es válida para este endpoint; si
    no, (None, respuesta de error)."""
    conn = get_db_connection()
    try:
        fila = _integ.buscar(conn, clave)
        user = None
        if fila:
            row = conn.execute('SELECT * FROM users WHERE id=?', (fila['user_id'],)).fetchone()
            user = dict(row) if row and row['is_approved'] else None
            if user and time.time() - _alexa_uso_at.get(fila['id'], 0) > 60:
                _alexa_uso_at[fila['id']] = time.time()
                conn.execute('UPDATE integracion_claves SET ultimo_uso_at=? WHERE id=?',
                             (_utcnow_iso()[:19], fila['id']))
                conn.commit()
    finally:
        conn.close()
    if not user:
        _alexa_avisar_rechazo('la clave fue revocada o no existe')
        return None, (jsonify({'error': 'invalid_token'}), 401)
    if request.endpoint not in ALEXA_ENDPOINTS:
        _alexa_avisar_rechazo(f'la skill pidió algo no permitido: {request.path}')
        return None, (jsonify({'error': 'not_allowed_for_integration'}), 403)
    user['via_integracion'] = 'alexa'
    return user, None


def _alexa_user_id_de_token(token):
    """Para /api/v1/alexa/stream y /track (token por header o ?token=):
    acepta la clave de Alexa o una clave de sesión normal. user_id o None."""
    if _integ.es_clave_integracion(token):
        user, error = _alexa_usuario_por_clave(token)
        return user['id'] if user else None
    try:
        return _api_token_signer.loads(token, max_age=_API_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        _alexa_avisar_rechazo('su clave venció; genera una que no vence con ./clave_alexa.sh generar')
        return None


def api_login_required(view):
    """Para /api/v1/*: acepta token Bearer (cliente nativo) O cookie de
    sesión activa (fetch() same-origin desde la propia web) — así la web
    puede reusar estos mismos endpoints para el perfil extendido sin que
    tengamos que duplicar cada ruta en una versión 'web' y otra 'nativa'.
    Siempre responde JSON, nunca redirige a una página HTML de login."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        user = None
        if auth.startswith('Bearer ') and _integ.es_clave_integracion(auth[len('Bearer '):]):
            # Ticket 42b: clave propia de la skill de Alexa (no vence, alcance acotado).
            user, error = _alexa_usuario_por_clave(auth[len('Bearer '):])
            if error:
                return error
        elif auth.startswith('Bearer '):
            token = auth[len('Bearer '):]
            try:
                user_id = _api_token_signer.loads(token, max_age=_API_TOKEN_MAX_AGE)
            except (BadSignature, SignatureExpired):
                return jsonify({'error': 'invalid_token'}), 401
            conn = get_db_connection()
            try:
                row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                user = dict(row) if row else None
            finally:
                conn.close()
            if user and user['is_approved']:
                _api_presence_touch(user)  # Ticket B-01: solo apps (Bearer); la web ya tiene su heartbeat
        elif session.get('user_id'):
            conn = get_db_connection()
            try:
                row = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
                user = dict(row) if row else None
            finally:
                conn.close()
        elif session.get('is_collab_guest') and request.endpoint in _COLLAB_GUEST_API_READ:
            # Ticket C-03: el invitado de la playlist colaborativa (cookie, sin
            # cuenta) puede LEER la biblioteca con estos mismos endpoints, para
            # la web de invitados (/colab/app). Actúa como el anfitrión (dueño
            # de la sesión) solo para lo que esas lecturas necesitan (tamaño de
            # página de sus ajustes); nunca como admin.
            conn = get_db_connection()
            try:
                sess = _collab_active_session(conn)
                row = None
                if sess and sess['id'] == session.get('collab_session_id'):
                    row = conn.execute('SELECT * FROM users WHERE id=?', (sess['created_by'],)).fetchone()
            finally:
                conn.close()
            if not row:
                return jsonify({'error': 'collab_session_ended'}), 401
            user = dict(row, is_admin=0, is_collab_guest=True)
        if not user or not user['is_approved']:
            return jsonify({'error': 'not_authenticated'}), 401
        g.api_user = user
        return view(*args, **kwargs)
    return wrapped


def _api_dispositivo():
    """Identidad del DISPOSITIVO que hace el pedido (no de la cuenta): un
    hash del token de la app (cada inicio de sesión en cada equipo tiene el
    suyo y se guarda entre aperturas) o, en la web, un id por cookie. Lo usan
    el Modo TV (la tele muestra solo el dispositivo que la mandó) y
    «Reproducir en…» (al cerrar/abrir la app se sueltan sus equipos)."""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return 'app:' + hashlib.sha256(auth[len('Bearer '):].encode()).hexdigest()[:16]
    if not session.get('disp_id'):
        session['disp_id'] = secrets.token_hex(8)
    return 'web:' + session['disp_id']


# Ticket C-03: endpoints /api/v1 de SOLO LECTURA que puede usar un invitado
# de la playlist colaborativa (ver api_login_required). Cualquier otro
# /api/v1 sigue exigiendo una cuenta real.
_COLLAB_GUEST_API_READ = {
    'api_v1_home_facets', 'api_v1_albums', 'api_v1_meta_tracks', 'api_v1_album_tracks',
    'api_v1_album_detail', 'api_v1_artist_detail', 'api_v1_artist_tracks',
    'api_v1_search', 'api_v1_search_advanced', 'api_v1_search_advanced_options',
    'api_v1_avatars',
}

def api_admin_required(view):
    """Como api_login_required (Bearer o cookie, siempre JSON, nunca
    redirige) más el chequeo de is_admin. Compone api_login_required en vez
    de duplicar la extracción de token/cookie — Ticket 18 §2. Para uso en
    /api/v1/admin/collab/*: el gate global _require_login solo exime rutas
    bajo /api/v1/, así que un endpoint admin fuera de ese prefijo nunca
    llegaría a este decorator con un token Bearer sin cookie de sesión."""
    @wraps(view)
    def _check_admin(*args, **kwargs):
        if not g.api_user.get('is_admin'):
            return jsonify({'error': 'not_authorized_admin'}), 403
        return view(*args, **kwargs)
    return api_login_required(_check_admin)

@app.route('/api/v1/auth/login', methods=['POST'])
def api_v1_login():
    """Login para el cliente nativo. Recibe JSON {email, password}, devuelve
    un token Bearer de larga duración (30 días) si las credenciales son
    válidas y la cuenta ya fue aprobada por el admin."""
    data = request.get_json(silent=True) or {}
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        user = dict(row) if row else None
    finally:
        conn.close()
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'invalid_credentials'}), 401
    if not user['is_approved']:
        return jsonify({'error': 'account_not_approved'}), 403
    _touch_user_activity(user['id'])
    token = _api_token_signer.dumps(user['id'])
    return jsonify({
        'token': token,
        'user': {
            'id': user['id'],
            'email': user['email'],
            'is_admin': bool(user['is_admin']),
            # Depuración C-05: el nickname desde el login, para que las apps
            # no muestren el correo hasta la próxima consulta de /auth/me.
            'nickname': user.get('nickname'),
        }
    })

@app.route('/api/v1/auth/signup', methods=['POST'])
def api_v1_signup():
    """Signup del cliente nativo — mismas reglas de validación y mismo mail
    HTML de 'pendiente de aprobación' que /signup (web). A propósito NO
    devuelve token: igual que la web, el usuario recién creado tiene que
    pasar por /api/v1/auth/login después (y si no es el admin, esperar
    aprobación) — no hay auto-login para mantener el mismo comportamiento."""
    data = request.get_json(silent=True) or {}
    email    = (data.get('email') or '').strip().lower()
    password = data.get('password') or ''

    if not email or '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'error': 'invalid_email'}), 400
    if len(password) < 8:
        return jsonify({'error': 'weak_password'}), 400

    conn = get_db_connection()
    try:
        if conn.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone():
            return jsonify({'error': 'email_taken'}), 409

        is_admin_email = (email == ADMIN_EMAIL)
        now = _utcnow_iso()
        conn.execute(
            'INSERT INTO users (email, password_hash, is_admin, is_approved, created_at, approved_at) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (email, generate_password_hash(password),
             1 if is_admin_email else 0, 1 if is_admin_email else 0,
             now, now if is_admin_email else None)
        )
        conn.commit()
    finally:
        conn.close()

    if not is_admin_email:
        _send_signup_pending_email(email)

    return jsonify({'status': 'admin_created' if is_admin_email else 'pending'})

@app.route('/api/v1/auth/change-password', methods=['POST'])
@api_login_required
def api_v1_change_password():
    """Espejo de /account (web), en JSON, para el cliente nativo. Reusa
    _change_user_password — misma validación en los dos lados."""
    data = request.get_json(silent=True) or {}
    current_password = data.get('current_password') or ''
    new_password     = data.get('new_password') or ''

    err = _change_user_password(g.api_user['id'], current_password, new_password)
    if err == 'wrong_current_password':
        return jsonify({'error': 'wrong_current_password'}), 401
    if err == 'weak_password':
        return jsonify({'error': 'weak_password'}), 400
    return jsonify({'status': 'ok'})

@app.route('/api/v1/avatars')
@api_login_required
def api_v1_avatars():
    """Mismo catálogo que ya arma _collab_avatar_catalog() para la playlist
    colaborativa — reusado tal cual, sin duplicar el escaneo de carpetas.
    Devuelve {'femeninos': [...], 'masculinos': [...]} para poder armar las
    2 pestañas del selector igual que en collab_join.html."""
    return jsonify(_collab_avatar_catalog())

@app.route('/api/v1/genres')
@api_login_required
def api_v1_genres():
    """Géneros reales del catálogo (no texto libre) — para que 'géneros
    favoritos' sirva de verdad para sugerencias/notificaciones futuras."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT genre_primary FROM album_meta "
            "WHERE genre_primary IS NOT NULL AND genre_primary != '' "
            "ORDER BY genre_primary"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({'genres': [r['genre_primary'] for r in rows]})

@app.route('/api/v1/artists/search')
@api_login_required
def api_v1_artists_search():
    """Buscador de bandas para elegir favoritas — reusa la tabla artists
    existente, no hay catálogo separado que mantener."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'artists': []})
    conn = get_db_connection()
    try:
        rows = conn.execute(
            'SELECT id, name FROM artists WHERE name LIKE ? ORDER BY name LIMIT 20',
            (f'%{q}%',)
        ).fetchall()
    finally:
        conn.close()
    return jsonify({'artists': [{'id': r['id'], 'name': r['name']} for r in rows]})

def _profile_payload(user_id):
    """Arma el JSON de perfil extendido — usado tanto por GET como después
    de cada PUT/POST/DELETE, para no duplicar el shape de la respuesta."""
    conn = get_db_connection()
    try:
        user = conn.execute(
            'SELECT avatar, bio, favorite_genres_json, nickname FROM users WHERE id=?', (user_id,)
        ).fetchone()
        favorites = conn.execute(
            '''SELECT a.id, a.name FROM user_favorite_artists ufa
               JOIN artists a ON a.id = ufa.artist_id
               WHERE ufa.user_id=? ORDER BY ufa.added_at''',
            (user_id,)
        ).fetchall()
    finally:
        conn.close()
    genres = json.loads(user['favorite_genres_json']) if user and user['favorite_genres_json'] else []
    return {
        'avatar': user['avatar'] if user else None,
        'bio': user['bio'] if user else None,
        'nickname': user['nickname'] if user else None,
        'favorite_genres': genres,
        'favorite_artists': [{'id': r['id'], 'name': r['name']} for r in favorites],
    }

@app.route('/api/v1/profile')
@api_login_required
def api_v1_profile_get():
    return jsonify(_profile_payload(g.api_user['id']))

@app.route('/api/v1/profile', methods=['PUT'])
@api_login_required
def api_v1_profile_update():
    data = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        if 'avatar' in data:
            conn.execute('UPDATE users SET avatar=? WHERE id=?', (data['avatar'], g.api_user['id']))
        if 'bio' in data:
            conn.execute('UPDATE users SET bio=? WHERE id=?', ((data['bio'] or '')[:500], g.api_user['id']))
        if 'nickname' in data:
            # Ticket 19, Feature 5: tope de 40 caracteres (es lo que se
            # muestra en el Header, no una bio). String vacío/solo
            # espacios se persiste como NULL — el cliente ya sabe caer al
            # email cuando nickname es None, no hace falta duplicar esa
            # lógica de fallback acá.
            nickname = (data['nickname'] or '').strip()[:40]
            conn.execute('UPDATE users SET nickname=? WHERE id=?', (nickname or None, g.api_user['id']))
        if 'favorite_genres' in data:
            genres = data['favorite_genres'] if isinstance(data['favorite_genres'], list) else []
            conn.execute('UPDATE users SET favorite_genres_json=? WHERE id=?',
                         (json.dumps(genres), g.api_user['id']))
        conn.commit()
    finally:
        conn.close()
    return jsonify(_profile_payload(g.api_user['id']))

@app.route('/api/v1/profile/favorite-artists', methods=['POST'])
@api_login_required
def api_v1_profile_add_favorite_artist():
    data = request.get_json(silent=True) or {}
    artist_id = data.get('artist_id')
    if not artist_id:
        return jsonify({'error': 'missing_artist_id'}), 400
    conn = get_db_connection()
    try:
        count = conn.execute(
            'SELECT COUNT(*) as c FROM user_favorite_artists WHERE user_id=?', (g.api_user['id'],)
        ).fetchone()['c']
        if count >= 5:
            return jsonify({'error': 'max_favorites_reached'}), 400
        if not conn.execute('SELECT id FROM artists WHERE id=?', (artist_id,)).fetchone():
            return jsonify({'error': 'artist_not_found'}), 404
        conn.execute(
            'INSERT OR IGNORE INTO user_favorite_artists (user_id, artist_id) VALUES (?, ?)',
            (g.api_user['id'], artist_id)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(_profile_payload(g.api_user['id']))

@app.route('/api/v1/profile/favorite-artists/<int:artist_id>', methods=['DELETE'])
@api_login_required
def api_v1_profile_remove_favorite_artist(artist_id):
    conn = get_db_connection()
    try:
        conn.execute(
            'DELETE FROM user_favorite_artists WHERE user_id=? AND artist_id=?',
            (g.api_user['id'], artist_id)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(_profile_payload(g.api_user['id']))

@app.route('/api/v1/auth/me')
@api_login_required
def api_v1_me():
    """Perfil del usuario autenticado — mismos campos que ya existen en la
    tabla users, para la pantalla de Cuenta del cliente nativo."""
    user = g.api_user
    return jsonify({
        'id':           user['id'],
        'email':        user['email'],
        'is_admin':     bool(user['is_admin']),
        'is_approved':  bool(user['is_approved']),
        'created_at':   user['created_at'],
        'last_seen':    user['last_seen'],
        'last_device':  user['last_device'],
        'last_ip':      user['last_ip'],
        # Ticket 19, Feature 5 — nickname a mostrar en el Header en vez
        # del email cuando está seteado. g.api_user viene de SELECT *,
        # así que la columna ya está ahí una vez migrada.
        'nickname':     user['nickname'],
    })

# Ticket 26 — apartado de Configuración. DEFAULT_USER_SETTINGS es la
# fuente de verdad del shape completo: agregar una preferencia nueva acá
# es la única migración que hace falta (settings_json ya es una sola
# columna JSON, ver Lote 1). Los usuarios existentes heredan el default
# de una key nueva hasta que la pisen explícitamente vía PUT — no hace
# falta backfill.
DEFAULT_USER_SETTINGS = {
    # Categoría B (Ticket 26) — cantidades de Orbitron y búsqueda general.
    # Mismos valores que _PLAYLIST_SIZE/_MAX_CANTIDAD (ai_playlist.py) y
    # PAGE_SIZE (este archivo) al momento de esta auditoría — la lectura
    # efectiva de esos valores por-usuario se conecta en un lote aparte,
    # esto solo persiste la preferencia.
    'orbitron_default_results': 25,
    'orbitron_max_top_n': 100,
    'search_page_size': 20,
    # Categoría C (Ticket 26) — gesto de triple-tap para reportar (Ticket
    # 19/25), confirmado con Niko como toggle on/off simple.
    'triple_tap_report_enabled': True,
    # Categoría A (Ticket 26) — timer de sueño. Confirmado con Niko: vive
    # solo en Settings (no como acción rápida desde reproducción). Este
    # valor es el último usado / default del picker, no el estado de un
    # timer corriendo (eso es efímero, vive en el cliente).
    'sleep_timer_default_minutes': 30,
    # Categoría D (Ticket 26) — techo de calidad DSD según el DAC
    # conectado. 'auto' preserva el comportamiento actual (DSD64 nativo,
    # DSD128+ siempre transcodifica, Ticket 16) hasta que el motor de
    # audio del lado iOS use este valor para decidir distinto.
    'dac_max_dsd_tier': 'auto',
}

_DAC_DSD_TIERS = {'auto', 'dsd64', 'dsd128', 'dsd256'}


def _settings_payload(user_id):
    """Arma el JSON de settings — merge de lo persistido sobre los
    defaults (nunca al revés), para que una key nueva en
    DEFAULT_USER_SETTINGS aparezca para todos los usuarios sin tocar
    filas existentes. Ignora keys persistidas que ya no existen en
    DEFAULT_USER_SETTINGS (setting removida en una versión futura)."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT settings_json FROM users WHERE id=?', (user_id,)
        ).fetchone()
    finally:
        conn.close()
    stored = json.loads(row['settings_json']) if row and row['settings_json'] else {}
    merged = dict(DEFAULT_USER_SETTINGS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULT_USER_SETTINGS})
    return merged


@app.route('/api/v1/settings')
@api_login_required
def api_v1_settings_get():
    return jsonify(_settings_payload(g.api_user['id']))


@app.route('/api/v1/settings', methods=['PUT'])
@api_login_required
def api_v1_settings_update():
    """Update parcial — solo se validan/pisan las keys presentes en el
    body, el resto queda como estaba. Todas las validaciones son sobre
    el JSON en memoria antes de persistir nada, así un solo campo
    inválido no deja el resto a mitad de aplicar (falla atómica)."""
    data = request.get_json(silent=True) or {}
    current = _settings_payload(g.api_user['id'])
    errors = {}

    # orbitron_max_top_n se procesa antes que orbitron_default_results
    # para que, si vienen ambos en el mismo PUT, el tope ya actualizado
    # sea el que valida al default (no el viejo).
    if 'orbitron_max_top_n' in data:
        v = data['orbitron_max_top_n']
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 500:
            current['orbitron_max_top_n'] = v
        else:
            errors['orbitron_max_top_n'] = 'must be an integer between 1 and 500'

    if 'orbitron_default_results' in data:
        v = data['orbitron_default_results']
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= current['orbitron_max_top_n']:
            current['orbitron_default_results'] = v
        else:
            errors['orbitron_default_results'] = 'must be an integer between 1 and orbitron_max_top_n'

    if 'search_page_size' in data:
        v = data['search_page_size']
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100:
            current['search_page_size'] = v
        else:
            errors['search_page_size'] = 'must be an integer between 1 and 100'

    if 'triple_tap_report_enabled' in data:
        v = data['triple_tap_report_enabled']
        if isinstance(v, bool):
            current['triple_tap_report_enabled'] = v
        else:
            errors['triple_tap_report_enabled'] = 'must be a boolean'

    if 'sleep_timer_default_minutes' in data:
        v = data['sleep_timer_default_minutes']
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 480:
            current['sleep_timer_default_minutes'] = v
        else:
            errors['sleep_timer_default_minutes'] = 'must be an integer between 1 and 480 minutes'

    if 'dac_max_dsd_tier' in data:
        v = data['dac_max_dsd_tier']
        if isinstance(v, str) and v in _DAC_DSD_TIERS:
            current['dac_max_dsd_tier'] = v
        else:
            errors['dac_max_dsd_tier'] = f'must be one of {sorted(_DAC_DSD_TIERS)}'

    if errors:
        return jsonify({'error': 'validation_failed', 'details': errors}), 400

    conn = get_db_connection()
    try:
        conn.execute(
            'UPDATE users SET settings_json=? WHERE id=?',
            (json.dumps(current), g.api_user['id'])
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(current)


@app.route('/api/v1/report-issue', methods=['POST'])
@api_login_required
def api_v1_report_issue():
    """Ticket 19, Feature 4 — reporte de errores de la beta. Disponible
    para CUALQUIER usuario autenticado, no solo admin (confirmado con el
    PO, Ticket 19 §4 pregunta 1: el objetivo declarado es justamente que
    el pequeño grupo de la beta pueda reportar) — por eso
    @api_login_required acá, no @api_admin_required.

    El email del usuario NO se toma del body — se resuelve de
    `g.api_user` (la sesión ya autenticada), igual que el resto de
    endpoints protegidos por este mismo decorator. Evita que el cliente
    pueda falsear qué usuario está reportando.

    `screenshot_base64` y `log_content` son opcionales — un usuario
    puede reportar sin captura (ej: el bug no es visual) o sin log
    disponible; ninguno de los dos bloquea el envío del reporte en sí,
    mismo criterio "nunca romper el flujo" que ya tiene `_send_email`.
    """
    data = request.get_json(silent=True) or {}
    description = (data.get('description') or '').strip()
    if not description:
        return jsonify({'error': 'description_required'}), 400

    screen = (data.get('screen') or '').strip()
    app_version = (data.get('app_version') or '').strip()
    log_content = data.get('log_content') or ''
    screenshot_b64 = data.get('screenshot_base64')

    attachments = []
    if screenshot_b64:
        try:
            # Acepta tanto un data URI completo
            # ("data:image/png;base64,...") como el base64 pelado —
            # ImageRenderer en iOS da PNG típicamente, pero no se asume
            # el formato exacto del prefijo del lado del cliente.
            raw_b64 = screenshot_b64.split(',', 1)[-1] if ',' in screenshot_b64 else screenshot_b64
            screenshot_bytes = base64.b64decode(raw_b64, validate=False)
            attachments.append(('captura.png', screenshot_bytes, 'image/png'))
        except Exception as e:
            app.logger.warning(f'report-issue: no se pudo decodificar la captura: {e}')

    if log_content:
        # Ticket 25 (pedido por Niko: "adjuntar de manera automática el
        # log del dispositivo... si pesa más comprimir"). El cliente
        # ahora manda el archivo COMPLETO (antes se truncaba a ~200KB
        # del lado de iOS) — OrbyteLogger se auto-poda en 5MB, así que en
        # la práctica esto rara vez es grande. En vez de comprimir solo
        # cuando ya "es un problema" (umbral arbitrario a elegir), se
        # comprime siempre que valga la pena (texto de log comprime
        # 90%+ típico, gzip es prácticamente gratis en CPU) — así se
        # elimina de raíz cualquier escenario donde el tamaño del
        # adjunto sea un problema, en vez de solo empujar el umbral.
        log_bytes = log_content.encode('utf-8', errors='replace')
        if len(log_bytes) > 50_000:
            attachments.append(('log.txt.gz', gzip.compress(log_bytes), 'application/gzip'))
        else:
            attachments.append(('log.txt', log_bytes, 'text/plain'))

    user_email = g.api_user['email']
    reported_at = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    extra_rows = ''
    if screen:
        extra_rows += f'<p><strong style="color:#EFEFEF;">Pantalla:</strong> {_xml_escape(screen)}</p>'
    if app_version:
        extra_rows += f'<p><strong style="color:#EFEFEF;">Versión:</strong> {_xml_escape(app_version)}</p>'

    body_html = _email_wrapper('Nuevo reporte de la beta', f'''
        <p><strong style="color:#EFEFEF;">Usuario:</strong> {_xml_escape(user_email)}</p>
        {extra_rows}
        <p><strong style="color:#EFEFEF;">Reportado:</strong> {reported_at}</p>
        <p style="margin-top:20px;"><strong style="color:#EFEFEF;">Descripción:</strong></p>
        <p style="white-space:pre-wrap;">{_xml_escape(description)}</p>
        {'<p style="color:#666;font-size:12px;margin-top:16px;">Captura y/o log adjuntos.</p>' if attachments else ''}
    ''')

    sent = _send_email(
        ADMIN_EMAIL,
        f'[Orbyte] Reporte de {user_email}',
        body_html,
        attachments=attachments or None
    )
    return jsonify({'sent': sent})

@app.route('/api/v1/auth/logout', methods=['POST'])
@api_login_required
def api_v1_logout():
    """No hay estado server-side que revocar con tokens itsdangerous — el
    logout real ocurre cuando el cliente borra el token del Keychain. Este
    endpoint solo registra la actividad y da un cierre prolijo a la API."""
    _touch_user_activity(g.api_user['id'])
    return jsonify({'status': 'ok'})

def api_admin_required(view):
    """Como api_login_required, pero además exige is_admin=1 (403 si no).
    Envuelve api_login_required en vez de reimplementar la validación del
    token, para no duplicar esa lógica."""
    @api_login_required
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.api_user['is_admin']:
            return jsonify({'error': 'not_admin'}), 403
        return view(*args, **kwargs)
    return wrapped

@app.route('/api/v1/admin/users')
@api_admin_required
def api_v1_admin_users():
    """Espejo de admin_users (web) en JSON — mismo orden, mismo criterio de
    'online' (ONLINE_WINDOW_MINUTES), para la pantalla de administración
    del cliente nativo."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM users ORDER BY is_approved ASC, created_at DESC'
        ).fetchall()
        users = [dict(r) for r in rows]
    finally:
        conn.close()

    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(minutes=ONLINE_WINDOW_MINUTES)).isoformat()

    def _shape(u):
        return {
            'id':           u['id'],
            'email':        u['email'],
            'is_admin':     bool(u['is_admin']),
            'is_approved':  bool(u['is_approved']),
            'created_at':   u['created_at'],
            'approved_at':  u['approved_at'],
            'last_seen':    u['last_seen'],
            'last_device':  u['last_device'],
            'last_ip':      u['last_ip'],
            'online':       bool(u['last_seen'] and u['last_seen'] >= cutoff),
        }

    shaped = [_shape(u) for u in users]
    pending = [u for u in shaped if not u['is_approved']]
    approved = [u for u in shaped if u['is_approved']]
    return jsonify({
        'pending': pending,
        'approved': approved,
        'online_count': sum(1 for u in shaped if u['online']),
    })

@app.route('/api/v1/admin/users/status')
@api_admin_required
def api_v1_admin_users_status():
    """Espejo de admin_users_estado (web) — JSON liviano para refrescar
    online/last_seen/last_device sin traer la lista completa de nuevo.
    Pensado para pollearse cada 15s, igual que hace la web."""
    conn = get_db_connection()
    try:
        rows = conn.execute('SELECT id, last_seen, last_device FROM users').fetchall()
    finally:
        conn.close()
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(minutes=ONLINE_WINDOW_MINUTES)).isoformat()
    users = {}
    for r in rows:
        users[str(r['id'])] = {
            'online':      bool(r['last_seen'] and r['last_seen'] >= cutoff),
            'last_seen':   r['last_seen'],
            'last_device': r['last_device'],
        }
    return jsonify({'users': users, 'online_count': sum(1 for u in users.values() if u['online'])})

@app.route('/api/v1/admin/users/<int:user_id>/approve', methods=['POST'])
@api_admin_required
def api_v1_admin_approve(user_id):
    """Idéntico a admin_approve_user (web), en JSON."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT email, is_approved FROM users WHERE id=?', (user_id,)).fetchone()
        already_approved = bool(row and row['is_approved'])
        conn.execute('UPDATE users SET is_approved=1, approved_at=? WHERE id=?',
                     (_utcnow_iso(), user_id))
        conn.commit()
    finally:
        conn.close()
    # Fix (19/08): no era idempotente — mandaba el mail de "cuenta
    # aprobada" en CADA llamada, sin chequear el estado previo. Un
    # reintento del cliente ante un timeout (ver fix de timeout de
    # Tailscale, mismo incidente real) terminaba mandando el mismo mail
    # varias veces al mismo usuario. El UPDATE de arriba sigue siendo
    # idempotente a propósito (approved_at se refresca igual, inofensivo)
    # — solo el mail se manda una única vez, la primera.
    if row and not already_approved:
        _send_account_approved_email(row['email'])
    return jsonify({'status': 'ok'})

@app.route('/api/v1/admin/users/<int:user_id>/reject', methods=['POST'])
@api_admin_required
def api_v1_admin_reject(user_id):
    """Idéntico a admin_reject_user (web): BORRA al usuario, no es un
    estado. Protege la cuenta de administrador igual que la web."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
        if row and row['email'] == ADMIN_EMAIL:
            return jsonify({'error': 'cannot_modify_admin'}), 400
        conn.execute('DELETE FROM users WHERE id=?', (user_id,))
        conn.commit()
    finally:
        conn.close()
    if row:
        _send_account_rejected_email(row['email'])
    return jsonify({'status': 'ok'})

@app.route('/api/v1/admin/users/<int:user_id>/revoke', methods=['POST'])
@api_admin_required
def api_v1_admin_revoke(user_id):
    """Idéntico a admin_revoke_user (web). Protege la cuenta de
    administrador igual que la web."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
        if row and row['email'] == ADMIN_EMAIL:
            return jsonify({'error': 'cannot_modify_admin'}), 400
        conn.execute('UPDATE users SET is_approved=0, approved_at=NULL WHERE id=?', (user_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok'})

# ── Ticket 23: logs como archivo, sin necesitar SSH cada vez ────────────────
_LOG_FILENAME_RE = re.compile(r'^orbyte\.log(\.\d+)?$')

@app.route('/api/v1/admin/logs')
@api_admin_required
def api_v1_admin_logs_list():
    """Lista el log activo (orbyte.log) más los rotados por
    RotatingFileHandler (orbyte.log.1, orbyte.log.2, ...) con tamaño y
    fecha, para elegir cuál bajar sin adivinar nombres por SSH."""
    entries = []
    if os.path.isdir(_LOG_DIR):
        for name in os.listdir(_LOG_DIR):
            if not _LOG_FILENAME_RE.match(name):
                continue
            full = os.path.join(_LOG_DIR, name)
            stat = os.stat(full)
            entries.append({
                'name': name,
                'size_bytes': stat.st_size,
                'modified_at': datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(timespec='seconds'),
            })
    entries.sort(key=lambda e: e['name'])
    return jsonify({'logs': entries})

@app.route('/api/v1/admin/logs/<name>')
@api_admin_required
def api_v1_admin_logs_download(name):
    """Descarga un archivo de log puntual. `name` se valida contra el
    mismo patrón que la lista de arriba antes de tocar el filesystem —
    nunca un path arbitrario (path traversal vía '../', etc.), solo
    'orbyte.log' o 'orbyte.log.N'."""
    if not _LOG_FILENAME_RE.match(name):
        return jsonify({'error': 'invalid_log_name'}), 400
    full = os.path.join(_LOG_DIR, name)
    if not os.path.isfile(full):
        return jsonify({'error': 'not_found'}), 404
    return send_file(full, mimetype='text/plain', as_attachment=True, download_name=name)

def _api_v1_filtered_albums(conn, filter_type, filter_value, page, sort, dir_, page_size=None):
    """Despacha al mismo helper que ya usan las rutas web (/mood/, /genre/,
    /led/, etc.) — así el resultado que ve el cliente nativo es idéntico al
    de la web, sin reimplementar cada tipo de filtro.

    `page_size` (Ticket 26, Categoría B): resuelto por api_v1_albums()
    desde settings_json del usuario — este helper es exclusivo de
    /api/v1/albums (único caller), así que a diferencia de _meta_browse/
    _paginate no hace falta preservar un default silencioso para rutas web:
    None acá simplemente cae en PAGE_SIZE más abajo, igual que siempre."""
    order = _album_order(sort, dir_)
    if filter_type in ('mood', 'idioma', 'momento', 'era', 'tier', 'tema_lirico'):
        return _meta_browse(conn, filter_type, filter_value, page, '', filter_type, sort, dir_, page_size=page_size)
    if filter_type == 'genre':
        count_sql = '''SELECT COUNT(DISTINCT al.id) FROM albums al
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       WHERE t.genre=? OR tm.genre_primary=?'''
        data_sql = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, 'yellow' as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       LEFT JOIN artists ar ON al.artist_id=ar.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       WHERE t.genre=? OR tm.genre_primary=?'''
        return _paginate(conn, count_sql, [filter_value, filter_value], data_sql, [filter_value, filter_value], page, order, page_size=page_size)
    if filter_type == 'led':
        count_sql = '''SELECT COUNT(DISTINCT al.id)
                       FROM albums al JOIN tracks t ON t.album_id=al.id
                       WHERE ''' + _quality_color_sql('t') + '=?'
        data_sql = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, ? as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN artists ar ON al.artist_id=ar.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE ''' + _quality_color_sql('t') + '=?'
        return _paginate(conn, count_sql, [filter_value], data_sql, [filter_value, filter_value], page, order, page_size=page_size)
    return [], 0, 1

@app.route('/api/v1/home/facets')
@api_login_required
def api_v1_home_facets():
    """Un solo request para las 8 secciones del Home — mismo cálculo que
    hace '/' (web) pero devuelto como JSON, sin duplicar las queries."""
    conn = get_db_connection()
    try:
        total_artists  = conn.execute('SELECT COUNT(*) FROM artists a WHERE EXISTS (SELECT 1 FROM albums al WHERE al.artist_id=a.id)').fetchone()[0]
        total_albums   = conn.execute('SELECT COUNT(*) FROM albums').fetchone()[0]
        total_tracks   = conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]
        total_duration = conn.execute('SELECT COALESCE(SUM(duration),0) FROM tracks').fetchone()[0]
        total_size     = conn.execute('SELECT COALESCE(SUM(file_size),0) FROM tracks').fetchone()[0]

        led_rows = conn.execute(
            f'SELECT {_quality_color_sql("")} AS led_color, COUNT(*) as c FROM tracks WHERE led_color IS NOT NULL GROUP BY 1 ORDER BY c DESC'
        ).fetchall()
        mood_rows = conn.execute(
            'SELECT mood, COUNT(*) as c FROM track_meta WHERE mood IS NOT NULL GROUP BY mood ORDER BY c DESC LIMIT 14'
        ).fetchall()
        momento_rows = conn.execute(
            'SELECT momento, COUNT(*) as c FROM track_meta WHERE momento IS NOT NULL GROUP BY momento ORDER BY c DESC'
        ).fetchall()
        era_order = [
            'early_rock_era', 'british_invasion_era', 'classic_rock_era',
            'nwobhm_synth_era', 'grunge_alternative_era', 'post_millennial_era',
            'streaming_era', 'current_era'
        ]
        era_raw  = conn.execute('SELECT era, COUNT(*) as c FROM track_meta WHERE era IS NOT NULL GROUP BY era').fetchall()
        era_dict = {r['era']: r['c'] for r in era_raw}
        temas_rows = conn.execute(
            'SELECT tema_lirico, COUNT(*) as c FROM track_meta WHERE tema_lirico IS NOT NULL GROUP BY tema_lirico ORDER BY c DESC LIMIT 10'
        ).fetchall()
        genre_rows = conn.execute(
            'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" GROUP BY genre ORDER BY c DESC LIMIT 8'
        ).fetchall()
        # Mismo query sin LIMIT que ya usa home() (web) para el panel
        # "ver todos los géneros" — se manda acá también para que el
        # cliente nativo no necesite un segundo request aparte.
        all_genre_rows = conn.execute(
            'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" GROUP BY genre ORDER BY c DESC'
        ).fetchall()
        lang_rows = conn.execute(
            'SELECT idioma, COUNT(*) as c FROM track_meta WHERE idioma IS NOT NULL AND idioma!="" GROUP BY idioma ORDER BY c DESC LIMIT 12'
        ).fetchall()
        recent_raw = conn.execute('''
            SELECT al.id, al.name, al.cover_path, al.year, al.track_count, al.total_duration,
                   al.artist_id, ar.name as artist_name
            FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
            ORDER BY al.created_at DESC LIMIT 20
        ''').fetchall()
        recent = []
        for a in recent_raw:
            d = dict(a)
            d['cover_url'] = cover_url_filter(d.pop('cover_path'))
            recent.append(d)

        return jsonify({
            'stats': {
                'artists': total_artists, 'albums': total_albums, 'tracks': total_tracks,
                'duration_hours': total_duration / 3600, 'size_tb': total_size / (1024**4),
            },
            'led':      [{'value': r['led_color'], 'count': r['c']} for r in led_rows],
            'mood':     [{'value': r['mood'], 'count': r['c']} for r in mood_rows],
            'momento':  [{'value': r['momento'], 'count': r['c']} for r in momento_rows],
            'era':      [{'value': e, 'count': era_dict[e]} for e in era_order if e in era_dict],
            'tema':     [{'value': r['tema_lirico'], 'count': r['c']} for r in temas_rows],
            'genre':    [{'value': r['genre'], 'count': r['c']} for r in genre_rows],
            'all_genres': [{'value': r['genre'], 'count': r['c']} for r in all_genre_rows],
            'idioma':   [{'value': r['idioma'], 'count': r['c']} for r in lang_rows],
            'recent_albums': recent,
        })
    finally:
        conn.close()

@app.route('/api/v1/albums/<int:album_id>/tracks')
@api_login_required
def api_v1_album_tracks(album_id):
    """Espejo de /api/album/<id>/tracks (web) — misma query, protegido con
    token en vez de cookie de sesión."""
    conn = get_db_connection()
    try:
        alb = conn.execute('SELECT name, cover_path, artist_id FROM albums WHERE id=?', (album_id,)).fetchone()
        album_cover     = clean_db_path(alb['cover_path']) if alb else None
        album_artist_id = alb['artist_id'] if alb else None
        album_name      = alb['name'] if alb else None
        tracks = conn.execute(
            'SELECT * FROM tracks WHERE album_id=? ORDER BY disc_number, CAST(track_number AS INTEGER)',
            (album_id,)
        ).fetchall()
        result = []
        for t in tracks:
            d = dict(t)
            fmt, led = _fmt_format(d)
            result.append({
                'id': d['id'],
                'title': d.get('title'),
                'track_number': d.get('track_number'),
                'disc_number': d.get('disc_number'),
                'duration': d.get('duration'),
                'duration_fmt': _fmt_seconds(d.get('duration')),
                'format_display': fmt,
                'format_color': led,
                'artist_id': album_artist_id,
                'album_name': album_name,
                'cover_url': cover_url_filter(album_cover),
                'stream_url': f'/api/v1/stream/{d["id"]}',
                'is_dsd': d.get('is_dsd'),
                'dsd_rate': d.get('dsd_rate'),
                'sample_rate_real': d.get('sample_rate_real'),
                'bit_depth': d.get('bit_depth'),
                'container_ext': _container_ext(d),
            })
        return jsonify({'tracks': result})
    finally:
        conn.close()


@app.route('/api/v1/artist/<int:artist_id>/tracks')
@api_login_required
def api_v1_artist_tracks(artist_id):
    """Ticket 19, Feature 3 — discografía COMPLETA de un artista en un
    solo request, para "Reproducir Todo" en la pestaña Artistas de
    Favoritos del cliente nativo: todos los álbumes disponibles del
    artista en la biblioteca, no solo lo marcado favorito (confirmado
    con el PO, Ticket 19 §3.1 — a diferencia de la pestaña Álbumes, que
    sí se limita a lo favorito).

    Mismo shape de pista que /api/v1/albums/<id>/tracks de acá arriba
    (mismos helpers: _fmt_format/_fmt_seconds/_container_ext/
    cover_url_filter/clean_db_path) para que el cliente lo decodifique
    en el mismo modelo OrbyteTrack sin cambios — con artist_name
    agregado (constante en todas las filas, pero así el cliente arma el
    PlayableTrack sin tener que pasarlo aparte desde afuera, como sí
    hace con un álbum individual vía playAlbum()).

    LIMIT 2000 como techo de seguridad (no paginación real) — mismo
    criterio que el LIMIT 100 de álbumes en /api/v1/artist/<id>, ninguna
    discografía real de una biblioteca personal debería acercarse a ese
    número de pistas.
    """
    conn = get_db_connection()
    try:
        artist = conn.execute('SELECT name FROM artists WHERE id=?', (artist_id,)).fetchone()
        if not artist:
            return jsonify({'error': 'not_found'}), 404
        rows = conn.execute(
            '''SELECT t.*, al.name as album_name, al.cover_path, al.year as album_year
               FROM tracks t
               JOIN albums al ON al.id = t.album_id
               WHERE al.artist_id=?
               ORDER BY al.year, al.name, t.disc_number, CAST(t.track_number AS INTEGER)
               LIMIT 2000''',
            (artist_id,)
        ).fetchall()
        result = []
        for t in rows:
            d = dict(t)
            fmt, led = _fmt_format(d)
            result.append({
                'id': d['id'],
                'title': d.get('title'),
                'track_number': d.get('track_number'),
                'disc_number': d.get('disc_number'),
                'duration': d.get('duration'),
                'duration_fmt': _fmt_seconds(d.get('duration')),
                'format_display': fmt,
                'format_color': led,
                'artist_id': artist_id,
                'artist_name': artist['name'],
                'album_name': d.get('album_name'),
                'cover_url': cover_url_filter(clean_db_path(d.get('cover_path'))),
                'stream_url': f'/api/v1/stream/{d["id"]}',
                'is_dsd': d.get('is_dsd'),
                'dsd_rate': d.get('dsd_rate'),
                'sample_rate_real': d.get('sample_rate_real'),
                'bit_depth': d.get('bit_depth'),
                'container_ext': _container_ext(d),
            })
        return jsonify({'tracks': result})
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# Ticket 10 — Favoritos generales, Playlists, e info enriquecida
# (Artist/Album/Track). Ver TICKET10_PLAYLISTS_COLA_ACCIONES_CONTEXTUALES.md
# §3.2/§3.3/§3.4 para el diseño completo.
# ═══════════════════════════════════════════════════════════════════════════

# ── Favoritos generales (§3.3) ───────────────────────────────────────────────

@app.route('/api/v1/favorites', methods=['GET'])
@api_login_required
def api_v1_favorites_list():
    """Lista los favoritos del usuario. `type` opcional (artist|album|track);
    sin filtro devuelve los tres tipos mezclados.

    `limit`/`offset` opcionales (Ticket 13 §3 Lote A) — default alto (200)
    para no romper el único consumo actual (resolución de estado ♥, que
    necesita la lista completa), pero disponibles si el día de mañana se
    arma una pantalla "Mis Favoritos" paginada."""
    item_type = request.args.get('type')
    if item_type and item_type not in ('artist', 'album', 'track'):
        return jsonify({'error': 'invalid_type'}), 400
    try:
        limit = int(request.args.get('limit', 200))
        offset = int(request.args.get('offset', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'invalid_pagination'}), 400
    if limit < 1 or limit > 500:
        return jsonify({'error': 'invalid_pagination'}), 400
    if offset < 0:
        return jsonify({'error': 'invalid_pagination'}), 400
    conn = get_db_connection()
    try:
        if item_type:
            rows = conn.execute(
                'SELECT item_type, item_id, added_at FROM user_item_favorites '
                'WHERE user_id=? AND item_type=? ORDER BY added_at DESC '
                'LIMIT ? OFFSET ?',
                (g.api_user['id'], item_type, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT item_type, item_id, added_at FROM user_item_favorites '
                'WHERE user_id=? ORDER BY added_at DESC '
                'LIMIT ? OFFSET ?',
                (g.api_user['id'], limit, offset)
            ).fetchall()
        return jsonify({'favorites': [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route('/api/v1/favorites/detailed', methods=['GET'])
@api_login_required
def api_v1_favorites_detailed():
    """Ticket 15 §2 — favoritos con datos completos para mostrar (nombre,
    portada, artista), para la pantalla nativa "Mis Favoritos".

    A diferencia de /api/v1/favorites (arriba), acá `type` es
    OBLIGATORIO — la UI son 3 pestañas (Artistas/Álbumes/Pistas), una
    consulta por pestaña, nunca una lista mezclada. No se toca el
    endpoint liviano existente: otras pantallas ya dependen de su forma
    actual (item_type/item_id/added_at únicamente) para resolver el
    estado del corazón ♥, y agregarle los JOINs de acá sería trabajo
    desperdiciado en cada una de esas llamadas.

    Paginación por página (no limit/offset) — mismo criterio que
    /api/v1/albums en modo filtrado y /api/v1/meta/tracks, mismo
    PAGE_SIZE (Ticket 13 §3 Lote A) — para que esta pantalla pagine
    igual que el resto de la app. No se reusa _paginate() tal cual: esa
    función post-procesa cada fila asumiendo columnas de álbum
    (cover_path -> cover_path limpio sin resolver a URL, más un
    'album_led' con default 'yellow' que no aplica ni a artistas ni a
    pistas) —Forzarla acá ensuciaría las 2 de los 3 shapes con un campo
    que no les corresponde. Se repite a mano el mismo patrón de
    page/PAGE_SIZE/total_pages en vez de eso.
    """
    item_type = request.args.get('type')
    if item_type not in ('artist', 'album', 'track'):
        return jsonify({'error': 'invalid_type'}), 400
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    # Ticket 19, Feature 3: "Reproducir Todo" necesita TODOS los
    # favoritos hasta el tope acordado con el PO (20 artistas / 50
    # álbumes / 500 pistas) en un solo request, no N páginas
    # secuenciales de a search_page_size (Ticket 26, Categoría B — antes
    # PAGE_SIZE fijo). `limit`, si viene, reemplaza el default para ese
    # request puntual — page se ignora en ese caso (siempre offset 0),
    # es un pedido de "dame todo de una", no una página más. Tope duro
    # de 500 del lado del servidor pase lo que pase en el query param,
    # para no habilitar un abuso tipo ?limit=999999.
    default_page_size = _settings_payload(g.api_user['id'])['search_page_size']
    limit_param = request.args.get('limit')
    page_size = default_page_size
    offset = (page - 1) * default_page_size
    if limit_param is not None:
        try:
            page_size = max(1, min(500, int(limit_param)))
        except (TypeError, ValueError):
            page_size = default_page_size
        offset = 0

    conn = get_db_connection()
    try:
        if item_type == 'artist':
            total = conn.execute(
                'SELECT COUNT(*) FROM user_item_favorites WHERE user_id=? AND item_type=?',
                (g.api_user['id'], item_type)
            ).fetchone()[0]
            rows = conn.execute(
                '''SELECT uif.item_id AS id, uif.added_at, ar.name, ar.nationality,
                          (SELECT COUNT(*) FROM albums al2 WHERE al2.artist_id = ar.id) AS album_count
                   FROM user_item_favorites uif
                   JOIN artists ar ON ar.id = uif.item_id
                   WHERE uif.user_id=? AND uif.item_type='artist'
                   ORDER BY uif.added_at DESC LIMIT ? OFFSET ?''',
                (g.api_user['id'], page_size, offset)
            ).fetchall()
            items = [dict(r) for r in rows]

        elif item_type == 'album':
            total = conn.execute(
                'SELECT COUNT(*) FROM user_item_favorites WHERE user_id=? AND item_type=?',
                (g.api_user['id'], item_type)
            ).fetchone()[0]
            rows = conn.execute(
                '''SELECT uif.item_id AS id, uif.added_at, al.name, al.year,
                          al.track_count, al.total_duration, al.cover_path,
                          ar.id AS artist_id, ar.name AS artist_name
                   FROM user_item_favorites uif
                   JOIN albums al ON al.id = uif.item_id
                   LEFT JOIN artists ar ON ar.id = al.artist_id
                   WHERE uif.user_id=? AND uif.item_type='album'
                   ORDER BY uif.added_at DESC LIMIT ? OFFSET ?''',
                (g.api_user['id'], page_size, offset)
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d['cover_url'] = cover_url_filter(d.get('cover_path'))
                items.append(d)

        else:  # track
            total = conn.execute(
                'SELECT COUNT(*) FROM user_item_favorites WHERE user_id=? AND item_type=?',
                (g.api_user['id'], item_type)
            ).fetchone()[0]
            # Mismo patrón de columnas/joins que _api_meta_tracks_payload()
            # (app.py, /api/v1/meta/tracks) para que el nativo decodifique
            # esto en el mismo modelo `OrbyteTrack` sin cambios — la
            # diferencia es la fuente (favoritos del usuario, no un filtro
            # de campo) y que acá no hacen falta los joins de track_meta/
            # pop_score (no se muestran en esta lista).
            rows = conn.execute(
                '''SELECT uif.added_at, t.*, a.name as album_name, a.cover_path,
                          a.year as album_year, ar.id as artist_id, ar.name as artist_name
                   FROM user_item_favorites uif
                   JOIN tracks t ON t.id = uif.item_id
                   LEFT JOIN albums a ON a.id = t.album_id
                   LEFT JOIN artists ar ON ar.id = a.artist_id
                   WHERE uif.user_id=? AND uif.item_type='track'
                   ORDER BY uif.added_at DESC LIMIT ? OFFSET ?''',
                (g.api_user['id'], page_size, offset)
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                d['cover_path']  = clean_db_path(d.get('cover_path'))
                d['cover_url']   = cover_url_filter(d['cover_path'])
                d['file_path']   = clean_db_path(d.get('file_path'))
                d['audio_url']   = audio_url_filter(d['file_path'])
                d['stream_url']  = f'/api/v1/stream/{d["id"]}'
                fmt, led = _fmt_format(d)
                d['format_display'] = fmt
                d['format_color']   = led
                d['duration_fmt']   = _fmt_seconds(d.get('duration'))
                items.append(d)

        total_pages = max(1, (total + page_size - 1) // page_size)
        return jsonify({
            'favorites':   items,
            'total':       total,
            'page':        page,
            'total_pages': total_pages,
        })
    finally:
        conn.close()


@app.route('/api/v1/favorites/toggle', methods=['POST'])
@api_login_required
def api_v1_favorites_toggle():
    """Agrega o saca un favorito. {item_type, item_id}. Reemplaza al viejo
    /api/favorites/toggle (solo pistas, sin user_id) — la web (base.html/
    player.js/favorites.html) también apunta acá ahora, vía cookie de
    sesión (api_login_required acepta Bearer O cookie)."""
    data = request.get_json(silent=True) or {}
    item_type = data.get('item_type')
    item_id   = data.get('item_id')
    if item_type not in ('artist', 'album', 'track') or not item_id:
        return jsonify({'error': 'missing_or_invalid_fields'}), 400
    conn = get_db_connection()
    try:
        existing = conn.execute(
            'SELECT 1 FROM user_item_favorites WHERE user_id=? AND item_type=? AND item_id=?',
            (g.api_user['id'], item_type, item_id)
        ).fetchone()
        if existing:
            conn.execute(
                'DELETE FROM user_item_favorites WHERE user_id=? AND item_type=? AND item_id=?',
                (g.api_user['id'], item_type, item_id)
            )
            is_favorite = False
        else:
            conn.execute(
                'INSERT OR IGNORE INTO user_item_favorites (user_id, item_type, item_id) VALUES (?, ?, ?)',
                (g.api_user['id'], item_type, item_id)
            )
            is_favorite = True
        conn.commit()
        return jsonify({'item_type': item_type, 'item_id': item_id, 'is_favorite': is_favorite})
    finally:
        conn.close()


# ── Playlists (§3.1/§3.2) ────────────────────────────────────────────────────

def _playlist_owned_or_404(conn, playlist_id, user_id):
    """Fila de playlist si existe Y pertenece a user_id, si no None. 404 (no
    403) a propósito en los callers — no delatar que existe una playlist
    ajena con ese id."""
    return conn.execute(
        'SELECT * FROM playlists WHERE id=? AND user_id=?', (playlist_id, user_id)
    ).fetchone()

def _playlist_acceso(conn, playlist_id, user_id):
    """Ticket C-10 etapa 2: (fila, rol) — rol 'dueno' | 'editar' | 'ver' —
    o (None, None) si no es suya ni se la compartieron (los callers responden
    404, mismo criterio que _playlist_owned_or_404)."""
    pl = conn.execute('SELECT * FROM playlists WHERE id=?', (playlist_id,)).fetchone()
    if not pl:
        return None, None
    if pl['user_id'] == user_id:
        return pl, 'dueno'
    c = conn.execute('SELECT permiso FROM playlist_compartidas WHERE playlist_id=? AND usuario_id=?',
                     (playlist_id, user_id)).fetchone()
    if c:
        return pl, ('editar' if c['permiso'] == 'editar' else 'ver')
    return None, None


@app.route('/api/v1/playlists', methods=['GET'])
@api_login_required
def api_v1_playlists_list():
    # Ticket 20, Ítem 3: alphabetical | last_played | most_played.
    # Ausente/no reconocido -> _playlist_order conserva updated_at DESC
    # (comportamiento previo a este ticket).
    # Ticket C-10 etapa 2: ?compartidas=1 suma las que me compartieron
    # (rol 'ver'/'editar' + dueño). Sin el parámetro, solo las propias (las
    # apps viejas no ven playlists ajenas que no sabrían manejar).
    sort = request.args.get('sort', '')
    yo = g.api_user['id']
    con_compartidas = request.args.get('compartidas') in ('1', 'true')
    conn = get_db_connection()
    try:
        donde = 'p.user_id = ? OR pc.usuario_id IS NOT NULL' if con_compartidas else 'p.user_id = ?'
        rows = conn.execute(f'''
            SELECT p.id, p.name, p.created_at, p.updated_at,
                   p.last_played_at, p.play_count, p.user_id AS dueno_id, pc.permiso AS mi_permiso,
                   (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) as track_count,
                   (SELECT COUNT(*) FROM playlist_compartidas x WHERE x.playlist_id = p.id) as compartida_n
            FROM playlists p
            LEFT JOIN playlist_compartidas pc ON pc.playlist_id = p.id AND pc.usuario_id = ?
            WHERE {donde}
            ORDER BY {_playlist_order(sort)}
        ''', (yo, yo)).fetchall()
        cache = {}
        out = []
        for r in rows:
            d = dict(r)
            dueno_id = d.pop('dueno_id')
            permiso = d.pop('mi_permiso')
            if dueno_id == yo:
                d['rol'], d['dueno'] = 'dueno', None
            else:
                d['rol'] = 'editar' if permiso == 'editar' else 'ver'
                if dueno_id not in cache:
                    cache[dueno_id] = _social_usuario(conn, dueno_id)
                d['dueno'] = cache[dueno_id]
            out.append(d)
        return jsonify({'playlists': out})
    finally:
        conn.close()

@app.route('/api/v1/playlists', methods=['POST'])
@api_login_required
def api_v1_playlists_create():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'missing_name'}), 400
    conn = get_db_connection()
    try:
        cur = conn.execute(
            'INSERT INTO playlists (user_id, name) VALUES (?, ?)',
            (g.api_user['id'], name)
        )
        # Ticket C-10 etapa 2: «crear en conjunto» — {amigos: [ids]} entran
        # con permiso de editar desde el comienzo.
        n = 0
        for uid in (data.get('amigos') or [])[:50]:
            if _playlist_compartir_con(conn, cur.lastrowid, name, g.api_user['id'], uid, 'editar'):
                n += 1
        conn.commit()
        return jsonify({'id': cur.lastrowid, 'name': name, 'track_count': 0, 'compartida_n': n}), 201
    finally:
        conn.close()

@app.route('/api/v1/playlists/<int:playlist_id>', methods=['GET'])
@api_login_required
def api_v1_playlist_detail(playlist_id):
    """Detalle con las pistas ya resueltas — mismo shape que
    /api/v1/albums/<id>/tracks, para armar PlayableTrack sin otra llamada.

    Ticket 24 (iOS), Lote 4: se suma artist_name/album_year/genre_primary/
    mood/tema_lirico a cada pista — antes esta respuesta solo traía
    artist_id (sin nombre) y nada de track_meta, así que el cliente no
    tenía con qué armar el "Resumen" de la playlist sin pedir un
    endpoint nuevo por separado. Se amplía el SELECT existente en vez de
    agregar un endpoint de agregados (evaluado en el ticket: alcanza con
    esto, el resumen se calcula del lado del cliente a partir de estas
    mismas pistas). Mismo patrón de joins que ya usan otros endpoints de
    este archivo (LEFT JOIN artists / LEFT JOIN track_meta, ver por
    ejemplo api_v1_track_detail más abajo) — nada nuevo, solo replicado
    acá. genre_primary usa el mismo COALESCE(tm.genre_primary, t.genre)
    que ya usa el resto del archivo (línea ~2527): track_meta es
    metadata enriquecida y puede no estar poblada para todas las pistas,
    t.genre es el tag nativo del escaneo.
    """
    conn = get_db_connection()
    try:
        pl, rol = _playlist_acceso(conn, playlist_id, g.api_user['id'])
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        miembros = _playlist_miembros(conn, playlist_id)
        cache = {}
        rows = conn.execute('''
            SELECT t.*, pt.position, pt.added_by AS pt_added_by, al.name as album_name, al.year as album_year,
                   al.cover_path, al.artist_id, ar.name as artist_name,
                   COALESCE(tm.genre_primary, t.genre) as genre_primary,
                   tm.mood, tm.tema_lirico
            FROM playlist_tracks pt
            JOIN tracks t ON t.id = pt.track_id
            LEFT JOIN albums al ON al.id = t.album_id
            LEFT JOIN artists ar ON al.artist_id = ar.id
            LEFT JOIN track_meta tm ON tm.track_id = t.id
            WHERE pt.playlist_id = ?
            ORDER BY pt.position
        ''', (playlist_id,)).fetchall()
        tracks = []
        for r in rows:
            d = dict(r)
            fmt, led = _fmt_format(d)
            cover = clean_db_path(d.get('cover_path'))
            tracks.append({
                'id':             d['id'],
                'title':          d.get('title'),
                'track_number':   d.get('track_number'),
                'disc_number':    d.get('disc_number'),
                'duration':       d.get('duration'),
                'duration_fmt':   _fmt_seconds(d.get('duration')),
                'format_display': fmt,
                'format_color':   led,
                'artist_id':      d.get('artist_id'),
                'artist_name':    d.get('artist_name'),
                'album_name':     d.get('album_name'),
                'album_year':     d.get('album_year'),
                'genre_primary':  d.get('genre_primary'),
                'mood':           d.get('mood'),
                'tema_lirico':    d.get('tema_lirico'),
                'cover_url':      cover_url_filter(cover),
                'stream_url':     f'/api/v1/stream/{d["id"]}',
                'is_dsd':         d.get('is_dsd'),
                'dsd_rate':       d.get('dsd_rate'),
                'sample_rate_real': d.get('sample_rate_real'),
                'bit_depth':      d.get('bit_depth'),
                'container_ext':  _container_ext(d),
                'position':       d.get('position'),
            })
            # Ticket C-10 etapa 2: quién la agregó (solo si la playlist es
            # compartida; sin dato = el dueño).
            if miembros:
                quien = d.get('pt_added_by') or pl['user_id']
                if quien not in cache:
                    cache[quien] = _social_usuario(conn, quien)
                tracks[-1]['agregada_por'] = cache[quien]
        return jsonify({
            'id': pl['id'], 'name': pl['name'],
            'created_at': pl['created_at'], 'updated_at': pl['updated_at'],
            'tracks': tracks,
            'rol': rol,
            'dueno': _social_usuario(conn, pl['user_id']),
            'miembros': miembros,
            'copia_de': pl['copia_de'],
            # Etapa 3: ❤️ y comentarios (solo tienen sentido si es compartida).
            'likes': conn.execute("SELECT COUNT(*) FROM social_likes WHERE tipo='playlist' AND ref_id=?",
                                  (playlist_id,)).fetchone()[0],
            'me_gusta': bool(conn.execute("SELECT 1 FROM social_likes WHERE tipo='playlist' AND ref_id=? AND de_id=?",
                                          (playlist_id, g.api_user['id'])).fetchone()),
            'comentarios_n': conn.execute('SELECT COUNT(*) FROM playlist_comentarios WHERE playlist_id=?',
                                          (playlist_id,)).fetchone()[0],
        })
    finally:
        conn.close()

@app.route('/api/v1/playlists/<int:playlist_id>', methods=['PATCH'])
@api_login_required
def api_v1_playlist_rename(playlist_id):
    """Renombrar (alcance sumado a este ticket a pedido del PO — ver §5)."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'missing_name'}), 400
    conn = get_db_connection()
    try:
        pl = _playlist_owned_or_404(conn, playlist_id, g.api_user['id'])
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        conn.execute(
            "UPDATE playlists SET name=?, updated_at=datetime('now') WHERE id=?",
            (name, playlist_id)
        )
        conn.commit()
        return jsonify({'id': playlist_id, 'name': name})
    finally:
        conn.close()

@app.route('/api/v1/playlists/<int:playlist_id>', methods=['DELETE'])
@api_login_required
def api_v1_playlist_delete(playlist_id):
    conn = get_db_connection()
    try:
        pl = _playlist_owned_or_404(conn, playlist_id, g.api_user['id'])
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        conn.execute('DELETE FROM playlist_tracks WHERE playlist_id=?', (playlist_id,))
        conn.execute('DELETE FROM playlist_compartidas WHERE playlist_id=?', (playlist_id,))
        conn.execute('DELETE FROM playlist_comentarios WHERE playlist_id=?', (playlist_id,))
        conn.execute("DELETE FROM social_likes WHERE tipo='playlist' AND ref_id=?", (playlist_id,))
        conn.execute('DELETE FROM playlists WHERE id=?', (playlist_id,))
        conn.commit()
        return jsonify({'deleted': True})
    finally:
        conn.close()

@app.route('/api/v1/playlists/<int:playlist_id>/mark-played', methods=['POST'])
@api_login_required
def api_v1_playlist_mark_played(playlist_id):
    """Ticket 20, Ítem 3. Se llama desde playAll(_:) en PlaylistDetailView
    (botón "Reproducir Todo") — criterio confirmado por el PO: cuenta con
    arrancar la reproducción desde la sección de playlist, no hace falta
    escucharla completa. Tocar una pista suelta dentro del detalle
    (play(_:in:), navegación directa) NO pasa por acá — mismo criterio.

    No toca `updated_at`: esa columna es del contenido (agregar/sacar
    pistas), no de la reproducción — mezclarlas rompería el orden por
    Última Reproducción apenas alguien edite una playlist vieja."""
    conn = get_db_connection()
    try:
        pl, rol = _playlist_acceso(conn, playlist_id, g.api_user['id'])
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        if rol != 'dueno':
            # C-10 etapa 2: el orden «Última reproducción» es del dueño.
            # Etapa 3: cuenta como escucha de un amigo (dashboard del dueño).
            conn.execute('INSERT INTO playlist_escuchas (playlist_id, usuario_id, creada_at) VALUES (?, ?, ?)',
                         (playlist_id, g.api_user['id'], _utcnow_iso()))
            conn.commit()
            return jsonify({'marked': True})
        conn.execute(
            "UPDATE playlists SET play_count = play_count + 1, "
            "last_played_at = datetime('now') WHERE id=?",
            (playlist_id,)
        )
        conn.commit()
        return jsonify({'marked': True})
    finally:
        conn.close()

@app.route('/api/v1/playlists/<int:playlist_id>/tracks', methods=['POST'])
@api_login_required
def api_v1_playlist_add_track(playlist_id):
    """Idempotente: si ya está, no falla ni duplica (UNIQUE + INSERT OR IGNORE)."""
    data = request.get_json(silent=True) or {}
    track_id = data.get('track_id')
    if not track_id:
        return jsonify({'error': 'missing_track_id'}), 400
    conn = get_db_connection()
    try:
        yo = g.api_user['id']
        pl, rol = _playlist_acceso(conn, playlist_id, yo)
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        if rol == 'ver':
            return jsonify({'error': 'solo_lectura'}), 403
        if not conn.execute('SELECT id FROM tracks WHERE id=?', (track_id,)).fetchone():
            return jsonify({'error': 'track_not_found'}), 404
        next_pos = conn.execute(
            'SELECT COALESCE(MAX(position), -1) + 1 as p FROM playlist_tracks WHERE playlist_id=?',
            (playlist_id,)
        ).fetchone()['p']
        cur = conn.execute(
            'INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position, added_by) VALUES (?, ?, ?, ?)',
            (playlist_id, track_id, next_pos, yo)
        )
        conn.execute("UPDATE playlists SET updated_at=datetime('now') WHERE id=?", (playlist_id,))
        if cur.rowcount:
            _playlist_avisar_cambio(conn, pl, yo)
        conn.commit()
        return jsonify({'added': True, 'track_id': track_id})
    finally:
        conn.close()

@app.route('/api/v1/playlists/<int:playlist_id>/tracks/<int:track_id>', methods=['DELETE'])
@api_login_required
def api_v1_playlist_remove_track(playlist_id, track_id):
    conn = get_db_connection()
    try:
        pl, rol = _playlist_acceso(conn, playlist_id, g.api_user['id'])
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        if rol == 'ver':
            return jsonify({'error': 'solo_lectura'}), 403
        conn.execute(
            'DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?',
            (playlist_id, track_id)
        )
        conn.execute("UPDATE playlists SET updated_at=datetime('now') WHERE id=?", (playlist_id,))
        conn.commit()
        return jsonify({'removed': True, 'track_id': track_id})
    finally:
        conn.close()

@app.route('/api/v1/playlists/<int:playlist_id>/tracks/reorder', methods=['PUT'])
@api_login_required
def api_v1_playlist_reorder(playlist_id):
    """Reordenar (alcance sumado a este ticket a pedido del PO — ver §5).
    Recibe la lista COMPLETA de track_ids en el orden final deseado (no un
    par from/to) — más simple para el cliente nativo, que ya arma la lista
    completa localmente durante el drag & drop. Valida que sea exactamente
    el mismo set de pistas que ya tiene la playlist, ni más ni menos."""
    data = request.get_json(silent=True) or {}
    track_ids = data.get('track_ids')
    if not isinstance(track_ids, list) or not track_ids:
        return jsonify({'error': 'missing_track_ids'}), 400
    conn = get_db_connection()
    try:
        pl, rol = _playlist_acceso(conn, playlist_id, g.api_user['id'])
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        if rol == 'ver':
            return jsonify({'error': 'solo_lectura'}), 403
        existing_ids = {
            r['track_id'] for r in conn.execute(
                'SELECT track_id FROM playlist_tracks WHERE playlist_id=?', (playlist_id,)
            ).fetchall()
        }
        if set(track_ids) != existing_ids:
            return jsonify({'error': 'track_id_set_mismatch'}), 400
        for pos, tid in enumerate(track_ids):
            conn.execute(
                'UPDATE playlist_tracks SET position=? WHERE playlist_id=? AND track_id=?',
                (pos, playlist_id, tid)
            )
        conn.execute("UPDATE playlists SET updated_at=datetime('now') WHERE id=?", (playlist_id,))
        conn.commit()
        return jsonify({'reordered': True})
    finally:
        conn.close()


# ── Info enriquecida: Artist / Album / Track (§2.5/§3.4) ────────────────────
# No hay JSON existente que espejar para artist/album (a diferencia de
# track) — se arman desde cero tomando como base exactamente los mismos
# queries que ya arman artist()/album() para el HTML, así no se reinventa
# qué datos importan.

def _normalize_lastfm_count(value):
    """Ticket 39 (bugfix, reportado por Niko): lastfm_listeners/
    lastfm_playcount pueden llegar desde SQLite como número o como string
    según la afinidad de tipo real de la columna en esa fila puntual —
    mismo problema exacto que ya se documentó y resolvió del lado del
    cliente para otros campos mixtos (ver FlexibleNumberStringSerializer/
    FlexibleNumberString en Android/iOS). Los clientes declaran
    OrbyteArtistDetail.lastfmListeners/lastfmPlaycount como Int? estricto
    (sin ese serializer flexible), así que una fila con el valor guardado
    como string rompe el decode COMPLETO del JSON de la pantalla de
    detalle de artista. Se normaliza acá, en el origen, para arreglar la
    causa raíz de una sola vez para ambos clientes en vez de parchear cada
    modelo por separado — mismo criterio recomendado en TICKET_39 §2."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@app.route('/api/v1/artist/<int:artist_id>')
@api_login_required
def api_v1_artist_detail(artist_id):
    conn = get_db_connection()
    try:
        ar = conn.execute('SELECT * FROM artists WHERE id=?', (artist_id,)).fetchone()
        if not ar:
            return jsonify({'error': 'not_found'}), 404
        # Ticket 13 §3 Lote A: LIMIT 100 como techo de seguridad (no
        # paginación real) — ningún artista real de una biblioteca personal
        # debería superarlo.
        albums_raw = conn.execute(
            'SELECT *, artist_id FROM albums WHERE artist_id=? ORDER BY year, name LIMIT 100',
            (artist_id,)
        ).fetchall()
        total_tracks = sum(a['track_count'] or 0 for a in albums_raw)
        albums = []
        for a in albums_raw:
            d = dict(a)
            d['cover_path'] = clean_db_path(d.get('cover_path'))
            d['cover_url']  = cover_url_filter(d['cover_path'])
            led_row = conn.execute(
                '''SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END AS led_color FROM tracks
                   WHERE album_id=? AND led_color IS NOT NULL
                   GROUP BY 1
                   ORDER BY CASE led_color
                     WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                     WHEN 'red'     THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                     ELSE 6 END, MIN((COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')))
                   LIMIT 1''',
                (a['id'],)
            ).fetchone()
            d['album_led'] = led_row['led_color'] if led_row else 'yellow'
            albums.append(d)

        ar_data = dict(ar)
        ar_data['flag'] = nationality_flag(ar_data.get('nationality') or '')

        genre_row = conn.execute('''
            SELECT COALESCE(tm.genre_primary, t.genre) as g, COUNT(*) as c
            FROM tracks t
            JOIN albums al ON al.id=t.album_id
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            WHERE al.artist_id=?
              AND COALESCE(tm.genre_primary, t.genre) IS NOT NULL
              AND COALESCE(tm.genre_primary, t.genre) != ''
            GROUP BY g ORDER BY c DESC LIMIT 5
        ''', (artist_id,)).fetchall()
        genres = [r['g'] for r in genre_row]

        similar_artists = build_similar_artists(conn, ar_data.get('similar_artists_json'), limit=12)

        # Ticket 12 (fixes): dos correcciones sobre "Populares" —
        # 1) Dedupe: reusa `_track_dedupe_condition` (mismo helper que ya
        #    usan las vistas de pistas generales, Ticket 09) en vez de
        #    reinventar la lógica — una canción que aparece en el álbum
        #    original Y en un recopilatorio/reedición del mismo artista
        #    ya no sale duplicada, se queda solo la copia de mejor
        #    calidad (score = calidad*0.7 + pop_score*0.3, empate
        #    resuelto por id menor). Acotado a `al.artist_id = ?` (mismo
        #    criterio que el WHERE exterior — no compara contra la
        #    biblioteca entera, solo contra el catálogo de este artista).
        # 2) LIMIT 10 -> 15, pedido explícito del PO.
        dedupe_clause = _track_dedupe_condition(extra_where='al.artist_id = ?', track_alias='t', pop_alias='tpc')
        top_tracks_raw = conn.execute(
            f'''SELECT t.*, al.name as album_name, al.cover_path as album_cover,
                      COALESCE(tpc.pop_score, 0) as pop_score
               FROM tracks t
               JOIN albums al ON al.id = t.album_id
               LEFT JOIN track_pop_cache tpc ON tpc.track_id = t.id
               WHERE al.artist_id = ?
                 AND {dedupe_clause}
               ORDER BY pop_score DESC, t.title
               LIMIT 15''',
            (artist_id, artist_id)
        ).fetchall()
        top_tracks = []
        for t in top_tracks_raw:
            d = dict(t)
            cover = clean_db_path(d.get('album_cover'))
            fmt, led = _fmt_format(d)
            top_tracks.append({
                'id': d['id'], 'title': d.get('title'),
                'duration': d.get('duration'), 'duration_fmt': _fmt_seconds(d.get('duration')),
                'format_display': fmt, 'format_color': led,
                'album_id': d.get('album_id'), 'album_name': d.get('album_name'),
                'cover_url': cover_url_filter(cover),
                'stream_url': f'/api/v1/stream/{d["id"]}',
                # Fix (Motor 2 - prerrequisito): este dict se armaba a mano y
                # dejaba afuera TODOS los campos de calidad, pese a que ya
                # estaban en `d` via el `t.*` de arriba - una pista DSD en
                # Populares sonaba por el camino equivocado desde que se
                # conecto Play real ahi (Ticket 34), porque el cliente no
                # tenia forma de saber que lo era. Mismas claves que
                # track_to_json()/los demas endpoints que devuelven pistas.
                #
                # Fix (Ticket 39, reportado por Niko: "No se pudo cargar
                # el artista" -- reproducido con Helloween, confirmado con
                # traceback real de Android): el bool(...) de aca abajo
                # era el UNICO lugar de los 6 en todo el proyecto que
                # envuelve is_dsd/is_mqa en bool() antes de mandarlo --
                # jsonify() serializa eso como true/false real (JSON
                # boolean), mientras que track_to_json() y los demas 5
                # endpoints que devuelven pistas (incluido el pass-through
                # normal de esta misma tabla) mandan el 0/1 crudo tal cual
                # sale de SQLite (Python int, no bool). SearchTrack.isDSD/
                # isMqa (Android) y su equivalente Swift estan tipados
                # Int?/Int? A PROPOSITO, no Boolean (ver comentario en
                # SearchModels.kt) -- kotlinx.serialization rompe el
                # decode COMPLETO del JSON apenas encuentra un `true`
                # literal donde espera un numero. Sacar el bool() alcanza:
                # d.get(...) ya devuelve el 0/1 crudo esperado, igual que
                # en los otros 5 lugares.
                'is_dsd': d.get('is_dsd'),
                'dsd_rate': d.get('dsd_rate'),
                'sample_rate_real': d.get('sample_rate_real'),
                'bit_depth': d.get('bit_depth'),
                'container_ext': _container_ext(d),
                'is_mqa': d.get('is_mqa'),
            })

        is_favorite = bool(conn.execute(
            "SELECT 1 FROM user_item_favorites WHERE user_id=? AND item_type='artist' AND item_id=?",
            (g.api_user['id'], artist_id)
        ).fetchone())

        return jsonify({
            'id': ar_data['id'], 'name': ar_data.get('name'),
            'bio': ar_data.get('bio'), 'nationality': ar_data.get('nationality'),
            'flag': ar_data.get('flag'), 'genres': genres,
            'total_tracks': total_tracks, 'albums': albums,
            'similar_artists': similar_artists, 'top_tracks': top_tracks,
            # Ticket 39: normalizado (ver _normalize_lastfm_count) -- antes
            # se mandaba ar_data.get(...) crudo, que podia llegar como
            # string desde SQLite y romper el decode COMPLETO del JSON en
            # Android/iOS (OrbyteArtistDetail.lastfmListeners/lastfmPlaycount
            # son Int? estricto en ambos clientes).
            'lastfm_listeners': _normalize_lastfm_count(ar_data.get('lastfm_listeners')),
            'lastfm_playcount': _normalize_lastfm_count(ar_data.get('lastfm_playcount')),
            'is_favorite': is_favorite,
        })
    finally:
        conn.close()

@app.route('/api/v1/album/<int:album_id>')
@api_login_required
def api_v1_album_detail(album_id):
    conn = get_db_connection()
    try:
        alb = conn.execute(
            'SELECT al.*, ar.name as artist_name FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id WHERE al.id=?',
            (album_id,)
        ).fetchone()
        if not alb:
            return jsonify({'error': 'not_found'}), 404
        tracks = conn.execute(
            'SELECT * FROM tracks WHERE album_id=? ORDER BY disc_number, CAST(track_number AS INTEGER)',
            (album_id,)
        ).fetchall()
        alb = dict(alb)
        alb['cover_path'] = clean_db_path(alb.get('cover_path'))
        alb['cover_url']  = cover_url_filter(alb['cover_path'])

        fp = conn.execute(
            'SELECT publisher FROM tracks WHERE album_id=? AND publisher IS NOT NULL AND publisher!="" LIMIT 1',
            (album_id,)
        ).fetchone()
        alb['publisher'] = fp['publisher'] if fp else None

        am = conn.execute('SELECT * FROM album_meta WHERE album_id=?', (album_id,)).fetchone()
        _AM_DEFAULTS = {
            'mood_predominante': None, 'momento_predominante': None, 'era': None,
            'idioma_principal': None, 'avg_energy': None, 'avg_valence': None,
            'avg_tension': None, 'avg_depth': None, 'avg_bailabilidad': None,
            'tracks_con_letra': None, 'tracks_sincronizados': None,
            'genre_primary': None, 'genre_secondary': None,
            'lastfm_listeners': None, 'lastfm_playcount': None,
        }
        album_meta = {**_AM_DEFAULTS, **(dict(am) if am else {})}

        track_list = []
        for t in tracks:
            d = dict(t)
            fmt, led = _fmt_format(d)
            track_list.append({
                'id': d['id'], 'title': d.get('title'),
                'track_number': d.get('track_number'), 'disc_number': d.get('disc_number'),
                'duration': d.get('duration'), 'duration_fmt': _fmt_seconds(d.get('duration')),
                'format_display': fmt, 'format_color': led,
                'artist_id': alb.get('artist_id'), 'album_name': alb.get('name'),
                'cover_url': alb['cover_url'],
                'stream_url': f'/api/v1/stream/{d["id"]}',
                'is_dsd': d.get('is_dsd'),
                'dsd_rate': d.get('dsd_rate'),
                'sample_rate_real': d.get('sample_rate_real'),
                'bit_depth': d.get('bit_depth'),
                'container_ext': _container_ext(d),
            })

        if not album_meta.get('genre_primary'):
            row = conn.execute(
                '''SELECT COALESCE(tm.genre_primary, t.genre) as g, COUNT(*) as c
                   FROM tracks t LEFT JOIN track_meta tm ON tm.track_id=t.id
                   WHERE t.album_id=? AND COALESCE(tm.genre_primary, t.genre) IS NOT NULL
                   GROUP BY g ORDER BY c DESC LIMIT 1''',
                (album_id,)
            ).fetchone()
            if row:
                album_meta['genre_primary'] = row['g']

        artist_row = conn.execute(
            'SELECT nationality FROM artists WHERE id=?', (alb.get('artist_id'),)
        ).fetchone()
        artist_nationality = artist_row['nationality'] if artist_row and artist_row['nationality'] else ''
        artist_flag = nationality_flag(artist_nationality) if artist_nationality else ''

        is_favorite = bool(conn.execute(
            "SELECT 1 FROM user_item_favorites WHERE user_id=? AND item_type='album' AND item_id=?",
            (g.api_user['id'], album_id)
        ).fetchone())

        return jsonify({
            'id': alb['id'], 'name': alb.get('name'), 'year': alb.get('year'),
            'artist_id': alb.get('artist_id'), 'artist_name': alb.get('artist_name'),
            'cover_url': alb['cover_url'], 'publisher': alb.get('publisher'),
            'primary_format': alb.get('primary_format') or 'Unknown',
            'artist_nationality': artist_nationality, 'artist_flag': artist_flag,
            'album_meta': album_meta, 'tracks': track_list,
            'is_favorite': is_favorite,
        })
    finally:
        conn.close()

@app.route('/api/v1/track/<int:track_id>')
@api_login_required
def api_v1_track_detail(track_id):
    """Espejo Bearer-friendly de /api/track/<id> — reusa track_to_json() tal
    cual (§2.5), suma is_favorite."""
    conn = get_db_connection()
    try:
        t = conn.execute('''
            SELECT t.*, a.name as album_name, a.cover_path, a.year as album_year, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums a ON t.album_id=a.id
            LEFT JOIN artists ar ON a.artist_id=ar.id
            WHERE t.id=?
        ''', (track_id,)).fetchone()
        if not t:
            return jsonify({'error': 'not_found'}), 404
        result = track_to_json(t)
        tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (track_id,)).fetchone()
        if tm:
            for k in tm.keys():
                if k != 'track_id':
                    result[f'meta_{k}'] = tm[k]
        result['is_favorite'] = bool(conn.execute(
            "SELECT 1 FROM user_item_favorites WHERE user_id=? AND item_type='track' AND item_id=?",
            (g.api_user['id'], track_id)
        ).fetchone())
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/v1/stream/<int:track_id>')
def api_v1_stream(track_id):
    """Streaming para el cliente nativo — a propósito NO reusa /audio/<path>
    (esa ruta depende de la cookie de sesión, no de token, y es tu
    biblioteca completa: mejor un endpoint dedicado que tocar ese gate
    global). Reusa _serve_audio tal cual, range requests incluidos.

    A propósito NO usa @api_login_required tal cual (solo header): AVURLAsset
    en iOS no tiene una forma soportada de mandar headers HTTP custom
    (AVURLAssetHTTPHeaderFieldsKey nunca fue una API pública de Apple, ver
    AGENTS.md — Ticket 07, Lote D). Este endpoint puntual acepta el token
    también como ?token=... en la URL, solo para streaming de audio — el
    resto de /api/v1/* sigue exigiendo el header, esto no se generaliza."""
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[len('Bearer '):] if auth_header.startswith('Bearer ') else request.args.get('token')
    if not token:
        app.logger.warning(f"[api/v1/stream] track={track_id}: sin token (ni header ni query)")
        return jsonify({'error': 'not_authenticated'}), 401
    try:
        user_id = _api_token_signer.loads(token, max_age=_API_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired) as e:
        app.logger.warning(f"[api/v1/stream] track={track_id}: token inválido ({e.__class__.__name__})")
        return jsonify({'error': 'invalid_token'}), 401

    conn = get_db_connection()
    try:
        user = conn.execute('SELECT is_approved FROM users WHERE id=?', (user_id,)).fetchone()
        if not user or not user['is_approved']:
            app.logger.warning(f"[api/v1/stream] track={track_id}: user={user_id} no aprobado o inexistente")
            return jsonify({'error': 'not_authenticated'}), 401
        row = conn.execute('SELECT file_path FROM tracks WHERE id=?', (track_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        app.logger.warning(f"[api/v1/stream] track={track_id}: no existe en la DB")
        return jsonify({'error': 'track_not_found'}), 404
    # file_path en la DB ya viene con el prefijo de MUSIC_ROOT incluido
    # (ej: "mnt/musica/S/System Of A Down/..."), tal cual lo maneja
    # audio_url_filter() para las URLs /audio/ de la web — hay que
    # sacárselo antes de volver a anteponer MUSIC_ROOT, si no queda
    # duplicado ("/mnt/musica/mnt/musica/...") y el archivo nunca se
    # encuentra. audio_url_filter() no sufre esto porque construye la URL
    # relativa ANTES de mandarla al cliente; este endpoint, al recibir
    # solo el id, tiene que volver a armar la ruta desde el file_path
    # crudo de la DB, así que necesita el mismo paso de limpieza acá.
    relative_path = clean_db_path(row['file_path']).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if relative_path.startswith(root + '/'):
        relative_path = relative_path[len(root) + 1:]
    elif relative_path.startswith(root):
        relative_path = relative_path[len(root):]
    absolute_path = os.path.join(MUSIC_ROOT, relative_path)
    if not os.path.isfile(absolute_path):
        app.logger.warning(f"[api/v1/stream] track={track_id}: archivo no encontrado en disco: {absolute_path}")
        return jsonify({'error': 'file_not_found'}), 404
    app.logger.info(
        f"[api/v1/stream] track={track_id}: sirviendo {os.path.basename(absolute_path)} "
        f"(range={request.headers.get('Range', '-')})",
        extra={'track_id': track_id, 'user_id': user_id}
    )
    return _serve_audio(absolute_path)

@app.route('/api/v1/stream-pcm/<int:track_id>')
def api_v1_stream_pcm(track_id):
    """Ticket 16 Lote 3e — fallback PCM para pistas DSD que SFBAudioEngine
    no puede convertir on-device (todo lo que no sea DSD64 exacto —
    confirmado leyendo el código fuente real de la librería,
    SFBDSDPCMDecoder rechaza cualquier otra tasa). El PO fue explícito:
    sin DAC, TODO tiene que poder sonar, sea la tasa que sea, sin
    interrumpir la reproducción, con la mejor calidad posible.

    Dedicado a iOS, mismo criterio que /api/v1/stream/<id>: NO reusa
    /stream-dsd (esa es de la PWA, no tocar lo que la afecta). SÍ reusa
    la lógica de caché ya probada y en producción de
    _dsd_cache_path/_dsd_cache_cleanup — transcodifica el archivo
    COMPLETO a disco antes de servir nada (nunca en vivo/streaming, ese
    fue justo el origen de los cortes que ya conocías) y lo sirve con el
    mismo _serve_audio() de siempre, Range requests incluidos. Mismo
    comando de ffmpeg que /stream-dsd ya tiene probado: 176.4kHz/32-bit
    FLAC — la resolución real de cualquier tasa DSD, sin recortar nada
    (176.4kHz porque es el estándar profesional para masterización DSD,
    equivalente a DXD). Si el mismo archivo ya se transcodificó antes
    para la PWA (misma absolute_path+mtime), esta ruta reusa esa misma
    caché — menos CPU, menos espera.

    Mismo auth que /api/v1/stream/<id>: token también como ?token=...
    en la URL (AVURLAsset no puede mandar headers custom).
    """
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[len('Bearer '):] if auth_header.startswith('Bearer ') else request.args.get('token')
    if not token:
        app.logger.warning(f"[api/v1/stream-pcm] track={track_id}: sin token (ni header ni query)")
        return jsonify({'error': 'not_authenticated'}), 401
    try:
        user_id = _api_token_signer.loads(token, max_age=_API_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired) as e:
        app.logger.warning(f"[api/v1/stream-pcm] track={track_id}: token inválido ({e.__class__.__name__})")
        return jsonify({'error': 'invalid_token'}), 401

    conn = get_db_connection()
    try:
        user = conn.execute('SELECT is_approved FROM users WHERE id=?', (user_id,)).fetchone()
        if not user or not user['is_approved']:
            app.logger.warning(f"[api/v1/stream-pcm] track={track_id}: user={user_id} no aprobado o inexistente")
            return jsonify({'error': 'not_authenticated'}), 401
        row = conn.execute('SELECT file_path FROM tracks WHERE id=?', (track_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        app.logger.warning(f"[api/v1/stream-pcm] track={track_id}: no existe en la DB")
        return jsonify({'error': 'track_not_found'}), 404

    # Mismo criterio de limpieza de path que /api/v1/stream/<id> — ver ese
    # endpoint para el porqué del doble MUSIC_ROOT si no se hace así.
    relative_path = clean_db_path(row['file_path']).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if relative_path.startswith(root + '/'):
        relative_path = relative_path[len(root) + 1:]
    elif relative_path.startswith(root):
        relative_path = relative_path[len(root):]
    absolute_path = os.path.join(MUSIC_ROOT, relative_path)
    if not os.path.isfile(absolute_path):
        app.logger.warning(f"[api/v1/stream-pcm] track={track_id}: archivo no encontrado en disco: {absolute_path}")
        return jsonify({'error': 'file_not_found'}), 404

    _dsd_cache_cleanup()
    cache_path = _dsd_cache_path(absolute_path)
    cache_hit  = os.path.isfile(cache_path)

    if not cache_hit:
        tmp_path = f'{cache_path}.{os.getpid()}.tmp'
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', absolute_path,
            '-vn',                        # sin portada embebida (DSF la trae)
            '-ar', '176400',              # 176.4 kHz — mismo estándar que /stream-dsd
            '-sample_fmt', 's32',         # 32-bit, rango dinámico completo
            '-c:a', 'flac',
            '-compression_level', '0',    # encode más rápido
            '-f', 'flac',                 # explícito — tmp_path termina en .tmp
            tmp_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0 or not os.path.isfile(tmp_path):
                app.logger.error(
                    f"[api/v1/stream-pcm] ffmpeg failed for {absolute_path}: "
                    f"{result.stderr.decode(errors='replace')[:500]}"
                )
                return jsonify({'error': 'transcode_failed'}), 500
            os.replace(tmp_path, cache_path)
        finally:
            if os.path.isfile(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass

    app.logger.info(
        f"[api/v1/stream-pcm] track={track_id}: sirviendo {os.path.basename(cache_path)} "
        f"(cache={'hit' if cache_hit else 'miss'}, range={request.headers.get('Range', '-')})",
        extra={'track_id': track_id, 'user_id': user_id}
    )
    resp = _serve_audio(cache_path)
    resp.headers['Content-Type']      = 'audio/flac'
    resp.headers['X-Transcode-Cache'] = 'hit' if cache_hit else 'miss'
    return resp

# ── Ticket D-16 (pedido por Niko): DSD por segmentos HLS para iOS/Android ───
# Antes, un DSD en el movil esperaba el archivo COMPLETO: DSD64 bajaba el .dsf
# entero (~210 MB por 5 min) y DSD128+ esperaba a que ffmpeg convirtiera todo
# y despues bajaba el FLAC entero (~300 MB). Por Tailscale eso son minutos.
# Ahora el servidor convierte en trozos de 4 s (HLS, fMP4) a medida que
# avanza y el cliente arranca con los primeros: AVPlayer (iOS) y ExoPlayer
# (Android) entienden HLS de forma nativa (buffer, pantalla bloqueada, seek).
#
# Calidad: PCM 176.4 kHz / 24 bit sin perdida (ALAC para iOS, FLAC para
# Android) - misma tasa que /api/v1/stream-pcm; 24 bit cubre de sobra el
# rango dinamico real de un DSD. Solo lo usan los clientes cuando NO hay un
# DAC USB conectado (con DAC siguen por el camino de siempre).
#
# Aislado: no toca /api/v1/stream ni /api/v1/stream-pcm.
_HLS_DIR = os.path.join(tempfile.gettempdir(), 'orbyte_hls')
_HLS_CODECS = {'alac': ('alac', 's32p'), 'flac': ('flac', 's32')}
_HLS_SEGUNDOS = 4
_HLS_MAX_HORAS = 12
_hls_jobs = {}          # dir -> subprocess.Popen en curso
_hls_duraciones = {}    # (ruta, mtime) -> segundos (ffprobe)
# Ticket D-17: frames de 4096 muestras fijos -> los cortes de ffmpeg son
# predecibles (ver _hls_playlist_prevista).
_HLS_FS = 176400
_HLS_FRAME = 4096
_hls_lock = threading.Lock()
_hls_soxr = None


def _hls_tiene_soxr():
    """Una sola prueba por proceso: ¿este ffmpeg trae el remuestreador SoX?"""
    global _hls_soxr
    if _hls_soxr is None:
        try:
            r = subprocess.run(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-f', 'lavfi',
                 '-i', 'anullsrc=r=44100:cl=stereo', '-t', '0.1',
                 '-af', 'aresample=resampler=soxr', '-f', 'null', '-'],
                capture_output=True, timeout=20)
            _hls_soxr = (r.returncode == 0)
        except Exception:
            _hls_soxr = False
    return _hls_soxr


def _hls_auth():
    """Mismo criterio que /api/v1/stream-pcm: token Bearer o ?token= (AVPlayer
    y ExoPlayer piden playlist y segmentos sin headers propios). Devuelve
    (user_id, token, None) o (None, None, respuesta_de_error)."""
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[len('Bearer '):] if auth_header.startswith('Bearer ') else request.args.get('token')
    if not token:
        return None, None, (jsonify({'error': 'not_authenticated'}), 401)
    try:
        user_id = _api_token_signer.loads(token, max_age=_API_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None, None, (jsonify({'error': 'invalid_token'}), 401)
    conn = get_db_connection()
    try:
        user = conn.execute('SELECT is_approved FROM users WHERE id=?', (user_id,)).fetchone()
    finally:
        conn.close()
    if not user or not user['is_approved']:
        return None, None, (jsonify({'error': 'not_authenticated'}), 401)
    return user_id, token, None


def _hls_ruta_pista(track_id):
    """Ruta absoluta del archivo de la pista (mismo criterio que stream-pcm), o None."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT file_path FROM tracks WHERE id=?', (track_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    relative_path = clean_db_path(row['file_path']).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if relative_path.startswith(root + '/'):
        relative_path = relative_path[len(root) + 1:]
    elif relative_path.startswith(root):
        relative_path = relative_path[len(root):]
    absolute_path = os.path.join(MUSIC_ROOT, relative_path)
    return absolute_path if os.path.isfile(absolute_path) else None


def _hls_dir(absolute_path, codec):
    try:
        mtime = os.path.getmtime(absolute_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f'{absolute_path}:{mtime}:{codec}:{_HLS_SEGUNDOS}'.encode('utf-8')).hexdigest()
    return os.path.join(_HLS_DIR, key)


def _hls_limpiar():
    """Borra conversiones viejas (> _HLS_MAX_HORAS sin uso), nunca una en curso."""
    try:
        ahora = time.time()
        for nombre in os.listdir(_HLS_DIR):
            d = os.path.join(_HLS_DIR, nombre)
            job = _hls_jobs.get(d)
            if job is not None and job.poll() is None:
                continue
            if ahora - os.path.getmtime(d) > _HLS_MAX_HORAS * 3600:
                shutil.rmtree(d, ignore_errors=True)
                _hls_jobs.pop(d, None)
    except OSError:
        pass


def _hls_asegurar(absolute_path, codec):
    """Devuelve el directorio de la conversion, arrancandola si hace falta.
    Si ya termino (playlist con ENDLIST) no hace nada; si hay un ffmpeg en
    curso, lo deja seguir; si quedo a medias (p. ej. reinicio del server),
    empieza de nuevo."""
    d = _hls_dir(absolute_path, codec)
    playlist = os.path.join(d, 'index.m3u8')
    with _hls_lock:
        job = _hls_jobs.get(d)
        if job is not None and job.poll() is None:
            return d
        if os.path.isfile(playlist):
            with open(playlist, encoding='utf-8', errors='ignore') as f:
                if '#EXT-X-ENDLIST' in f.read():
                    os.utime(d, None)  # marca de uso para la limpieza
                    return d
        os.makedirs(_HLS_DIR, exist_ok=True)
        _hls_limpiar()
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
        encoder, sample_fmt = _HLS_CODECS[codec]
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
               '-i', absolute_path, '-vn', '-map', '0:a:0']
        if _hls_tiene_soxr():
            cmd += ['-af', 'aresample=resampler=soxr:precision=28']
        cmd += ['-ar', str(_HLS_FS), '-sample_fmt', sample_fmt, '-bits_per_raw_sample', '24',
                '-c:a', encoder, '-frame_size', str(_HLS_FRAME),
                '-f', 'hls', '-hls_time', str(_HLS_SEGUNDOS), '-hls_list_size', '0',
                '-hls_playlist_type', 'event', '-hls_segment_type', 'fmp4',
                '-hls_fmp4_init_filename', 'init.mp4',
                '-hls_segment_filename', os.path.join(d, 'seg%05d.m4s'),
                '-hls_flags', 'temp_file', playlist]
        log = open(os.path.join(d, 'ffmpeg.log'), 'wb')
        _hls_jobs[d] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log)
        app.logger.info(f"[hls] conversion iniciada: {os.path.basename(absolute_path)} ({codec})")
        return d


def _hls_duracion(absolute_path):
    """Duracion de la pista, en cache. DSF/DFF: por el conteo de muestras de su
    cabecera — ffprobe la estima por el tamaño del archivo y cuenta la carátula
    incrustada (segundos de más): la playlist prevista anunciaba segmentos
    finales que nunca existían (404 al final de la pista)."""
    try:
        clave = (absolute_path, os.path.getmtime(absolute_path))
    except OSError:
        return None
    if clave not in _hls_duraciones and absolute_path.lower().endswith(('.dsf', '.dff')):
        try:
            from ingesta import dsd as _dsd
            i = _dsd.info(absolute_path)
            if i.get('muestras') and i.get('fs'):
                _hls_duraciones[clave] = i['muestras'] / i['fs']
        except Exception:
            pass
    if clave not in _hls_duraciones:
        try:
            r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                                '-of', 'csv=p=0', absolute_path], capture_output=True, timeout=20)
            _hls_duraciones[clave] = float(r.stdout.decode().strip())
        except Exception:
            return None
    return _hls_duraciones[clave]


def _hls_playlist_prevista(duracion):
    """Ticket D-17: playlist VOD COMPLETA calculada de antemano. Con frames
    fijos de _HLS_FRAME muestras, ffmpeg corta el segmento k en el primer
    frame >= k*_HLS_SEGUNDOS: todas las duraciones salen exactas salvo la
    ultima (< 0.02 s de diferencia, verificado con DSD64/128, ALAC y FLAC).
    Un ultimo segmento de menos de 2 frames se descarta (podria no existir).
    Devuelve [(nombre, segundos), ...]."""
    total = math.ceil(duracion * _HLS_FS / _HLS_FRAME)
    segs, a, k = [], 0, 1
    while a < total:
        b = min(math.ceil(_HLS_SEGUNDOS * k * _HLS_FS / _HLS_FRAME), total)
        segs.append([f'seg{k - 1:05d}.m4s', (b - a) * _HLS_FRAME / _HLS_FS])
        a, k = b, k + 1
    if len(segs) > 1 and segs[-1][1] < 2 * _HLS_FRAME / _HLS_FS:
        segs.pop()
    return segs


@app.route('/api/v1/hls/<int:track_id>/index.m3u8')
def api_v1_hls_playlist(track_id):
    user_id, token, err = _hls_auth()
    if err:
        return err
    codec = request.args.get('c', 'flac')
    if codec not in _HLS_CODECS:
        return jsonify({'error': 'codec_no_soportado'}), 400
    absolute_path = _hls_ruta_pista(track_id)
    if not absolute_path:
        return jsonify({'error': 'file_not_found'}), 404

    d = _hls_asegurar(absolute_path, codec)
    playlist = os.path.join(d, 'index.m3u8')
    sufijo = f'?token={quote(token, safe="")}&c={codec}'

    def _fallo_ffmpeg():
        job = _hls_jobs.get(d)
        return job is not None and job.poll() is not None and not os.path.isfile(os.path.join(d, 'init.mp4'))

    texto = ''
    if os.path.isfile(playlist):
        with open(playlist, encoding='utf-8', errors='ignore') as f:
            texto = f.read()
    duracion = None if '#EXT-X-ENDLIST' in texto else _hls_duracion(absolute_path)

    if duracion:
        # Ticket D-17: conversion en curso -> playlist VOD COMPLETA prevista
        # (duracion real, seek a cualquier punto). Antes se entregaba la
        # playlist parcial "en vivo" de ffmpeg: el reproductor veia solo lo
        # ya convertido (Niko: "duracion 12 s") y se trababa esperando mas.
        # Los segmentos que aun no existen los espera api_v1_hls_segmento.
        fin = time.monotonic() + 30
        while not os.path.isfile(os.path.join(d, 'init.mp4')) and time.monotonic() < fin:
            if _fallo_ffmpeg():
                app.logger.error(f"[hls] track={track_id}: ffmpeg fallo al arrancar")
                return jsonify({'error': 'transcode_failed'}), 500
            time.sleep(0.2)
        segs = _hls_playlist_prevista(duracion)
        lineas = ['#EXTM3U', '#EXT-X-VERSION:7',
                  f'#EXT-X-TARGETDURATION:{max(round(x) for _, x in segs)}',
                  '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-PLAYLIST-TYPE:VOD',
                  '#EXT-X-INDEPENDENT-SEGMENTS',
                  f'#EXT-X-MAP:URI="init.mp4{sufijo}"']
        for nombre, segundos in segs:
            lineas += [f'#EXTINF:{segundos:.6f},', nombre + sufijo]
        lineas.append('#EXT-X-ENDLIST')
    else:
        # Conversion ya terminada (o sin duracion conocida): la playlist real
        # de ffmpeg. Sin ENDLIST todavia, esperar al menos un segmento.
        fin = time.monotonic() + 60
        while '#EXTINF' not in texto and time.monotonic() < fin:
            if _fallo_ffmpeg():
                return jsonify({'error': 'transcode_failed'}), 500
            time.sleep(0.25)
            if os.path.isfile(playlist):
                with open(playlist, encoding='utf-8', errors='ignore') as f:
                    texto = f.read()
        if '#EXTINF' not in texto:
            return jsonify({'error': 'transcode_timeout'}), 504
        lineas = []
        for linea in texto.splitlines():
            if linea.startswith('#EXT-X-MAP:'):
                linea = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{m.group(1)}{sufijo}"', linea)
            elif linea and not linea.startswith('#'):
                linea = linea + sufijo
            lineas.append(linea)
    resp = Response('\n'.join(lineas) + '\n', mimetype='application/vnd.apple.mpegurl')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/api/v1/hls/<int:track_id>/<nombre>')
def api_v1_hls_segmento(track_id, nombre):
    if not re.fullmatch(r'init\.mp4|seg\d{5}\.m4s', nombre):
        return jsonify({'error': 'not_found'}), 404
    user_id, token, err = _hls_auth()
    if err:
        return err
    codec = request.args.get('c', 'flac')
    if codec not in _HLS_CODECS:
        return jsonify({'error': 'codec_no_soportado'}), 400
    absolute_path = _hls_ruta_pista(track_id)
    if not absolute_path:
        return jsonify({'error': 'file_not_found'}), 404
    d = _hls_dir(absolute_path, codec)
    ruta = os.path.join(d, nombre)
    # Ticket D-17: con la playlist prevista, un segmento puede no existir
    # todavia (conversion en curso, o seek muy adelante): se espera.
    fin = time.monotonic() + 90
    while not os.path.isfile(ruta) and time.monotonic() < fin:
        job = _hls_jobs.get(d)
        if job is None or job.poll() is not None:
            break
        time.sleep(0.25)
    if not os.path.isfile(ruta):
        return jsonify({'error': 'segment_not_found'}), 404
    return send_file(ruta, mimetype='audio/mp4', max_age=3600)


@app.route('/api/v1/alexa/stream/<int:track_id>')
def api_v1_alexa_stream(track_id):
    """Ticket 42 — streaming para la skill de Alexa. Aislado a propósito de
    /api/v1/stream y /api/v1/stream-pcm (mismo criterio que esos dos: no
    tocar lo que ya sirve a iOS/Android). El dispositivo Echo reproduce la
    URL directamente vía la directiva AudioPlayer.Play de Alexa — no puede
    mandar headers custom, así que, igual que /api/v1/stream, el token
    también se acepta como ?token=... en la URL.

    AudioPlayer no soporta FLAC/DSD ni sample rates altos (restricción
    confirmada — ver Ticket 42) — se transcodea a MP3 192kbps/44.1kHz, el
    formato más simple de la lista soportada, sin importar la fuente
    original (la calidad no es un objetivo en esta ruta puntual). Igual que
    /api/v1/stream-pcm: transcodeo COMPLETO a un caché en disco antes de
    servir nada (nunca en vivo), mismo criterio que evitó los cortes de
    reproducción en ese endpoint."""
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[len('Bearer '):] if auth_header.startswith('Bearer ') else request.args.get('token')
    if not token:
        app.logger.warning(f"[api/v1/alexa/stream] track={track_id}: sin token (ni header ni query)")
        return jsonify({'error': 'not_authenticated'}), 401
    user_id = _alexa_user_id_de_token(token)   # Ticket 42b: clave de Alexa o de sesión
    if user_id is None:
        app.logger.warning(f"[api/v1/alexa/stream] track={track_id}: token inválido")
        return jsonify({'error': 'invalid_token'}), 401

    conn = get_db_connection()
    try:
        user = conn.execute('SELECT is_approved FROM users WHERE id=?', (user_id,)).fetchone()
        if not user or not user['is_approved']:
            app.logger.warning(f"[api/v1/alexa/stream] track={track_id}: user={user_id} no aprobado o inexistente")
            return jsonify({'error': 'not_authenticated'}), 401
        row = conn.execute('SELECT file_path FROM tracks WHERE id=?', (track_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        app.logger.warning(f"[api/v1/alexa/stream] track={track_id}: no existe en la DB")
        return jsonify({'error': 'track_not_found'}), 404

    # Mismo criterio de limpieza de path que /api/v1/stream y /api/v1/stream-pcm.
    relative_path = clean_db_path(row['file_path']).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if relative_path.startswith(root + '/'):
        relative_path = relative_path[len(root) + 1:]
    elif relative_path.startswith(root):
        relative_path = relative_path[len(root):]
    absolute_path = os.path.join(MUSIC_ROOT, relative_path)
    if not os.path.isfile(absolute_path):
        app.logger.warning(f"[api/v1/alexa/stream] track={track_id}: archivo no encontrado en disco: {absolute_path}")
        return jsonify({'error': 'file_not_found'}), 404

    _alexa_cache_cleanup()
    cache_path = _alexa_cache_path(absolute_path)
    cache_hit  = os.path.isfile(cache_path)

    if not cache_hit:
        tmp_path = f'{cache_path}.{os.getpid()}.tmp'
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', absolute_path,
            '-vn',                 # sin portada embebida
            '-ar', '44100',        # AudioPlayer no necesita ni soporta hi-res
            '-ac', '2',
            '-c:a', 'libmp3lame',
            '-b:a', '192k',
            '-f', 'mp3',
            tmp_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0 or not os.path.isfile(tmp_path):
                app.logger.error(
                    f"[api/v1/alexa/stream] ffmpeg failed for {absolute_path}: "
                    f"{result.stderr.decode(errors='replace')[:500]}"
                )
                return jsonify({'error': 'transcode_failed'}), 500
            os.replace(tmp_path, cache_path)
        finally:
            if os.path.isfile(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass

    app.logger.info(
        f"[api/v1/alexa/stream] track={track_id}: sirviendo {os.path.basename(cache_path)} "
        f"(cache={'hit' if cache_hit else 'miss'}, range={request.headers.get('Range', '-')})",
        extra={'track_id': track_id, 'user_id': user_id}
    )
    resp = _serve_audio(cache_path)
    resp.headers['Content-Type']      = 'audio/mpeg'
    resp.headers['X-Transcode-Cache'] = 'hit' if cache_hit else 'miss'
    return resp

@app.route('/api/v1/alexa/track/<int:track_id>')
def api_v1_alexa_track(track_id):
    """Ticket 42, Lote 10 — lookup mínimo (título/artista/álbum/portada)
    para que Next/Previous/Resume/el encolado automático de la skill de
    Alexa puedan mostrar la metadata real de CUALQUIER pista, no solo la
    primera de cada pedido (esas sí la tienen a mano desde la respuesta
    de /api/v1/ai/playlist). Aislado a propósito de /api/v1/track/<id>
    (ese es de uso general de la app; este es puntual para Alexa, mismo
    criterio que /api/v1/alexa/stream). Consulta simple, sin Gemini de
    por medio — pensado para responder en milisegundos. Mismo patrón de
    auth que /api/v1/alexa/stream: token por header o por query, así la
    Lambda puede usarlo igual sin importar cuál elija."""
    auth_header = request.headers.get('Authorization', '')
    token = auth_header[len('Bearer '):] if auth_header.startswith('Bearer ') else request.args.get('token')
    if not token:
        return jsonify({'error': 'not_authenticated'}), 401
    user_id = _alexa_user_id_de_token(token)   # Ticket 42b: clave de Alexa o de sesión
    if user_id is None:
        return jsonify({'error': 'invalid_token'}), 401

    conn = get_db_connection()
    try:
        user = conn.execute('SELECT is_approved FROM users WHERE id=?', (user_id,)).fetchone()
        if not user or not user['is_approved']:
            return jsonify({'error': 'not_authenticated'}), 401
        t = conn.execute('''
            SELECT t.id, t.title, ar.name as artist_name, a.name as album_name, a.cover_path
            FROM tracks t
            LEFT JOIN albums a ON t.album_id=a.id
            LEFT JOIN artists ar ON a.artist_id=ar.id
            WHERE t.id=?
        ''', (track_id,)).fetchone()
    finally:
        conn.close()
    if not t:
        return jsonify({'error': 'track_not_found'}), 404
    return jsonify({
        'id': t['id'],
        'title': t['title'],
        'artist_name': t['artist_name'],
        'album_name': t['album_name'],
        'cover_url': cover_url_filter(t['cover_path']),
    })

@app.route('/api/v1/albums')
@api_login_required
def api_v1_albums():
    """Listado paginado de álbumes para la pantalla Home del cliente nativo.
    Reusa cover_url_filter (la misma función que ya usan los templates de la
    PWA) — así el cliente nativo y la web resuelven portadas exactamente
    igual, sin duplicar lógica de encoding de rutas."""
    filter_type  = request.args.get('filter_type')
    filter_value = request.args.get('filter_value')

    if filter_type and filter_value:
        # Modo filtrado: reusa exactamente la misma lógica que /genre/,
        # /mood/, /led/, etc. de la web, para que los resultados coincidan.
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            page = 1
        sort = request.args.get('sort', 'popularidad')
        dir_ = request.args.get('dir', 'desc')
        conn = get_db_connection()
        try:
            page_size = _settings_payload(g.api_user['id'])['search_page_size']
            albums_raw, total, total_pages = _api_v1_filtered_albums(conn, filter_type, filter_value, page, sort, dir_, page_size=page_size)
        finally:
            conn.close()
        albums = [{
            'id': a['id'], 'name': a['name'], 'year': a.get('year'),
            'track_count': a.get('track_count'), 'total_duration': a.get('total_duration'),
            'artist_id': a.get('artist_id'), 'artist_name': a.get('artist_name'),
            'cover_url': cover_url_filter(a.get('cover_path')),
        } for a in albums_raw]
        return jsonify({'albums': albums, 'total': total, 'page': page, 'total_pages': total_pages})

    try:
        limit = max(1, min(int(request.args.get('limit', 50)), 200))
    except ValueError:
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        offset = 0

    conn = get_db_connection()
    try:
        rows = conn.execute(
            '''SELECT al.id, al.name, al.year, al.cover_path, al.track_count,
                      al.total_duration, ar.id as artist_id, ar.name as artist_name
               FROM albums al
               LEFT JOIN artists ar ON ar.id = al.artist_id
               ORDER BY al.created_at DESC
               LIMIT ? OFFSET ?''',
            (limit, offset)
        ).fetchall()
        total = conn.execute('SELECT COUNT(*) as c FROM albums').fetchone()['c']
    finally:
        conn.close()

    albums = []
    for r in rows:
        d = dict(r)
        albums.append({
            'id':             d['id'],
            'name':           d['name'],
            'year':           d['year'],
            'track_count':    d['track_count'],
            'total_duration': d['total_duration'],
            'artist_id':      d['artist_id'],
            'artist_name':    d['artist_name'],
            'cover_url':      cover_url_filter(d['cover_path']),
        })
    return jsonify({'albums': albums, 'total': total, 'limit': limit, 'offset': offset})

@app.route('/api/v1/search')
@api_login_required
def api_v1_search():
    """Espejo nativo de /search (web) — Ticket 08, Lote A §4.1. Calca las
    mismas tres queries (artistas/álbumes/pistas por LIKE) tal cual, no
    reinventa el criterio de búsqueda. Protegido por token en vez de cookie
    de sesión, como el resto de /api/v1/*.

    A diferencia de /search, esta ruta nunca redirige a home() cuando la
    query viene vacía (no tiene sentido en una API JSON) — devuelve listas
    vacías en su lugar, así el cliente nativo no necesita distinguir ese
    caso especial.

    Enriquecido respecto de /search para consumo nativo: cover_url en vez
    de cover_path crudo, stream_url por pista (?token=... vía
    /api/v1/stream, ver esa ruta) en vez de audio_url (que depende de la
    cookie de sesión y no sirve acá), y un JOIN adicional a artists en la
    query de pistas para exponer artist_name — no cambia qué filas matchean
    el WHERE (misma cardinalidad, JOIN por primary key), solo agrega una
    columna de salida."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'artists': [], 'albums': [], 'tracks': [], 'query': query})
    conn = get_db_connection()
    try:
        like = f'%{query}%'
        artists = conn.execute(
            '''SELECT a.id, a.name, a.nationality, a.letter,
                      a.lastfm_listeners,
                      COUNT(DISTINCT al.id)                          AS album_count,
                      COALESCE(MAX(apc.pop_score), 0)                AS pop_score,
                      (SELECT t.genre FROM tracks t JOIN albums al2 ON t.album_id=al2.id
                       WHERE al2.artist_id=a.id AND t.genre IS NOT NULL AND t.genre != ""
                       GROUP BY t.genre ORDER BY COUNT(*) DESC LIMIT 1) AS most_common_genre
               FROM artists a
               LEFT JOIN albums al  ON al.artist_id=a.id
               LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
               WHERE a.name LIKE ?
                 AND EXISTS (SELECT 1 FROM albums al2 WHERE al2.artist_id=a.id)
               GROUP BY a.id
               ORDER BY a.name LIMIT 10''',
            (like,)
        ).fetchall()
        albums = conn.execute(
            '''SELECT al.id, al.name, al.cover_path, al.primary_format, al.year,
                      al.track_count, al.total_duration, al.artist_id, ar.name as artist_name,
                      (SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END FROM tracks WHERE album_id=al.id
                       ORDER BY CASE led_color
                         WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                         WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                         ELSE 6 END, (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) LIMIT 1) as album_led
               FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
               WHERE al.name LIKE ? OR ar.name LIKE ? ORDER BY al.name LIMIT 20''',
            (like, like)
        ).fetchall()
        tracks = conn.execute(
            '''SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.dsd_rate,
                      t.bit_depth, t.is_mqa, t.file_path,
                      t.codec, t.bitrate, t.compression_mode, t.duration, t.sample_rate_real,
                      a.id as album_id, a.name as album_name, a.cover_path,
                      ar.id as artist_id, ar.name as artist_name,
                      tm.mood as meta_mood, tm.momento as meta_momento, tm.tier as meta_tier
               FROM tracks t
               LEFT JOIN albums a ON t.album_id=a.id
               LEFT JOIN artists ar ON ar.id=a.artist_id
               LEFT JOIN track_meta tm ON tm.track_id=t.id
               WHERE t.title LIKE ? OR t.artist LIKE ? OR t.genre LIKE ?
               ORDER BY t.title LIMIT 50''',
            (like, like, like)
        ).fetchall()

        artists_out = [dict(a) for a in artists]
        # Ticket 39 (barrido preventivo, mismo bug de api_v1_artist_detail):
        # este endpoint tambien expone a.lastfm_listeners crudo desde
        # SQLite -- normalizamos por si algun cliente futuro (o una lista
        # de busqueda que muestre oyentes) llega a tipar este campo
        # estrictamente como Int, igual que le paso al detalle de artista.
        for _a in artists_out:
            _a['lastfm_listeners'] = _normalize_lastfm_count(_a.get('lastfm_listeners'))

        albums_out = []
        for a in albums:
            d = dict(a)
            d['cover_url'] = cover_url_filter(d.pop('cover_path'))
            albums_out.append(d)

        tracks_out = []
        for t in tracks:
            d = dict(t)
            fmt, led = _fmt_format(d)
            d['format_display'] = fmt
            d['format_color']   = led
            d['duration_fmt']   = _fmt_seconds(d.get('duration'))
            d['cover_url']      = cover_url_filter(d.pop('cover_path'))
            d['stream_url']     = f'/api/v1/stream/{d["id"]}'
            d['container_ext']  = _container_ext(d)
            d.pop('file_path', None)
            tracks_out.append(d)

        return jsonify({'artists': artists_out, 'albums': albums_out, 'tracks': tracks_out, 'query': query})
    finally:
        conn.close()

@app.route('/api/v1/search/advanced/options')
@api_login_required
def api_v1_search_advanced_options():
    """Opciones de filtro para la pantalla de Búsqueda Avanzada nativa —
    mismos datos que ya arma _advanced_search_options() para
    _advanced_search_modal.html (web), más las constantes de Calidad/
    Popularidad/Energía/Bailabilidad que la web tiene hardcodeadas en el
    template (QUALITY_OPTIONS, POP_BUCKETS, ENERGY_BUCKETS, BAIL_BUCKETS),
    para que el nativo no tenga que duplicarlas a mano.

    Deliberadamente separado de /api/v1/home/facets: éste trae las listas
    SIN LIMIT (adv_genres_primary, all_genres, available_years,
    nationalities) que Home no necesita en cada carga — agrandarían ese
    payload sin motivo. Se pide una sola vez al abrir la pantalla de
    Búsqueda Avanzada, no en cada apertura de Home (Ticket 08, Lote A §4.2).

    Decisión técnica a validar con el PO: género se expone en dos listas
    separadas — genres_primary (adv_genres_primary, RichMetaPro, sin
    límite) y genres_classic (all_genres, tracks.genre clásico, sin
    límite) — porque _build_adv_filters matchea el parámetro `genero`
    contra AMBAS fuentes a la vez (am.genre_primary/genre_secondary o
    t.genre/tm.genre_primary/genre_secondary, ver esa función). Cómo
    fusionar o presentar ambas listas en un solo picker queda para el
    Lote D (nativo) — ver §7 del ticket, decisión de diseño delegada."""
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
    finally:
        conn.close()
    return jsonify({
        'moods':            [{'value': v, 'count': c} for v, c in opts['moods']],
        'momentos':         [{'value': v, 'count': c} for v, c in opts['momentos']],
        'eras':             [{'value': v, 'count': c} for v, c in opts['eras']],
        'temas':            [{'value': v, 'count': c} for v, c in opts['temas']],
        'genres_primary':   [{'value': v, 'count': c} for v, c in opts['adv_genres_primary']],
        'genres_classic':   [{'value': v, 'count': c} for v, c in opts['all_genres']],
        'languages':        [{'value': v, 'count': c} for v, c in opts['languages']],
        'available_years':  opts['available_years'],
        'nationalities':    [{'value': v, 'count': c} for v, c in opts['nationalities']],
        'quality_options':      QUALITY_OPTIONS,
        'popularity_buckets':   sorted(POP_BUCKETS.keys()),
        'energy_buckets':       list(ENERGY_BUCKETS.keys()),
        'danceability_buckets': list(BAIL_BUCKETS.keys()),
    })

@app.route('/api/v1/ai/playlist', methods=['POST'])
@api_login_required
def api_v1_ai_playlist():
    """Ticket AI-01, primera ola del epic "Agente de IA" (Etapas 3+4) —
    recibe una petición en lenguaje natural y devuelve una playlist.

    Endpoint completamente nuevo y aislado: ai_playlist.handle_request()
    recibe track_to_json/_build_adv_filters/_track_dedupe_condition como
    parámetros (inyección explícita) en vez de que el módulo nuevo importe
    app.py — evita import circular y deja clarísimo, con solo mirar la
    firma, que ai_playlist.py únicamente LEE esas funciones, nunca las
    modifica (ver AI_AGENT_MASTER_PLAN.md §3, principio de cero regresión).

    Body esperado: {"query": "texto en lenguaje natural"}. Nunca devuelve
    una playlist vacía si hay al menos una pista en la biblioteca — ver
    ai_playlist.generate_playlist() para el algoritmo de relajación
    progresiva que lo garantiza.
    """
    data = request.get_json(silent=True) or {}
    raw_query = (data.get('query') or '').strip()
    if not raw_query:
        return jsonify({'error': 'missing_query'}), 400
    # Ticket AI-07 (Etapa 6) — turno de seguimiento de una conversación:
    # el cliente manda las 'entities' del turno anterior tal cual las
    # recibió, sin tocarlas. dict/None nada más — si viene otra cosa
    # (ej. un valor corrupto de un cliente viejo) se ignora en vez de
    # romper el turno nuevo.
    prior_entities = data.get('prior_entities')
    if not isinstance(prior_entities, dict):
        prior_entities = None
    # Ticket 42, Lote 4 (Alexa): campo nuevo y opcional, ignorado por
    # completo si no viene (iOS/Android nunca lo mandan, cero cambio de
    # comportamiento para ellos). Whitelist explícita — nunca se reenvía
    # a Gemini un valor arbitrario que venga en el body.
    thinking_level = data.get('thinking_level')
    if thinking_level not in ('minimal', 'low', 'medium', 'high'):
        thinking_level = None
    # Ticket 42, Lote 9 (Alexa): mismo criterio — whitelist explícita,
    # None si no viene o viene corrupto, cero cambio para iOS/Android.
    provider_order = data.get('provider_order')
    if (not isinstance(provider_order, list)
            or set(provider_order) - {'gemini', 'groq'}
            or len(provider_order) != len(set(provider_order))):
        provider_order = None
    conn = get_db_connection()
    try:
        # Ticket 26, Categoría B: default/tope por usuario en vez de los
        # constantes fijos de siempre — _settings_payload ya hace el merge
        # sobre DEFAULT_USER_SETTINGS, así que un usuario que nunca tocó
        # Configuración obtiene exactamente los mismos valores que antes
        # de este ticket (25/100).
        user_settings = _settings_payload(g.api_user['id'])
        result = ai_playlist.handle_request(
            conn, g.api_user['id'], raw_query,
            track_to_json_fn=track_to_json,
            build_adv_filters_fn=_build_adv_filters,
            dedupe_condition_fn=_track_dedupe_condition,
            build_similar_artists_fn=build_similar_artists,
            # Ticket AI-30: sugerencias de Gemini validadas contra la
            # biblioteca — normalize_title_fn agrupa versiones de un
            # mismo tema (estudio/vivo/remaster, Ticket 14) y
            # led_quality_rank_fn puntúa cuál de esas versiones ganar.
            normalize_title_fn=_normalize_title_for_radio_dedupe,
            led_quality_rank_fn=led_quality_rank,
            prior_entities=prior_entities,
            default_results=user_settings['orbitron_default_results'],
            max_top_n=user_settings['orbitron_max_top_n'],
            thinking_level=thinking_level,
            provider_order=provider_order,
        )
        return jsonify(result)
    finally:
        conn.close()

@app.route('/api/v1/ai/playlist/<int:request_id>/expand', methods=['POST'])
@api_login_required
def api_v1_ai_playlist_expand(request_id):
    """Ticket AI-27 (pedido por Niko) — botón "Expandir": sirve la
    siguiente tanda del pool que ya se resolvió y guardó al armar la
    respuesta original (ai_playlist_pagination.store_pool, llamado desde
    ai_playlist.handle_request) — no vuelve a consultar la base con
    ranking/cascada/dedupe, solo lee qué pistas siguen en la lista ya
    guardada. Reemplaza al Ticket AI-25 (que re-consultaba con una lista
    creciente de qué excluir, tocando 6 funciones compartidas con el
    resto de la app para poder hacerlo).
    """
    conn = get_db_connection()
    try:
        tracks = ai_playlist_pagination.next_page(
            conn, g.api_user['id'], request_id, track_to_json_fn=track_to_json,
        )
        if tracks is None:
            return jsonify({'error': 'not_found'}), 404
        # Fix (Ticket 39, reportado por Niko: "Expandir" no traia nada):
        # OrbyteAiPlaylistExpandResult.usedFallback (Swift) y su
        # equivalente Kotlin declaran `Bool`/`Boolean` NO opcional -- esta
        # respuesta nunca incluia used_fallback, asi que el decode del
        # JSON entero fallaba SIEMPRE (100% de los casos, confirmado en
        # el log de iOS: "AIPlaylistView: Expandir fallo - decoding"),
        # independientemente del tamano del pool o el ranking. Paginar
        # nunca vuelve a llamar al LLM ni re-evalua fallback (ver
        # next_page()/ai_playlist_pagination.py) -- el valor es
        # constante False a proposito, no hace falta leerlo de la fila
        # original.
        return jsonify({
            'request_id': request_id,
            'tracks': tracks,
            'track_count': len(tracks),
            'used_fallback': False,
        })
    finally:
        conn.close()

@app.route('/api/v1/behavior/listen', methods=['POST'])
@api_login_required
def api_v1_behavior_listen():
    """Ticket AI-02 (Etapa 1, backend) — logging de un intento de escucha ya
    terminado (skip, pausa larga, fin de pista o cambio de pista). El
    cliente lo dispara una sola vez por track_id, no en cada tick de
    reproducción. Ver behavior_engine.log_listen — idempotente vía
    client_event_id."""
    data = request.get_json(silent=True) or {}
    client_event_id = (data.get('client_event_id') or '').strip()
    track_id = data.get('track_id')
    if not client_event_id or not track_id:
        return jsonify({'error': 'missing_fields'}), 400
    conn = get_db_connection()
    try:
        logged = behavior_engine.log_listen(
            conn, g.api_user['id'], client_event_id, track_id,
            data.get('duration_played_seconds'), data.get('completed'), data.get('source'),
        )
        return jsonify({'logged': logged})
    finally:
        conn.close()

@app.route('/api/v1/behavior/search', methods=['POST'])
@api_login_required
def api_v1_behavior_search():
    """Ticket AI-02 (Etapa 1, backend) — logging de una búsqueda realizada
    por el usuario. Decoupled de las rutas de búsqueda reales (/api/search*,
    /api/v1/search*) a propósito: el cliente llama a esto aparte, después de
    recibir sus resultados, así ninguna ruta de búsqueda existente cambia de
    comportamiento (ver AI_AGENT_MASTER_PLAN.md §3). Idempotente vía
    client_event_id."""
    data = request.get_json(silent=True) or {}
    client_event_id = (data.get('client_event_id') or '').strip()
    query_text = (data.get('query_text') or '').strip()
    if not client_event_id or not query_text:
        return jsonify({'error': 'missing_fields'}), 400
    conn = get_db_connection()
    try:
        logged = behavior_engine.log_search(
            conn, g.api_user['id'], client_event_id, query_text, data.get('result_count'),
        )
        return jsonify({'logged': logged})
    finally:
        conn.close()

@app.route('/api/v1/behavior/session/start', methods=['POST'])
@api_login_required
def api_v1_behavior_session_start():
    """Ticket AI-02 (Etapa 1, backend) — inicio de sesión de uso. Idempotente
    vía client_session_id (UNIQUE user_id+client_session_id)."""
    data = request.get_json(silent=True) or {}
    client_session_id = (data.get('client_session_id') or '').strip()
    if not client_session_id:
        return jsonify({'error': 'missing_fields'}), 400
    conn = get_db_connection()
    try:
        started = behavior_engine.start_session(conn, g.api_user['id'], client_session_id, data.get('device'))
        return jsonify({'started': started})
    finally:
        conn.close()

@app.route('/api/v1/behavior/session/end', methods=['POST'])
@api_login_required
def api_v1_behavior_session_end():
    """Ticket AI-02 (Etapa 1, backend) — cierre de sesión de uso. Idempotente:
    solo pisa ended_at si todavía era NULL, ver behavior_engine.end_session."""
    data = request.get_json(silent=True) or {}
    client_session_id = (data.get('client_session_id') or '').strip()
    if not client_session_id:
        return jsonify({'error': 'missing_fields'}), 400
    conn = get_db_connection()
    try:
        ended = behavior_engine.end_session(conn, g.api_user['id'], client_session_id)
        return jsonify({'ended': ended})
    finally:
        conn.close()

# ── Web: las mismas escuchas y sesiones que envían las apps, con la sesión del navegador ──

@app.route('/api/behavior/listen', methods=['POST'])
@login_required
def api_web_behavior_listen():
    data = request.get_json(silent=True, force=True) or {}
    client_event_id = (data.get('client_event_id') or '').strip()
    track_id = data.get('track_id')
    if not client_event_id or not track_id:
        return jsonify({'error': 'missing_fields'}), 400
    conn = get_db_connection()
    try:
        logged = behavior_engine.log_listen(conn, session['user_id'], client_event_id, track_id,
                                            data.get('duration_played_seconds'), data.get('completed'),
                                            data.get('source') or 'web')
        return jsonify({'logged': logged})
    finally:
        conn.close()


@app.route('/api/behavior/session/start', methods=['POST'])
@login_required
def api_web_behavior_session_start():
    data = request.get_json(silent=True, force=True) or {}
    sid = (data.get('client_session_id') or '').strip()
    if not sid:
        return jsonify({'error': 'missing_fields'}), 400
    conn = get_db_connection()
    try:
        device = data.get('device') or _parse_device(request.headers.get('User-Agent', ''))
        return jsonify({'started': behavior_engine.start_session(conn, session['user_id'], sid, device)})
    finally:
        conn.close()


@app.route('/api/behavior/session/end', methods=['POST'])
@login_required
def api_web_behavior_session_end():
    data = request.get_json(silent=True, force=True) or {}
    sid = (data.get('client_session_id') or '').strip()
    if not sid:
        return jsonify({'error': 'missing_fields'}), 400
    conn = get_db_connection()
    try:
        return jsonify({'ended': behavior_engine.end_session(conn, session['user_id'], sid)})
    finally:
        conn.close()


# ── Estadísticas de uso (solo administrador): web y /api/v1 para las apps ──

def _dias_param():
    try:
        d = int(request.args.get('dias', 30))
    except ValueError:
        d = 30
    return d if d in (0, 7, 30, 90, 365) else 30


def _estadisticas_json(user_id=None):
    conn = get_db_connection()
    try:
        if user_id is None:
            return jsonify(estadisticas.general(conn, _dias_param(), _lossy_sql('t')))
        r = estadisticas.usuario(conn, user_id, _dias_param(), _lossy_sql('t'))
        return (jsonify(r), 200) if r else (jsonify({'error': 'not_found'}), 404)
    finally:
        conn.close()


@app.route('/admin/estadisticas')
@admin_required
def admin_estadisticas():
    return render_template('admin_estadisticas.html')


@app.route('/admin/estadisticas/datos')
@admin_required
def admin_estadisticas_datos():
    return _estadisticas_json()


@app.route('/admin/estadisticas/usuario/<int:user_id>')
@admin_required
def admin_estadisticas_usuario(user_id):
    return _estadisticas_json(user_id)


@app.route('/api/v1/admin/estadisticas')
@api_admin_required
def api_v1_admin_estadisticas():
    return _estadisticas_json()


@app.route('/api/v1/admin/estadisticas/usuario/<int:user_id>')
@api_admin_required
def api_v1_admin_estadisticas_usuario(user_id):
    return _estadisticas_json(user_id)


def _resumen_ia_json():
    """?dias=&usuario=<id>&nuevo=1 — el resumen IA de la vista general o de un usuario."""
    dias = _dias_param()
    try:
        uid = int(request.args.get('usuario') or 0)
    except ValueError:
        uid = 0
    conn = get_db_connection()
    try:
        datos = (estadisticas.usuario(conn, uid, dias, _lossy_sql('t')) if uid
                 else estadisticas.general(conn, dias, _lossy_sql('t')))
    finally:
        conn.close()
    if not datos:
        return jsonify({'error': 'not_found'}), 404
    r = estadisticas_ia.resumir(datos, 'usuario' if uid else 'general', dias,
                                ('usuario' if uid else 'general', uid, dias), nuevo=request.args.get('nuevo') == '1')
    return jsonify(r), (502 if r.get('error') else 200)


@app.route('/admin/estadisticas/resumen-ia')
@admin_required
def admin_estadisticas_resumen_ia():
    return _resumen_ia_json()


@app.route('/api/v1/admin/estadisticas/resumen-ia')
@api_admin_required
def api_v1_admin_estadisticas_resumen_ia():
    return _resumen_ia_json()


@app.route('/api/v1/ai/playlist/<int:request_id>/feedback', methods=['POST'])
@api_login_required
def api_v1_ai_playlist_feedback(request_id):
    """Ticket AI-05 (Etapa 7, mitad backend) — feedback sobre una playlist
    ya generada por el agente de IA: rating (1-5) y/o comentario opcional,
    y registro opcional de a qué playlist real quedó guardada. Guardar en
    sí NO es este endpoint — reusa /api/v1/playlists + /api/v1/playlists/
    <id>/tracks ya existentes (Ticket 10+); acá solo se anota el
    resultado, para no reinventar la creación de playlists."""
    data = request.get_json(silent=True) or {}
    rating = data.get('rating')
    comment = data.get('comment')
    saved_playlist_id = data.get('saved_playlist_id')
    if rating is None and comment is None and saved_playlist_id is None:
        return jsonify({'error': 'missing_fields'}), 400
    if rating is not None and not (isinstance(rating, int) and 1 <= rating <= 5):
        return jsonify({'error': 'invalid_rating'}), 400
    conn = get_db_connection()
    try:
        updated = ai_playlist.record_feedback(
            conn, g.api_user['id'], request_id, rating, comment, saved_playlist_id
        )
        if not updated:
            return jsonify({'error': 'not_found'}), 404
        return jsonify({'updated': True})
    finally:
        conn.close()

@app.route('/api/heartbeat', methods=['POST'])
@login_required
def api_heartbeat():
    _touch_user_activity(session['user_id'])
    return jsonify({'status': 'ok'})

@app.route('/admin')
@admin_required
def admin_dashboard():
    return redirect(url_for('admin_users'))

@app.route('/admin/usuarios')
@admin_required
def admin_users():
    conn = get_db_connection()
    try:
        rows  = conn.execute('SELECT * FROM users ORDER BY is_approved ASC, created_at DESC').fetchall()
        users = [dict(r) for r in rows]
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=ONLINE_WINDOW_MINUTES)).isoformat()
        for u in users:
            u['online'] = bool(u['last_seen'] and u['last_seen'] >= cutoff)
        pending  = [u for u in users if not u['is_approved']]
        approved = [u for u in users if u['is_approved']]
        response = app.make_response(render_template(
            'admin_users.html', pending=pending, approved=approved,
            online_count=sum(1 for u in users if u['online']),
            admin_email=ADMIN_EMAIL
        ))
        # No-store: esta vista no debe quedar en ningún caché (navegador, SW,
        # proxy) — el estado en línea / última conexión tiene que ser siempre
        # el actual, nunca una copia vieja servida desde caché.
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        return response
    finally:
        conn.close()

@app.route('/admin/usuarios/estado')
@admin_required
def admin_users_estado():
    """JSON liviano para refrescar en vivo el estado (en línea / última
    conexión / dispositivo) del panel admin sin recargar toda la página."""
    conn = get_db_connection()
    try:
        rows = conn.execute('SELECT id, last_seen, last_device FROM users').fetchall()
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=ONLINE_WINDOW_MINUTES)).isoformat()
        users = {}
        for r in rows:
            users[r['id']] = {
                'online': bool(r['last_seen'] and r['last_seen'] >= cutoff),
                'last_seen': r['last_seen'],
                'last_device': r['last_device'],
            }
        online_count = sum(1 for u in users.values() if u['online'])
        response = jsonify({'users': users, 'online_count': online_count})
        response.headers['Cache-Control'] = 'no-store'
        return response
    finally:
        conn.close()

@app.route('/admin/usuarios/<int:user_id>/approve', methods=['POST'])
@admin_required
def admin_approve_user(user_id):
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT email, is_approved FROM users WHERE id=?', (user_id,)).fetchone()
        already_approved = bool(row and row['is_approved'])
        conn.execute('UPDATE users SET is_approved=1, approved_at=? WHERE id=?',
                     (_utcnow_iso(), user_id))
        conn.commit()
    finally:
        conn.close()
    # Fix (19/08): mismo problema que api_v1_admin_approve — ver ese
    # comentario. No era idempotente, mandaba el mail en cada click.
    if row and not already_approved:
        _send_account_approved_email(row['email'])
    return redirect(url_for('admin_users'))

@app.route('/admin/usuarios/<int:user_id>/reject', methods=['POST'])
@admin_required
def admin_reject_user(user_id):
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
        if row and row['email'] == ADMIN_EMAIL:
            return "No se puede eliminar la cuenta de administrador", 400
        conn.execute('DELETE FROM users WHERE id=?', (user_id,))
        conn.commit()
    finally:
        conn.close()
    if row:
        _send_account_rejected_email(row['email'])
    return redirect(url_for('admin_users'))

@app.route('/admin/usuarios/<int:user_id>/revoke', methods=['POST'])
@admin_required
def admin_revoke_user(user_id):
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
        if row and row['email'] == ADMIN_EMAIL:
            return "No se puede revocar al administrador", 400
        conn.execute('UPDATE users SET is_approved=0, approved_at=NULL WHERE id=?', (user_id,))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('admin_users'))

# ── Playlist colaborativa ──────────────────────────────────────────────────────
# Ver ticket: invitados se unen vía QR (una sesión = un QR, válido hasta que
# el admin la cierra), navegan la biblioteca (reusando browse/artist/album/
# track/search tal cual existen) y solo pueden AÑADIR pistas a una cola en el
# servidor — nunca reproducir. El admin la "levanta" desde /admin/colaborativa
# y la reparte en SU reproductor local con /api/admin/colaborativa/cola-pendiente,
# a partir de ahí es una playlist más que maneja con total libertad (reordenar/
# quitar/reproducir ya lo hace el playlist-panel existente, sin tocarlo).

COLLAB_ALBUM_WARNING_THRESHOLD = 3     # aviso (no bloqueo) al pasar 3 pistas del mismo álbum
COLLAB_DEFAULT_MAX_TRACKS      = 20
COLLAB_DEFAULT_WINDOW_HOURS    = 2

def _collab_avatar_sort_key(fname):
    """Orden natural (1, 2, ..., 10, 11) en vez del orden lexicográfico que
    daría os.listdir (1, 10, 11, 2, ...). Los archivos que no empiezan con
    un número (no debería haber, pero por las dudas) quedan al final."""
    m = re.match(r'(\d+)', fname)
    return (0, int(m.group(1)), fname) if m else (1, 0, fname)

def _collab_list_avatar_files(category):
    """Lista los archivos de static/avatares/<category>/ tal cual están en
    disco — sin asumir extensión .png, porque no todos la tienen (ver
    masculinos/13). Cualquier archivo regular no oculto sirve; el navegador
    ya sabe mostrar un PNG sin extensión con Content-Type correcto porque lo
    sirve la ruta estática de Flask, que detecta el tipo por contenido/ruta
    de todas formas vía send_from_directory."""
    folder = os.path.join(AVATAR_DIR, category)
    if not os.path.isdir(folder):
        return []
    files = [f for f in os.listdir(folder)
             if not f.startswith('.') and os.path.isfile(os.path.join(folder, f))]
    files.sort(key=_collab_avatar_sort_key)
    return files

def _collab_avatar_catalog():
    """Catálogo completo para renderizar las 2 pestañas (Femeninos/Masculinos)
    en collab_join.html."""
    return {
        'femeninos':  _collab_list_avatar_files('femeninos'),
        'masculinos': _collab_list_avatar_files('masculinos'),
    }

def _collab_initials(name):
    """Iniciales para el avatar circular por defecto (estilo Microsoft Teams)
    cuando el invitado no elige ninguna imagen. 'Natalia Torres' -> NT
    (primera letra del nombre + primera del último token). Una sola palabra,
    'Natalia' -> NA (sus 2 primeras letras). Ver ticket."""
    words = [w for w in (name or '').strip().split() if w]
    if not words:
        return '?'
    if len(words) == 1:
        w = words[0]
        return (w[:2] if len(w) >= 2 else w[0] * 2).upper()
    return (words[0][0] + words[-1][0]).upper()

def _collab_resolve_avatar_ref(form):
    """Valida lo que mandó el <form> de collab_join.html contra lo que
    realmente existe en static/avatares/ — nunca confiar en el path que
    manda el cliente. Devuelve (category, file) o (None, None) si no
    eligió ninguno (avatar por iniciales)."""
    category = form.get('avatar_category') or ''
    fname = form.get('avatar_file') or ''
    if category in ('femeninos', 'masculinos') and fname:
        safe_fname = os.path.basename(fname)
        if safe_fname in _collab_list_avatar_files(category):
            return category, safe_fname
    return None, None

def _collab_avatar_display(category, fname, name, device_key=None):
    """Arma el dict {'type': 'image'|'initials', ...} listo para renderizar,
    a partir de la referencia guardada en collab_participants (avatar_category
    + avatar_file). No depende de ninguna cookie de sesión — por eso sirve
    tanto para el header del propio invitado como para el badge "agregado
    por" en el player del admin (otro navegador — ver ticket)."""
    if device_key == _COLLAB_INFINITO_KEY:
        return {'type': 'infinito', 'text': '∞'}   # Ticket C-04
    if category and fname:
        return {'type': 'image',
                'url': url_for('static', filename=f'avatares/{category}/{fname}')}
    return {'type': 'initials', 'text': _collab_initials(name)}


# Ticket C-04 (pedido por Niko): las pistas que suma el modo infinito del
# anfitrión se muestran en la playlist colaborativa como si las hubiera
# agregado un participante más, "Modo infinito", con el símbolo ∞ de avatar.
# Ese participante es una fila especial (device_key fijo): no aparece en la
# lista de participantes, no puede ser delegado y no tiene cupo. Sus pistas
# entran ya como entregadas (dispatched=1): están en la cola del anfitrión
# desde antes de avisarlas, así que "Recargar cola" nunca las vuelve a cargar.
_COLLAB_INFINITO_KEY = '__modo_infinito__'
_COLLAB_INFINITO_NOMBRE = 'Modo infinito'


def _collab_participante_infinito(conn, session_id):
    return _collab_participante_especial(conn, session_id, _COLLAB_INFINITO_KEY, _COLLAB_INFINITO_NOMBRE)


# ── Ticket C-05: participantes especiales, usuarios con cuenta y cupo ──
# "Anfitrión": las pistas que el anfitrión agrega a su cola durante la sesión
# (su avatar + etiqueta Admin, sin cupo). Igual que "Modo infinito", es una
# fila especial que no aparece como participante ni puede ser delegado.
_COLLAB_ANFITRION_KEY = '__anfitrion__'
_COLLAB_KEYS_ESPECIALES = (_COLLAB_INFINITO_KEY, _COLLAB_ANFITRION_KEY)
_COLLAB_SQL_NO_ESPECIAL = "cp.device_key NOT IN ('__modo_infinito__', '__anfitrion__')"
# Columnas del perfil del usuario (participantes con cuenta y anfitrión): el
# nombre y el avatar se leen del perfil en cada consulta.
_COLLAB_SQL_USER_COLS = "u.nickname AS u_nickname, u.avatar AS u_avatar, u.email AS u_email"
_COLLAB_SQL_USER_JOIN = "LEFT JOIN users u ON u.id = cp.user_id"


def _collab_user_key(user_id):
    return f'user:{int(user_id)}'


def _collab_nombre_usuario(nickname, email):
    nick = (nickname or '').strip()
    if nick:
        return nick
    return ((email or '').split('@')[0] or 'Usuario')[:40]


def _collab_avatar_usuario(avatar_path, nombre):
    """Avatar del perfil de un usuario ('femeninos/x.png' → imagen; sin
    avatar → iniciales)."""
    if avatar_path and '/' in avatar_path:
        cat, fname = avatar_path.split('/', 1)
        av = _collab_avatar_display(cat, fname, nombre)
        if av.get('type') == 'image':
            return av
    return {'type': 'initials', 'text': _collab_initials(nombre)}


def _collab_part_view(p):
    """(nombre, avatar con 'badge') de un participante — `p` es la fila de
    collab_participants, idealmente con las columnas u_* del perfil
    (_COLLAB_SQL_USER_COLS). badge: 'admin' (anfitrión), 'usuario' (con
    cuenta), 'infinito' o None (invitado web)."""
    p = dict(p)
    key = p.get('device_key') or ''
    if key == _COLLAB_INFINITO_KEY:
        return _COLLAB_INFINITO_NOMBRE, {'type': 'infinito', 'text': '∞', 'badge': 'infinito'}
    if p.get('user_id') is not None and (key == _COLLAB_ANFITRION_KEY or key.startswith('user:')):
        nombre = _collab_nombre_usuario(p.get('u_nickname'), p.get('u_email'))
        av = dict(_collab_avatar_usuario(p.get('u_avatar'), nombre))
        av['badge'] = 'admin' if key == _COLLAB_ANFITRION_KEY else 'usuario'
        return nombre, av
    nombre = p.get('name') or 'Invitado'
    av = dict(_collab_avatar_display(p.get('avatar_category'), p.get('avatar_file'), nombre))
    av['badge'] = None
    return nombre, av


def _collab_participante_especial(conn, session_id, key, nombre, user_id=None):
    row = conn.execute('SELECT id FROM collab_participants WHERE session_id=? AND device_key=?',
                       (session_id, key)).fetchone()
    if row:
        return row['id']
    cur = conn.execute(
        'INSERT INTO collab_participants (session_id, device_key, name, joined_at, last_seen, user_id) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (session_id, key, nombre, _utcnow_iso(), _utcnow_iso(), user_id))
    return cur.lastrowid


# Código corto de la sesión: 6 caracteres sin letras/números confusos (sin
# I, L, O, 0, 1). Se muestra como K7F-Q2M y se acepta con o sin guion.
_COLLAB_CODE_ALFABETO = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _collab_generar_codigo(conn):
    while True:
        code = ''.join(secrets.choice(_COLLAB_CODE_ALFABETO) for _ in range(6))
        if not conn.execute('SELECT 1 FROM collab_sessions WHERE is_active=1 AND code=?', (code,)).fetchone():
            return code


def _collab_codigo_display(code):
    return f'{code[:3]}-{code[3:]}' if code and len(code) == 6 else (code or None)


def _collab_normalizar_codigo(raw):
    return re.sub(r'[^A-Z0-9]', '', (raw or '').upper())


def _collab_sesion_por_codigo(conn, raw):
    code = _collab_normalizar_codigo(raw)
    if len(code) != 6:
        return None
    row = conn.execute('SELECT * FROM collab_sessions WHERE is_active=1 AND code=?', (code,)).fetchone()
    return dict(row) if row else None


# Límite de intentos de código (por IP o cuenta): 10 por minuto.
_collab_intentos_codigo = {}


def _collab_intento_codigo_permitido(clave):
    ahora = time.time()
    lista = [t for t in _collab_intentos_codigo.get(clave, []) if ahora - t < 60]
    permitido = len(lista) < 10
    if permitido:
        lista.append(ahora)
    _collab_intentos_codigo[clave] = lista
    return permitido


def _collab_parse_utc(iso):
    try:
        return datetime.fromisoformat(str(iso).replace('Z', '').replace(' ', 'T'))
    except (TypeError, ValueError):
        return None


def _collab_fmt_espera(segundos):
    m = max(1, int(round(segundos / 60.0)))
    if m < 60:
        return f'{m} min'
    h, r = divmod(m, 60)
    return f'{h} h {r} min' if r else f'{h} h'


def _collab_cupo(conn, sess, part):
    """Ticket C-05 — cupo fijo con bloqueo (reemplaza la ventana móvil):
    cada participante tiene max_tracks pistas sin importar cuánto tarde en
    usarlas; al agregar la última empieza un bloqueo de window_hours y,
    cumplido, vuelve a tener max_tracks completas. Anfitrión y Modo infinito
    no tienen cupo. Si el bloqueo ya venció, lo libera (y lo guarda)."""
    part = dict(part)
    maximo = int(sess['max_tracks'])
    # Ticket C-06: el Modo infinito es un participante más, con el mismo cupo;
    # solo el anfitrión (Admin) no tiene cupo.
    if (part.get('device_key') or '') == _COLLAB_ANFITRION_KEY:
        return {'max_tracks': maximo, 'used': 0, 'remaining': maximo, 'unlimited': True,
                'blocked_until': None, 'blocked_seconds': 0}
    ahora = datetime.now(timezone.utc).replace(tzinfo=None)
    desde, hasta = part.get('cupo_desde'), part.get('bloqueado_hasta')
    hasta_dt = _collab_parse_utc(hasta) if hasta else None
    if hasta and (hasta_dt is None or hasta_dt <= ahora):
        # Bloqueo cumplido: cupo completo de nuevo, contando desde ahora.
        desde, hasta, hasta_dt = ahora.isoformat(), None, None
        conn.execute('UPDATE collab_participants SET cupo_desde=?, bloqueado_hasta=NULL WHERE id=?',
                     (desde, part['id']))
        conn.commit()
    if desde:
        usadas = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE participant_id=? AND added_at>=? '
                              'AND COALESCE(rescate, 0)=0', (part['id'], desde)).fetchone()[0]
    else:
        usadas = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE participant_id=? '
                              'AND COALESCE(rescate, 0)=0', (part['id'],)).fetchone()[0]
    if hasta_dt is None and usadas >= maximo:
        # Llegó al tope sin bloqueo registrado (datos de antes del cambio o se
        # bajó el máximo): el bloqueo empieza ahora.
        hasta_dt = ahora + timedelta(hours=float(sess['window_hours']))
        hasta = hasta_dt.isoformat()
        conn.execute('UPDATE collab_participants SET bloqueado_hasta=? WHERE id=?', (hasta, part['id']))
        conn.commit()
    segundos = max(0, int((hasta_dt - ahora).total_seconds())) if hasta_dt else 0
    return {'max_tracks': maximo, 'used': min(usadas, maximo), 'remaining': 0 if hasta_dt else max(0, maximo - usadas),
            'unlimited': False, 'blocked_until': hasta, 'blocked_seconds': segundos}


# Ticket C-01 (reportado por Niko): el link del QR se armaba con la direccion
# por la que estaba conectado el ADMIN (url_for _external). Con el iPhone por
# Tailscale el QR apuntaba a 100.x y las visitas en el WiFi de la casa no
# podian abrirlo. Ahora el QR usa SIEMPRE la direccion de la LAN, y ademas se
# informa la de Tailscale para el caso "invitado que tambien tiene Tailscale"
# (en el auto). Configurables por variable de entorno.
_COLLAB_LAN_URL = os.environ.get('ORBYTE_LAN_URL', 'http://192.168.100.14:5001').rstrip('/')
_COLLAB_TAILSCALE_URL = os.environ.get('ORBYTE_TAILSCALE_URL', 'http://100.127.75.52:5001').rstrip('/')


def _collab_join_urls(token):
    ruta = url_for('collab_join', token=token)
    return {'lan': _COLLAB_LAN_URL + ruta, 'tailscale': _COLLAB_TAILSCALE_URL + ruta}


def _collab_join_url(token, red=None):
    urls = _collab_join_urls(token)
    return urls.get(red) or urls['lan']


def _collab_active_session(conn):
    row = conn.execute(
        'SELECT * FROM collab_sessions WHERE is_active=1 ORDER BY id DESC LIMIT 1'
    ).fetchone()
    return dict(row) if row else None

def _collab_get_session_by_token(conn, token):
    row = conn.execute(
        'SELECT * FROM collab_sessions WHERE token=? AND is_active=1', (token,)
    ).fetchone()
    return dict(row) if row else None

def _collab_device_key():
    """Identificador de DISPOSITIVO (no de sesión/cookie) para que un mismo
    celular no pueda 'reiniciar' su cupo de pistas re-escaneando el QR con
    otro nombre. IP + User-Agent es lo más parecido a una MAC address que un
    servidor web puede ver — no es infalible (un cambio de IP por
    reconexión de WiFi generaría una fila nueva), pero para el caso de uso
    (invitados de confianza en una reunión) es suficiente, y es justo lo que
    pediste usar."""
    ua = request.headers.get('User-Agent', '')
    raw = f"{request.remote_addr}|{ua}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]

def _collab_find_or_create_participant(conn, sess, name=None, avatar_category=None, avatar_file=None):
    """Busca un participante YA existente para este dispositivo en esta
    sesión (re-escaneo del QR, con o sin cookie de invitado vigente) y lo
    reutiliza — así su cupo de pistas sigue contando desde donde iba en vez
    de resetearse. Si no existe, lo crea con el nombre y la referencia de
    avatar dados (ver _collab_resolve_avatar_ref — solo category+file, la
    imagen en sí sigue viviendo únicamente en static/avatares/)."""
    device_key = _collab_device_key()
    row = conn.execute(
        'SELECT * FROM collab_participants WHERE session_id=? AND device_key=?',
        (sess['id'], device_key)
    ).fetchone()
    if row:
        conn.execute('UPDATE collab_participants SET last_seen=? WHERE id=?', (_utcnow_iso(), row['id']))
        conn.commit()
        return dict(row), False
    cur = conn.execute(
        'INSERT INTO collab_participants '
        '(session_id, device_key, name, joined_at, last_seen, avatar_category, avatar_file) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (sess['id'], device_key, name or 'Invitado', _utcnow_iso(), _utcnow_iso(),
         avatar_category, avatar_file)
    )
    conn.commit()
    return {'id': cur.lastrowid, 'name': name or 'Invitado',
            'avatar_category': avatar_category, 'avatar_file': avatar_file}, True

def _collab_window_count(conn, session_row, participant_id):
    """Cuántas pistas agregó este participante dentro de la ventana de tiempo
    configurada (ventana móvil desde 'ahora', no un contador que se resetea a
    horario fijo — así el ticket "puede volver a agregar más si el evento se
    extiende" funciona sin ningún job/cron adicional)."""
    window_start = (datetime.now(timezone.utc).replace(tzinfo=None)
                     - timedelta(hours=session_row['window_hours'])).isoformat()
    return conn.execute(
        'SELECT COUNT(*) FROM collab_queue_items WHERE participant_id=? AND added_at>=?',
        (participant_id, window_start)
    ).fetchone()[0]

# ── Ticket C-06: distribución de la reproducción y Modo infinito ──────────
# El servidor decide el orden de lo que todavía no sonó. Tres modos (los
# elige el anfitrión): 'turnos' (por defecto: suena quien lleva más tiempo
# sin escuchar algo suyo), 'llegada' (orden en que se agregaron) y 'menos'
# (primero quien lleva menos pistas en la sesión). El Modo infinito es un
# participante más (mismo cupo y prioridad) que propone tandas de 5 cuando
# los demás ya agregaron (ver _collab_infinito_tick).
_COLLAB_DISTRIBUCIONES = ('turnos', 'llegada', 'menos')
_COLLAB_INF_TANDA = 5            # pistas por tanda
_COLLAB_INF_MIN_USUARIOS = 5     # pistas de usuarios antes de su primera tanda
_COLLAB_INF_NUEVAS = 5           # pistas nuevas de usuarios entre tandas…
_COLLAB_INF_MINUTOS = 15         # …o este tiempo (con al menos 1 nueva)
_COLLAB_RESCATE_UMBRAL = 2       # "casi no quedan": pistas por sonar (incluida la que sigue)
_collab_rescate_fallido = {}     # session_id -> último rescate sin recomendaciones (no reintentar al toque)
# C-06c (sesión real de Niko, 18-10): dos avisos casi simultáneos (agregar +
# lo que suena, o dos dispositivos) corrían el modo infinito a la vez: tandas
# de 10 en vez de 5 y la misma pista repetida. Un candado por sesión.
_collab_infinito_locks = {}
_collab_infinito_locks_guard = threading.Lock()


def _collab_auto_carga(sess):
    v = dict(sess).get('auto_carga')
    return True if v is None else bool(v)


def _collab_iso(v):
    """Normaliza fechas guardadas con espacio o con T (para comparar como texto)."""
    return str(v or '').replace(' ', 'T')


def _collab_orden(conn, sess):
    """Pistas en espera (dispatched=0) en el orden en que deben sonar, según
    la distribución elegida por el anfitrión."""
    rows = [dict(r) for r in conn.execute(
        'SELECT id, participant_id, track_id, added_at FROM collab_queue_items '
        'WHERE session_id=? AND dispatched=0 ORDER BY added_at ASC, id ASC', (sess['id'],))]
    if not rows:
        return []
    modo = (dict(sess).get('distribucion') or 'turnos')
    if modo == 'llegada':
        return rows
    if modo == 'menos':
        totales = {r['participant_id']: r['n'] for r in conn.execute(
            'SELECT participant_id, COUNT(*) AS n FROM collab_queue_items WHERE session_id=? '
            'GROUP BY participant_id', (sess['id'],))}
        return sorted(rows, key=lambda r: (totales.get(r['participant_id'], 0), _collab_iso(r['added_at']), r['id']))
    # Turnos justos: suena el que lleva más tiempo sin que le entregaran algo
    # (quien nunca sonó cuenta desde su primera pista en espera); empate →
    # quien agregó antes. Al elegirlo pasa al final de la ronda.
    filas = {}
    for r in rows:
        filas.setdefault(r['participant_id'], []).append(r)
    ultimo = {r['participant_id']: _collab_iso(r['ult']) for r in conn.execute(
        'SELECT participant_id, MAX(dispatched_at) AS ult FROM collab_queue_items '
        'WHERE session_id=? AND dispatched=1 AND dispatched_at IS NOT NULL GROUP BY participant_id', (sess['id'],))}
    clave = {pid: ultimo.get(pid) or _collab_iso(items[0]['added_at']) for pid, items in filas.items()}
    out, turno = [], 0
    while True:
        candidatos = [pid for pid, items in filas.items() if items]
        if not candidatos:
            break
        pid = min(candidatos, key=lambda x: (clave[x], _collab_iso(filas[x][0]['added_at']), filas[x][0]['id']))
        out.append(filas[pid].pop(0))
        turno += 1
        clave[pid] = f'9999-{turno:08d}'   # siempre después de cualquier fecha real
    return out


def _collab_entregar(conn, sess, items):
    """Marca como entregadas (con su momento, en orden) las pistas `items` y
    devuelve su JSON para la app del anfitrión, con quién agregó cada una."""
    if not items:
        return []
    ids = [it['track_id'] for it in items]
    rows = conn.execute(
        f'''SELECT t.*, al.name as album_name, al.cover_path, al.year as album_year,
                   al.artist_id, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums al ON t.album_id=al.id
            LEFT JOIN artists ar ON al.artist_id=ar.id
            WHERE t.id IN ({','.join('?' * len(ids))})''', ids
    ).fetchall()
    by_id = {r['id']: r for r in rows}
    participant_ids = list({it['participant_id'] for it in items})
    added_by_map = {}
    for p in conn.execute(
            f'SELECT cp.*, {_COLLAB_SQL_USER_COLS} FROM collab_participants cp {_COLLAB_SQL_USER_JOIN} '
            f'WHERE cp.id IN ({",".join("?" * len(participant_ids))})', participant_ids):
        nombre, av = _collab_part_view(p)
        added_by_map[p['id']] = {'name': nombre, 'avatar': av}
    result = []
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, it in enumerate(items):
        conn.execute('UPDATE collab_queue_items SET dispatched=1, dispatched_at=? WHERE id=?',
                     ((base + timedelta(microseconds=i)).isoformat(), it['id']))
        if it['track_id'] not in by_id:
            continue
        d = track_to_json(by_id[it['track_id']])
        d['collab_added_by'] = added_by_map.get(it['participant_id'])
        # Ticket C-02: stream_url / container_ext que exige la app iOS.
        d['stream_url'] = f'/api/v1/stream/{d["id"]}'
        d['container_ext'] = _container_ext(d)
        result.append(d)
    conn.commit()
    return result


def _collab_marcar_sonando(conn, sess, track_id):
    """La app del anfitrión informa que empezó a sonar `track_id`: esa pista
    queda Reproducida, y lo que se le entregó antes y nunca sonó (lo saltó)
    también, para que no quede "en la cola" para siempre."""
    if not track_id:
        return False
    it = conn.execute(
        'SELECT id, dispatched_at FROM collab_queue_items WHERE session_id=? AND track_id=? '
        'AND dispatched=1 AND played_at IS NULL ORDER BY dispatched_at ASC, id ASC LIMIT 1',
        (sess['id'], track_id)).fetchone()
    if not it:
        return False
    ahora = _utcnow_iso()
    conn.execute('UPDATE collab_queue_items SET played_at=? WHERE id=?', (ahora, it['id']))
    if it['dispatched_at']:
        conn.execute('UPDATE collab_queue_items SET played_at=? WHERE session_id=? AND dispatched=1 '
                     'AND played_at IS NULL AND dispatched_at IS NOT NULL AND dispatched_at < ?',
                     (ahora, sess['id'], it['dispatched_at']))
    conn.commit()
    return True


def _collab_cancion_clave(r):
    """Misma canción aunque sea otra versión: título sin «(Live…)», «[…]»,
    « - Remaster…» + artista, normalizados."""
    r = dict(r)
    titulo = r.get('title') or r.get('t_title') or ''
    artista = r.get('artist') or ''
    return _dsc_fuentes.normalizar(titulo) + '|' + _dsc_fuentes.normalizar(artista)


def _collab_infinito_tanda(conn, sess, inf_id, n, rescate):
    """Agrega hasta `n` pistas del Modo infinito (en espera, como las de
    cualquiera). Cada una se inspira en un participante distinto — lo último
    que agregó — rotando entre ellos, para no favorecer a nadie. Nunca
    propone algo que ya esté en la sesión (en espera, en la cola o que ya
    sonó)."""
    por_part = {}
    for r in conn.execute(
            'SELECT participant_id, track_id FROM collab_queue_items WHERE session_id=? AND participant_id!=? '
            'ORDER BY added_at DESC, id DESC', (sess['id'], inf_id)):
        semillas = por_part.setdefault(r['participant_id'], [])
        if len(semillas) < 3:
            semillas.append(r['track_id'])
    partes = list(por_part.keys())
    if not partes:
        return 0
    excluir = {r['track_id'] for r in conn.execute(
        'SELECT track_id FROM collab_queue_items WHERE session_id=?', (sess['id'],))}
    # C-06c: tampoco la MISMA canción en otra versión («Radio Ga Ga» y «Radio
    # Ga Ga (Live Aid)»): en la tele parecía que el modo infinito se adjudicaba
    # la canción que había pedido un invitado.
    canciones = {_collab_cancion_clave(r) for r in conn.execute(
        'SELECT t.title, t.artist FROM collab_queue_items q JOIN tracks t ON t.id=q.track_id WHERE q.session_id=?',
        (sess['id'],))}
    hechas = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE participant_id=?', (inf_id,)).fetchone()[0]
    todas = [t for semillas in por_part.values() for t in semillas][:10]
    elegidas = []
    for i in range(n):
        pid = partes[(hechas + i) % len(partes)]
        rec = None
        for semillas in (por_part[pid], todas):
            for r in _radio_next_tracks(conn, sess['created_by'], semillas, list(excluir), 6):
                if _collab_cancion_clave(r) not in canciones:
                    rec = r
                    break
                excluir.add(r['id'])
            if rec:
                break
        if not rec:
            continue
        tid = rec['id']
        excluir.add(tid)
        canciones.add(_collab_cancion_clave(rec))
        elegidas.append(tid)
    base = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, tid in enumerate(elegidas):
        conn.execute('INSERT INTO collab_queue_items (session_id, participant_id, track_id, added_at, dispatched, rescate) '
                     'VALUES (?, ?, ?, ?, 0, ?)',
                     (sess['id'], inf_id, tid, (base + timedelta(microseconds=i)).isoformat(), 1 if rescate else 0))
    conn.commit()
    return len(elegidas)


def _collab_infinito_tick(conn, sess):
    """¿Le toca al Modo infinito? Devuelve cuántas pistas agregó.
    - Rescate: si ya sonó algo y quedan 2 o menos por sonar, agrega 2 y 3
      alternados (C-06d) aunque no tenga cupo (no cuentan en su cupo) — la
      música nunca se corta.
    - Tanda normal: con al menos 5 pistas de usuarios en la sesión y cupo
      disponible, propone 5; las siguientes cuando haya 5 nuevas de usuarios
      o pasen 15 minutos (con al menos 1 nueva)."""
    sess = conn.execute('SELECT * FROM collab_sessions WHERE id=? AND is_active=1', (sess['id'],)).fetchone()
    if not sess:
        return 0
    sess = dict(sess)
    inf_id = _collab_participante_infinito(conn, sess['id'])
    usuarios_n = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND participant_id!=?',
                              (sess['id'], inf_id)).fetchone()[0]
    if not usuarios_n:
        return 0
    por_sonar = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND played_at IS NULL',
                             (sess['id'],)).fetchone()[0]
    alguna_sono = conn.execute('SELECT 1 FROM collab_queue_items WHERE session_id=? AND played_at IS NOT NULL LIMIT 1',
                               (sess['id'],)).fetchone() is not None
    if alguna_sono and por_sonar <= _COLLAB_RESCATE_UMBRAL:
        if time.time() - _collab_rescate_fallido.get(sess['id'], 0) < 60:
            return 0
        # C-06d (pedido por Niko): el rescate suma poco, 2 y 3 alternados, para
        # que el modo infinito no se acumule mientras los usuarios agregan.
        hechas = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND participant_id=? '
                              'AND rescate=1', (sess['id'], inf_id)).fetchone()[0]
        n = _collab_infinito_tanda(conn, sess, inf_id, 2 if hechas % 5 == 0 else 3, rescate=True)
        if not n:
            _collab_rescate_fallido[sess['id']] = time.time()
        return n
    if usuarios_n < _COLLAB_INF_MIN_USUARIOS:
        return 0
    part = conn.execute('SELECT * FROM collab_participants WHERE id=?', (inf_id,)).fetchone()
    cupo = _collab_cupo(conn, sess, part)
    if cupo['remaining'] <= 0:
        return 0
    if sess.get('inf_ultima_at'):
        nuevas = usuarios_n - int(sess.get('inf_usuarios_n') or 0)
        ultima = _collab_parse_utc(sess['inf_ultima_at'])
        pasaron = (datetime.now(timezone.utc).replace(tzinfo=None) - ultima).total_seconds() if ultima else 0
        if not (nuevas >= _COLLAB_INF_NUEVAS or (nuevas >= 1 and pasaron >= _COLLAB_INF_MINUTOS * 60)):
            return 0
    agregadas = _collab_infinito_tanda(conn, sess, inf_id, min(_COLLAB_INF_TANDA, cupo['remaining']), rescate=False)
    conn.execute('UPDATE collab_sessions SET inf_ultima_at=?, inf_usuarios_n=? WHERE id=?',
                 (_utcnow_iso(), usuarios_n, sess['id']))
    conn.commit()
    if agregadas:
        # Si con esta tanda llegó al tope, empieza su bloqueo (como cualquiera).
        _collab_cupo(conn, sess, conn.execute('SELECT * FROM collab_participants WHERE id=?', (inf_id,)).fetchone())
    return agregadas


def _collab_infinito_tick_seguro(conn, sess):
    """Igual que _collab_infinito_tick, pero un error ahí nunca rompe lo que
    se estaba haciendo (agregar una pista, entregar la siguiente…). Una sola
    corrida a la vez por sesión (C-06c)."""
    with _collab_infinito_locks_guard:
        lock = _collab_infinito_locks.setdefault(sess['id'], threading.Lock())
    try:
        conn.commit()   # que la consulta vea lo que otra corrida ya guardó
        with lock:
            return _collab_infinito_tick(conn, sess)
    except Exception:
        app.logger.exception('collab: modo infinito')
        return 0

def _collab_set_delegate(conn, session_id, participant_id, enable):
    """Asigna o quita el permiso de 'delegado' (puede pedir la actualización
    remota de la cola). Solo puede haber UN delegado a la vez por sesión —
    asignarlo a alguien se lo quita automáticamente a cualquier otro (mismo
    criterio que collab_sessions.is_active: simple y sin ambigüedad sobre
    quién tiene la posta)."""
    if enable:
        conn.execute('UPDATE collab_participants SET can_pull=0 WHERE session_id=?', (session_id,))
        conn.execute(
            'UPDATE collab_participants SET can_pull=1 WHERE id=? AND session_id=?',
            (participant_id, session_id)
        )
    else:
        conn.execute(
            'UPDATE collab_participants SET can_pull=0 WHERE id=? AND session_id=?',
            (participant_id, session_id)
        )
    conn.commit()

def _collab_try_add(conn, session_id, participant_id, track_id, confirm_album):
    """Un solo intento de agregar UNA pista — usado por /api/collab/add.
    Devuelve un dict con 'status': ok | limit | duplicate | album_warning |
    expired | error. 'duplicate' es un BLOQUEO duro: si la pista ya está en
    la playlist colaborativa (la haya agregado quien sea) no se vuelve a
    agregar, punto. 'album_warning' sigue siendo un aviso con confirmación
    (agregar 4+ pistas propias del mismo álbum sigue siendo una elección
    legítima del invitado, solo se le sugiere variar)."""
    sess = conn.execute('SELECT * FROM collab_sessions WHERE id=? AND is_active=1', (session_id,)).fetchone()
    if not sess:
        return {'status': 'expired', 'message': 'La sesión colaborativa ya terminó.'}
    track = conn.execute('SELECT id, title, artist, album_id FROM tracks WHERE id=?', (track_id,)).fetchone()
    if not track:
        return {'status': 'error', 'message': 'Pista no encontrada.'}

    # Ticket C-05: cupo fijo con bloqueo (ver _collab_cupo).
    part = conn.execute('SELECT * FROM collab_participants WHERE id=? AND session_id=?',
                        (participant_id, session_id)).fetchone()
    if not part:
        return {'status': 'expired', 'message': 'La sesión colaborativa ya terminó.'}
    cupo = _collab_cupo(conn, sess, part)
    if not cupo['unlimited'] and cupo['remaining'] <= 0:
        espera = _collab_fmt_espera(cupo['blocked_seconds']) if cupo['blocked_seconds'] else None
        return {'status': 'limit', 'blocked_until': cupo['blocked_until'], 'blocked_seconds': cupo['blocked_seconds'],
                'message': f"Usaste tus {sess['max_tracks']} pistas."
                           + (f" Cupo nuevo en {espera}." if espera else "")}
    count = cupo['used']

    dup = conn.execute(
        f'''SELECT cp.*, {_COLLAB_SQL_USER_COLS} FROM collab_queue_items cqi
           JOIN collab_participants cp ON cp.id = cqi.participant_id
           {_COLLAB_SQL_USER_JOIN}
           WHERE cqi.session_id=? AND cqi.track_id=? AND cqi.played_at IS NULL LIMIT 1''',
        (session_id, track_id)
    ).fetchone()
    if dup:
        # Ticket C-06: solo se bloquea lo que está en la cola y todavía no
        # sonó; volver a pedir algo que ya sonó se permite.
        dup_nombre = _collab_part_view(dup)[0]
        return {'status': 'duplicate', 'added_by': dup_nombre,
                'message': f"«{track['title']}» ya está en la cola (la agregó {dup_nombre})."}

    if track['album_id'] and not confirm_album:
        album_count = conn.execute(
            'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND participant_id=? '
            'AND track_id IN (SELECT id FROM tracks WHERE album_id=?)',
            (session_id, participant_id, track['album_id'])
        ).fetchone()[0]
        if album_count >= COLLAB_ALBUM_WARNING_THRESHOLD:
            return {'status': 'album_warning',
                    'message': f"Ya agregaste {album_count} pistas de este álbum — "
                               f"probá sumar variedad, tu cupo de pistas es limitado."}

    ahora = _utcnow_iso()
    conn.execute(
        'INSERT INTO collab_queue_items (session_id, participant_id, track_id, added_at) VALUES (?, ?, ?, ?)',
        (session_id, participant_id, track_id, ahora)
    )
    restantes = sess['max_tracks'] - (count + 1)
    bloqueado_hasta = None
    if not cupo['unlimited'] and restantes <= 0:
        # Ticket C-05: usó la última de su cupo → empieza el bloqueo.
        bloqueado_hasta = (datetime.now(timezone.utc).replace(tzinfo=None)
                           + timedelta(hours=float(sess['window_hours']))).isoformat()
        conn.execute('UPDATE collab_participants SET bloqueado_hasta=? WHERE id=?', (bloqueado_hasta, participant_id))
    conn.commit()
    _collab_infinito_tick_seguro(conn, sess)
    return {'status': 'ok', 'remaining': max(0, restantes) if not cupo['unlimited'] else sess['max_tracks'],
            'used': count + 1, 'blocked_until': bloqueado_hasta,
            'blocked_seconds': int(float(sess['window_hours']) * 3600) if bloqueado_hasta else 0}


@app.route('/admin/colaborativa')
@admin_required
def admin_collab():
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        participants, pending_count, dispatched_count, join_url = [], 0, 0, None
        if sess:
            participants = [dict(r) for r in conn.execute(
                f'SELECT cp.* FROM collab_participants cp WHERE cp.session_id=? AND {_COLLAB_SQL_NO_ESPECIAL} '
                'AND COALESCE(cp.activo, 1)=1 ORDER BY cp.joined_at',
                (sess['id'],)
            ).fetchall()]
            pending_count = conn.execute(
                'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND dispatched=0', (sess['id'],)
            ).fetchone()[0]
            dispatched_count = conn.execute(
                'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND dispatched=1', (sess['id'],)
            ).fetchone()[0]
            join_url = _collab_join_url(sess['token'])
        return render_template('collab_host.html', collab_session=sess, participants=participants,
                               pending_count=pending_count, dispatched_count=dispatched_count,
                               join_url=join_url, qrcode_available=_QRCODE_AVAILABLE,
                               default_max=COLLAB_DEFAULT_MAX_TRACKS,
                               default_window=COLLAB_DEFAULT_WINDOW_HOURS)
    finally:
        conn.close()


def _collab_start_session(admin_user_id, max_tracks=None, window_hours=None):
    """Crea una sesión colaborativa nueva, cerrando cualquier otra activa
    (una sola sesión a la vez — mismo QR viejo deja de servir solo).
    Ticket 18 Lote 1: factorizado para no duplicar la lógica entre la ruta
    web (/admin/colaborativa/crear, form-encoded) y la API nativa
    (/api/v1/admin/collab/crear, JSON). Acepta max_tracks/window_hours en
    cualquier tipo parseable (o None) y cae a los defaults del proyecto."""
    try:
        max_tracks = int(max_tracks)
    except (TypeError, ValueError):
        max_tracks = COLLAB_DEFAULT_MAX_TRACKS
    try:
        window_hours = float(window_hours)
    except (TypeError, ValueError):
        window_hours = COLLAB_DEFAULT_WINDOW_HOURS
    max_tracks   = max(1, max_tracks)
    window_hours = max(0.5, window_hours)
    conn = get_db_connection()
    try:
        conn.execute('UPDATE collab_sessions SET is_active=0, closed_at=? WHERE is_active=1', (_utcnow_iso(),))
        token = secrets.token_urlsafe(16)
        code = _collab_generar_codigo(conn)   # Ticket C-05
        conn.execute(
            'INSERT INTO collab_sessions (token, created_by, max_tracks, window_hours, is_active, created_at, code) '
            'VALUES (?, ?, ?, ?, 1, ?, ?)',
            (token, admin_user_id, max_tracks, window_hours, _utcnow_iso(), code)
        )
        conn.commit()
    finally:
        conn.close()
    return {'token': token, 'max_tracks': max_tracks, 'window_hours': window_hours,
            'code': _collab_codigo_display(code),
            'join_url': _collab_join_url(token), 'join_urls': _collab_join_urls(token)}


def _collab_stop_active_session():
    """Cierra la sesión colaborativa activa, si hay una. Ticket 18 Lote 1:
    factorizado, mismo motivo que _collab_start_session."""
    conn = get_db_connection()
    try:
        conn.execute('UPDATE collab_sessions SET is_active=0, closed_at=? WHERE is_active=1', (_utcnow_iso(),))
        conn.commit()
    finally:
        conn.close()
    _tv_quitar_si_activa()   # Ticket C-07: al finalizar, la tele vuelve a lo que tenía


@app.route('/admin/colaborativa/crear', methods=['POST'])
@admin_required
def admin_collab_crear():
    _collab_start_session(
        session['user_id'],
        request.form.get('max_tracks', type=int),
        request.form.get('window_hours', type=float),
    )
    return redirect(url_for('admin_collab'))


@app.route('/admin/colaborativa/finalizar', methods=['POST'])
@admin_required
def admin_collab_finalizar():
    _collab_stop_active_session()
    return redirect(url_for('admin_collab'))


@app.route('/api/v1/admin/collab/crear', methods=['POST'])
@api_admin_required
def api_v1_admin_collab_crear():
    """Ticket 18 Lote 1: versión JSON de admin_collab_crear para la app
    nativa (Bearer, via api_admin_required) — misma lógica que la ruta web,
    factorizada en _collab_start_session. Body esperado: {max_tracks?,
    window_hours?} — ambos opcionales, caen a los defaults del proyecto si
    faltan o vienen con un tipo no parseable."""
    data = request.get_json(silent=True) or {}
    result = _collab_start_session(g.api_user['id'], data.get('max_tracks'), data.get('window_hours'))
    return jsonify({'status': 'ok', **result})


@app.route('/api/v1/admin/collab/finalizar', methods=['POST'])
@api_admin_required
def api_v1_admin_collab_finalizar():
    """Ticket 18 Lote 1: versión JSON de admin_collab_finalizar."""
    _collab_stop_active_session()
    return jsonify({'status': 'ok'})


@app.route('/admin/colaborativa/participante/<int:participant_id>/permiso', methods=['POST'])
@app.route('/api/v1/admin/collab/participante/<int:participant_id>/permiso', methods=['POST'])
@api_admin_required
def admin_collab_permiso(participant_id):
    """Alterna el permiso de 'delegado' de un participante de la sesión
    activa (ver _collab_set_delegate). Llamado por fetch() desde
    collab_host.js, no por navegación — responde JSON."""
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            return jsonify({'status': 'error', 'message': 'No hay sesión activa.'}), 404
        row = conn.execute(
            f'SELECT cp.* FROM collab_participants cp WHERE cp.id=? AND cp.session_id=? AND {_COLLAB_SQL_NO_ESPECIAL}',
            (participant_id, sess['id'])
        ).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': 'Participante no encontrado.'}), 404
        enable = not bool(row['can_pull'])
        _collab_set_delegate(conn, sess['id'], participant_id, enable)
        return jsonify({'status': 'ok', 'can_pull': enable})
    finally:
        conn.close()


@app.route('/admin/colaborativa/qr.png')
@admin_required
def admin_collab_qr():
    if not _QRCODE_AVAILABLE:
        return ("Falta instalar la librería qrcode — corré: pip install qrcode[pil]", 501)
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
    finally:
        conn.close()
    if not sess:
        return "No hay una sesión colaborativa activa", 404
    join_url = _collab_join_url(sess['token'], request.args.get('red'))   # ?red=tailscale
    img = qrcode.make(join_url)
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    resp = Response(buf.getvalue(), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/admin/colaborativa/estado')
@app.route('/api/v1/admin/collab/estado')
@api_admin_required
def api_admin_collab_estado():
    """JSON liviano para refrescar en vivo el panel /admin/colaborativa
    (participantes + cola pendiente) — mismo patrón que /admin/usuarios/estado."""
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            return jsonify({'active': False})
        # Ticket C-01: can_pull como true/false (SQLite lo guarda 0/1 y la app
        # iOS lo decodifica como Bool: con un solo participante el decode
        # entero fallaba y el admin dejaba de ver a los invitados).
        # Ticket C-05: nombre/avatar desde el perfil para los usuarios con cuenta
        # (is_user → logo de Orbyte junto al nombre en la app del anfitrión);
        # los que salieron del Modo Colaborativo no se listan.
        participants = []
        for r in conn.execute(
            f'SELECT cp.*, {_COLLAB_SQL_USER_COLS} FROM collab_participants cp {_COLLAB_SQL_USER_JOIN} '
            f'WHERE cp.session_id=? AND {_COLLAB_SQL_NO_ESPECIAL} AND COALESCE(cp.activo, 1)=1 '
            'ORDER BY cp.joined_at', (sess['id'],)
        ).fetchall():
            nombre, av = _collab_part_view(r)
            participants.append({'id': r['id'], 'name': nombre, 'joined_at': r['joined_at'] or '',
                                 'can_pull': bool(r['can_pull']), 'is_user': r['user_id'] is not None,
                                 'avatar': av})
        if not sess.get('code'):
            # Sesión creada antes del Ticket C-05: se le asigna código ahora.
            sess['code'] = _collab_generar_codigo(conn)
            conn.execute('UPDATE collab_sessions SET code=? WHERE id=?', (sess['code'], sess['id']))
            conn.commit()
        _collab_infinito_tick_seguro(conn, sess)   # Ticket C-06: tandas por tiempo
        pending = conn.execute(
            'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND dispatched=0', (sess['id'],)
        ).fetchone()[0]
        played = conn.execute(
            'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND played_at IS NOT NULL', (sess['id'],)
        ).fetchone()[0]
        # Ticket C-04: total agregado en la sesión (para el acceso a "Lo agregado")
        total = conn.execute(
            'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=?', (sess['id'],)
        ).fetchone()[0]
        # Pedido remoto de actualización (ver /api/collab/solicitar-pull): el
        # delegado lo dispara desde su celular, este poll (cada 6s) es lo que
        # se lo hace llegar al dispositivo del anfitrión sin que tenga que
        # tocar nada — ver collab_host.js.
        pull_requested = bool(sess.get('pull_requested_at'))
        pull_requested_by_name = None
        if pull_requested and sess.get('pull_requested_by'):
            req = conn.execute(
                f'SELECT cp.*, {_COLLAB_SQL_USER_COLS} FROM collab_participants cp {_COLLAB_SQL_USER_JOIN} '
                'WHERE cp.id=?', (sess['pull_requested_by'],)
            ).fetchone()
            pull_requested_by_name = _collab_part_view(req)[0] if req else None
        return jsonify({'active': True, 'participants': participants, 'pending_count': pending,
                        'total_count': total, 'played_count': played,
                        # Ticket C-06: ajustes del anfitrión
                        'auto_carga': _collab_auto_carga(sess),
                        'distribucion': sess.get('distribucion') or 'turnos',
                        'max_tracks': sess['max_tracks'], 'window_hours': sess['window_hours'],
                        'pull_requested': pull_requested, 'pull_requested_by_name': pull_requested_by_name,
                        'token': sess['token'],
                        'code': _collab_codigo_display(sess.get('code')),
                        'join_url': _collab_join_url(sess['token']),
                        'join_urls': _collab_join_urls(sess['token'])})
    finally:
        conn.close()


@app.route('/api/admin/colaborativa/cola-pendiente')
@app.route('/api/v1/admin/collab/cola-pendiente')
@api_admin_required
def api_admin_collab_pull():
    """"Recargar cola" (manual): devuelve — y marca como entregadas — todas
    las pistas en espera, en el orden de la distribución elegida (Ticket
    C-06, ver _collab_orden). La app del anfitrión las suma al final de su
    cola local."""
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            return jsonify([])
        _collab_infinito_tick_seguro(conn, sess)
        ordered_items = _collab_orden(conn, sess)
        # Se limpia el pedido remoto (si había uno) apenas se atiende el
        # pull, haya o no pistas nuevas — así el delegado no queda con el
        # pedido "trabado" si ya no había nada pendiente para cargar.
        conn.execute(
            'UPDATE collab_sessions SET pull_requested_at=NULL, pull_requested_by=NULL WHERE id=?',
            (sess['id'],)
        )
        conn.commit()
        return jsonify(_collab_entregar(conn, sess, ordered_items))
    finally:
        conn.close()


@app.route('/api/v1/admin/collab/siguiente', methods=['POST'])
@api_admin_required
def api_v1_admin_collab_siguiente():
    """Ticket C-06 — Recarga automática: la app del anfitrión pide la(s)
    siguiente(s) pista(s) cada vez que empieza una (mantiene solo la que
    suena + la siguiente). El orden se decide en este momento, así lo que
    se agregó recién entra al reparto. Body opcional: {n: 1..5}."""
    data = request.get_json(silent=True) or {}
    try:
        n = max(1, min(int(data.get('n', 1)), 5))
    except (TypeError, ValueError):
        n = 1
    conn = get_db_connection()
    try:
        # Solo la app del anfitrión que creó la sesión (otro admin con la app
        # abierta no debe "llevarse" las pistas a su propia cola).
        sess = _collab_sesion_del_anfitrion(conn)
        if not sess:
            return jsonify([])
        _collab_infinito_tick_seguro(conn, sess)
        return jsonify(_collab_entregar(conn, sess, _collab_orden(conn, sess)[:n]))
    finally:
        conn.close()


@app.route('/api/v1/admin/collab/ajustes', methods=['POST'])
@api_admin_required
def api_v1_admin_collab_ajustes():
    """Ticket C-06: el anfitrión cambia la Recarga automática y/o la
    distribución. Body: {auto_carga?: bool, distribucion?: 'turnos'|'llegada'|'menos'}."""
    data = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            return jsonify({'status': 'error', 'message': 'No hay una playlist colaborativa activa.'}), 404
        if 'auto_carga' in data:
            conn.execute('UPDATE collab_sessions SET auto_carga=? WHERE id=?',
                         (1 if data.get('auto_carga') else 0, sess['id']))
        if 'distribucion' in data:
            d = str(data.get('distribucion') or '')
            if d not in _COLLAB_DISTRIBUCIONES:
                return jsonify({'status': 'error', 'message': 'Distribución no válida.'}), 400
            conn.execute('UPDATE collab_sessions SET distribucion=? WHERE id=?', (d, sess['id']))
        conn.commit()
        fila = dict(conn.execute('SELECT auto_carga, distribucion FROM collab_sessions WHERE id=?', (sess['id'],)).fetchone())
        return jsonify({'status': 'ok', 'auto_carga': bool(fila['auto_carga'] if fila['auto_carga'] is not None else 1),
                        'distribucion': fila['distribucion'] or 'turnos'})
    finally:
        conn.close()


def _collab_sesion_del_anfitrion(conn):
    """Sesión colaborativa activa si quien llama es el anfitrión que la creó
    (app con Bearer o web con cookie; nunca un invitado). Si no, None."""
    sess = _collab_active_session(conn)
    if not sess or session.get('is_collab_guest') or sess['created_by'] != g.api_user['id']:
        return None
    return sess


@app.route('/api/v1/admin/collab/pistas')
@api_admin_required
def api_v1_admin_collab_pistas():
    """Ticket C-04 (pedido por Niko): vista del anfitrión con TODO lo que se
    fue agregando a la playlist colaborativa — quién, cuándo, si ya está en la
    cola o en espera — y el resumen por participante. La app la consulta en
    vivo (cada pocos segundos) mientras la pantalla está abierta."""
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            return jsonify({'active': False})
        items = _collab_items_sesion(conn, sess)
        parts = {}
        for r in conn.execute(f'SELECT cp.*, {_COLLAB_SQL_USER_COLS} FROM collab_participants cp '
                              f'{_COLLAB_SQL_USER_JOIN} WHERE cp.session_id=? ORDER BY cp.joined_at', (sess['id'],)):
            nombre, av = _collab_part_view(r)
            especial = r['device_key'] in _COLLAB_KEYS_ESPECIALES
            parts[r['id']] = {'id': r['id'], 'name': nombre, 'avatar': av, 'badge': av.get('badge'),
                              'infinito': r['device_key'] == _COLLAB_INFINITO_KEY,
                              'especial': especial,
                              'activo': bool(r['activo'] if r['activo'] is not None else 1),
                              'total': 0, 'pending': 0}
        for t in items:
            p = parts.get(t['participant_id'])
            if p:
                p['total'] += 1
                p['pending'] += 0 if t['dispatched'] else 1
        # Especiales (Admin / Modo infinito) y los que salieron: solo si aportaron algo.
        resumen = sorted((p for p in parts.values() if p['total'] or (not p['especial'] and p['activo'])),
                         key=lambda p: (p['especial'], -p['total'], p['name'].lower()))
        pend = sum(1 for t in items if not t['dispatched'])
        return jsonify({
            'active': True,
            'total': len(items), 'pending': pend, 'dispatched': len(items) - pend,
            'played': sum(1 for t in items if t['played']),
            'participants': resumen,
            'tracks': list(reversed(items)),   # lo más nuevo primero
        })
    finally:
        conn.close()


@app.route('/api/v1/collab/infinito', methods=['POST'])
@api_login_required
def api_v1_collab_infinito():
    """Ticket C-04: la app del anfitrión (iOS / Desktop) avisa las pistas que
    acaba de sumar su modo infinito. Body: {track_ids: [...]}. Se registran en
    la playlist colaborativa como agregadas por "Modo infinito" — solo si hay
    sesión activa y quien avisa es el anfitrión que la creó; si no, se ignora
    (las apps avisan siempre, sin tener que saber si hay sesión)."""
    data = request.get_json(silent=True) or {}
    ids = []
    for x in (data.get('track_ids') or [])[:50]:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            pass
    conn = get_db_connection()
    try:
        sess = _collab_sesion_del_anfitrion(conn)
        if not sess:
            return jsonify({'status': 'ignored', 'added': 0})
        if not ids:
            return jsonify({'status': 'ok', 'added': 0})
        pid = _collab_participante_infinito(conn, sess['id'])
        ya = {r['track_id'] for r in conn.execute(
            'SELECT track_id FROM collab_queue_items WHERE session_id=?', (sess['id'],))}
        validos = {r['id'] for r in conn.execute(
            f"SELECT id FROM tracks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        agregadas = 0
        for tid in ids:
            if tid in ya or tid not in validos:
                continue
            ya.add(tid)
            conn.execute('INSERT INTO collab_queue_items '
                         '(session_id, participant_id, track_id, added_at, dispatched, dispatched_at) '
                         'VALUES (?, ?, ?, ?, 1, ?)',
                         (sess['id'], pid, tid, _utcnow_iso(), _utcnow_iso()))
            agregadas += 1
        conn.commit()
        return jsonify({'status': 'ok', 'added': agregadas})
    finally:
        conn.close()


# ── Ticket C-04 (pedido por Niko): reproductor de solo lectura para invitados ──
# La app del anfitrión (iOS / Desktop) informa qué está sonando mientras hay
# una sesión colaborativa: al cambiar de pista, al pausar/reanudar y cada ~6 s.
# Se guarda en memoria (igual que la cola de cast): es efímero, si el servidor
# se reinicia el próximo aviso lo repone. Los invitados lo consultan con
# /api/collab/sonando y ven pista, artista, álbum, formato, progreso, quién la
# agregó y lo que viene — sin ningún control.
_collab_sonando = {}            # session_id -> {track_id, position, duration, playing, next_ids, at}
_COLLAB_SONANDO_VIGENCIA_S = 45  # sin avisos por más que esto = no se sabe qué suena
_collab_sonando_disp = {}       # C-07j: (session_id, dispositivo) -> lo mismo, por dispositivo


@app.route('/api/v1/collab/sonando', methods=['POST'])
@api_login_required
def api_v1_collab_sonando_post():
    """Body: {track_id|null, position, duration, playing, next_ids: [...]}."""
    data = request.get_json(silent=True) or {}

    def _num(v):
        try:
            v = float(v)
            return v if math.isfinite(v) and v >= 0 else 0.0
        except (TypeError, ValueError):
            return 0.0
    try:
        track_id = int(data['track_id']) if data.get('track_id') is not None else None
    except (TypeError, ValueError):
        track_id = None
    next_ids = []
    for x in (data.get('next_ids') or [])[:5]:
        try:
            next_ids.append(int(x))
        except (TypeError, ValueError):
            pass
    conn = get_db_connection()
    try:
        sess = _collab_sesion_del_anfitrion(conn)
        if sess and track_id:
            # Ticket C-06: lo que empieza a sonar queda Reproducida; si quedan
            # pocas por sonar, el Modo infinito agrega su tanda de rescate.
            if _collab_marcar_sonando(conn, sess, track_id):
                _collab_infinito_tick_seguro(conn, sess)
    finally:
        conn.close()
    disp = _api_dispositivo()
    if not sess:
        # Ticket C-07: sin playlist colaborativa, un admin con el Modo TV
        # activo igual informa lo que suena (para la pantalla de la tele).
        # C-07j: por dispositivo (la tele muestra solo el que la mandó).
        if g.api_user.get('is_admin') and _tv_hay_activa():
            _tv_sonando[disp] = {
                'track_id': track_id, 'position': _num(data.get('position')), 'duration': _num(data.get('duration')),
                'playing': bool(data.get('playing')), 'next_ids': next_ids, 'at': time.time(),
            }
            return jsonify({'status': 'ok', 'tv': True})
        return jsonify({'status': 'ignored'})
    st = {'track_id': track_id, 'position': _num(data.get('position')), 'duration': _num(data.get('duration')),
          'playing': bool(data.get('playing')), 'next_ids': next_ids, 'at': time.time()}
    _collab_sonando[sess['id']] = st
    _collab_sonando_disp[(sess['id'], disp)] = st   # C-07j: para la tele
    return jsonify({'status': 'ok'})


def _collab_anfitrion_display(conn, sess):
    """Nombre y avatar del anfitrión (con la etiqueta Admin), para las pistas
    que puso él mismo — mismo criterio que su participante especial."""
    row = conn.execute('SELECT nickname AS u_nickname, avatar AS u_avatar, email AS u_email FROM users WHERE id=?',
                       (sess['created_by'],)).fetchone()
    fila = dict(row) if row else {}
    fila.update({'device_key': _COLLAB_ANFITRION_KEY, 'user_id': sess['created_by']})
    return _collab_part_view(fila)


def _collab_pistas_detalle(conn, sess, ids):
    """Detalle de las pistas `ids` (en ese orden) con quién las agregó: el
    invitado (o "Modo infinito") si están en la playlist colaborativa, o el
    anfitrión si las puso él."""
    if not ids:
        return []
    ph = ','.join('?' * len(ids))
    rows = {r['id']: dict(r) for r in conn.execute(
        f'''SELECT t.id, t.title, t.duration, t.led_color, t.is_dsd, t.dsd_rate, t.bit_depth,
                   t.is_mqa, t.codec, t.bitrate, t.compression_mode, t.sample_rate_real, t.genre,
                   al.id AS album_id, al.name AS album_name, al.cover_path, al.year AS album_year,
                   ar.id AS artist_id, ar.name AS artist_name
            FROM tracks t
            LEFT JOIN albums al ON al.id = t.album_id
            LEFT JOIN artists ar ON ar.id = al.artist_id
            WHERE t.id IN ({ph})''', ids)}
    quien = {r['track_id']: dict(r) for r in conn.execute(
        f'''SELECT cqi.track_id, cp.*, {_COLLAB_SQL_USER_COLS}
            FROM collab_queue_items cqi JOIN collab_participants cp ON cp.id = cqi.participant_id
            {_COLLAB_SQL_USER_JOIN}
            WHERE cqi.session_id=? AND cqi.track_id IN ({ph})
            ORDER BY cqi.added_at, cqi.id''', [sess['id']] + list(ids))}
    host_nombre = host_avatar = None
    out = []
    for tid in ids:
        d = rows.get(tid)
        if not d:
            continue
        fmt, led = _fmt_format(d)
        q = quien.get(tid)
        if q:
            by, by_av = _collab_part_view(q)
            by_host = (q.get('device_key') == _COLLAB_ANFITRION_KEY)
        else:
            if host_nombre is None:
                host_nombre, host_avatar = _collab_anfitrion_display(conn, sess)
            by, by_av, by_host = host_nombre, host_avatar, True
        out.append({
            'id': tid, 'title': d['title'], 'duration': d['duration'],
            'format_display': fmt, 'format_color': led, 'genre': d['genre'],
            'album_id': d['album_id'], 'album_name': d['album_name'], 'album_year': d['album_year'],
            'artist_id': d['artist_id'], 'artist_name': d['artist_name'],
            'cover_url': cover_url_filter(clean_db_path(d['cover_path'])),
            'added_by': by, 'added_by_avatar': by_av, 'added_by_host': by_host,
        })
    return out


@app.route('/api/v1/admin/collab/agregada-por')
@api_admin_required
def api_v1_admin_collab_agregada_por():
    """Ticket C-06b (pedido por Niko): quién agregó la pista que suena, para
    mostrarlo (avatar + nickname) en el reproductor en primer plano del
    anfitrión — pensado para espejarlo en la TV en un evento. Query:
    ?track_id=N. Si la pista no está en la playlist colaborativa, la puso el
    anfitrión (su avatar + Admin)."""
    try:
        track_id = int(request.args.get('track_id', ''))
    except (TypeError, ValueError):
        return jsonify({'active': False})
    conn = get_db_connection()
    try:
        sess = _collab_sesion_del_anfitrion(conn)
        if not sess:
            return jsonify({'active': False})
        detalle = _collab_pistas_detalle(conn, sess, [track_id])
        if not detalle:
            return jsonify({'active': True, 'track_id': track_id, 'added_by': None})
        d = detalle[0]
        return jsonify({'active': True, 'track_id': track_id, 'added_by': d['added_by'],
                        'added_by_avatar': d['added_by_avatar'], 'added_by_host': d['added_by_host']})
    finally:
        conn.close()


@app.route('/api/collab/sonando')
def api_collab_sonando():
    """Para la web de invitados: lo que suena en el reproductor del anfitrión."""
    conn = get_db_connection()
    try:
        sess, part = _collab_guest_ctx(conn)
        if not sess or not part:
            return jsonify({'active': False})
        return jsonify(_collab_sonando_payload(conn, sess))
    finally:
        conn.close()


def _collab_sonando_payload(conn, sess, dispositivo=None):
    """Lo que suena en el anfitrión (web de invitados y Modo Colaborativo).
    `dispositivo` (Modo TV, C-07j): solo lo que informa ese dispositivo."""
    if True:
        st = _collab_sonando_disp.get((sess['id'], dispositivo)) if dispositivo else _collab_sonando.get(sess['id'])
        ahora = time.time()
        if not st or not st['track_id'] or ahora - st['at'] > _COLLAB_SONANDO_VIGENCIA_S:
            return {'active': True, 'track': None}
        # Ticket C-06: con la cola corta del anfitrión, "a continuación" se
        # completa con el orden previsto de lo que está en espera.
        siguientes = list(st['next_ids'])
        for it in _collab_orden(conn, sess):
            if len(siguientes) >= 5:
                break
            if it['track_id'] not in siguientes and it['track_id'] != st['track_id']:
                siguientes.append(it['track_id'])
        detalle = _collab_pistas_detalle(conn, sess, [st['track_id']] + siguientes)
        if not detalle or detalle[0]['id'] != st['track_id']:
            return {'active': True, 'track': None}
        pos = st['position'] + ((ahora - st['at']) if st['playing'] else 0)
        dur = st['duration'] or (detalle[0]['duration'] or 0)
        if dur:
            pos = min(pos, dur)
        return {'active': True, 'track': detalle[0], 'next': detalle[1:],
                'position': round(pos, 1), 'duration': dur, 'playing': st['playing']}


@app.route('/colab/join/<token>', methods=['GET', 'POST'])
def collab_join(token):
    """Pantalla pública (sin sesión) a la que apunta el QR. Si el
    DISPOSITIVO (no la cookie) ya es participante de esta sesión — porque
    todavía tiene la sesión de invitado activa, o porque la perdió y volvió
    a escanear el mismo QR — se lo reengancha directo a la fila que ya
    tenía, sin pedirle el nombre de nuevo y sin resetear su cupo de pistas.
    Recién si es la primera vez que este dispositivo entra a ESTA sesión se
    muestra el formulario para elegir un nombre."""
    conn = get_db_connection()
    try:
        sess = _collab_get_session_by_token(conn, token)
        if not sess:
            return render_template('collab_join.html', error='invalid', collab_session=None, token=token)

        device_key = _collab_device_key()
        existing_row = conn.execute(
            'SELECT * FROM collab_participants WHERE session_id=? AND device_key=?',
            (sess['id'], device_key)
        ).fetchone()

        if existing_row:
            participant = dict(existing_row)
            conn.execute('UPDATE collab_participants SET last_seen=? WHERE id=?', (_utcnow_iso(), participant['id']))
            conn.commit()
            # Reconexión del mismo dispositivo (QR re-escaneado o cookie
            # perdida) — no se le vuelve a pedir avatar: ya quedó guardado
            # en su fila (avatar_category/avatar_file) desde que se unió la
            # primera vez, así que se reconstruye igual pase lo que pase con
            # la cookie.
            avatar = _collab_avatar_display(participant.get('avatar_category'),
                                             participant.get('avatar_file'), participant['name'])
        elif request.method == 'POST':
            name = (request.form.get('name') or '').strip()[:40] or 'Invitado'
            avatar_category, avatar_file = _collab_resolve_avatar_ref(request.form)
            participant, _ = _collab_find_or_create_participant(conn, sess, name, avatar_category, avatar_file)
            avatar = _collab_avatar_display(avatar_category, avatar_file, name)
        else:
            return render_template('collab_join.html', error=None, collab_session=sess, token=token,
                                    avatar_catalog=_collab_avatar_catalog())

        session.clear()
        session.permanent = True
        session['is_collab_guest']       = True
        session['collab_session_id']     = sess['id']
        session['collab_participant_id'] = participant['id']
        session['collab_name']           = participant['name']
        session['collab_avatar']         = avatar or {'type': 'initials',
                                                        'text': _collab_initials(participant['name'])}
        return redirect(url_for('collab_guest_app'))   # Ticket C-03
    finally:
        conn.close()


@app.route('/colab/salir')
def collab_leave():
    session.clear()
    return redirect(url_for('login'))


# ── Ticket C-03 (pedido por Niko): web de invitados con el diseño de Desktop ──
# Página única para celular (templates/colab_app.html + static/web/*). Lee la
# biblioteca con los mismos /api/v1 que usan las apps (_COLLAB_GUEST_API_READ)
# y agrega pistas con /api/collab/add. Sin reproductor ni configuración.

def _collab_guest_ctx(conn):
    """(sesión activa, fila del participante) del invitado actual, o (None, None)."""
    if not session.get('is_collab_guest'):
        return None, None
    sess = _collab_active_session(conn)
    if not sess or sess['id'] != session.get('collab_session_id'):
        return None, None
    row = conn.execute('SELECT * FROM collab_participants WHERE id=?',
                       (session.get('collab_participant_id'),)).fetchone()
    return sess, (dict(row) if row else None)


@app.route('/colab/app')
def collab_guest_app():
    if not session.get('is_collab_guest'):
        return redirect(url_for('login'))
    conn = get_db_connection()
    try:
        sess, part = _collab_guest_ctx(conn)
    finally:
        conn.close()
    return render_template('colab_app.html', sesion_activa=bool(sess and part))


def _collab_items_sesion(conn, sess):
    """Todas las pistas de la playlist colaborativa activa, en el orden en que
    se agregaron, con quién las agregó (avatar listo para mostrar) y si ya
    llegaron al reproductor del anfitrión. Lo usan la web de invitados
    (/api/collab/yo) y la vista del anfitrión (/api/v1/admin/collab/pistas)."""
    rows = conn.execute(
        '''SELECT cqi.id AS item_id, cqi.track_id, cqi.added_at, cqi.dispatched, cqi.participant_id, cqi.played_at,
                  t.title, t.duration, t.led_color, t.is_dsd, t.dsd_rate, t.bit_depth,
                  t.is_mqa, t.codec, t.bitrate, t.compression_mode, t.sample_rate_real,
                  al.id AS album_id, al.name AS album_name, al.cover_path, al.year AS album_year,
                  ar.id AS artist_id, ar.name AS artist_name,
                  cp.name, cp.avatar_category, cp.avatar_file, cp.device_key, cp.user_id,
                  u.nickname AS u_nickname, u.avatar AS u_avatar, u.email AS u_email
           FROM collab_queue_items cqi
           JOIN tracks t ON t.id = cqi.track_id
           LEFT JOIN albums al ON al.id = t.album_id
           LEFT JOIN artists ar ON ar.id = al.artist_id
           JOIN collab_participants cp ON cp.id = cqi.participant_id
           LEFT JOIN users u ON u.id = cp.user_id
           WHERE cqi.session_id=? ORDER BY cqi.added_at, cqi.id''', (sess['id'],)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        fmt, led = _fmt_format(d)
        nombre, av = _collab_part_view(d)
        out.append({
            'id': d['track_id'], 'title': d['title'], 'duration': d['duration'],
            'duration_fmt': _fmt_seconds(d['duration']),
            'format_display': fmt, 'format_color': led,
            'album_id': d['album_id'], 'album_name': d['album_name'], 'album_year': d['album_year'],
            'artist_id': d['artist_id'], 'artist_name': d['artist_name'],
            'cover_url': cover_url_filter(clean_db_path(d['cover_path'])),
            'added_at': d['added_at'], 'dispatched': bool(d['dispatched']),
            # Ticket C-06: ya sonó (o se saltó) — y el id de la fila, porque la
            # misma pista puede volver a pedirse después de sonar.
            'played': d['played_at'] is not None, 'item_id': d['item_id'],
            'participant_id': d['participant_id'],
            'added_by': nombre,
            'added_by_avatar': av,
            'added_by_badge': av.get('badge'),
            'added_by_infinito': d['device_key'] == _COLLAB_INFINITO_KEY,
        })
    return out


@app.route('/api/collab/yo')
def api_collab_yo():
    """Estado del invitado: nombre, avatar, cupo, permiso de delegado, sus
    pistas agregadas (con si ya llegaron al anfitrión) y la cola de la sesión."""
    conn = get_db_connection()
    try:
        sess, part = _collab_guest_ctx(conn)
        if not sess or not part:
            return jsonify({'active': False})
        return jsonify(_collab_yo_payload(conn, sess, part))
    finally:
        conn.close()


def _collab_yo_payload(conn, sess, part):
    """Estado del participante (invitado web o usuario con la app): nombre,
    avatar, cupo (Ticket C-05: cupo fijo con bloqueo), delegado, sus pistas y
    las de la sesión."""
    fila = conn.execute(f'SELECT cp.*, {_COLLAB_SQL_USER_COLS} FROM collab_participants cp '
                        f'{_COLLAB_SQL_USER_JOIN} WHERE cp.id=?', (part['id'],)).fetchone()
    nombre, avatar = _collab_part_view(fila)
    cupo = _collab_cupo(conn, sess, fila)
    sesion = _collab_items_sesion(conn, sess)
    mias = [t for t in sesion if t['participant_id'] == part['id']]
    host_nombre = _collab_anfitrion_display(conn, sess)[0]
    return {
        'active': True,
        'joined': True,
        'is_user': fila['user_id'] is not None,
        'host_name': host_nombre,
        'name': nombre,
        'avatar': avatar,
        'avatar_category': fila['avatar_category'], 'avatar_file': fila['avatar_file'],
        'max_tracks': sess['max_tracks'], 'window_hours': sess['window_hours'],
        'used': cupo['used'], 'remaining': cupo['remaining'],
        'blocked_until': cupo['blocked_until'], 'blocked_seconds': cupo['blocked_seconds'],
        # Ticket C-06: con Recarga automática el botón del delegado no hace
        # falta (todo entra solo): se informa como sin permiso.
        'can_pull': bool(fila['can_pull']) and not _collab_auto_carga(sess),
        'auto_carga': _collab_auto_carga(sess),
        # Ticket C-04: para el botón del delegado — cuántas pistas de la
        # sesión todavía no llegaron al reproductor del anfitrión y si hay
        # una recarga pedida que la app del anfitrión aún no atendió.
        'pending_count': sum(1 for t in sesion if not t['dispatched']),
        'pull_pending': bool(sess.get('pull_requested_at')),
        'mine': mias, 'session_tracks': sesion,
    }


def _collab_solicitar_pull(conn, sess, part):
    """El delegado pide que el anfitrión cargue las pistas nuevas. Devuelve
    (dict, http_status)."""
    row = conn.execute('SELECT can_pull FROM collab_participants WHERE id=?', (part['id'],)).fetchone()
    if not row or not row['can_pull']:
        return {'status': 'error', 'message': 'No tienes permiso para recargar la cola.'}, 403
    if _collab_auto_carga(sess):
        return {'status': 'error', 'message': 'La cola se recarga sola (Recarga automática activada).'}, 409
    conn.execute('UPDATE collab_sessions SET pull_requested_at=?, pull_requested_by=? WHERE id=?',
                 (_utcnow_iso(), part['id'], sess['id']))
    conn.commit()
    return {'status': 'ok'}, 200


@app.route('/api/collab/perfil', methods=['POST'])
def api_collab_perfil():
    """El invitado cambia su apodo y/o avatar (extracto del perfil de un
    usuario normal). Mismas validaciones que el formulario de ingreso."""
    data = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        sess, part = _collab_guest_ctx(conn)
        if not sess or not part:
            return jsonify({'status': 'error', 'message': 'La sesión colaborativa ya terminó.'}), 410
        name = (data.get('name') or '').strip()[:40] or part['name']
        if 'avatar_category' in data or 'avatar_file' in data:
            cat, fname = _collab_resolve_avatar_ref(data)
        else:
            cat, fname = part.get('avatar_category'), part.get('avatar_file')
        conn.execute('UPDATE collab_participants SET name=?, avatar_category=?, avatar_file=? WHERE id=?',
                     (name, cat, fname, part['id']))
        conn.commit()
        avatar = _collab_avatar_display(cat, fname, name)
        session['collab_name'] = name
        session['collab_avatar'] = avatar
        return jsonify({'status': 'ok', 'name': name, 'avatar': avatar,
                        'avatar_category': cat, 'avatar_file': fname})
    finally:
        conn.close()


@app.route('/api/collab/add', methods=['POST'])
def api_collab_add():
    if not session.get('is_collab_guest'):
        return jsonify({'status': 'error', 'message': 'No sos parte de una sesión colaborativa activa.'}), 403
    data = request.get_json(silent=True) or {}
    track_id = data.get('track_id')
    if not track_id:
        return jsonify({'status': 'error', 'message': 'Falta track_id'}), 400
    conn = get_db_connection()
    try:
        result = _collab_try_add(
            conn, session['collab_session_id'], session['collab_participant_id'], track_id,
            confirm_album=bool(data.get('confirm_album'))
        )
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/collab/mi-permiso')
def api_collab_mi_permiso():
    """Consultado en loop corto por collab_guest.js para mostrar/ocultar el
    botón 'Actualizar cola' en el header del invitado — el admin puede
    asignar/quitar el permiso en cualquier momento, así que no alcanza con
    lo que ya quedó en la cookie de sesión al unirse (ver collab_join)."""
    if not session.get('is_collab_guest'):
        return jsonify({'can_pull': False})
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess or sess['id'] != session.get('collab_session_id'):
            return jsonify({'can_pull': False})
        row = conn.execute(
            'SELECT can_pull FROM collab_participants WHERE id=?', (session['collab_participant_id'],)
        ).fetchone()
        return jsonify({'can_pull': bool(row['can_pull']) if row else False})
    finally:
        conn.close()


@app.route('/api/collab/solicitar-pull', methods=['POST'])
def api_collab_solicitar_pull():
    """El delegado pide, desde SU celular, que el anfitrión cargue las
    últimas pistas agregadas a la cola. No ejecuta el pull acá — solo dispara
    la bandera que /api/admin/colaborativa/estado le hace llegar al
    dispositivo del anfitrión (el único que tiene la playlist real, ver
    ticket) en el próximo poll (máx. 6s, ver collab_host.js)."""
    if not session.get('is_collab_guest'):
        return jsonify({'status': 'error', 'message': 'No sos parte de una sesión colaborativa activa.'}), 403
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess or sess['id'] != session.get('collab_session_id'):
            return jsonify({'status': 'error', 'message': 'La sesión colaborativa ya terminó.'}), 410
        row = conn.execute(
            'SELECT can_pull FROM collab_participants WHERE id=?', (session['collab_participant_id'],)
        ).fetchone()
        if not row or not row['can_pull']:
            return jsonify({'status': 'error', 'message': 'No tenés permiso para actualizar la cola.'}), 403
        if _collab_auto_carga(sess):   # Ticket C-06
            return jsonify({'status': 'error', 'message': 'La cola se recarga sola (Recarga automática activada).'}), 409
        conn.execute(
            'UPDATE collab_sessions SET pull_requested_at=?, pull_requested_by=? WHERE id=?',
            (_utcnow_iso(), session['collab_participant_id'], sess['id'])
        )
        conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()



# ── Ticket C-05 (pedido por Niko): Modo Colaborativo para usuarios con la app ──
# Un usuario registrado se une a la playlist colaborativa desde la app (QR o
# código): queda como participante atado a su CUENTA (no al dispositivo), con
# nombre y avatar de su perfil y el mismo cupo que los invitados. Mismas
# reglas que /api/collab/* (duplicados, aviso de álbum, cupo); el anfitrión no
# puede unirse a su propia sesión.

def _collab_user_ctx(conn, user):
    """(sesión activa, participante activo) del usuario con cuenta, o (sess, None)."""
    sess = _collab_active_session(conn)
    if not sess:
        return None, None
    row = conn.execute('SELECT * FROM collab_participants WHERE session_id=? AND device_key=? '
                       'AND COALESCE(activo, 1)=1', (sess['id'], _collab_user_key(user['id']))).fetchone()
    return sess, (dict(row) if row else None)


@app.route('/api/v1/collab/activa')
@api_login_required
def api_v1_collab_activa():
    """Para el aviso de Home: ¿hay una sesión activa? ¿soy el anfitrión? ¿ya estoy dentro?"""
    conn = get_db_connection()
    try:
        sess, part = _collab_user_ctx(conn, g.api_user)
        if not sess:
            return jsonify({'active': False})
        return jsonify({'active': True, 'host_name': _collab_anfitrion_display(conn, sess)[0],
                        'is_host': sess['created_by'] == g.api_user['id'], 'joined': part is not None})
    finally:
        conn.close()


@app.route('/api/v1/collab/unirse', methods=['POST'])
@api_login_required
def api_v1_collab_unirse():
    """Body: {code} (K7F-Q2M, con o sin guion) o {token} (el del QR, o el link
    completo del QR). Devuelve el mismo estado que /api/v1/collab/yo."""
    data = request.get_json(silent=True) or {}
    user = g.api_user
    if not _collab_intento_codigo_permitido(f"user:{user['id']}"):
        return jsonify({'status': 'error', 'error': 'demasiados_intentos',
                        'message': 'Demasiados intentos. Espera un minuto y vuelve a probar.'}), 429
    conn = get_db_connection()
    try:
        sess = None
        token = (data.get('token') or '').strip()
        if token:
            m = re.search(r'/colab/join/([^/?#\s]+)', token)
            sess = _collab_get_session_by_token(conn, m.group(1) if m else token)
        elif data.get('code'):
            sess = _collab_sesion_por_codigo(conn, data.get('code'))
        if not sess:
            return jsonify({'status': 'error', 'error': 'codigo_invalido',
                            'message': 'Ese código o QR no corresponde a una playlist colaborativa activa.'}), 404
        if sess['created_by'] == user['id']:
            return jsonify({'status': 'error', 'error': 'es_anfitrion',
                            'message': 'Eres el anfitrión de esta playlist colaborativa: no puedes unirte como participante.'}), 409
        key = _collab_user_key(user['id'])
        nombre = _collab_nombre_usuario(user.get('nickname'), user.get('email'))
        row = conn.execute('SELECT id FROM collab_participants WHERE session_id=? AND device_key=?',
                           (sess['id'], key)).fetchone()
        if row:
            conn.execute('UPDATE collab_participants SET activo=1, name=?, last_seen=? WHERE id=?',
                         (nombre, _utcnow_iso(), row['id']))
            pid = row['id']
        else:
            pid = conn.execute(
                'INSERT INTO collab_participants (session_id, device_key, name, joined_at, last_seen, user_id, activo) '
                'VALUES (?, ?, ?, ?, ?, ?, 1)',
                (sess['id'], key, nombre, _utcnow_iso(), _utcnow_iso(), user['id'])).lastrowid
        conn.commit()
        part = dict(conn.execute('SELECT * FROM collab_participants WHERE id=?', (pid,)).fetchone())
        return jsonify(dict(_collab_yo_payload(conn, sess, part), status='ok'))
    finally:
        conn.close()


@app.route('/api/v1/collab/yo')
@api_login_required
def api_v1_collab_yo():
    conn = get_db_connection()
    try:
        sess, part = _collab_user_ctx(conn, g.api_user)
        if not sess:
            return jsonify({'active': False, 'joined': False})
        if not part:
            return jsonify({'active': True, 'joined': False})
        conn.execute('UPDATE collab_participants SET last_seen=? WHERE id=?', (_utcnow_iso(), part['id']))
        conn.commit()
        return jsonify(_collab_yo_payload(conn, sess, part))
    finally:
        conn.close()


@app.route('/api/v1/collab/agregar', methods=['POST'])
@api_login_required
def api_v1_collab_agregar():
    """Body: {track_id, confirm_album?}. Mismas respuestas que /api/collab/add."""
    data = request.get_json(silent=True) or {}
    track_id = data.get('track_id')
    if not track_id:
        return jsonify({'status': 'error', 'message': 'Falta track_id'}), 400
    conn = get_db_connection()
    try:
        sess, part = _collab_user_ctx(conn, g.api_user)
        if not sess or not part:
            return jsonify({'status': 'expired', 'message': 'No estás en una playlist colaborativa activa.'})
        return jsonify(_collab_try_add(conn, sess['id'], part['id'], track_id,
                                       confirm_album=bool(data.get('confirm_album'))))
    finally:
        conn.close()


@app.route('/api/v1/collab/sonando-ahora')
@api_login_required
def api_v1_collab_sonando_ahora():
    """Reproductor de solo lectura del Modo Colaborativo (mismo contenido que
    /api/collab/sonando de los invitados)."""
    conn = get_db_connection()
    try:
        sess, part = _collab_user_ctx(conn, g.api_user)
        if not sess or not part:
            return jsonify({'active': False})
        return jsonify(_collab_sonando_payload(conn, sess))
    finally:
        conn.close()


@app.route('/api/v1/collab/solicitar-pull', methods=['POST'])
@api_login_required
def api_v1_collab_solicitar_pull():
    conn = get_db_connection()
    try:
        sess, part = _collab_user_ctx(conn, g.api_user)
        if not sess or not part:
            return jsonify({'status': 'error', 'message': 'La sesión colaborativa ya terminó.'}), 410
        body, code = _collab_solicitar_pull(conn, sess, part)
        return jsonify(body), code
    finally:
        conn.close()


@app.route('/api/v1/collab/salir', methods=['POST'])
@api_login_required
def api_v1_collab_salir():
    """Sale del Modo Colaborativo: sus pistas quedan en la sesión; deja de
    figurar como participante y pierde el permiso de delegado. Puede volver a
    unirse mientras la sesión siga activa (conserva su cupo)."""
    conn = get_db_connection()
    try:
        sess, part = _collab_user_ctx(conn, g.api_user)
        if sess and part:
            conn.execute('UPDATE collab_participants SET activo=0, can_pull=0 WHERE id=?', (part['id'],))
            conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


@app.route('/api/v1/collab/anfitrion', methods=['POST'])
@api_login_required
def api_v1_collab_anfitrion():
    """Ticket C-05: pistas que el anfitrión agrega a su cola durante la sesión
    ("Agregar a la cola" / "Reproducir a continuación" / "Agregar a la cola"
    del aviso de reemplazo). Body: {track_ids}. Quedan con su avatar y la
    etiqueta Admin, ya entregadas y sin cupo. Sin sesión (o si no es el
    anfitrión) se ignora: las apps avisan siempre."""
    data = request.get_json(silent=True) or {}
    ids = []
    for x in (data.get('track_ids') or [])[:200]:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            pass
    conn = get_db_connection()
    try:
        sess = _collab_sesion_del_anfitrion(conn)
        if not sess:
            return jsonify({'status': 'ignored', 'added': 0})
        if not ids:
            return jsonify({'status': 'ok', 'added': 0})
        pid = _collab_participante_especial(conn, sess['id'], _COLLAB_ANFITRION_KEY, 'Anfitrión',
                                            user_id=sess['created_by'])
        # Ticket C-06: solo se evita duplicar lo que todavía no sonó.
        ya = {r['track_id'] for r in conn.execute(
            'SELECT track_id FROM collab_queue_items WHERE session_id=? AND played_at IS NULL', (sess['id'],))}
        validos = {r['id'] for r in conn.execute(
            f"SELECT id FROM tracks WHERE id IN ({','.join('?' * len(ids))})", ids)}
        agregadas = 0
        for tid in ids:
            if tid in ya or tid not in validos:
                continue
            ya.add(tid)
            conn.execute('INSERT INTO collab_queue_items '
                         '(session_id, participant_id, track_id, added_at, dispatched, dispatched_at) '
                         'VALUES (?, ?, ?, ?, 1, ?)',
                         (sess['id'], pid, tid, _utcnow_iso(), _utcnow_iso()))
            agregadas += 1
        conn.commit()
        return jsonify({'status': 'ok', 'added': agregadas})
    finally:
        conn.close()


@app.route('/colab', methods=['GET', 'POST'])
def collab_codigo():
    """Ticket C-05: entrar a la playlist colaborativa escribiendo el código
    (para quien no puede escanear el QR). Lleva al mismo ingreso que el QR."""
    error = None
    code = ''
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        if not _collab_intento_codigo_permitido(f"ip:{request.remote_addr}"):
            error = 'Demasiados intentos. Espera un minuto y vuelve a probar.'
        else:
            conn = get_db_connection()
            try:
                sess = _collab_sesion_por_codigo(conn, code)
            finally:
                conn.close()
            if sess:
                return redirect(url_for('collab_join', token=sess['token']))
            error = 'Ese código no corresponde a una playlist colaborativa activa.'
    return render_template('collab_codigo.html', error=error, code=code)

def _fmt_seconds(seconds):
    if not seconds or seconds < 0:
        return "0:00"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

def _fmt_bitrate(bps, con_perdida=False, kbps_nominal=None):
    if not bps:
        return "N/A"
    if con_perdida:
        return f"{kbps_nominal or int(bps / 1000)} kbps · Lossy"
    kbps = bps / 1000
    if kbps >= 1000:
        label = f"{kbps/1000:.2f} Mbps"
    else:
        label = f"{int(kbps):,} kbps"
    if kbps <= 320:
        tag = "MP3-level"
    elif kbps <= 1412:
        tag = "CD Lossless"
    else:
        tag = "Hi-Res"
    return f"{label} · {tag}"

def _container_ext(track):
    """Ticket 16 Lote 3 — extensión real del contenedor ('dsf'/'dff'/'flac'/
    etc, sin punto, minúscula), a partir de file_path. El cliente iOS la
    necesita para elegir el decoder correcto de SFBAudioEngine en pistas
    DSD (DSF vs DFF no son intercambiables) — antes de esto no había forma
    de saberlo del lado nativo. Mismo criterio que ya usa el resto del
    proyecto (audio_url_filter, _serve_audio, etc): os.path.splitext.
    """
    file_path = track.get('file_path') or ''
    ext = os.path.splitext(file_path)[1].lower().lstrip('.')
    return ext or None

def _dsd_label(track):
    """Return clean DSD label: DSD64, DSD128, DSD256 etc."""
    rate = (track.get('dsd_rate') or '').strip()
    if rate:
        # Already in correct form: 'DSD64', 'DSD128', 'DSD256'
        return rate if rate.startswith('DSD') else 'DSD' + rate
    # Derive from sample_rate_real
    sr = track.get('sample_rate_real') or 0
    if   sr >= 11289600: return 'DSD256'
    elif sr >=  5644800: return 'DSD128'
    elif sr >=  2822400: return 'DSD64'
    elif sr >=  1411200: return 'DSD32'
    return 'DSD'

def _fmt_format(track):
    """Return (display_label, led_color) using DB led_color as sole truth."""
    led = (track.get('led_color') or 'yellow').lower()
    if not track.get('is_dsd') and not track.get('is_mqa') and _es_con_perdida(track):
        # Distintivo plateado; led_color en BD sigue 'yellow' (DAC).
        return _lossy_label(track), LOSSY_COLOR
    if track.get('is_dsd'):
        label = _dsd_label(track)
    elif track.get('is_mqa'):
        label = 'MQA Studio' if led == 'blue' else ('MQB' if led == 'magenta' else 'MQA')
    else:
        codec = (track.get('codec') or 'FLAC').upper()
        label = f'{codec} · Hi-Res' if led == 'white' else codec
    return label, led

def _led_for_album_tracks(conn, album_id):
    """Get dominant (highest-quality) LED color for an album from its tracks."""
    priority = {c: i for i, c in enumerate(reversed(LED_ORDER))}
    rows = conn.execute(
        'SELECT led_color, COUNT(*) as c FROM tracks WHERE album_id=? AND led_color IS NOT NULL GROUP BY led_color',
        (album_id,)
    ).fetchall()
    if not rows:
        return 'yellow'
    # Return highest-priority color that exists
    best = sorted(rows, key=lambda r: priority.get(r['led_color'], -1), reverse=True)
    return best[0]['led_color']

def track_to_json(t):
    """Serialize a track Row/dict with all display fields. led_color comes from DB, never recomputed."""
    d = dict(t)
    d['file_path']   = clean_db_path(d.get('file_path'))
    d['cover_path']  = clean_db_path(d.get('cover_path'))
    d['cover_url']   = cover_url_filter(d['cover_path'])
    d['audio_url']   = audio_url_filter(d['file_path'])
    d['duration_fmt'] = _fmt_seconds(d.get('duration'))
    fmt, led = _fmt_format(d)
    d['format_display'] = fmt
    d['format_color']   = led
    # led_color stays as-is from DB
    con_perdida = led == LOSSY_COLOR
    d['con_perdida']     = con_perdida
    d['kbps']            = _kbps_nominal(d) if con_perdida else None
    d['bitrate_fmt']     = _fmt_bitrate(d.get('bitrate'), con_perdida, d['kbps'])
    sr = d.get('sample_rate_real')
    d['sample_rate_fmt'] = f"{sr/1000:.1f} kHz" if sr else "N/A"
    # MP3/AAC no tienen profundidad de bits (en la BD queda 0): antes se
    # mostraba "24 bit" por el default.
    if con_perdida and not d.get('bit_depth'):
        d['bit_depth_fmt'] = '—'
    else:
        d['bit_depth_fmt'] = f"{d.get('bit_depth') or 24} bit"
    return d

# ── Jinja2 filters ────────────────────────────────────────────────────────────

@app.template_filter('format_size')
def format_size_filter(b):
    if not b: return "0 B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.2f} MB"
    return f"{b/1024**3:.2f} GB"

@app.template_filter('format_duration')
def format_duration_filter(seconds):
    return _fmt_seconds(seconds)

@app.template_filter('cover_url')
def cover_url_filter(cover_path):
    if not cover_path: return ''
    path = clean_db_path(cover_path).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if path.startswith(root + '/'):
        path = path[len(root)+1:]
    elif path.startswith(root):
        path = path[len(root):]
    # Encode each segment so special chars like # don't break URLs
    encoded = '/'.join(quote(seg, safe='') for seg in path.split('/'))
    return f"/cover/{encoded}"

@app.template_filter('audio_url')
def audio_url_filter(file_path):
    if not file_path: return ''
    path = clean_db_path(file_path).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if path.startswith(root + '/'):
        path = path[len(root)+1:]
    elif path.startswith(root):
        path = path[len(root):]
    # Encode each segment so special chars like # don't break URLs
    encoded = '/'.join(quote(seg, safe='') for seg in path.split('/'))
    # DSD files (DSF/DFF) must be transcoded on the fly — browsers can't play raw DSD
    ext = os.path.splitext(file_path)[1].lower()
    if ext in ('.dsf', '.dff'):
        return f"/stream-dsd/{encoded}"
    return f"/audio/{encoded}"

def build_similar_artists(conn, similar_artists_json, limit=12):
    """Parsea artists.similar_artists_json y arma la lista enriquecida con
       cover_url (portada del álbum más popular de cada artista, si existe
       en la biblioteca). Usado por /artist/<id> (tab Similares) y por
       /api/track/<id>/similar-artists (NP overlay)."""
    similar_artists = []
    if not similar_artists_json:
        return similar_artists
    try:
        similar_raw = json.loads(similar_artists_json)
    except Exception:
        return similar_artists
    if not isinstance(similar_raw, list):
        return similar_artists
    for s in similar_raw[:limit]:
        name = s.get('name', '') if isinstance(s, dict) else (s if isinstance(s, str) else '')
        if not name:
            continue
        existing = conn.execute(
            'SELECT id FROM artists WHERE LOWER(name)=LOWER(?)', (name,)
        ).fetchone()
        cover_url = ''
        if existing:
            cov = conn.execute(
                '''SELECT al.cover_path FROM albums al
                   LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                   WHERE al.artist_id=?
                   ORDER BY COALESCE(apc.pop_score,0) DESC, al.year DESC
                   LIMIT 1''',
                (existing['id'],)
            ).fetchone()
            if cov and cov['cover_path']:
                cover_url = cover_url_filter(clean_db_path(cov['cover_path']))
        similar_artists.append({
            'name': name,
            'id': existing['id'] if existing else None,
            'cover_url': cover_url
        })
    return similar_artists

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    """Serve SW from root so it controls the entire app scope."""
    from flask import send_from_directory
    resp = send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/')
def home():
    conn = get_db_connection()
    try:
        total_artists  = conn.execute('SELECT COUNT(*) FROM artists a WHERE EXISTS (SELECT 1 FROM albums al WHERE al.artist_id=a.id)').fetchone()[0]
        total_albums   = conn.execute('SELECT COUNT(*) FROM albums').fetchone()[0]
        total_tracks   = conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]
        total_duration = conn.execute('SELECT COALESCE(SUM(duration),0) FROM tracks').fetchone()[0]
        total_size     = conn.execute('SELECT COALESCE(SUM(file_size),0) FROM tracks').fetchone()[0]

        # LED breakdown — direct from DB field, no computation
        led_rows = conn.execute(
            f'SELECT {_quality_color_sql("")} AS led_color, COUNT(*) as c FROM tracks WHERE led_color IS NOT NULL GROUP BY 1 ORDER BY c DESC'
        ).fetchall()
        led_counts = {r['led_color']: r['c'] for r in led_rows}

        # Genre breakdown (top 8)
        genre_rows = conn.execute(
            'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" GROUP BY genre ORDER BY c DESC LIMIT 8'
        ).fetchall()
        genres = [(r['genre'], r['c']) for r in genre_rows]

        # Recently added albums
        recent_albums_raw = conn.execute('''
            SELECT al.id, al.name, al.cover_path, al.year, al.track_count, al.total_duration,
                   al.artist_id, al.primary_format, ar.name as artist_name
            FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
            ORDER BY al.created_at DESC LIMIT 20
        ''').fetchall()
        recent_albums = []
        for a in recent_albums_raw:
            d = {**dict(a), 'cover_path': clean_db_path(a['cover_path'])}
            led = conn.execute(
                """SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END AS led_color FROM tracks WHERE album_id=?
                   ORDER BY CASE led_color
                     WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                     WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                     ELSE 6 END, (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) LIMIT 1""",
                (a['id'],)
            ).fetchone()
            d['album_led'] = led['led_color'] if led else 'yellow'
            recent_albums.append(d)

        # ── RichMetaPro data ──────────────────────────────────────────────────
        mood_rows = conn.execute(
            'SELECT mood, COUNT(*) as c FROM track_meta WHERE mood IS NOT NULL GROUP BY mood ORDER BY c DESC LIMIT 14'
        ).fetchall()
        moods = [(r['mood'], r['c']) for r in mood_rows]

        momento_rows = conn.execute(
            'SELECT momento, COUNT(*) as c FROM track_meta WHERE momento IS NOT NULL GROUP BY momento ORDER BY c DESC'
        ).fetchall()
        momentos = [(r['momento'], r['c']) for r in momento_rows]

        era_order = [
            'early_rock_era', 'british_invasion_era', 'classic_rock_era',
            'nwobhm_synth_era', 'grunge_alternative_era', 'post_millennial_era',
            'streaming_era', 'current_era'
        ]
        era_raw  = conn.execute('SELECT era, COUNT(*) as c FROM track_meta WHERE era IS NOT NULL GROUP BY era').fetchall()
        era_dict = {r['era']: r['c'] for r in era_raw}
        eras = [(e, era_dict[e]) for e in era_order if e in era_dict]

        tema_rows = conn.execute(
            'SELECT tema_lirico, COUNT(*) as c FROM track_meta WHERE tema_lirico IS NOT NULL GROUP BY tema_lirico ORDER BY c DESC LIMIT 10'
        ).fetchall()
        temas = [(r['tema_lirico'], r['c']) for r in tema_rows]

        tier_rows = conn.execute(
            'SELECT tier, COUNT(*) as c FROM track_meta WHERE tier IS NOT NULL GROUP BY tier ORDER BY c DESC'
        ).fetchall()
        tiers = {r['tier']: r['c'] for r in tier_rows}

        lrc = conn.execute('SELECT SUM(has_lyrics) as wl, SUM(has_synced_lrc) as syn FROM track_meta').fetchone()
        lyrics_stats = {'with_lyrics': lrc['wl'] or 0, 'synced': lrc['syn'] or 0}

        # Language (idioma) breakdown from track_meta
        lang_rows = conn.execute(
            'SELECT idioma, COUNT(*) as c FROM track_meta WHERE idioma IS NOT NULL AND idioma!="" GROUP BY idioma ORDER BY c DESC LIMIT 12'
        ).fetchall()
        languages = [(r['idioma'], r['c']) for r in lang_rows]

        # Genre primary breakdown from track_meta (enriched metadata)
        genre_primary_rows = conn.execute(
            'SELECT genre_primary, COUNT(*) as c FROM track_meta WHERE genre_primary IS NOT NULL AND genre_primary!="" GROUP BY genre_primary ORDER BY c DESC LIMIT 15'
        ).fetchall()
        genres_primary = [(r['genre_primary'], r['c']) for r in genre_primary_rows]

        # Unlimited version of genres_primary, dedicated to the Búsqueda
        # Avanzada Género capsule (see _advanced_search_options() for the
        # matching copy used on /busqueda-avanzada itself). Kept separate
        # from genres_primary above so this compact section's LIMIT 15 is
        # untouched.
        adv_genre_primary_rows = conn.execute(
            'SELECT genre_primary, COUNT(*) as c FROM track_meta WHERE genre_primary IS NOT NULL AND genre_primary!="" GROUP BY genre_primary ORDER BY c DESC'
        ).fetchall()
        adv_genres_primary = [(r['genre_primary'], r['c']) for r in adv_genre_primary_rows]

        # Max era count for proportional bars
        max_era_count = max((c for _, c in eras), default=1)

        # All genres for extended selector (sorted by count desc)
        all_genre_rows = conn.execute(
            'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" GROUP BY genre ORDER BY c DESC'
        ).fetchall()
        all_genres = [(r['genre'], r['c']) for r in all_genre_rows]

        # ── Búsqueda Avanzada support data ──────────────────────────────────
        # Distinct release years (for the "Año" filter) and artist
        # nationalities (for "País Origen"), both driven directly by existing
        # columns (albums.year, artists.nationality) — no new DB fields.
        year_rows = conn.execute(
            'SELECT DISTINCT year FROM albums WHERE year IS NOT NULL ORDER BY year DESC'
        ).fetchall()
        available_years = [r['year'] for r in year_rows]

        nat_rows = conn.execute(
            'SELECT nationality, COUNT(*) as c FROM artists '
            'WHERE nationality IS NOT NULL AND nationality!="" '
            'GROUP BY nationality ORDER BY c DESC'
        ).fetchall()
        nationalities = [(r['nationality'], r['c']) for r in nat_rows]

        return render_template('home.html',
            total_artists=total_artists, total_albums=total_albums,
            total_tracks=total_tracks,
            duration_hours=total_duration / 3600,
            size_tb=total_size / (1024**4),
            led_counts=led_counts,
            led_order=LED_ORDER, led_labels=LED_LABELS,
            genres=genres, all_genres=all_genres,
            genres_primary=genres_primary, adv_genres_primary=adv_genres_primary,
            languages=languages,
            recent_albums=recent_albums,
            moods=moods, momentos=momentos, eras=eras,
            max_era_count=max_era_count,
            temas=temas, tiers=tiers, lyrics_stats=lyrics_stats,
            available_years=available_years, nationalities=nationalities)
    finally:
        conn.close()


@app.route('/letter/<letter>')
def letter(letter):
    conn = get_db_connection()
    try:
        artists = conn.execute('''
            SELECT
              a.id, a.name, a.nationality,
              a.lastfm_listeners, a.lastfm_playcount,
              COUNT(DISTINCT al.id)                        AS album_count,
              COALESCE(MAX(apc.pop_score), 0)              AS pop_score,
              (SELECT t.genre
               FROM tracks t JOIN albums al2 ON t.album_id=al2.id
               WHERE al2.artist_id=a.id AND t.genre IS NOT NULL AND t.genre != ""
               GROUP BY t.genre ORDER BY COUNT(*) DESC LIMIT 1)  AS most_common_genre
            FROM artists a
            INNER JOIN albums al  ON al.artist_id = a.id
            LEFT  JOIN album_pop_cache apc ON apc.album_id = al.id
            WHERE a.letter = ?
            GROUP BY a.id
            HAVING album_count > 0
            ORDER BY a.name
        ''', (letter,)).fetchall()
        return render_template('letter.html', artists=[dict(r) for r in artists], letter=letter)
    finally:
        conn.close()


@app.route('/artist/<int:artist_id>')
def artist(artist_id):
    conn = get_db_connection()
    try:
        ar = conn.execute('SELECT * FROM artists WHERE id=?', (artist_id,)).fetchone()
        if not ar: return "Artist not found", 404
        # albums already have track_count and total_duration pre-computed
        albums_raw = conn.execute(
            'SELECT *, artist_id FROM albums WHERE artist_id=? ORDER BY year, name', (artist_id,)
        ).fetchall()
        total_tracks = sum(a['track_count'] or 0 for a in albums_raw)

        # Enrich each album with dominant led_color from its tracks
        albums = []
        for a in albums_raw:
            d = dict(a)
            d['cover_path'] = clean_db_path(d.get('cover_path'))
            # Dominant led_color: highest-quality color that appears most
            led_row = conn.execute(
                '''SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END AS led_color FROM tracks
                   WHERE album_id=? AND led_color IS NOT NULL
                   GROUP BY 1
                   ORDER BY CASE led_color
                     WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                     WHEN 'red'     THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                     ELSE 6 END, MIN((COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')))
                   LIMIT 1''',
                (a['id'],)
            ).fetchone()
            d['album_led'] = led_row['led_color'] if led_row else 'yellow'
            albums.append(d)

        ar_data = dict(ar)
        ar_data['flag'] = nationality_flag(ar_data.get('nationality') or '')

        # Dominant genre across artist's tracks
        genre_row = conn.execute('''
            SELECT COALESCE(tm.genre_primary, t.genre) as g, COUNT(*) as c
            FROM tracks t
            JOIN albums al ON al.id=t.album_id
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            WHERE al.artist_id=?
              AND COALESCE(tm.genre_primary, t.genre) IS NOT NULL
              AND COALESCE(tm.genre_primary, t.genre) != ''
            GROUP BY g ORDER BY c DESC LIMIT 5
        ''', (artist_id,)).fetchall()
        genres = [r['g'] for r in genre_row]

        # Similar artists — enriquecidos con cover_url para el carrusel (tab Similares)
        similar_artists = build_similar_artists(conn, ar_data.get('similar_artists_json'), limit=12)

        # Top / most popular tracks for this artist — powers the "Populares" tab
        top_tracks_raw = conn.execute(
            '''SELECT t.*, al.name as album_name, al.cover_path as album_cover,
                      COALESCE(tpc.pop_score, 0) as pop_score,
                      COALESCE(tpc.stars, 0) as pop_stars
               FROM tracks t
               JOIN albums al ON al.id = t.album_id
               LEFT JOIN track_pop_cache tpc ON tpc.track_id = t.id
               WHERE al.artist_id = ?
               ORDER BY pop_score DESC, t.title
               LIMIT 10''',
            (artist_id,)
        ).fetchall()
        top_tracks = []
        for t in top_tracks_raw:
            d = dict(t)
            d['file_path'] = clean_db_path(d.get('file_path'))
            cover = clean_db_path(d.get('album_cover'))
            d['cover_path'] = cover
            d['cover_url'] = cover_url_filter(cover)
            d['audio_url'] = audio_url_filter(d['file_path'])
            fmt, led = _fmt_format(d)
            d['format_display'] = fmt
            d['format_color'] = led
            d['duration_fmt'] = _fmt_seconds(d.get('duration'))
            d['artist_id'] = artist_id
            top_tracks.append(d)

        return render_template('artist.html', artist=ar_data, albums=albums,
                               total_tracks=total_tracks,
                               fav_ids=_user_fav_track_ids(conn, session.get('user_id')),
                               genres=genres, similar_artists=similar_artists,
                               top_tracks=top_tracks,
                               artist_nationality=ar_data.get('nationality', ''))
    finally:
        conn.close()


def _lookup_artist_website(conn, artist_id):
    """Busca (y cachea) la URL del sitio oficial de un artista vía
    MusicBrainz. Extraído a función compartida (Ticket 10, Lote G) para
    que /api/artist/<id>/website (web) y /api/v1/artist/<id>/website
    (nativo, @api_login_required — la ruta web no sirve para el cliente
    nativo, queda detrás de _require_login que exige cookie de sesión, no
    Bearer) no dupliquen las mismas ~35 líneas de lookup+caché."""
    row = conn.execute(
        'SELECT name, website_url FROM artists WHERE id=?', (artist_id,)
    ).fetchone()
    if not row:
        return None

    # Return cached value if present
    if row['website_url']:
        # '-' means "looked up, nothing found" — don't retry
        return None if row['website_url'] == '-' else row['website_url']

    # MusicBrainz search — no auth required, rate limit is 1 req/s
    import urllib.request, urllib.parse
    query = urllib.parse.quote(row['name'])
    mb_url = (
        f'https://musicbrainz.org/ws/2/artist/?query=artist:"{query}"'
        f'&limit=1&fmt=json'
    )
    req = urllib.request.Request(mb_url, headers={
        'User-Agent': 'Orbyte/1.0 (music-browser; contact@orbyte.local)'
    })
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read())

    artists_mb = data.get('artists', [])
    official_url = None
    if artists_mb:
        mbid = artists_mb[0].get('id', '')
        if mbid:
            rel_url = f'https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels&fmt=json'
            rel_req = urllib.request.Request(rel_url, headers={
                'User-Agent': 'Orbyte/1.0 (music-browser; contact@orbyte.local)'
            })
            with urllib.request.urlopen(rel_req, timeout=4) as rr:
                rdata = json.loads(rr.read())
            for rel in rdata.get('relations', []):
                if rel.get('type') in ('official homepage', 'streaming', 'social network'):
                    if rel.get('type') == 'official homepage':
                        official_url = rel.get('url', {}).get('resource')
                        break
                    elif not official_url:
                        official_url = rel.get('url', {}).get('resource')

    # Store sentinel '-' when no URL found, to avoid re-querying MusicBrainz on every visit
    store_val = official_url if official_url else '-'
    conn.execute(
        'UPDATE artists SET website_url=? WHERE id=?', (store_val, artist_id)
    )
    conn.commit()
    return official_url

@app.route('/api/artist/<int:artist_id>/website')
def api_artist_website(artist_id):
    """Return (and cache) the official website URL for an artist via MusicBrainz."""
    conn = get_db_connection()
    try:
        return jsonify({'url': _lookup_artist_website(conn, artist_id)})
    except Exception as e:
        app.logger.debug(f'[website lookup] artist {artist_id}: {e}')
        return jsonify({'url': None})
    finally:
        conn.close()

@app.route('/api/v1/artist/<int:artist_id>/website')
@api_login_required
def api_v1_artist_website(artist_id):
    """Espejo nativo de /api/artist/<id>/website (Ticket 10, Lote G) —
    separado del resto de /api/v1/artist/<id> a propósito, mismo criterio
    que el JS de la web (loadDrawerPopularity/artist-website-link): no
    bloquear la carga principal de la pantalla por un lookup externo a
    MusicBrainz que puede demorar (timeout de 4s x 2 requests en el peor
    caso, la primera vez que se consulta un artista)."""
    conn = get_db_connection()
    try:
        return jsonify({'url': _lookup_artist_website(conn, artist_id)})
    except Exception as e:
        app.logger.debug(f'[website lookup] artist {artist_id}: {e}')
        return jsonify({'url': None})
    finally:
        conn.close()


@app.route('/album/<int:album_id>')
def album(album_id):
    conn = get_db_connection()
    try:
        alb = conn.execute(
            'SELECT al.*, ar.name as artist_name FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id WHERE al.id=?',
            (album_id,)
        ).fetchone()
        if not alb: return "Album not found", 404

        tracks = conn.execute(
            'SELECT * FROM tracks WHERE album_id=? ORDER BY disc_number, CAST(track_number AS INTEGER)',
            (album_id,)
        ).fetchall()

        alb = dict(alb)
        alb['cover_path'] = clean_db_path(alb.get('cover_path'))

        fp = conn.execute(
            'SELECT publisher FROM tracks WHERE album_id=? AND publisher IS NOT NULL AND publisher!="" LIMIT 1',
            (album_id,)
        ).fetchone()
        alb['publisher'] = fp['publisher'] if fp else None

        am = conn.execute('SELECT * FROM album_meta WHERE album_id=?', (album_id,)).fetchone()
        # Always include all expected keys so Jinja2 "album_meta.field is not none" never raises
        _AM_DEFAULTS = {
            'mood_predominante': None, 'momento_predominante': None, 'era': None,
            'idioma_principal': None, 'avg_energy': None, 'avg_valence': None,
            'avg_tension': None, 'avg_depth': None, 'avg_bailabilidad': None,
            'tracks_con_letra': None, 'tracks_sincronizados': None,
            'genre_primary': None, 'genre_secondary': None,
            'lastfm_listeners': None, 'lastfm_playcount': None,
        }
        album_meta = {**_AM_DEFAULTS, **(dict(am) if am else {})}

        track_list = []
        for t in tracks:
            td = {**dict(t),
                  'file_path':  clean_db_path(dict(t).get('file_path')),
                  'cover_path': alb['cover_path'],
                  'album_name': alb.get('name'),
                  'artist_id':  alb.get('artist_id')}
            td['audio_url'] = audio_url_filter(td['file_path'])
            td['cover_url'] = cover_url_filter(td['cover_path'])
            fmt, led = _fmt_format(td)
            td['format_display'] = fmt
            td['format_color']   = led
            # led_color stays as-is from DB
            tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (t['id'],)).fetchone()
            if tm:
                for k in tm.keys():
                    if k != 'track_id':
                        td[f'meta_{k}'] = tm[k]
            track_list.append(td)

        disc_numbers = set(t['disc_number'] for t in track_list if t['disc_number'])
        total_discs  = len(disc_numbers) if disc_numbers else 1

        # Issue 10: genre fallback — use most common track genre if album_meta lacks it
        if not album_meta.get('genre_primary'):
            row = conn.execute(
                '''SELECT COALESCE(tm.genre_primary, t.genre) as g, COUNT(*) as c
                   FROM tracks t LEFT JOIN track_meta tm ON tm.track_id=t.id
                   WHERE t.album_id=? AND COALESCE(tm.genre_primary, t.genre) IS NOT NULL
                   GROUP BY g ORDER BY c DESC LIMIT 1''',
                (album_id,)
            ).fetchone()
            if row:
                album_meta['genre_primary'] = row['g']

        # Issue 11: artist nationality + flag
        artist_row = conn.execute(
            'SELECT nationality, similar_artists_json FROM artists WHERE id=?',
            (alb.get('artist_id'),)
        ).fetchone()
        artist_nationality = ''
        artist_flag        = ''
        if artist_row and artist_row['nationality']:
            artist_nationality = artist_row['nationality']
            artist_flag        = nationality_flag(artist_nationality)

        # Favorites set for this page — per-usuario (Ticket 10 §2.2)
        fav_ids = _user_fav_track_ids(conn, session.get('user_id'))

        return render_template('album.html', album=alb, tracks=track_list,
                               primary_format=alb.get('primary_format') or 'Unknown',
                               total_discs=total_discs, multi_disc=total_discs > 1,
                               album_meta=album_meta,
                               artist_nationality=artist_nationality,
                               artist_flag=artist_flag,
                               fav_ids=fav_ids)
    finally:
        conn.close()


@app.route('/track/<int:track_id>')
def track(track_id):
    conn = get_db_connection()
    try:
        t = conn.execute('''
            SELECT t.*, a.name as album_name, a.cover_path, a.year as album_year, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums a ON t.album_id=a.id
            LEFT JOIN artists ar ON a.artist_id=ar.id
            WHERE t.id=?
        ''', (track_id,)).fetchone()
        if not t: return "Track not found", 404
        t = dict(t)
        t['file_path']  = clean_db_path(t.get('file_path'))
        t['cover_path'] = clean_db_path(t.get('cover_path'))
        t['publisher']  = clean_db_path(t.get('publisher'))
        fmt, led = _fmt_format(t)
        # led_color already in t from DB
        sr = t.get('sample_rate_real')
        tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (track_id,)).fetchone()
        track_meta = dict(tm) if tm else {}
        pop_row = conn.execute(
            'SELECT pop_score FROM track_pop_cache WHERE track_id=?', (track_id,)
        ).fetchone()
        track_pop_score = int(pop_row['pop_score']) if pop_row else 0
        return render_template('track.html', track=t,
                               format_display=fmt, format_color=led,
                               sample_rate=f"{sr/1000:.1f} kHz" if sr else "N/A",
                               bit_depth=f"{t.get('bit_depth') or 24} bit",
                               bitrate_fmt=_fmt_bitrate(t.get('bitrate')),
                               track_meta=track_meta,
                               track_pop_score=track_pop_score,
                               led_labels=LED_LABELS)
    finally:
        conn.close()


@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query: return home()
    conn = get_db_connection()
    try:
        like = f'%{query}%'
        artists = conn.execute(
            '''SELECT a.id, a.name, a.nationality, a.letter,
                      a.lastfm_listeners,
                      COUNT(DISTINCT al.id)                          AS album_count,
                      COALESCE(MAX(apc.pop_score), 0)                AS pop_score,
                      (SELECT t.genre FROM tracks t JOIN albums al2 ON t.album_id=al2.id
                       WHERE al2.artist_id=a.id AND t.genre IS NOT NULL AND t.genre != ""
                       GROUP BY t.genre ORDER BY COUNT(*) DESC LIMIT 1) AS most_common_genre
               FROM artists a
               LEFT JOIN albums al  ON al.artist_id=a.id
               LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
               WHERE a.name LIKE ?
                 AND EXISTS (SELECT 1 FROM albums al2 WHERE al2.artist_id=a.id)
               GROUP BY a.id
               ORDER BY a.name LIMIT 10''',
            (like,)
        ).fetchall()
        albums = conn.execute(
            '''SELECT al.id, al.name, al.cover_path, al.primary_format, al.year,
                      al.track_count, al.total_duration, al.artist_id, ar.name as artist_name,
                      (SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END FROM tracks WHERE album_id=al.id
                       ORDER BY CASE led_color
                         WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                         WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                         ELSE 6 END, (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) LIMIT 1) as album_led
               FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
               WHERE al.name LIKE ? OR ar.name LIKE ? ORDER BY al.name LIMIT 20''',
            (like, like)
        ).fetchall()
        tracks = conn.execute(
            '''SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.is_mqa,
                      t.codec, t.bitrate, t.compression_mode, t.duration, t.sample_rate_real,
                      a.id as album_id, a.name as album_name, a.cover_path,
                      tm.mood as meta_mood, tm.momento as meta_momento, tm.tier as meta_tier
               FROM tracks t
               LEFT JOIN albums a ON t.album_id=a.id
               LEFT JOIN track_meta tm ON tm.track_id=t.id
               WHERE t.title LIKE ? OR t.artist LIKE ? OR t.genre LIKE ?
               ORDER BY t.title LIMIT 50''',
            (like, like, like)
        ).fetchall()

        albums_out = [{**dict(a), 'cover_path': clean_db_path(a['cover_path'])} for a in albums]
        tracks_out = []
        for t in tracks:
            d = dict(t)
            d['cover_path'] = clean_db_path(d.get('cover_path'))
            _, led = _fmt_format(d)
            d['format_color'] = led
            tracks_out.append(d)

        return render_template('search.html',
                               artists=[dict(a) for a in artists],
                               albums=albums_out, tracks=tracks_out, query=query)
    finally:
        conn.close()

# ── Pagination helper ──────────────────────────────────────────────────────────

PAGE_SIZE = 20

# ── album_meta field mapping for api_meta_tracks ───────────────────────────────
# Maps the 'field' param values to their column names in the album_meta table.
# Fields NOT listed here are queried at track_meta level instead.
_ALBUM_META_FIELD = {
    'mood':    'mood_predominante',
    'momento': 'momento_predominante',
    'era':     'era',
    'idioma':  'idioma_principal',
}

# ── Sort system ───────────────────────────────────────────────────────────────
# Whitelisted sort columns for albums and tracks (prevents SQL injection)

ALBUM_SORT_MAP = {
    'nombre':      'al.name',
    'artista':     'ar.name',
    'año':         'al.year',
    'pistas':      'al.track_count',
    'popularidad': 'COALESCE(apc.pop_score, 0)',   # pre-computed score: quality + tier + metadata
    'random':      'RANDOM()',
}
TRACK_SORT_MAP = {
    'artista':      't.artist',
    'titulo':       't.title',
    'año':          'a.year',
    'bpm':          'tm.bpm',
    'energia':      'tm.energy',
    'bailabilidad': 'tm.bailabilidad',
    'tier':         "CASE tm.tier WHEN 'silver' THEN 0 WHEN 'bronze' THEN 1 ELSE 2 END",
    'popularidad':  'COALESCE(tpc.pop_score, 0)',
    'random':       'RANDOM()',
    # 'intercalar' is handled client-side, server sends 'random' for it
}

def _album_order(sort_key, direction):
    """Return validated ORDER BY clause for album queries."""
    col = ALBUM_SORT_MAP.get(sort_key, 'al.track_count')
    if col == 'RANDOM()':
        return 'RANDOM()'
    # Popularidad siempre DESC por defecto (más popular primero)
    if sort_key == 'popularidad' and direction == 'asc':
        direction = 'desc'
    dir_ = 'DESC' if direction == 'desc' else 'ASC'
    return f'{col} {dir_} NULLS LAST'

def _track_order(sort_key, direction):
    """Return validated ORDER BY clause for track queries."""
    col = TRACK_SORT_MAP.get(sort_key, 't.artist')
    if col == 'RANDOM()':
        return 'RANDOM()'
    # Popularidad siempre DESC
    if sort_key == 'popularidad' and direction == 'asc':
        direction = 'desc'
    dir_ = 'DESC' if direction == 'desc' else 'ASC'
    return f'{col} {dir_} NULLS LAST'

def _playlist_order(sort_key):
    """Return validated ORDER BY clause for /api/v1/playlists (Ticket 20,
    Ítem 3). A diferencia de _album_order/_track_order no recibe
    `direction` — cada criterio tiene un único sentido natural que el PO
    confirmó (alfabético A→Z, última reproducción y más reproducidas con
    la más reciente/más alta primero), no hace falta un toggle asc/desc
    para esto. `sort_key` inválido o ausente conserva el comportamiento
    previo a este ticket (updated_at DESC) — ningún cliente viejo que no
    mande `sort` ve un cambio de orden."""
    if sort_key == 'alphabetical':
        return 'p.name COLLATE NOCASE ASC'
    if sort_key == 'last_played':
        # NULLS LAST explícito (mismo criterio que _album_order/_track_order):
        # las playlists nunca reproducidas van al final, no arriba por
        # cómo SQLite trata NULL en un ORDER BY DESC sin esto.
        return 'p.last_played_at DESC NULLS LAST'
    if sort_key == 'most_played':
        # Empate en play_count (frecuente: 0 es el caso más común) se
        # resuelve alfabético, para que el orden sea estable entre
        # requests en vez de depender del orden físico en disco.
        return 'p.play_count DESC, p.name COLLATE NOCASE ASC'
    return 'p.updated_at DESC'

# ── Deduplicación de pistas repetidas (misma canción, distinto álbum) ──────────
# Issue: en la vista de pistas (Búsqueda Avanzada y las pestañas Pistas de
# mood/momento/era/tema/tier/idioma/género) una misma canción puede aparecer
# varias veces si vive en más de un álbum (ediciones deluxe, remasters,
# compilados...). Además de ensuciar el listado, esto rompe el botón
# "➕ Añadir a cola": trackRowHtml()/addTrackWithFeedback() (advanced_search.html)
# y su equivalente en browse.html indexan por posición dentro del array de
# resultados, así que cada copia agrega la pista correcta — el problema real
# es mostrar N copias visualmente indistinguibles (mismo título) donde el
# usuario esperaba una sola.
#
# Solución: colapsar a UNA fila por (Título, Artista), quedándonos con la de
# mejor score = 70% Calidad + 30% Popularidad (ambos normalizados 0-100).
# La Calidad reutiliza el mismo ranking LED que ya se usa en todo este
# archivo para elegir la versión "mejor" de un álbum (ver los "ORDER BY CASE
# led_color ... LIMIT 1" repetidos arriba y _led_for_album_tracks) en vez de
# inventar una escala nueva: magenta es la mejor calidad, amarillo/otros la
# peor. La Popularidad es el pop_score (0-100) ya precalculado en
# track_pop_cache, igual que en todos los sorts de "popularidad" existentes.
#
# Importante: el ticket pide filtrar duplicados "de los resultados obtenidos"
# — es decir, la comparación debe quedar acotada a los mismos filtros que ya
# está aplicando la vista, no a la biblioteca completa. Si no fuera así, un
# filtro "Calidad: CD" podría hacer desaparecer una canción entera de los
# resultados solo porque existe una copia DSD256 de mejor score en otro
# álbum que ni siquiera cumple ese filtro — justo lo opuesto de lo que el
# usuario pidió al filtrar. Por eso _track_dedupe_condition recibe el WHERE
# ya construido por la vista (extra_where) y lo reaplica, con los mismos
# alias re-prefijados con "dup_", a la copia candidata antes de compararla.
#
# Si el usuario quiere la otra versión, puede seguir encontrándola buscándola
# manualmente (álbum, vista de álbumes, etc.) — este filtro solo afecta la
# vista de pistas, tal como pide el ticket.
_TRACK_QUALITY_RANK_SQL = """(CASE
        WHEN COALESCE({alias}.compression_mode,'')='Lossy'
          OR LOWER(COALESCE({alias}.codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')
          THEN -16.6666666667
        ELSE CASE {alias}.led_color
        WHEN 'magenta' THEN 100.0
        WHEN 'blue'    THEN 83.3333333333
        WHEN 'green'   THEN 66.6666666667
        WHEN 'red'     THEN 50.0
        WHEN 'cyan'    THEN 33.3333333333
        WHEN 'white'   THEN 16.6666666667
        ELSE 0.0
    END END)"""

# Ticket AI-30 — mismo mapeo que _TRACK_QUALITY_RANK_SQL de arriba, pero
# como dict de Python en vez de fragmento SQL. ai_playlist.py necesita
# puntuar la "calidad" de distintas versiones de una pista (estudio,
# remaster, en vivo) ya traídas como dicts en memoria — no tiene sentido
# volver a la base solo para eso, y ai_playlist.py no puede ejecutar SQL
# con este CASE directamente sin duplicar el mapeo. Se inyecta como
# función (led_quality_rank_fn) al mismo estilo que track_to_json_fn/
# build_adv_filters_fn/dedupe_condition_fn — ver AGENTE.md regla 2 (nunca
# reinventar, y ai_playlist.py solo LEE funciones de este módulo, nunca
# las modifica). Mantenido pegado al SQL de arriba a propósito: si se
# agrega un color nuevo, actualizar los dos en el mismo commit.
LED_QUALITY_RANK = {
    'magenta': 100.0, 'blue': 83.3333333333, 'green': 66.6666666667,
    'red': 50.0, 'cyan': 33.3333333333, 'white': 16.6666666667,
    'silver': -16.6666666667,   # con pérdida: siempre gana la copia sin pérdida
}


def led_quality_rank(led_color):
    return LED_QUALITY_RANK.get(led_color, 0.0)

# Alias usados por las dos consultas que llaman a _track_dedupe_condition —
# se re-prefijan con "dup_" para poder unir una segunda copia de tracks/
# albums/artists/track_meta/track_pop_cache dentro de la subconsulta
# correlacionada sin chocar con los alias de la fila "exterior".
_DEDUPE_ALIASES = ('t', 'tm', 'al', 'ar', 'tpc')

def _track_dedupe_condition(extra_where='', track_alias='t', pop_alias='tpc'):
    """
    Fragmento SQL (sin el 'AND' inicial, mismo estilo que _build_adv_filters)
    para insertar en el WHERE de una consulta de pistas ya unida (JOIN) con
    albums/artists/track_meta/track_pop_cache. Excluye una pista si existe
    otra CON LOS MISMOS FILTROS ACTIVOS (mismo Título + Artista, comparación
    case/espacios-insensible) y mejor score; en empate de score se conserva
    el id menor, para un resultado estable entre requests.

    extra_where: el WHERE que la vista ya aplica (usando los alias t/al/ar/
    tm/tpc), para que la pista candidata deba cumplir EXACTAMENTE los mismos
    filtros que la fila que reemplaza — no cualquier copia de la canción en
    toda la biblioteca. Pasar '' cuando no hay filtros activos. Los mismos
    valores de `extra_where` deben añadirse DOS VECES a la lista de params
    de la consulta que llama a esta función (una para el WHERE exterior, una
    para la copia re-prefijada de aquí adentro) — ver comentarios en los
    call sites.
    """
    def _reprefix(sql):
        for alias in _DEDUPE_ALIASES:
            sql = re.sub(rf'\b{alias}\.', f'dup_{alias}.', sql)
        return sql

    dup_extra = f' AND {_reprefix(extra_where)}' if extra_where else ''
    score_self  = f"({_TRACK_QUALITY_RANK_SQL.format(alias=track_alias)} * 0.7 " \
                  f"+ COALESCE({pop_alias}.pop_score, 0) * 0.3)"
    score_other = f"({_TRACK_QUALITY_RANK_SQL.format(alias='dup_t')} * 0.7 " \
                  f"+ COALESCE(dup_tpc.pop_score, 0) * 0.3)"
    return f"""NOT EXISTS (
        SELECT 1 FROM tracks dup_t
        JOIN albums dup_al ON dup_al.id = dup_t.album_id
        LEFT JOIN artists dup_ar ON dup_ar.id = dup_al.artist_id
        LEFT JOIN track_meta dup_tm ON dup_tm.track_id = dup_t.id
        LEFT JOIN track_pop_cache dup_tpc ON dup_tpc.track_id = dup_t.id
        WHERE LOWER(TRIM(dup_t.title))  = LOWER(TRIM({track_alias}.title))
          AND LOWER(TRIM(dup_t.artist)) = LOWER(TRIM({track_alias}.artist))
          AND dup_t.id != {track_alias}.id
          {dup_extra}
          AND ({score_other} > {score_self}
               OR ({score_other} = {score_self} AND dup_t.id < {track_alias}.id))
    )"""


# Ticket 14: dedupe de "casi-duplicados" para Infinite -- distinto de
# _track_dedupe_condition de arriba (esa colapsa copias EXACTAS de
# Título+Artista entre distintas ediciones/calidades). Esta es para el
# caso "Headknocker" y "Headknocker (2008 Remaster)" apareciendo
# consecutivos en la cola de Infinite: mismo tema, pero el título NO es
# idéntico (tiene un sufijo de versión), así que _track_dedupe_condition
# no lo agarra. Se aplica en Python (no SQL) sobre el pool de candidatos
# ya traído, comparando (artista, título-sin-sufijo) contra lo que ya
# está en la cola.
_RADIO_DEDUPE_SUFFIX_RE = re.compile(
    r'''\s*[\(\[]\s*(?:\d{4}\s*)?
        (?:re-?master(?:ed)?
          |deluxe(?:\s+edition)?
          |live(?:\s+(?:at|from|in|on)\s+[^)\]]*)?
          # Ticket AI-34 (log real de Niko: Tears for Fears solo tiene "Mad
          # World (Live From Franklin, TN)" y la sugerencia "Mad World" no
          # resolvía): también "Live From/In/On …" y el español "En Vivo" /
          # "En Directo".
          |en\s+(?:vivo|directo)(?:\s+[^)\]]*)?
          |acoustic
          |radio\s+edit
          |bonus\s+track
          |demo
          |mono(?:\s+mix)?
          |stereo(?:\s+mix)?
          |extended(?:\s+mix)?
          |single\s+version
          |album\s+version
          |remix
        )
        # Ticket AI-30 (bug real confirmado por Niko: "One" de Metallica
        # resolvía a una versión en vivo en vez de la de estudio, al
        # elegir la mejor versión para una sugerencia de Gemini —
        # _resolve_suggested_track_version). Causa: el original en la
        # biblioteca está guardado como "One (Remastered 2018)" —
        # palabra ANTES del año — pero acá solo se contemplaba el orden
        # "(2018 Remastered)" (año ANTES de la palabra, vía el grupo
        # opcional del principio). Se agrega el mismo grupo de año
        # OPCIONAL también DESPUÉS de la palabra clave, para cubrir
        # ambos órdenes. Validado contra los 68.803 títulos reales de
        # la biblioteca de Niko: 1139 títulos cambian de resultado, los
        # 25 revisados al azar quedan todos correctos (ninguno se corta
        # de más ni de menos).
        \s*(?:\d{4}\s*)?
        \s*[\)\]]\s*$''',
    re.IGNORECASE | re.VERBOSE
)


def _normalize_title_for_radio_dedupe(title):
    """Pela sufijos de version (remaster/live/deluxe/acoustic/etc) del
    final de un titulo, repitiendo por si hay mas de uno encadenado (ej.
    "Track (Live) (Remastered)"). Case-insensitive. Usado solo para
    Infinite, no toca la base ni el dedupe general de la app."""
    if not title:
        return ''
    normalized = title.strip()
    prev = None
    while prev != normalized:
        prev = normalized
        normalized = _RADIO_DEDUPE_SUFFIX_RE.sub('', normalized).strip()
    return normalized.lower()


def _paginate(conn, count_sql, count_params, data_sql, data_params, page,
              order_by='al.name ASC', page_size=None):
    """Run paginated query with dynamic ORDER BY.

    `page_size` (Ticket 26, Categoría B): None (default) usa PAGE_SIZE tal
    cual — todas las rutas web (/genre/, /led/, /format/, /mood/, etc.)
    siguen llamando esta función sin pasarlo, cero cambio de comportamiento
    para ellas. Solo las rutas /api/v1/* que resuelven el valor desde
    settings_json lo pasan explícito."""
    effective_page_size = page_size or PAGE_SIZE
    total  = conn.execute(count_sql, count_params).fetchone()[0]
    offset = (page - 1) * effective_page_size
    final_sql = data_sql + f' ORDER BY {order_by} LIMIT ? OFFSET ?'
    rows  = conn.execute(final_sql, list(data_params) + [effective_page_size, offset]).fetchall()
    albums = []
    for a in rows:
        d = dict(a)
        d['cover_path'] = clean_db_path(d.get('cover_path'))
        d['album_led']  = d.get('album_led') or 'yellow'
        albums.append(d)
    total_pages = max(1, (total + effective_page_size - 1) // effective_page_size)
    return albums, total, total_pages

# ── Búsqueda Avanzada (multi-filter search) ─────────────────────────────────────
# One page + one API that combine ANY subset of the filters already used
# individually by /mood, /genre, /led, /language, etc. Reuses the exact same
# conventions as the routes above (PAGE_SIZE, _paginate, _album_order,
# _track_order, track_to_json, get_db_connection) instead of introducing new
# ones — see AGENTE.md rule 2.

# Calidad values requested by the ticket. NOTE: 'DSD512' is intentionally left
# out — the DB only distinguishes DSD tiers up to DSD256 (see _dsd_label()
# above: sample_rate_real thresholds top out at 11289600 Hz = DSD256, and no
# track in the schema is tagged higher). Selecting a "DSD512" value would
# either silently relabel DSD256 files or match nothing; instead of inventing
# that convention, this is called out for a product decision — see chat reply.
QUALITY_OPTIONS = ['CD', 'HI-RES', 'DSD64', 'DSD128', 'DSD256', 'MQA', 'MQA Studio', 'OSR', 'LOSSY']

# Popularidad buckets mirror the 5-star scale already rendered in browse.html
# (Math.floor(pop_score / 20)), so a filter labelled "★★★★★" matches exactly
# what the user already sees as 5 stars elsewhere in the app.
POP_BUCKETS = {'1': (0, 19), '2': (20, 39), '3': (40, 59), '4': (60, 79), '5': (80, 100)}

# Energía is stored as a -1..1 float (see the (energy+1)/2*100 display math in
# browse.html / base.html's drawer bars). Bailabilidad is already 0..100 (see
# the bailBar width calc in browse.html). Bucket edges below are a first pass
# at "Baja / Media / Alta" — flagged in the chat reply as worth confirming.
ENERGY_BUCKETS = {'baja': (-1.0, -0.34), 'media': (-0.34, 0.34), 'alta': (0.34, 1.0)}
BAIL_BUCKETS   = {'baja': (0, 33), 'media': (34, 66), 'alta': (67, 100)}

def _quality_condition(value):
    """SQL boolean fragment (references alias t.) for a Calidad filter value."""
    v = (value or '').strip()
    if v == 'CD':         return f"t.led_color = 'yellow' AND NOT {_lossy_sql('t')}"
    if v == LOSSY_QUALITY or v.upper() in ('CON PERDIDA', 'CON PÉRDIDA', 'LOSSY'):
        return _lossy_sql('t')
    if v == 'HI-RES':     return "t.led_color = 'white'"
    if v == 'DSD64':      return ("t.is_dsd=1 AND (UPPER(COALESCE(t.dsd_rate,'')) LIKE 'DSD64%' "
                                   "OR (COALESCE(t.dsd_rate,'')='' AND t.sample_rate_real>=2822400 "
                                   "AND t.sample_rate_real<5644800))")
    if v == 'DSD128':     return ("t.is_dsd=1 AND (UPPER(COALESCE(t.dsd_rate,'')) LIKE 'DSD128%' "
                                   "OR (COALESCE(t.dsd_rate,'')='' AND t.sample_rate_real>=5644800 "
                                   "AND t.sample_rate_real<11289600))")
    if v == 'DSD256':     return ("t.is_dsd=1 AND (UPPER(COALESCE(t.dsd_rate,'')) LIKE 'DSD256%' "
                                   "OR (COALESCE(t.dsd_rate,'')='' AND t.sample_rate_real>=11289600))")
    if v == 'MQA':        return "t.is_mqa=1 AND t.led_color = 'green'"
    if v == 'MQA Studio': return "t.is_mqa=1 AND t.led_color = 'blue'"
    if v == 'OSR':        return "t.led_color = 'magenta'"  # "Original Sample Rate" (MQB)
    return '0'  # unknown value → explicitly no matches, never silently ignored

def _range_condition(col, bucket_key, bucket_map):
    """Generic BETWEEN condition builder for popularidad / energia / bailabilidad."""
    bounds = bucket_map.get(bucket_key)
    if not bounds:
        return None, []
    return f'{col} BETWEEN ? AND ?', [bounds[0], bounds[1]]

def _build_adv_filters(args, pop_alias, for_albums):
    """
    Build (where_clauses, params) from every active Búsqueda Avanzada filter in
    request.args. Both the albums query and the tracks query alias tracks as
    't', track_meta as 'tm', albums as 'al' and artists as 'ar' — only the
    popularity-cache alias differs (apc for albums, tpc for tracks), hence the
    pop_alias parameter.

    Every field accepts ONE OR MORE values (repeat the query param, e.g.
    ?mood=Feliz&mood=Triste). Multiple values within the SAME field are
    combined with OR — a track only has a single mood/momento/era/etc, so
    combining them with AND would always return zero rows; OR ("cualquiera
    de estos") is the only combination that makes sense there. Different
    fields are still combined with AND, same as before.

    for_albums controls which table mood/momento/era/idioma/genero/energia/
    bailabilidad are matched against — this is the exact same distinction
    _ALBUM_META_FIELD already makes for the single-filter /mood, /momento,
    /era, /language routes: a single mismatched or outlier track's
    track_meta value shouldn't be enough to pull an otherwise-unrelated
    album into the Albums view, so that view matches against album_meta's
    predominant/average field instead. The Tracks view keeps matching
    per-track (tm.*/t.*), which is correct there.

    Fields intentionally NOT split this way:
    - tema (Categoría Letra): album_meta has no equivalent column — stays
      track_meta-only for both views.
    - pais, anio: already single-level (artists.nationality, albums.year),
      no track/album ambiguity to begin with.
    - popularidad: already correctly split via the pop_alias param (apc for
      albums, tpc for tracks) — both are pre-computed scores, not raw
      per-track values, so no change was needed here.
    - calidad: albums has a `primary_format` column, but its value domain
      hasn't been confirmed to line up with QUALITY_OPTIONS (CD/HI-RES/
      DSD64.../OSR), so it's left per-track for both views rather than
      guessing at the mapping.
    """
    clauses, params = [], []

    calidad_vals = args.getlist('calidad')
    if calidad_vals:
        sub = [_quality_condition(v) for v in calidad_vals]
        clauses.append('(' + ' OR '.join(sub) + ')')

    for field, tm_col in (('mood', 'mood'), ('momento', 'momento'), ('era', 'era'),
                           ('idioma', 'idioma')):
        vals = args.getlist(field)
        if vals:
            placeholders = ','.join('?' * len(vals))
            col = f'am.{_ALBUM_META_FIELD[field]}' if for_albums else f'tm.{tm_col}'
            clauses.append(f'{col} IN ({placeholders})')
            params += vals

    tema_vals = args.getlist('tema')  # Categoría Letra
    if tema_vals:
        placeholders = ','.join('?' * len(tema_vals))
        clauses.append(f'tm.tema_lirico IN ({placeholders})')
        params += tema_vals

    genero_vals = args.getlist('genero')
    if genero_vals:
        sub = []
        if for_albums:
            for g in genero_vals:
                sub.append('(am.genre_primary = ? OR am.genre_secondary = ?)')
                params += [g, g]
        else:
            for g in genero_vals:
                sub.append('(t.genre = ? OR tm.genre_primary = ? OR tm.genre_secondary = ?)')
                params += [g, g, g]
        clauses.append('(' + ' OR '.join(sub) + ')')

    pais_vals = args.getlist('pais')  # País Origen (artists.nationality)
    if pais_vals:
        placeholders = ','.join('?' * len(pais_vals))
        clauses.append(f'ar.nationality IN ({placeholders})')
        params += pais_vals

    anio_vals = [v for v in args.getlist('anio') if v.lstrip('-').isdigit()]  # Año(s) de lanzamiento
    if anio_vals:
        placeholders = ','.join('?' * len(anio_vals))
        clauses.append(f'al.year IN ({placeholders})')
        params += [int(v) for v in anio_vals]

    pop_vals = args.getlist('popularidad')
    if pop_vals:
        sub, p = [], []
        for v in pop_vals:
            cond, cp = _range_condition(f'COALESCE({pop_alias}.pop_score,0)', v, POP_BUCKETS)
            if cond:
                sub.append(cond)
                p += cp
        if sub:
            clauses.append('(' + ' OR '.join(sub) + ')')
            params += p

    energia_vals = args.getlist('energia')
    if energia_vals:
        sub, p = [], []
        energia_col = 'am.avg_energy' if for_albums else 'tm.energy'
        for v in energia_vals:
            cond, cp = _range_condition(energia_col, v, ENERGY_BUCKETS)
            if cond:
                sub.append(cond)
                p += cp
        if sub:
            clauses.append('(' + ' OR '.join(sub) + ')')
            params += p

    bail_vals = args.getlist('bailabilidad')
    if bail_vals:
        sub, p = [], []
        bail_col = 'am.avg_bailabilidad' if for_albums else 'tm.bailabilidad'
        for v in bail_vals:
            cond, cp = _range_condition(bail_col, v, BAIL_BUCKETS)
            if cond:
                sub.append(cond)
                p += cp
        if sub:
            clauses.append('(' + ' OR '.join(sub) + ')')
            params += p

    return clauses, params

def _advanced_search_options(conn):
    """
    Gather the same filter-option lists home() already computes for its own
    sections (moods, momentos, eras, temas, genres, languages) plus the two
    new ones added there (available_years, nationalities). Kept as its own
    function — rather than reusing home()'s inline queries — so this route
    can supply exactly what _advanced_search_modal.html needs without
    depending on (or risking a merge conflict with) the rest of home()'s
    unrelated stats/recent-albums queries.
    NOTE: this duplicates a handful of SELECTs that already exist in home().
    Once app.py is fully reviewed it would be worth factoring both into one
    shared helper — left as-is here to keep this diff minimal and safe.
    """
    moods = [(r['mood'], r['c']) for r in conn.execute(
        'SELECT mood, COUNT(*) as c FROM track_meta WHERE mood IS NOT NULL '
        'GROUP BY mood ORDER BY c DESC LIMIT 14').fetchall()]

    momentos = [(r['momento'], r['c']) for r in conn.execute(
        'SELECT momento, COUNT(*) as c FROM track_meta WHERE momento IS NOT NULL '
        'GROUP BY momento ORDER BY c DESC').fetchall()]

    era_order = ['early_rock_era', 'british_invasion_era', 'classic_rock_era',
                 'nwobhm_synth_era', 'grunge_alternative_era', 'post_millennial_era',
                 'streaming_era', 'current_era']
    era_dict = {r['era']: r['c'] for r in conn.execute(
        'SELECT era, COUNT(*) as c FROM track_meta WHERE era IS NOT NULL GROUP BY era').fetchall()}
    eras = [(e, era_dict[e]) for e in era_order if e in era_dict]

    temas = [(r['tema_lirico'], r['c']) for r in conn.execute(
        'SELECT tema_lirico, COUNT(*) as c FROM track_meta WHERE tema_lirico IS NOT NULL '
        'GROUP BY tema_lirico ORDER BY c DESC LIMIT 10').fetchall()]

    genres_primary = [(r['genre_primary'], r['c']) for r in conn.execute(
        'SELECT genre_primary, COUNT(*) as c FROM track_meta '
        'WHERE genre_primary IS NOT NULL AND genre_primary!="" '
        'GROUP BY genre_primary ORDER BY c DESC LIMIT 15').fetchall()]

    genres = [(r['genre'], r['c']) for r in conn.execute(
        'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" '
        'GROUP BY genre ORDER BY c DESC LIMIT 8').fetchall()]

    languages = [(r['idioma'], r['c']) for r in conn.execute(
        'SELECT idioma, COUNT(*) as c FROM track_meta WHERE idioma IS NOT NULL AND idioma!="" '
        'GROUP BY idioma ORDER BY c DESC LIMIT 12').fetchall()]

    # Unlimited genre lists — dedicated to the Búsqueda Avanzada Género capsule.
    # genres_primary/genres above are capped (15/8) for home.html's own compact
    # "Géneros / Subgéneros" section; the modal needs every option so it isn't
    # missing anything, per the ticket's point 2. Separate variables so fixing
    # this never risks changing that unrelated home.html section.
    adv_genres_primary = [(r['genre_primary'], r['c']) for r in conn.execute(
        'SELECT genre_primary, COUNT(*) as c FROM track_meta '
        'WHERE genre_primary IS NOT NULL AND genre_primary!="" '
        'GROUP BY genre_primary ORDER BY c DESC').fetchall()]

    all_genres = [(r['genre'], r['c']) for r in conn.execute(
        'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" '
        'GROUP BY genre ORDER BY c DESC').fetchall()]

    available_years = [r['year'] for r in conn.execute(
        'SELECT DISTINCT year FROM albums WHERE year IS NOT NULL ORDER BY year DESC').fetchall()]

    nationalities = [(r['nationality'], r['c']) for r in conn.execute(
        'SELECT nationality, COUNT(*) as c FROM artists '
        'WHERE nationality IS NOT NULL AND nationality!="" '
        'GROUP BY nationality ORDER BY c DESC').fetchall()]

    return dict(moods=moods, momentos=momentos, eras=eras, temas=temas,
                genres_primary=genres_primary, genres=genres, languages=languages,
                adv_genres_primary=adv_genres_primary, all_genres=all_genres,
                available_years=available_years, nationalities=nationalities)

@app.route('/busqueda-avanzada')
def advanced_search_page():
    """
    Búsqueda Avanzada results shell. The album/track RESULTS come from
    /api/search/advanced client-side (so the same view works no matter which
    combination of filters was used) — this route only supplies the filter
    OPTION lists so the modal can be reopened ("Modificar filtros") without
    a round-trip back to home.
    """
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
    finally:
        conn.close()
    # Pass the query string down instead of making the client read
    # window.location.search: navigateTo() (base.html) runs this page's
    # <script> BEFORE it calls history.pushState(), so a client-side read of
    # window.location.search on an SPA navigation would still see the
    # PREVIOUS page's URL. Baking the real filters in here — same approach
    # browse.html uses for its single filter_type/filter_value — sidesteps
    # that race entirely.
    return render_template('advanced_search.html', initial_query=request.query_string.decode('utf-8'), **opts)

def _api_search_advanced_payload(args, page_size=None):
    """
    Multi-filter search across every RichMetaPro + technical field at once.
    ?view=albums (default) or ?view=tracks selects which result set to return;
    the front end calls this twice (lazily, only when the user switches tabs)
    rather than computing both server-side on every request.

    Extraído de la ruta web a una función compartida (Ticket 08, Lote A) para
    que /api/search/advanced (web) y /api/v1/search/advanced (nativo, con
    @api_login_required) no mantengan dos copias de las mismas ~120 líneas
    de SQL con riesgo de divergir con el tiempo — mismo criterio que ya usa
    el propio código en varios comentarios de este archivo. Agrega
    'stream_url' por pista incondicionalmente (para ambos): el JS de la web
    ya ignora campos extra que no usa, y así el cliente nativo puede
    reproducir sin un segundo request a /api/v1/albums/<id>/tracks.

    `page_size` (Ticket 26, Categoría B): None (default) preserva PAGE_SIZE
    tal cual — api_search_advanced() (web) llama esta función sin pasarlo,
    a propósito. Solo api_v1_search_advanced() resuelve el valor desde
    settings_json y lo pasa explícito.
    """
    page = max(1, args.get('page', 1, type=int))
    sort = args.get('sort', 'popularidad')
    dir_ = args.get('dir', 'desc')
    view = args.get('view', 'albums')
    effective_page_size = page_size or PAGE_SIZE

    conn = get_db_connection()
    try:
        if view == 'tracks':
            clauses, params = _build_adv_filters(args, pop_alias='tpc', for_albums=False)
            # Colapsa duplicados (misma canción en distintos álbumes) — ver
            # _track_dedupe_condition. Se le pasan los filtros YA activos
            # (extra_where) para que la comparación quede acotada a "otras
            # copias que también cumplirían este mismo filtro/búsqueda", tal
            # como pide el ticket ("filtre de los resultados obtenidos") —
            # así un filtro de Calidad, por ejemplo, no hace desaparecer una
            # canción solo porque existe una copia mejor fuera de ese filtro.
            extra_where = ' AND '.join(clauses)
            dedupe_clause = _track_dedupe_condition(extra_where=extra_where, track_alias='t', pop_alias='tpc')
            clauses = clauses + [dedupe_clause]
            # dedupe_clause reincorpora una copia (re-prefijada dup_*) de los
            # mismos filtros — cada uno de sus '?' necesita su valor de
            # nuevo, en el mismo orden, así que los params se duplican.
            params = params + params
            where = (' AND ' + ' AND '.join(clauses)) if clauses else ''

            count_sql = f'''SELECT COUNT(*) FROM tracks t
                             JOIN albums al ON al.id=t.album_id
                             LEFT JOIN artists ar ON ar.id=al.artist_id
                             LEFT JOIN track_meta tm ON tm.track_id=t.id
                             LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                             WHERE 1=1{where}'''
            total = conn.execute(count_sql, params).fetchone()[0]

            order = _track_order(sort, dir_)
            offset = (page - 1) * effective_page_size
            data_sql = f'''SELECT t.*, al.id as album_id, al.name as album_name,
                                  al.year as album_year, al.cover_path,
                                  ar.id as artist_id, ar.name as artist_name,
                                  tm.mood, tm.momento, tm.era, tm.tema_lirico, tm.idioma,
                                  tm.genre_primary, tm.genre_secondary, tm.bpm, tm.energy,
                                  tm.bailabilidad, tm.tier, COALESCE(tpc.pop_score,0) as pop_score
                           FROM tracks t
                           JOIN albums al ON al.id=t.album_id
                           LEFT JOIN artists ar ON ar.id=al.artist_id
                           LEFT JOIN track_meta tm ON tm.track_id=t.id
                           LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                           WHERE 1=1{where}
                           ORDER BY {order} LIMIT ? OFFSET ?'''
            rows = conn.execute(data_sql, params + [effective_page_size, offset]).fetchall()

            tracks = []
            for r in rows:
                d = track_to_json(dict(r))
                d['album_id']      = r['album_id']
                d['album_name']    = r['album_name']
                d['album_year']    = r['album_year']
                d['artist_id']     = r['artist_id']
                d['artist_name']   = r['artist_name']
                d['mood']          = r['mood']
                d['momento']       = r['momento']
                d['era']           = r['era']
                d['tema_lirico']   = r['tema_lirico']
                d['idioma']        = r['idioma']
                d['genre_primary'] = r['genre_primary']
                d['bpm']           = r['bpm']
                d['energy']        = r['energy']
                d['bailabilidad']  = r['bailabilidad']
                d['tier']          = r['tier']
                d['pop_score']     = r['pop_score']
                d['stream_url']    = f'/api/v1/stream/{d["id"]}'
                tracks.append(d)

            total_pages = max(1, (total + effective_page_size - 1) // effective_page_size)
            return {'tracks': tracks, 'total': total, 'total_pages': total_pages, 'page': page}

        # view == 'albums'
        clauses, params = _build_adv_filters(args, pop_alias='apc', for_albums=True)
        where = (' AND ' + ' AND '.join(clauses)) if clauses else ''

        count_sql = f'''SELECT COUNT(DISTINCT al.id) FROM albums al
                         JOIN tracks t ON t.album_id=al.id
                         LEFT JOIN artists ar ON ar.id=al.artist_id
                         LEFT JOIN track_meta tm ON tm.track_id=t.id
                         LEFT JOIN album_meta am ON am.album_id=al.id
                         LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                         WHERE 1=1{where}'''
        data_sql = f'''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name,
                              (SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END FROM tracks WHERE album_id=al.id
                               ORDER BY CASE led_color
                                   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                                   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                                   ELSE 6 END, (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) LIMIT 1) as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN artists ar ON ar.id=al.artist_id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       LEFT JOIN album_meta am ON am.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE 1=1{where}'''
        order = _album_order(sort, dir_)
        albums, total, total_pages = _paginate(conn, count_sql, params, data_sql, params, page, order, page_size=page_size)
        # _paginate() only cleans cover_path — it never had to add cover_url
        # because every other caller renders albums through Jinja (which
        # applies the |cover_url filter at template time). This is a plain
        # JSON API, so the URL has to be computed here or the <img src>
        # ends up pointing at a raw filesystem path the browser can't load.
        for a in albums:
            a['cover_url'] = cover_url_filter(a.get('cover_path'))
            a['duration_fmt'] = _fmt_seconds(a.get('total_duration'))
        return {'albums': albums, 'total': total, 'total_pages': total_pages, 'page': page}
    finally:
        conn.close()

@app.route('/api/search/advanced')
def api_search_advanced():
    return jsonify(_api_search_advanced_payload(request.args))

@app.route('/api/v1/search/advanced')
@api_login_required
def api_v1_search_advanced():
    """Espejo nativo de /api/search/advanced — Ticket 08, Lote A §4.2. Misma
    lógica exacta vía _api_search_advanced_payload (ver comentario ahí),
    protegido por token en vez de cookie de sesión.

    Ticket 26, Categoría B: a diferencia de la ruta web (arriba), acá se
    resuelve search_page_size desde settings_json del usuario."""
    page_size = _settings_payload(g.api_user['id'])['search_page_size']
    return jsonify(_api_search_advanced_payload(request.args, page_size=page_size))

# ── Format browse ──────────────────────────────────────────────────────────────

@app.route('/format/<fmt>')
def browse_format(fmt):
    page  = max(1, request.args.get('page', 1, type=int))
    sort  = request.args.get('sort', 'popularidad')
    dir_  = request.args.get('dir',  'desc')
    conn  = get_db_connection()
    try:
        base = '''SELECT al.id, al.name, al.cover_path, al.primary_format, al.year,
                         al.track_count, al.total_duration, al.artist_id, ar.name as artist_name,
                         (SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END FROM tracks WHERE album_id=al.id
                          ORDER BY CASE led_color
                            WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                            WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                            ELSE 6 END, (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) LIMIT 1) as album_led
                  FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id'''
        if fmt == 'DSD':   where = ' WHERE al.primary_format="DSD"'
        elif fmt == 'MQA':  where = ' WHERE al.primary_format="MQA"'
        elif fmt == 'FLAC': where = ' WHERE (al.primary_format="FLAC" OR al.primary_format IS NULL)'
        else:               where = ''
        order = _album_order(sort, dir_)
        count_sql = f'SELECT COUNT(*) FROM albums al{where}'
        albums, total, total_pages = _paginate(conn, count_sql, [], base + where, [], page, order)
        return render_template('browse.html', albums=albums, title=f"Formato: {fmt}",
                               filter_type='format', filter_value=fmt,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_)
    finally:
        conn.close()

# ── LED browse ─────────────────────────────────────────────────────────────────

@app.route('/led/<color>')
def browse_led(color):
    if color not in LED_LABELS:
        return "Unknown LED color", 404
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        # Feeds the "Filtrar" button's preloaded Búsqueda Avanzada modal
        # (same helper /busqueda-avanzada uses) — without this, the modal
        # only has Calidad/Popularidad/Energía/Bailabilidad to show, since
        # those don't depend on any data from the route.
        opts = _advanced_search_options(conn)
        count_sql = '''SELECT COUNT(DISTINCT al.id)
                       FROM albums al JOIN tracks t ON t.album_id=al.id
                       WHERE ''' + _quality_color_sql('t') + '=?'
        data_sql  = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, ? as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN artists ar ON al.artist_id=ar.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE ''' + _quality_color_sql('t') + '=?'
        order = _album_order(sort, dir_)
        albums, total, total_pages = _paginate(
            conn, count_sql, [color], data_sql, [color, color], page, order)
        # Enrich albums with dominant dsd_rate so badges show DSD64/DSD128 correctly
        if color in ('cyan', 'red'):
            for alb in albums:
                row = conn.execute(
                    """SELECT dsd_rate, sample_rate_real FROM tracks
                       WHERE album_id=? AND is_dsd=1
                       ORDER BY sample_rate_real DESC LIMIT 1""",
                    (alb['id'],)
                ).fetchone()
                if row:
                    alb['primary_format'] = _dsd_label(dict(row))
        return render_template('browse.html', albums=albums,
                               title=(LOSSY_QUALITY if color == LOSSY_COLOR
                                      else f"LED {color.capitalize()} — {LED_LABELS[color]}"),
                               filter_type='led', filter_value=color,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()

# ── Genre browse ───────────────────────────────────────────────────────────────

@app.route('/genre/<path:genre>')
def browse_genre(genre):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        # Match by genre_primary in track_meta OR classic genre field in tracks
        count_sql = '''SELECT COUNT(DISTINCT al.id) FROM albums al
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       WHERE t.genre=? OR tm.genre_primary=?'''
        data_sql  = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, 'yellow' as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       LEFT JOIN artists ar ON al.artist_id=ar.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       WHERE t.genre=? OR tm.genre_primary=?'''
        order = _album_order(sort, dir_)
        albums, total, total_pages = _paginate(conn, count_sql, [genre, genre], data_sql, [genre, genre], page, order)
        return render_template('browse.html', albums=albums, title=f"Género: {genre}",
                               filter_type='genre', filter_value=genre,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()

# ── RichMetaPro browse ─────────────────────────────────────────────────────────

def _meta_browse(conn, field, value, page, title, filter_type, sort='popularidad', dir_='desc', page_size=None):
    # Map filter field to album_meta predominant column where available
    _AM_COL = {
        'mood':    'mood_predominante',
        'idioma':  'idioma_principal',
        'momento': 'momento_predominante',
        'era':     'era',
    }
    am_col = _AM_COL.get(field)

    if am_col:
        # Album-level predominant value — accurate, fast, no DISTINCT needed
        count_sql = (
            "SELECT COUNT(al.id) FROM albums al "
            "JOIN album_meta am ON am.album_id=al.id "
            "WHERE am.{col}=?".format(col=am_col)
        )
        data_sql = (
            "SELECT al.id, al.name, al.cover_path, al.primary_format, al.year, "
            "al.track_count, al.total_duration, al.artist_id, "
            "ar.name as artist_name, "
            "(SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END FROM tracks WHERE album_id=al.id "
            " ORDER BY CASE led_color "
            "   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2 "
            "   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5 "
            "   ELSE 6 END, (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) LIMIT 1) as album_led, "
            "COALESCE(apc.pop_score, 0) as pop_score "
            "FROM albums al "
            "LEFT JOIN artists ar ON al.artist_id=ar.id "
            "LEFT JOIN album_pop_cache apc ON apc.album_id=al.id "
            "JOIN album_meta am ON am.album_id=al.id "
            "WHERE am.{col}=?".format(col=am_col)
        )
    else:
        # Fallback for tier/tema_lirico: majority of tracks must match (>50%)
        count_sql = (
            "SELECT COUNT(*) FROM ("
            "  SELECT al.id FROM albums al "
            "  JOIN tracks t ON t.album_id=al.id "
            "  JOIN track_meta tm ON tm.track_id=t.id "
            "  WHERE tm.{f}=? "
            "  GROUP BY al.id "
            "  HAVING COUNT(*)*2 > (SELECT COUNT(*) FROM tracks t2 WHERE t2.album_id=al.id)"
            ")".format(f=field)
        )
        data_sql = (
            "SELECT al.id, al.name, al.cover_path, al.primary_format, al.year, "
            "al.track_count, al.total_duration, al.artist_id, "
            "ar.name as artist_name, "
            "(SELECT CASE WHEN (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) THEN 'silver' ELSE led_color END FROM tracks WHERE album_id=al.id "
            " ORDER BY CASE led_color "
            "   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2 "
            "   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5 "
            "   ELSE 6 END, (COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2')) LIMIT 1) as album_led, "
            "COALESCE(apc.pop_score, 0) as pop_score "
            "FROM albums al "
            "LEFT JOIN artists ar ON al.artist_id=ar.id "
            "LEFT JOIN album_pop_cache apc ON apc.album_id=al.id "
            "JOIN tracks t ON t.album_id=al.id "
            "JOIN track_meta tm ON tm.track_id=t.id "
            "WHERE tm.{f}=? "
            "GROUP BY al.id "
            "HAVING COUNT(*)*2 > (SELECT COUNT(*) FROM tracks t2 WHERE t2.album_id=al.id)".format(f=field)
        )
    order = _album_order(sort, dir_)
    return _paginate(conn, count_sql, [value], data_sql, [value], page, order, page_size=page_size)


@app.route('/mood/<path:mood>')
def browse_mood(mood):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'mood', mood, page, mood, 'mood', sort, dir_)
        display = MOOD_LABELS.get(mood, mood)
        return render_template('browse.html', albums=albums, title=f"Mood: {display}",
                               filter_type='mood', filter_value=mood,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/momento/<path:momento>')
def browse_momento(momento):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'momento', momento, page, momento, 'momento', sort, dir_)
        momento_labels = {
            'morning': 'Mañana ☀️', 'evening': 'Tarde 🌅', 'night': 'Noche 🌙',
            'sleep': 'Para dormir 😴', 'party': 'Fiesta 🎉', 'workout': 'Ejercicio 💪',
            'focus': 'Concentración 🎯', 'anytime': 'Cualquier momento 🎵',
        }
        label = momento_labels.get(momento, momento.capitalize())
        return render_template('browse.html', albums=albums, title=f"Momento: {label}",
                               filter_type='momento', filter_value=momento,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/era/<path:era>')
def browse_era(era):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'era', era, page, era, 'era', sort, dir_)
        era_labels = {
            'early_rock_era':         'Early Rock (50s–60s)',
            'british_invasion_era':   'British Invasion (60s)',
            'classic_rock_era':       'Classic Rock (70s)',
            'nwobhm_synth_era':       'NWOBHM / Synth (80s)',
            'grunge_alternative_era': 'Grunge / Alternative (90s)',
            'post_millennial_era':    'Post-millennial (2000s)',
            'streaming_era':          'Streaming Era (2010s)',
            'current_era':            'Actualidad (2020s+)',
        }
        label = era_labels.get(era, era.replace('_', ' ').title())
        return render_template('browse.html', albums=albums, title=f"Era: {label}",
                               filter_type='era', filter_value=era,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/tema/<path:tema>')
def browse_tema(tema):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'tema_lirico', tema, page, tema, 'tema', sort, dir_)
        return render_template('browse.html', albums=albums, title=f"Tema lírico: {tema.capitalize()}",
                               filter_type='tema', filter_value=tema,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/tier/<path:tier>')
def browse_tier(tier):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        albums, total, total_pages = _meta_browse(conn, 'tier', tier, page, tier, 'tier', sort, dir_)
        tier_labels = {'silver': 'Silver ⭐⭐', 'bronze': 'Bronze ⭐', 'review': 'Por revisar 🔍'}
        label = tier_labels.get(tier, tier.capitalize())
        return render_template('browse.html', albums=albums, title=f"Tier: {label}",
                               filter_type='tier', filter_value=tier,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_)
    finally:
        conn.close()


@app.route('/language/<path:lang>')
def browse_language(lang):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'idioma', lang, page, lang, 'language', sort, dir_)
        lang_labels = {
            'en': 'Inglés 🇬🇧', 'es': 'Español 🇪🇸', 'de': 'Alemán 🇩🇪',
            'fr': 'Francés 🇫🇷', 'pt': 'Portugués 🇵🇹', 'it': 'Italiano 🇮🇹',
            'ja': 'Japonés 🇯🇵', 'ko': 'Coreano 🇰🇷', 'nl': 'Holandés 🇳🇱',
        }
        label = lang_labels.get(lang.lower(), lang.upper())
        return render_template('browse.html', albums=albums, title=f"Idioma: {label}",
                               filter_type='language', filter_value=lang,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()

# ── JSON API ──────────────────────────────────────────────────────────────────

@app.route('/api/track/<int:track_id>')
def api_track(track_id):
    conn = get_db_connection()
    try:
        t = conn.execute('''
            SELECT t.*, a.name as album_name, a.cover_path, a.year as album_year, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums a ON t.album_id=a.id
            LEFT JOIN artists ar ON a.artist_id=ar.id
            WHERE t.id=?
        ''', (track_id,)).fetchone()
        if not t: return jsonify({'error': 'not found'}), 404
        result = track_to_json(t)
        tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (track_id,)).fetchone()
        if tm:
            for k in tm.keys():
                if k != 'track_id':
                    result[f'meta_{k}'] = tm[k]
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/track/<int:track_id>/similar')
def api_track_similar(track_id):
    """Pistas similares a la indicada — alimenta el modal 'Similares' del
       Now Playing. Usa track_meta.similar_tracks_json (ya rankeado por score)
       y las serializa con track_to_json para que sean reproducibles/encolables
       directamente, igual que /api/album/<id>/tracks."""
    conn = get_db_connection()
    try:
        tm = conn.execute(
            'SELECT similar_tracks_json FROM track_meta WHERE track_id=?', (track_id,)
        ).fetchone()
        if not tm or not tm['similar_tracks_json']:
            return jsonify([])
        try:
            similar_raw = json.loads(tm['similar_tracks_json'])
        except Exception:
            return jsonify([])
        if not isinstance(similar_raw, list):
            return jsonify([])
        ids = [s.get('track_id') for s in similar_raw[:10] if isinstance(s, dict) and s.get('track_id')]
        if not ids:
            return jsonify([])
        placeholders = ','.join('?' * len(ids))
        rows = conn.execute(
            f'''SELECT t.*, a.name as album_name, a.cover_path, a.year as album_year,
                       ar.id as artist_id, ar.name as artist_name
                FROM tracks t
                LEFT JOIN albums a ON t.album_id=a.id
                LEFT JOIN artists ar ON a.artist_id=ar.id
                WHERE t.id IN ({placeholders})''',
            ids
        ).fetchall()
        by_id = {r['id']: r for r in rows}
        result = []
        for s in similar_raw[:10]:
            row = by_id.get(s.get('track_id'))
            if not row:
                continue
            d = track_to_json(row)
            d['same_artist'] = bool(s.get('same_artist'))
            d['sim_score']   = s.get('score')
            result.append(d)
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/track/<int:track_id>/similar-artists')
def api_track_similar_artists(track_id):
    """Artistas similares al artista de la pista en reproducción — alimenta
       el botón 'banda' del Now Playing. Resuelve track -> álbum -> artista y
       reusa build_similar_artists(), la misma lógica que la tab Similares
       de /artist/<id>."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            '''SELECT ar.similar_artists_json
               FROM tracks t
               JOIN albums al ON al.id = t.album_id
               JOIN artists ar ON ar.id = al.artist_id
               WHERE t.id = ?''', (track_id,)
        ).fetchone()
        if not row:
            return jsonify([])
        return jsonify(build_similar_artists(conn, row['similar_artists_json'], limit=12))
    finally:
        conn.close()


@app.route('/api/album/<int:album_id>/tracks')
def api_album_tracks(album_id):
    conn = get_db_connection()
    try:
        alb = conn.execute('SELECT name, cover_path, artist_id FROM albums WHERE id=?', (album_id,)).fetchone()
        album_cover      = clean_db_path(alb['cover_path']) if alb else None
        album_artist_id  = alb['artist_id'] if alb else None
        album_name       = alb['name'] if alb else None
        tracks = conn.execute(
            'SELECT * FROM tracks WHERE album_id=? ORDER BY disc_number, CAST(track_number AS INTEGER)',
            (album_id,)
        ).fetchall()
        result = []
        for t in tracks:
            d = dict(t)
            d['file_path']  = clean_db_path(d.get('file_path'))
            d['cover_path'] = album_cover
            d['cover_url']  = cover_url_filter(album_cover)
            d['audio_url']  = audio_url_filter(d['file_path'])
            fmt, led = _fmt_format(d)
            d['format_display'] = fmt
            d['format_color']   = led
            d['duration_fmt']   = _fmt_seconds(d.get('duration'))
            d['artist_id']      = album_artist_id   # needed by player for navigation
            d['album_name']     = album_name        # needed by player for display/navigation
            tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (t['id'],)).fetchone()
            if tm:
                for k in tm.keys():
                    if k != 'track_id':
                        d[f'meta_{k}'] = tm[k]
            result.append(d)
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/tracks/resolve', methods=['POST'])
def api_tracks_resolve():
    """
    Resolve a list of file_path values (as recovered from an imported M3U
    playlist, which only carries title/artist/duration/path) back into full
    track records from the DB — same shape as /api/track/<id> and
    /api/album/<id>/tracks — so the player can restore cover, LED quality
    indicator, format badge, artist/album links and (via the real track id)
    lyrics, exactly as it does for tracks loaded from any other page.
    Paths not found in the library are simply omitted from the response;
    the caller keeps the minimal M3U-only object for those.
    """
    data  = request.get_json(silent=True) or {}
    paths = data.get('file_paths')
    if not isinstance(paths, list) or not paths:
        return jsonify([])

    # Defensive cap — this endpoint is meant for playlist-sized batches
    cleaned = [clean_db_path(p) for p in paths[:2000] if p]
    if not cleaned:
        return jsonify([])

    conn = get_db_connection()
    try:
        placeholders = ','.join('?' * len(cleaned))
        rows = conn.execute(f'''
            SELECT t.*, al.name as album_name, al.cover_path, al.year as album_year,
                   al.artist_id, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums al ON t.album_id=al.id
            LEFT JOIN artists ar ON al.artist_id=ar.id
            WHERE t.file_path IN ({placeholders})
        ''', cleaned).fetchall()

        by_path = {}
        for r in rows:
            d = track_to_json(r)
            by_path[d['file_path']] = d
        # Return in the same order as the request, one entry per matched path
        result = [by_path[p] for p in cleaned if p in by_path]
        return jsonify(result)
    finally:
        conn.close()


def _read_embedded_lyrics(file_path):
    """
    Read SYNCEDLYRICS and LYRICS from any audio file.
    Uses manual case-insensitive key iteration — mutagen VComment.get() is NOT
    reliably case-insensitive across all versions and formats.
    Returns (synced_lrc: str|None, plain_lyrics: str|None).
    """
    import sys
    if not file_path or not os.path.isfile(file_path):
        return None, None
    try:
        from mutagen import File as MFile
        audio = MFile(file_path, easy=False)
        if audio is None:
            print(f"[lyrics] mutagen could not open: {file_path}", file=sys.stderr, flush=True)
            return None, None

        tags = audio.tags
        if tags is None:
            print(f"[lyrics] no tags block in file: {file_path}", file=sys.stderr, flush=True)
            return None, None

        # Dump ALL tag keys for diagnostics
        try:
            all_keys = list(tags.keys())
            print(f"[lyrics] tags in file: {all_keys}", file=sys.stderr, flush=True)
        except Exception as ke:
            print(f"[lyrics] could not list tag keys: {ke}", file=sys.stderr, flush=True)
            all_keys = []

        synced_tag = None
        plain_tag  = None

        # Manual case-insensitive iteration — works for VComment, ID3, MP4, etc.
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ('.flac', '.ogg', '.opus', '.oga'):
            # VorbisComment: keys() returns original-case strings
            # values accessed via tags[key] → list of strings
            for key in tags.keys():
                k = key.lower().strip()
                if k == 'syncedlyrics' and synced_tag is None:
                    vals = tags[key]
                    synced_tag = vals[0] if isinstance(vals, list) else str(vals)
                elif k == 'lyrics' and plain_tag is None:
                    vals = tags[key]
                    plain_tag = vals[0] if isinstance(vals, list) else str(vals)

        elif ext in ('.mp3', '.dsf'):
            # ID3: SYLT = Synchronised Lyrics, USLT = Unsynchronised Lyrics.
            # DSF (DSD Stream File) embeds the SAME ID3 block as MP3 — mutagen
            # just opens it via a different container class (DSF vs MP3), but
            # the tag keys (USLT/SYLT) are identical. DFF (DSDIFF) is excluded:
            # it has no editable tag standard and never reaches this function.
            for key in tags.keys():
                if key.startswith('USLT') and plain_tag is None:
                    v = tags[key]
                    plain_tag = getattr(v, 'text', str(v))
                elif key.startswith('SYLT') and synced_tag is None:
                    v = tags[key]
                    pairs = getattr(v, 'text', None)
                    if pairs:
                        lines = []
                        for lyric_text, ms in pairs:
                            m  = ms // 60000
                            s  = (ms % 60000) // 1000
                            cs = (ms % 1000) // 10
                            lines.append(f"[{m:02}:{s:02}.{cs:02}] {lyric_text}")
                        synced_tag = "\n".join(lines)

        elif ext in ('.m4a', '.aac', '.mp4', '.alac'):
            # MP4 atoms: ©lyr = lyrics
            lyr = tags.get('\xa9lyr')
            if lyr:
                plain_tag = lyr[0] if isinstance(lyr, list) else str(lyr)

        else:
            # Generic fallback for WAV, AIFF, DFF etc. (Vorbis-style comments,
            # used by formats that don't have a dedicated branch above)
            for key in tags.keys():
                k = key.lower().strip()
                if k == 'syncedlyrics' and synced_tag is None:
                    vals = tags[key]
                    synced_tag = vals[0] if isinstance(vals, list) else str(vals)
                elif k == 'lyrics' and plain_tag is None:
                    vals = tags[key]
                    plain_tag = vals[0] if isinstance(vals, list) else str(vals)

        if synced_tag:
            print(f"[lyrics] SYNCEDLYRICS found ({len(synced_tag)} chars)", file=sys.stderr, flush=True)
        elif plain_tag:
            print(f"[lyrics] LYRICS found ({len(plain_tag)} chars)", file=sys.stderr, flush=True)
        else:
            print(f"[lyrics] neither SYNCEDLYRICS nor LYRICS found among keys: {all_keys}", file=sys.stderr, flush=True)

        return synced_tag, plain_tag

    except ImportError:
        import sys
        print("[lyrics] mutagen not installed — pip install mutagen", file=sys.stderr, flush=True)
    except Exception as e:
        import sys
        print(f"[lyrics] mutagen error ({file_path}): {e}", file=sys.stderr, flush=True)
    return None, None


def _get_file_duration(file_path):
    """
    Read actual playback duration from audio stream via mutagen.
    More reliable than DB metadata for lrclib duration matching.
    """
    if not file_path or not os.path.isfile(file_path):
        return None
    try:
        from mutagen import File
        audio = File(file_path)
        if audio and hasattr(audio, 'info') and hasattr(audio.info, 'length'):
            return float(audio.info.length)
    except Exception:
        pass
    return None


def _lyrics_payload(artist, title, track_id, version):
    """Core de resolución de letras (fallback en cascada: tags embebidos ->
    cache local por lrclib_id -> búsqueda en vivo contra lrclib.net).
    Devuelve un dict plano (no jsonify) para que /api/lyrics (web) y
    /api/v1/lyrics (nativo, Ticket 11 Lote A) lo compartan sin duplicar
    esta lógica — mismo criterio que _lookup_artist_website (Ticket 10
    Lote G)."""
    import sys

    def _log(msg):
        print(f"[lyrics] {msg}", file=sys.stderr, flush=True)

    def _empty(msg=''):
        return {'has_lyrics': False, 'has_synced': False,
                'lyrics': '', 'synced': '', 'alternatives': [], 'error': msg}

    def _ok_file(synced, plain, source):
        return {
            'has_lyrics': bool(synced or plain),
            'has_synced': bool(synced),
            'lyrics':     plain  or '',
            'synced':     synced or '',
            'alternatives': [],
            'source': source,
        }

    def _fmt_lrc(data):
        return {
            'id':         data.get('id'),
            'title':      data.get('trackName',    title),
            'artist':     data.get('artistName',   artist),
            'album':      data.get('albumName',    ''),
            'duration':   data.get('duration',     0),
            'lyrics':     data.get('plainLyrics',  '') or '',
            'synced':     data.get('syncedLyrics', '') or '',
            'has_lyrics': bool(data.get('plainLyrics')),
            'has_synced': bool(data.get('syncedLyrics')),
        }

    # ── Resolve file_path and album early (used across multiple steps) ─────────
    file_path  = None
    album_name = ''
    file_dur   = None

    if track_id:
        try:
            conn = get_db_connection()
            row = conn.execute(
                '''SELECT t.file_path, t.duration, a.name as album_name
                   FROM tracks t LEFT JOIN albums a ON t.album_id=a.id
                   WHERE t.id=?''',
                (track_id,)
            ).fetchone()
            conn.close()
            if row:
                file_path  = clean_db_path(row['file_path'])
                album_name = row['album_name'] or ''
                file_dur   = row['duration']
        except Exception as e:
            _log(f"DB resolve error: {e}")

    # ── 1. Embedded file tags (SYNCEDLYRICS / LYRICS Vorbis comment) ──────────
    if file_path:
        synced_tag, plain_tag = _read_embedded_lyrics(file_path)
        if synced_tag:
            _log(f"SYNCEDLYRICS found in file tags — track_id={track_id}")
            return _ok_file(synced_tag, plain_tag, 'embedded_tag')
        elif plain_tag:
            _log(f"LYRICS found in file tags — track_id={track_id}")
            return _ok_file(None, plain_tag, 'embedded_tag')
        else:
            _log(f"No embedded lyrics in file — track_id={track_id}, path={file_path}")

    # ── 2. Get actual file duration from audio stream (more reliable) ──────────
    if file_path and file_dur is None:
        file_dur = _get_file_duration(file_path)
    elif file_path and file_dur:
        actual = _get_file_duration(file_path)
        if actual: file_dur = actual   # prefer stream duration

    # ── 3. lrclib.net direct fetch by stored lrclib_id ────────────────────────
    if track_id and req_lib:
        try:
            conn = get_db_connection()
            tm = conn.execute(
                'SELECT lrclib_id, has_lyrics, has_synced_lrc FROM track_meta WHERE track_id=?',
                (track_id,)
            ).fetchone()
            conn.close()
            if tm and tm['lrclib_id']:
                _log(f"lrclib direct fetch — id={tm['lrclib_id']}")
                r = req_lib.get(f"https://lrclib.net/api/get/{tm['lrclib_id']}", timeout=6)
                if r.status_code == 200:
                    data = r.json()
                    if data.get('plainLyrics') or data.get('syncedLyrics'):
                        return {**_fmt_lrc(data), 'alternatives': [], 'source': 'lrclib_id'}
            else:
                _log(f"No lrclib_id in track_meta — track_id={track_id}")
        except Exception as e:
            _log(f"lrclib direct fetch error: {e}")

    if not artist or not title:
        return _empty('artist and title required')
    if not req_lib:
        return _empty('requests library not available')

    # ── 4. lrclib.net version override ────────────────────────────────────────
    try:
        from urllib.parse import quote

        if version:
            r = req_lib.get(f"https://lrclib.net/api/get/{version}", timeout=6)
            if r.status_code == 200:
                return {**_fmt_lrc(r.json()), 'alternatives': []}

        # ── 5. lrclib.net search — scored by duration + synced + name match ───
        # Strategy: try album+artist+title first, then artist+title, then title alone
        def _search(q):
            try:
                r = req_lib.get(
                    f"https://lrclib.net/api/search?q={quote(q)}",
                    timeout=6
                )
                return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
            except Exception:
                return []

        def _score(item):
            pts = 0
            # Synced lyrics are strongly preferred
            if item.get('syncedLyrics'): pts += 20
            # Duration match (using actual file duration)
            if file_dur and item.get('duration'):
                diff = abs(float(item['duration']) - file_dur)
                if diff < 2:   pts += 15
                elif diff < 5: pts += 8
                elif diff < 15: pts += 2
            # Title match
            if title.lower() in (item.get('trackName', '') or '').lower(): pts += 6
            # Artist match
            if artist.lower() in (item.get('artistName', '') or '').lower(): pts += 5
            # Album match
            if album_name and album_name.lower() in (item.get('albumName', '') or '').lower(): pts += 4
            return pts

        candidates = []

        # Search 1: album + artist + title (most specific)
        if album_name:
            q1 = f"{artist} {album_name} {title}"
            candidates = _search(q1)
            _log(f"lrclib search 1 '{q1[:60]}': {len(candidates)} results")

        # Search 2: artist + title (fallback)
        if not any(_score(c) >= 25 for c in candidates):
            q2 = f"{artist} {title}"
            more = _search(q2)
            _log(f"lrclib search 2 '{q2[:60]}': {len(more)} results")
            # Merge without duplicates
            seen_ids = {c.get('id') for c in candidates}
            candidates += [m for m in more if m.get('id') not in seen_ids]

        if candidates:
            candidates.sort(key=_score, reverse=True)
            best = candidates[0]
            top_score = _score(best)
            _log(f"lrclib best: '{best.get('trackName')}' score={top_score} synced={bool(best.get('syncedLyrics'))}")
            alts = [
                {'id': x.get('id'), 'title': x.get('trackName', ''),
                 'artist': x.get('artistName', ''), 'album': x.get('albumName', ''),
                 'has_synced': bool(x.get('syncedLyrics')), 'duration': x.get('duration', 0)}
                for x in candidates[:8]
            ]
            return {**_fmt_lrc(best), 'alternatives': alts, 'source': 'lrclib_search'}

        _log(f"No lyrics found for artist='{artist}' title='{title}'")
        return _empty()

    except Exception as e:
        _log(f"Unexpected error: {e}")
        return _empty(str(e))


@app.route('/api/lyrics')
def api_lyrics():
    artist   = request.args.get('artist',   '').strip()
    title    = request.args.get('title',    '').strip()
    track_id = request.args.get('track_id', None, type=int)
    version  = request.args.get('version',  None)
    return jsonify(_lyrics_payload(artist, title, track_id, version))


@app.route('/api/v1/lyrics')
@api_login_required
def api_v1_lyrics():
    """Espejo nativo de /api/lyrics (Ticket 11, Lote A) — mismo patrón que
    /api/v1/artist/<id>/website (Ticket 10, Lote G): la ruta web vive
    detrás del gate de sesión-cookie (_require_login implícito por no
    tener @api_login_required), así que este mirror expone el mismo
    contrato JSON bajo Bearer token sin duplicar el fallback en cascada."""
    artist   = request.args.get('artist',   '').strip()
    title    = request.args.get('title',    '').strip()
    track_id = request.args.get('track_id', None, type=int)
    version  = request.args.get('version',  None)
    return jsonify(_lyrics_payload(artist, title, track_id, version))


def _radio_candidate_rows(conn, seed, exclude_track_ids, over_fetch):
    """Una pasada del query de candidatos de 'Infinite' (Ticket 11 §4.7.3,
    rediseñado en Ticket 14 con 10 niveles de señal en vez de los 6
    originales). Separada de _radio_next_tracks para poder reintentar con
    un `seed` relajado (cascada del caso borde de biblioteca chica) sin
    duplicar el SQL.

    `seed` es el dict armado en _radio_next_tracks con todas las señales
    de las pistas semilla (artistas, albumes, generos, mood, tema
    lirico, momento, idioma, nacionalidad, energy/bpm promedio, tracks
    similares, favoritos). Pasar un dict con sets/valores vacios para la
    relajacion en cascada (mismo patron que antes, ahora con más campos).

    Niveles (ver Ticket 14, tabla de pesos acordada con el PO):
      1) Misma pista/album semilla + popularidad Last.fm      -> hasta 60
      2) Mismo artista (otro album) + popularidad Last.fm      -> hasta 50
      3) Artista similar (MusicBrainz) + popularidad + bonus si
         similar_tracks_json de alguna semilla apunta a esta pista -> hasta 47
      4) Favorito (artista/album/pista) relacionado con la semilla
         (similar-artista O genero compartido; graduado: +18 si cumple
         una condicion, +28 si cumple las dos)
      5) Mismo genero primario/secundario                      -> +25/+10
      6) Genero cercano (mapa de similitud, genre_similarity()) -> hasta +12
         (solo si NO es ya un match exacto del nivel 5, para no duplicar)
      7) Mood/tema lirico/bpm/energia                          -> +15/+10/hasta10/hasta10
      8) Momento/decada/idioma/nacionalidad del artista          -> +8/+5/+5/+4
      +) Calidad de audio (LED, "fuente de verdad, nunca recalcular")
                                                                 -> hasta +15
      +) Ruido aleatorio (variedad, sin esto la cola se siente repetitiva)
                                                                 -> +0 a 19
    """
    def ph(values):
        return ','.join('?' for _ in values) or 'NULL'

    album_ph          = ph(seed['album_ids'])
    artist_ph         = ph(seed['artist_ids'])
    related_artist_ph = ph(seed['related_artist_ids'])
    genres_ph_1        = ph(seed['genres'])
    genres_ph_2        = ph(seed['genres'])
    genres_ph_3        = ph(seed['genres'])   # nivel 4 (favoritos relacionados)
    genres_ph_4        = ph(seed['genres'])
    genres_ph_5        = ph(seed['genres'])   # nivel 6 (genero cercano, exclusion de match exacto)
    genres_ph_6        = ph(seed['genres'])
    fav_artist_ph      = ph(seed['favorited_artist_ids'])
    fav_album_ph       = ph(seed['favorited_album_ids'])
    fav_track_ph       = ph(seed['favorited_track_ids'])
    fav_artist_ph_2    = ph(seed['favorited_artist_ids'])
    similar_track_ph   = ph(seed['similar_track_ids'])
    moods_ph           = ph(seed['moods'])
    temas_ph           = ph(seed['temas'])
    momentos_ph        = ph(seed['momentos'])
    idiomas_ph         = ph(seed['idiomas'])
    nationalities_ph   = ph(seed['nationalities'])
    decadas_ph         = ph(seed['decadas'])

    # Nivel 6: genero cercano vía el mapa de similitud (genre_similarity,
    # registrada como funcion SQL en get_db_connection). MAX(...) escalar
    # de SQLite (no agregado) entre todas las combinaciones
    # candidato-campo x semilla-genero -- toma la mejor similitud
    # encontrada. Si no hay generos semilla, el termino es simplemente 0.
    genre_sim_terms = []
    genre_sim_params = []
    if seed['genres']:
        for cand_field in ('tm.genre_primary', 'tm.genre_secondary'):
            for g in seed['genres']:
                genre_sim_terms.append(f"genre_similarity({cand_field}, ?)")
                genre_sim_params.append(g)
        genre_sim_expr = f"MAX({', '.join(genre_sim_terms)})"
    else:
        genre_sim_expr = "0"

    # Nivel 7 continuo: energy (-1..1) y bpm (rango asumido 40-220, ver
    # Ticket 14) -- similitud 1 - distancia_normalizada, NUNCA negativo
    # (MAX con 0 por las dudas de que la resta de un valor NULL rompa el
    # calculo -- COALESCE ya cubre eso, pero MAX(...,0) es la red de
    # seguridad final).
    energy_expr = "0"
    if seed['avg_energy'] is not None:
        energy_expr = (
            f"MAX(0, 1 - ABS(COALESCE(tm.energy, {seed['avg_energy']}) "
            f"- ({seed['avg_energy']})) / 2.0)"
        )
    bpm_expr = "0"
    if seed['avg_bpm'] is not None:
        bpm_expr = (
            f"MAX(0, 1 - MIN(ABS(COALESCE(tm.bpm, {seed['avg_bpm']}) "
            f"- ({seed['avg_bpm']})), 60) / 60.0)"
        )

    sql = f'''
        SELECT t.id, t.title, t.artist,
               -- Niveles 1/2/3: mutuamente excluyentes por prioridad
               ( CASE
                   WHEN al.id IN ({album_ph}) THEN
                       45 + popularity_score(tm.lastfm_listeners, tm.lastfm_playcount) * 15
                   WHEN al.artist_id IN ({artist_ph}) THEN
                       35 + popularity_score(tm.lastfm_listeners, tm.lastfm_playcount) * 15
                   WHEN al.artist_id IN ({related_artist_ph}) THEN
                       25 + popularity_score(tm.lastfm_listeners, tm.lastfm_playcount) * 12
                          + ( CASE WHEN t.id IN ({similar_track_ph}) THEN 10 ELSE 0 END )
                   ELSE 0
                 END )
               -- Nivel 4: favorito relacionado con la semilla, graduado
             + ( CASE (
                   CASE WHEN al.artist_id IN ({fav_artist_ph}) OR al.id IN ({fav_album_ph})
                             OR t.id IN ({fav_track_ph}) THEN
                     ( CASE WHEN al.artist_id IN ({artist_ph}) OR al.artist_id IN ({related_artist_ph})
                            THEN 1 ELSE 0 END )
                   + ( CASE WHEN tm.genre_primary IN ({genres_ph_1}) OR tm.genre_secondary IN ({genres_ph_2})
                            THEN 1 ELSE 0 END )
                   ELSE -1
                 END )
                 WHEN 2 THEN 28 WHEN 1 THEN 18 ELSE 0
               END )
               -- Nivel 5: mismo genero (existente, sin cambios)
             + ( CASE WHEN tm.genre_primary IN ({genres_ph_3}) THEN 25 ELSE 0 END )
             + ( CASE WHEN tm.genre_secondary IN ({genres_ph_4}) THEN 10 ELSE 0 END )
               -- Nivel 6: genero cercano (mapa), solo si NO es ya match exacto del nivel 5
             + ( CASE WHEN tm.genre_primary IN ({genres_ph_5}) OR tm.genre_secondary IN ({genres_ph_6})
                      THEN 0 ELSE {genre_sim_expr} * 12 END )
               -- Nivel 7: mood / tema lirico / bpm / energia
             + ( CASE WHEN tm.mood IN ({moods_ph}) THEN 15 ELSE 0 END )
             + ( CASE WHEN tm.tema_lirico IN ({temas_ph}) THEN 10 ELSE 0 END )
             + ( {bpm_expr} ) * 10
             + ( {energy_expr} ) * 10
               -- Nivel 8: momento / decada / idioma / nacionalidad del artista
             + ( CASE WHEN tm.momento IN ({momentos_ph}) THEN 8 ELSE 0 END )
             + ( CASE WHEN tm.decada IN ({decadas_ph}) THEN 5 ELSE 0 END )
             + ( CASE WHEN tm.idioma IN ({idiomas_ph}) THEN 5 ELSE 0 END )
             + ( CASE WHEN ar.nationality IN ({nationalities_ph}) THEN 4 ELSE 0 END )
               -- Calidad de audio (LED, fuente de verdad ya establecida — ver LED_LABELS/LED_ORDER)
             + ( (6 - CASE t.led_color
                        WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                        WHEN 'red'     THEN 3 WHEN 'cyan'  THEN 4 WHEN 'white' THEN 5
                        ELSE 6 END) / 6.0 * 15 )
             - ( CASE WHEN COALESCE(t.compression_mode,'')='Lossy' OR LOWER(COALESCE(t.codec,'')) IN ('mp3','aac','vorbis','opus','wmav1','wmav2','mp2') THEN 3 ELSE 0 END )
               -- Ruido aleatorio (variedad, existente, sin cambios)
             + (ABS(RANDOM()) % 20)
               AS score
        FROM tracks t
        JOIN albums al ON al.id = t.album_id
        LEFT JOIN artists ar ON ar.id = al.artist_id
        LEFT JOIN track_meta tm ON tm.track_id = t.id
    '''
    params = (
        list(seed['album_ids']) + list(seed['artist_ids'])
        + list(seed['related_artist_ids']) + list(seed['similar_track_ids'])
        + list(seed['favorited_artist_ids']) + list(seed['favorited_album_ids']) + list(seed['favorited_track_ids'])
        + list(seed['artist_ids']) + list(seed['related_artist_ids'])
        + list(seed['genres']) + list(seed['genres'])
        + list(seed['genres']) + list(seed['genres'])
        + list(seed['genres']) + list(seed['genres']) + genre_sim_params
        + list(seed['moods']) + list(seed['temas'])
        + list(seed['momentos'])
        + list(seed['decadas'])
        + list(seed['idiomas']) + list(seed['nationalities'])
    )
    if exclude_track_ids:
        sql += f' WHERE t.id NOT IN ({",".join("?" for _ in exclude_track_ids)})'
        params += list(exclude_track_ids)
    sql += ' ORDER BY score DESC LIMIT ?'
    params.append(over_fetch)
    return conn.execute(sql, params).fetchall()


def _empty_radio_seed(keep_core_from=None):
    """Dict vacio con todas las claves que espera _radio_candidate_rows --
    usado tal cual para la relajacion final de la cascada del caso borde
    de biblioteca chica, o parcialmente poblado (keep_core_from) para
    conservar artista/album/genero en un paso intermedio."""
    seed = {
        'artist_ids': set(), 'album_ids': set(), 'related_artist_ids': set(),
        'genres': set(), 'moods': set(), 'temas': set(), 'momentos': set(),
        'idiomas': set(), 'nationalities': set(), 'decadas': set(),
        'avg_energy': None, 'avg_bpm': None,
        'similar_track_ids': set(),
        'favorited_artist_ids': set(), 'favorited_album_ids': set(),
        'favorited_track_ids': set(),
    }
    if keep_core_from:
        for key in ('artist_ids', 'album_ids', 'related_artist_ids', 'genres'):
            seed[key] = keep_core_from[key]
    return seed


def _radio_next_tracks(conn, user_id, seed_track_ids, exclude_track_ids, limit=5):
    """Core del algoritmo de 'Infinite' (Ticket 11 §4.7, rediseñado en
    Ticket 14 con 10 niveles de señal). Semilla = pistas ya reproducidas
    en la cola actual (§4.7.1, confirmado con el PO: sin historial
    persistente para la v1). Sobre-pide candidatos y hace un muestreo
    aleatorio ponderado por score en vez de devolver el top-N directo,
    para que la 'radio' no se sienta siempre idéntica con la misma
    semilla — mismo approach descripto por Spotify/Apple Music en sus
    blogs técnicos (ponderado + no-determinístico, no ranking fijo)."""
    if not seed_track_ids:
        return []

    seed_ph = ','.join('?' for _ in seed_track_ids)
    seed_rows = conn.execute(
        f'''SELECT al.id AS album_id, al.artist_id, ar.nationality,
                   tm.genre_primary, tm.genre_secondary, tm.mood, tm.tema_lirico,
                   tm.momento, tm.idioma, tm.decada, tm.energy, tm.bpm,
                   tm.similar_tracks_json
            FROM tracks t
            LEFT JOIN albums al ON al.id = t.album_id
            LEFT JOIN artists ar ON ar.id = al.artist_id
            LEFT JOIN track_meta tm ON tm.track_id = t.id
            WHERE t.id IN ({seed_ph})''',
        list(seed_track_ids)
    ).fetchall()

    seed_artist_ids = {r['artist_id'] for r in seed_rows if r['artist_id']}
    seed_album_ids  = {r['album_id']  for r in seed_rows if r['album_id']}
    seed_genres = {r['genre_primary'] for r in seed_rows if r['genre_primary']}
    seed_genres |= {r['genre_secondary'] for r in seed_rows if r['genre_secondary']}
    seed_moods      = {r['mood']        for r in seed_rows if r['mood']}
    seed_temas      = {r['tema_lirico'] for r in seed_rows if r['tema_lirico']}
    seed_momentos   = {r['momento']     for r in seed_rows if r['momento']}
    seed_idiomas    = {r['idioma']      for r in seed_rows if r['idioma']}
    seed_decadas    = {r['decada']      for r in seed_rows if r['decada']}
    seed_nationalities = {r['nationality'] for r in seed_rows if r['nationality']}

    energy_vals = [r['energy'] for r in seed_rows if r['energy'] is not None]
    bpm_vals    = [r['bpm']    for r in seed_rows if r['bpm']    is not None]
    avg_energy = sum(energy_vals) / len(energy_vals) if energy_vals else None
    avg_bpm    = sum(bpm_vals)    / len(bpm_vals)    if bpm_vals    else None

    # similar_tracks_json de cada pista semilla -> pool de tracks
    # "recomendados como parecidos" (Nivel 3). Mismo formato que ya usa
    # /api/track/<id>/similar: lista de {"track_id":..,"score":..}.
    similar_track_ids = set()
    for r in seed_rows:
        raw = r['similar_tracks_json']
        if not raw:
            continue
        try:
            items = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get('track_id'):
                similar_track_ids.add(item['track_id'])
    similar_track_ids -= set(seed_track_ids)  # no recomendar la semilla misma

    # Expandir artistas semilla con sus similares (reusa build_similar_artists,
    # que ya resuelve nombre -> id local; nos quedamos solo con los que
    # existen en la biblioteca).
    related_artist_ids = set()
    for artist_id in seed_artist_ids:
        row = conn.execute(
            'SELECT similar_artists_json FROM artists WHERE id=?', (artist_id,)
        ).fetchone()
        if row and row['similar_artists_json']:
            for sim in build_similar_artists(conn, row['similar_artists_json'], limit=12):
                if sim['id'] and sim['id'] not in seed_artist_ids:
                    related_artist_ids.add(sim['id'])

    favorited_artist_ids = {
        r['item_id'] for r in conn.execute(
            "SELECT item_id FROM user_item_favorites WHERE user_id=? AND item_type='artist'",
            (user_id,)
        ).fetchall()
    }
    favorited_album_ids = {
        r['item_id'] for r in conn.execute(
            "SELECT item_id FROM user_item_favorites WHERE user_id=? AND item_type='album'",
            (user_id,)
        ).fetchall()
    }
    favorited_track_ids = {
        r['item_id'] for r in conn.execute(
            "SELECT item_id FROM user_item_favorites WHERE user_id=? AND item_type='track'",
            (user_id,)
        ).fetchall()
    }

    seed = {
        'artist_ids': seed_artist_ids, 'album_ids': seed_album_ids,
        'related_artist_ids': related_artist_ids, 'genres': seed_genres,
        'moods': seed_moods, 'temas': seed_temas, 'momentos': seed_momentos,
        'idiomas': seed_idiomas, 'nationalities': seed_nationalities,
        'decadas': seed_decadas, 'avg_energy': avg_energy, 'avg_bpm': avg_bpm,
        'similar_track_ids': similar_track_ids,
        'favorited_artist_ids': favorited_artist_ids,
        'favorited_album_ids': favorited_album_ids,
        'favorited_track_ids': favorited_track_ids,
    }

    over_fetch = max(limit * 6, 30)
    candidates = _radio_candidate_rows(conn, seed, exclude_track_ids, over_fetch)

    # Caso borde §4.7.3: biblioteca chica o semilla sin señal resoluble —
    # relajar en cascada. Paso 1: solo artista/album/genero (la señal mas
    # fuerte), todo lo demas (mood/tema/momento/idioma/nacionalidad/
    # favoritos/decada) vacio -- son señales mas especificas/raras, mas
    # propensas a no encontrar nada en una biblioteca chica. Paso 2: todo
    # vacio, orden por calidad de audio + jitter nomas.
    if len(candidates) < limit and (seed_artist_ids or related_artist_ids or seed_genres):
        candidates = _radio_candidate_rows(
            conn, _empty_radio_seed(keep_core_from=seed), exclude_track_ids, over_fetch
        )
    if len(candidates) < limit:
        candidates = _radio_candidate_rows(
            conn, _empty_radio_seed(), exclude_track_ids, over_fetch
        )

    if not candidates:
        return []

    # Dedupe de "casi-duplicados" (Ticket 14): mismo tema con distinto
    # sufijo de version (remaster/live/deluxe/acoustic/etc) no deberia
    # aparecer dos veces seguidas en la cola. Se arma el set de
    # (artista, titulo-normalizado) YA presentes en la cola actual
    # (exclude_track_ids) para no repetir con lo que ya sonó, y se va
    # sumando lo que se elige en este batch para no repetir tampoco
    # dentro del mismo batch.
    already_playing_keys = set()
    if exclude_track_ids:
        excl_ph = ','.join('?' for _ in exclude_track_ids)
        for r in conn.execute(
            f'SELECT artist, title FROM tracks WHERE id IN ({excl_ph})',
            list(exclude_track_ids)
        ).fetchall():
            already_playing_keys.add((
                (r['artist'] or '').strip().lower(),
                _normalize_title_for_radio_dedupe(r['title']),
            ))

    # Muestreo ponderado por score sobre el pool sobre-pedido (no
    # determinístico a propósito, ver docstring), saltando candidatos
    # cuyo (artista, titulo-normalizado) ya está en la cola o ya se
    # eligió en este mismo batch.
    pool = list(candidates)
    weights = [max(1, p['score']) for p in pool]
    chosen_ids = []
    chosen_keys = set(already_playing_keys)
    target = min(limit, len(pool))
    # Límite de intentos generoso (no infinito) por si el pool queda sin
    # nada nuevo que ofrecer tras descartar duplicados -- evita loop
    # eterno en una biblioteca muy chica con muchas versiones del mismo tema.
    max_attempts = len(pool) * 2
    attempts = 0
    while len(chosen_ids) < target and pool and attempts < max_attempts:
        attempts += 1
        total = sum(weights)
        pick = random.uniform(0, total)
        upto = 0.0
        for i, w in enumerate(weights):
            upto += w
            if upto >= pick:
                candidate = pool[i]
                key = (
                    (candidate['artist'] or '').strip().lower(),
                    _normalize_title_for_radio_dedupe(candidate['title']),
                )
                del pool[i]
                del weights[i]
                if key in chosen_keys:
                    break  # descartado por duplicado, sigue el while con el pool ya mas chico
                chosen_keys.add(key)
                chosen_ids.append(candidate['id'])
                break

    if not chosen_ids:
        return []

    chosen_ph = ','.join('?' for _ in chosen_ids)
    rows = conn.execute(
        f'''SELECT t.*, al.name as album_name, al.cover_path, al.artist_id
            FROM tracks t
            LEFT JOIN albums al ON al.id = t.album_id
            WHERE t.id IN ({chosen_ph})''',
        chosen_ids
    ).fetchall()
    by_id = {r['id']: r for r in rows}

    tracks = []
    for tid in chosen_ids:
        r = by_id.get(tid)
        if not r:
            continue
        d = dict(r)
        fmt, led = _fmt_format(d)
        cover = clean_db_path(d.get('cover_path'))
        tracks.append({
            'id':             d['id'],
            'title':          d.get('title'),
            'track_number':   d.get('track_number'),
            'disc_number':    d.get('disc_number'),
            'duration':       d.get('duration'),
            'duration_fmt':   _fmt_seconds(d.get('duration')),
            'format_display': fmt,
            'format_color':   led,
            'artist_id':      d.get('artist_id'),
            'album_name':     d.get('album_name'),
            'cover_url':      cover_url_filter(cover),
            'stream_url':     f'/api/v1/stream/{d["id"]}',
            # Ticket 16 (fix, 18/08): faltaban acá — 'format_display' ya
            # salía bien ('DSD128' etc, via _fmt_format que sí tiene
            # 'is_dsd' disponible en `d`), pero el cliente nativo decide
            # QUÉ MOTOR usar mirando estos campos crudos, no el badge. Sin
            # ellos, is_dsd llega None -> isDSDTrack=false -> la app manda
            # una pista DSD real por el streaming genérico (AVPlayer no
            # puede abrir un contenedor DSD crudo, "Cannot Open" en
            # dispositivo). Mismos 5 campos que ya mandan album/playlist/
            # album-detail (comparar contra esos tres bloques) — este
            # bloque (Infinite/radio) se armó aparte y quedó desalineado.
            'is_dsd': d.get('is_dsd'),
            'dsd_rate': d.get('dsd_rate'),
            'sample_rate_real': d.get('sample_rate_real'),
            'bit_depth': d.get('bit_depth'),
            'container_ext': _container_ext(d),
        })
    return tracks


@app.route('/api/v1/radio/next', methods=['POST'])
@api_login_required
def api_v1_radio_next():
    """'Infinite' (Ticket 11 §4.7): dado lo ya reproducido en la cola
    actual (seed_track_ids) y toda la cola actual como exclusión
    (exclude_track_ids), devuelve hasta `limit` pistas nuevas relacionadas.
    Semilla = solo cola actual, sin historial persistente (confirmado con
    el PO, §4.7.1 — no existe tabla de historial en el schema actual)."""
    data = request.get_json(silent=True) or {}

    def _int_list(key):
        raw = data.get(key) or []
        out = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    seed_track_ids    = _int_list('seed_track_ids')
    exclude_track_ids = _int_list('exclude_track_ids')
    try:
        limit = max(1, min(int(data.get('limit', 5)), 20))
    except (TypeError, ValueError):
        limit = 5

    conn = get_db_connection()
    try:
        tracks = _radio_next_tracks(conn, g.api_user['id'], seed_track_ids, exclude_track_ids, limit)
        return jsonify({'tracks': tracks})
    finally:
        conn.close()


def _api_meta_tracks_payload(args, page_size=None):
    """Cuerpo compartido de /api/meta/tracks (web) y /api/v1/meta/tracks
    (nativo, con @api_login_required) — Ticket 08, Lote A §4.3. Extraído de
    la ruta web al agregar el mirror nativo, mismo criterio que
    _api_search_advanced_payload (no mantener dos copias de la misma
    lógica de dedupe/paginación). Devuelve (payload_dict, status_code) para
    que ambas rutas puedan propagar el 400 de 'field/value inválido' sin
    duplicar esa validación. Agrega 'stream_url' por pista
    incondicionalmente, igual que en _api_search_advanced_payload.

    `page_size` (Ticket 26, Categoría B): None (default) preserva PAGE_SIZE
    tal cual para la ruta web — solo api_v1_meta_tracks() lo resuelve desde
    settings_json y lo pasa explícito. No aplica al modo `intercalar`, que
    ya tenía su propio tope fijo (500) independiente de la paginación
    normal — eso no cambia."""
    field      = args.get('field',      '').strip()
    value      = args.get('value',      '').strip()
    page       = max(1, args.get('page', 1, type=int))
    sort       = args.get('sort',       'popularidad')
    dir_       = args.get('dir',        'desc')
    intercalar = args.get('intercalar', '0') == '1'

    ALLOWED_FIELDS = {'mood', 'momento', 'era', 'tema_lirico', 'tier', 'idioma', 'genre', 'led'}
    if field not in ALLOWED_FIELDS or not value:
        return {'error': 'invalid field or value'}, 400

    conn = get_db_connection()
    try:
        # This endpoint lists INDIVIDUAL tracks (the Pistas tab) — always
        # match each track's own tag. album_meta's predominant-field logic
        # belongs to _meta_browse() (the Álbumes tab) only; applying it here
        # too was pulling in every track from a mostly-matching album even
        # when that specific track didn't carry the tag itself (e.g. a
        # French-tagged track on an English-dominant album wouldn't count
        # here, while home.html's per-track total — and the "cualquiera de
        # estos" Búsqueda Avanzada Pistas view — both already counted it).
        #
        # Colapsa duplicados (misma canción en distintos álbumes) — ver
        # _track_dedupe_condition. Le pasamos el filtro que esta vista YA
        # aplica (base_where) para que la comparación de duplicados quede
        # acotada a "otras copias que también cumplirían este mismo
        # filtro" — así, por ejemplo, filtrar por tier no hace desaparecer
        # una canción solo porque existe una copia de otro tier en otro
        # álbum. count y rows deben compartir exactamente la misma
        # condición (mismo base_where + mismo dedupe) o la paginación
        # queda desalineada con el total.
        if field == 'genre':
            base_where  = '(t.genre=? OR tm.genre_primary=?)'
            base_params = (value, value)
        elif field == 'led':
            # Ticket 20, Ítem 1: led_color vive en 'tracks' (t), no en
            # 'track_meta' (tm) — mismo caso que genre arriba, no un
            # tm.led=? que rompería contra una tabla sin esa columna.
            base_where  = _quality_color_sql('t') + '=?'
            base_params = (value,)
        else:
            base_where  = f'tm.{field}=?'
            base_params = (value,)

        dedupe = _track_dedupe_condition(extra_where=base_where, track_alias='t', pop_alias='tpc')
        # dedupe reincorpora una copia (re-prefijada dup_*) de base_where —
        # sus '?' necesitan su valor otra vez, en el mismo orden.
        full_params = base_params + base_params

        if field in ('genre', 'led'):
            # Mismo motivo que arriba: genre y led_color viven en 'tracks',
            # no en 'track_meta' — el COUNT tiene que arrancar desde tracks
            # con LEFT JOIN a track_meta, no un JOIN (inner) desde
            # track_meta que excluiría pistas sin fila en esa tabla.
            count = conn.execute(f'''
                SELECT COUNT(*) FROM tracks t
                LEFT JOIN track_meta tm ON tm.track_id=t.id
                LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                WHERE {base_where} AND {dedupe}
            ''', full_params).fetchone()[0]
        else:
            count = conn.execute(
                f'''SELECT COUNT(*) FROM track_meta tm
                    JOIN tracks t ON t.id=tm.track_id
                    LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                    WHERE {base_where} AND {dedupe}''', full_params
            ).fetchone()[0]

        # For intercalar mode: fetch a larger batch (up to 500) with random ordering
        # client will do round-robin by artist
        if intercalar:
            limit  = min(500, count)
            offset = 0
            order  = 'RANDOM()'
        else:
            effective_page_size = page_size or PAGE_SIZE
            limit  = effective_page_size
            offset = (page - 1) * effective_page_size
            order  = _track_order(sort, dir_)

        # Build the WHERE clause — per-track always (see comment above)
        where_clause = f'{base_where} AND {dedupe}'
        extra_join   = ''
        where_params = full_params

        rows = conn.execute(f'''
            SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.dsd_rate,
                   t.sample_rate_real, t.bit_depth, t.is_mqa, t.codec, t.bitrate, t.compression_mode,
                   t.duration, t.track_number, t.file_path,
                   a.id as album_id, a.name as album_name, a.cover_path, a.year as album_year,
                   ar.id as artist_id, ar.name as artist_name,
                   tm.mood, tm.momento, tm.tier, tm.bpm, tm.tonalidad,
                   tm.energy, tm.bailabilidad, tm.lrclib_id, tm.has_lyrics, tm.has_synced_lrc,
                   tm.tema_lirico, tm.idioma, tm.genre_primary,
                   COALESCE(tpc.pop_score, 0) as pop_score
            FROM tracks t
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            LEFT JOIN albums a ON a.id=t.album_id
            LEFT JOIN artists ar ON ar.id=a.artist_id
            LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
            {extra_join}
            WHERE {where_clause}
            ORDER BY {order}
            LIMIT ? OFFSET ?
        ''', (*where_params, limit, offset)).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d['cover_path'] = clean_db_path(d.get('cover_path'))
            d['cover_url']  = cover_url_filter(d['cover_path'])
            d['file_path']  = clean_db_path(d.get('file_path'))
            d['audio_url']  = audio_url_filter(d['file_path'])
            d['stream_url'] = f'/api/v1/stream/{d["id"]}'
            # Bug preexistente encontrado al construir Lote B (nativo): esta
            # función descartaba el segundo valor de _fmt_format() (el color
            # LED), a diferencia de track_to_json() y de la enriquecida en
            # api_v1_search(), que sí lo exponen como 'format_color'. El
            # nativo lo necesita para pintar el punto de color junto al
            # badge de formato (FacetDisplay.ledColor(_:), MixedTrackRow) —
            # sin este campo, esa vista queda sin el punto de color pero por
            # lo demás funciona igual. Se corrige acá porque esta función es
            # compartida (/api/meta/tracks web + /api/v1/meta/tracks
            # nativo): el campo extra no le cambia nada al JS de la web,
            # que ya lo ignora, mismo criterio que 'stream_url'.
            fmt, led = _fmt_format(d)
            d['format_display'] = fmt
            d['format_color']   = led
            d['duration_fmt']   = _fmt_seconds(d.get('duration'))
            d['container_ext']  = _container_ext(d)
            result.append(d)

        total_pages = 1 if intercalar else max(1, (count + effective_page_size - 1) // effective_page_size)
        return {
            'tracks':      result,
            'total':       count,
            'page':        page,
            'total_pages': total_pages,
            'intercalar':  intercalar,
        }, 200
    finally:
        conn.close()

@app.route('/api/meta/tracks')
def api_meta_tracks():
    payload, status = _api_meta_tracks_payload(request.args)
    return jsonify(payload), status

@app.route('/api/v1/meta/tracks')
@api_login_required
def api_v1_meta_tracks():
    """Espejo nativo de /api/meta/tracks — Ticket 08, Lote A §4.3. Misma
    lógica exacta vía _api_meta_tracks_payload (ver comentario ahí),
    protegido por token en vez de cookie de sesión.

    Ticket 26, Categoría B: a diferencia de la ruta web (arriba), acá se
    resuelve search_page_size desde settings_json del usuario."""
    page_size = _settings_payload(g.api_user['id'])['search_page_size']
    payload, status = _api_meta_tracks_payload(request.args, page_size=page_size)
    return jsonify(payload), status


@app.route('/api/debug/tags/<int:track_id>')
def api_debug_tags(track_id):
    """Return all mutagen tags for a track — for diagnosing embedded lyrics."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT file_path, title, artist FROM tracks WHERE id=?', (track_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'track not found'}), 404

    file_path = clean_db_path(row['file_path'])
    result = {
        'track_id':  track_id,
        'title':     row['title'],
        'artist':    row['artist'],
        'file_path': file_path,
        'file_exists': os.path.isfile(file_path),
        'tags': {},
        'has_syncedlyrics': False,
        'has_lyrics': False,
        'syncedlyrics_len': 0,
        'error': None,
    }
    if not result['file_exists']:
        result['error'] = 'File not found on disk'
        return jsonify(result)
    try:
        from mutagen import File as MFile
        audio = MFile(file_path, easy=False)
        if audio is None:
            result['error'] = 'mutagen could not open file'
            return jsonify(result)
        if audio.tags is None:
            result['error'] = 'No tags block in file'
            return jsonify(result)
        # Collect all tags (truncate large values for display)
        for key in audio.tags.keys():
            try:
                vals = audio.tags[key]
                v = vals[0] if isinstance(vals, list) else str(vals)
                k_lower = key.lower().strip()
                # Vorbis-style (FLAC/OGG): 'syncedlyrics' / 'lyrics'
                # ID3-style (MP3/DSF):    'SYLT...'      / 'USLT...'
                is_synced_key = (k_lower == 'syncedlyrics') or key.startswith('SYLT')
                is_plain_key  = (k_lower == 'lyrics')       or key.startswith('USLT')
                if is_synced_key or is_plain_key:
                    result['tags'][key] = f"[{len(str(v))} chars — first 200: {str(v)[:200]}]"
                else:
                    result['tags'][key] = str(v)[:300]
                if is_synced_key:
                    result['has_syncedlyrics'] = True
                    result['syncedlyrics_len'] = len(str(v))
                if is_plain_key:
                    result['has_lyrics'] = True
            except Exception as ke:
                result['tags'][key] = f'[error reading: {ke}]'
    except ImportError:
        result['error'] = 'mutagen not installed'
    except Exception as e:
        result['error'] = str(e)
    return jsonify(result)


@app.route('/api/v1/health')
def api_health():
    root_ok  = os.path.isdir(MUSIC_ROOT)
    root_contents = []
    if root_ok:
        try: root_contents = sorted(os.listdir(MUSIC_ROOT))[:10]
        except: pass
    db_ok = False
    track_count = 0
    try:
        c = get_db_connection()
        track_count = c.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]
        c.close()
        db_ok = True
    except: pass
    return jsonify({
        'music_root':     MUSIC_ROOT,
        'music_root_ok':  root_ok,
        'music_root_top': root_contents,
        'db_ok':          db_ok,
        'track_count':    track_count,
    })

@app.route('/api/outputs')
def api_outputs():
    try:
        result = subprocess.run(['mpc', 'outputs'], capture_output=True, text=True, timeout=3)
        outputs = []
        for line in result.stdout.strip().split('\n'):
            if 'Output' in line:
                parts = line.split()
                enabled = 'enabled' in line
                name = ' '.join(parts[1:-2]) if len(parts) > 3 else (parts[1] if len(parts) > 1 else 'Output')
                try: idx = int(parts[0].replace('Output','').strip())
                except: idx = 1
                outputs.append({'id': idx, 'name': name, 'enabled': enabled})
        return jsonify(outputs)
    except Exception:
        return jsonify([{'id': 1, 'name': 'Default Output', 'enabled': True}])


@app.route('/api/output/toggle', methods=['POST'])
def api_output_toggle():
    data = request.get_json() or {}
    try:
        cmd = 'enable' if data.get('enable', True) else 'disable'
        result = subprocess.run(
            ['mpc', 'output', cmd, str(data.get('id', 1))],
            capture_output=True, timeout=3
        )
        if result.returncode == 0:
            return jsonify({'status': 'ok'})
        err = result.stderr.decode(errors='replace').strip()
        return jsonify({'status': 'error', 'message': err or 'mpc failed'}), 200
    except FileNotFoundError:
        return jsonify({'status': 'error', 'message': 'mpc not installed'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 200

# ── Files / MPD ───────────────────────────────────────────────────────────────

@app.route('/audio/<path:filepath>')
def audio_file(filepath):
    absolute_path = os.path.join(MUSIC_ROOT, filepath.lstrip('/'))

    # Try exact path
    if os.path.isfile(absolute_path):
        return _serve_audio(absolute_path)

    # Try with different audio extensions (flac → dsf, wav, etc.)
    base, ext = os.path.splitext(absolute_path)
    for alt_ext in ('.flac', '.dsf', '.dff', '.wav', '.aiff', '.mp3', '.m4a'):
        if alt_ext != ext.lower():
            alt = base + alt_ext
            if os.path.isfile(alt):
                return _serve_audio(alt)

    # Log for diagnostics
    app.logger.warning(f"Audio 404: {absolute_path}")
    return "File not found", 404


def _serve_audio(absolute_path):
    file_size = os.path.getsize(absolute_path)
    rh = request.headers.get('Range')
    if rh:
        start, end = parse_range_header(rh, file_size)
        end = end if end is not None else file_size - 1
        length = end - start + 1
        with open(absolute_path, 'rb') as f:
            f.seek(start)
            data = f.read(length)
        mime, _ = mimetypes.guess_type(absolute_path)
        resp = Response(data, status=206, mimetype=mime or 'application/octet-stream')
        resp.headers['Content-Range']  = f'bytes {start}-{end}/{file_size}'
        resp.headers['Accept-Ranges']  = 'bytes'
        resp.headers['Content-Length'] = length
        return resp
    return send_file(absolute_path, conditional=True)


# ── Transmitir a dispositivo de red (UPnP/DLNA, YXC) ────────────────────────
# /audio/<path> exige sesión — un receiver o parlante MusicCast no puede
# loguearse. Esta ruta es la excepción pública: un admin genera un token de
# corta duración atado a UNA pista puntual justo antes de mandarle el
# comando SetAVTransportURI al dispositivo, así nunca queda un link
# permanente a la librería dando vueltas. Sirve el archivo CRUDO (ni
# siquiera FLAC/DSD pasan por transcodificación — /stream-dsd existe porque
# el navegador no puede reproducir DSD nativo, pero un renderer UPnP como el
# RX-A880 sí, así que acá directamente no hace falta).
_CAST_TOKEN_MAX_AGE = 600  # 10 minutos
_cast_signer = URLSafeTimedSerializer(app.secret_key, salt='cast-audio-v1')

def cast_token_for_track(track_id):
    return _cast_signer.dumps({'track_id': track_id})

@app.route('/cast-audio/<int:track_id>')
# Ticket D-04: version PCM de un DSD por PATH (/pcm/hi, /pcm/cd) en vez de
# ?pcm=1. El TX-8050 rechazo con HTTP 500 el FLAC 88.2k/24 servido con
# ?token=...&pcm=1, aunque el FLAC normal (URL sin '&') le funciona: o es el
# '&' en la URL o la resolucion. Path sin '&' + cascada de perfiles cubre
# ambas causas. Mismo endpoint 'cast_audio' -> sigue exento de _require_login.
@app.route('/cast-audio/<int:track_id>/pcm/<perfil>')
def cast_audio(track_id, perfil=None):
    token = request.args.get('token', '')
    try:
        data = _cast_signer.loads(token, max_age=_CAST_TOKEN_MAX_AGE)
    except SignatureExpired:
        return "Token vencido — pedí uno nuevo desde /admin/colaborativa o el reproductor", 403
    except BadSignature:
        return "Token inválido", 403
    if data.get('track_id') != track_id:
        return "Token no corresponde a esta pista", 403

    conn = get_db_connection()
    try:
        track = conn.execute('SELECT file_path FROM tracks WHERE id=?', (track_id,)).fetchone()
    finally:
        conn.close()
    if not track or not track['file_path']:
        return "Pista no encontrada", 404

    path = clean_db_path(track['file_path'])
    if not os.path.isfile(path):
        app.logger.warning(f"cast_audio: archivo no encontrado en disco: {path}")
        return "Archivo no encontrado en disco", 404
    # Ticket D-03/D-04: /pcm/<perfil> -> version FLAC de un DSD (ver _dsd_transcode_para_cast).
    # El token sigue atado al track_id, no hace falta otro.
    if perfil:  # Ticket D-09: tambien PCM hi-res (antes solo DSD)
        if perfil not in _CAST_PCM_PERFILES:
            return "Perfil PCM desconocido", 404
        pcm_path = _dsd_transcode_para_cast(path, perfil)
        if not pcm_path:
            return "No se pudo convertir el DSD a PCM", 500
        path = pcm_path
    # Ticket D-07: cabeceras DLNA en la respuesta - algunos renderers las piden
    # (getcontentFeatures.dlna.org: 1) para decidir si permiten Seek.
    resp = _serve_audio(path)
    resp.headers['contentFeatures.dlna.org'] = _CAST_DLNA_FLAGS
    resp.headers['transferMode.dlna.org'] = 'Streaming'
    resp.headers['Accept-Ranges'] = 'bytes'
    return resp


@app.route('/cast-cover/<int:track_id>')
def cast_cover(track_id):
    """Igual que /cast-audio pero para la portada — usa el MISMO token (está
    atado al track_id, sirve para las dos cosas de esa pista puntual)."""
    token = request.args.get('token', '')
    try:
        data = _cast_signer.loads(token, max_age=_CAST_TOKEN_MAX_AGE)
    except (SignatureExpired, BadSignature):
        return "Token inválido o vencido", 403
    if data.get('track_id') != track_id:
        return "Token no corresponde a esta pista", 403

    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT al.cover_path FROM tracks t LEFT JOIN albums al ON t.album_id = al.id WHERE t.id=?',
            (track_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row['cover_path']:
        return "Sin portada", 404
    cover_path = clean_db_path(row['cover_path'])
    if not os.path.isfile(cover_path):
        return "Portada no encontrada en disco", 404
    return send_file(cover_path, conditional=True)


# ── "Reproducir en…" — UPnP/DLNA hacia otros dispositivos de la casa ───────────
# Mismo mecanismo validado a mano con tools/cast_discovery.py y
# tools/cast_test.py contra el RX-A880 real — acá queda integrado al panel
# del reproductor. Los scripts de tools/ se dejan tal cual (ya probados
# contra hardware real) en vez de hacerlos depender de este código.

_CAST_SSDP_ADDR, _CAST_SSDP_PORT = '239.255.255.250', 1900
_CAST_MIME_BY_EXT = {
    '.flac': 'audio/flac', '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
    '.aiff': 'audio/aiff', '.aif': 'audio/aiff', '.m4a': 'audio/mp4',
    '.dsf': 'audio/x-dsd', '.dff': 'audio/x-dsd',
}
# DSD por UPnP/DLNA no tiene un MIME único estandarizado — distintos
# renderers reales esperan cosas distintas (confirmado investigando: hay
# implementaciones que solo aceptan audio/dsd o audio/x-dsd, otras solo
# audio/x-dsf/audio/dsf, sin relación con la extensión real del archivo).
# En vez de adivinar uno y fallar en silencio, se prueban en orden hasta
# que el dispositivo efectivamente arranca a reproducir.
_CAST_DSD_MIME_CANDIDATES = ['audio/x-dsd', 'audio/dsd', 'audio/x-dsf', 'audio/dsf']

def _cast_ssdp_discover(timeout=4):
    """Devuelve las LOCATION únicas que respondieron al M-SEARCH — mismo
    mecanismo que tools/cast_discovery.py, resumido acá para poder
    dispararlo desde el botón del panel sin depender de un script externo."""
    msg = '\r\n'.join([
        'M-SEARCH * HTTP/1.1', f'HOST: {_CAST_SSDP_ADDR}:{_CAST_SSDP_PORT}',
        'MAN: "ssdp:discover"', f'MX: {min(timeout, 5)}', 'ST: ssdp:all', '', '',
    ]).encode('utf-8')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    sock.sendto(msg, (_CAST_SSDP_ADDR, _CAST_SSDP_PORT))
    locations, deadline = set(), time.time() + timeout
    while time.time() < deadline:
        try:
            data, _addr = sock.recvfrom(65507)
        except socket.timeout:
            break
        for line in data.decode('utf-8', errors='ignore').split('\r\n'):
            if line.lower().startswith('location:'):
                locations.add(line.split(':', 1)[1].strip())
    sock.close()
    return locations

def _cast_fetch_device_info(location, timeout=4):
    """(friendlyName, manufacturer, modelName, av_transport_url, rendering_control_url, ip)
    o None si no es un dispositivo que sirva (sin AVTransport = no renderer
    de audio). rendering_control_url puede ser None — no todos los
    renderers lo separan de AVTransport, en ese caso el volumen por UPnP
    simplemente no está disponible para ese dispositivo (se degrada solo,
    no rompe el resto del cast)."""
    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError):
        return None
    ns = {'d': 'urn:schemas-upnp-org:device-1-0'}
    device = root.find('d:device', ns)
    if device is None:
        return None
    def text_of(tag, default='?'):
        el = device.find(f'd:{tag}', ns)
        return el.text.strip() if el is not None and el.text else default
    av_transport_url = None
    rendering_control_url = None
    seen_service_types = []
    for service in device.findall('.//d:service', ns):
        st_el = service.find('d:serviceType', ns)
        service_type = st_el.text if st_el is not None and st_el.text else ''
        seen_service_types.append(service_type)
        cu = service.find('d:controlURL', ns)
        cu_text = cu.text.strip() if cu is not None and cu.text else None
        if 'AVTransport' in service_type and cu_text:
            av_transport_url = urljoin(location, cu_text)
        elif 'RenderingControl' in service_type and cu_text:
            rendering_control_url = urljoin(location, cu_text)
    if not av_transport_url:
        return None
    app.logger.info(f"[cast] {text_of('friendlyName')} ({location}) servicios: {seen_service_types}")
    if not rendering_control_url:
        app.logger.info(f"[cast] {text_of('friendlyName')}: sin RenderingControl -> sin volumen por UPnP para este dispositivo")
    return text_of('friendlyName'), text_of('manufacturer'), text_of('modelName'), \
        av_transport_url, rendering_control_url, urlparse(location).hostname

def _cast_build_didl(track_id, title, artist, protocol_info, media_url, file_size=None,
                      album=None, genre=None, track_number=None, cover_url=None):
    size_attr = f' size="{file_size}"' if file_size else ''
    extra = ''
    if album:
        extra += f'<upnp:album>{_xml_escape(album)}</upnp:album>'
    if artist:
        extra += f'<upnp:artist>{_xml_escape(artist)}</upnp:artist>'
    if genre:
        extra += f'<upnp:genre>{_xml_escape(genre)}</upnp:genre>'
    if track_number:
        extra += f'<upnp:originalTrackNumber>{_xml_escape(str(track_number))}</upnp:originalTrackNumber>'
    if cover_url:
        # dlna:profileID="JPEG_TN" es lo que esperan la mayoría de los renderers
        # reales (confirmado contra implementaciones tipo Gerbera/Twonky) — sin
        # el namespace dlna correcto, muchos renderers directamente lo ignoran.
        extra += (f'<upnp:albumArtURI xmlns:dlna="urn:schemas-dlna-org:metadata-1-0" '
                  f'dlna:profileID="JPEG_TN">{_xml_escape(cover_url)}</upnp:albumArtURI>')
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="{track_id}" parentID="0" restricted="1">'
        f'<dc:title>{_xml_escape(title or "Pista")}</dc:title>'
        f'<dc:creator>{_xml_escape(artist or "")}</dc:creator>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'{extra}'
        f'<res protocolInfo="{_xml_escape(protocol_info)}"{size_attr}>{_xml_escape(media_url)}</res>'
        '</item></DIDL-Lite>'
    )

def _cast_soap_call(control_url, action, body_xml, timeout=8, service='AVTransport'):
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_xml}</s:Body></s:Envelope>'
    ).encode('utf-8')
    req = urllib.request.Request(control_url, data=envelope, method='POST')
    req.add_header('Content-Type', 'text/xml; charset="utf-8"')
    req.add_header('SOAPACTION', f'"urn:schemas-upnp-org:service:{service}:1#{action}"')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            try:
                data = resp.read()
            except http.client.IncompleteRead as e:
                # Renderers UPnP reales (stacks HTTP embebidos, de bajo costo)
                # a veces mandan un Content-Length que no coincide con lo que
                # realmente escriben en la respuesta. Esto pasa LEYENDO la
                # respuesta — el comando ya se le mandó y ejecutó igual — así
                # que nos quedamos con lo parcial en vez de tirar la llamada
                # entera (visto en la práctica contra el RX-A880 real).
                data = e.partial
            return status, data.decode('utf-8', errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='ignore')
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return None, str(e)

def _cast_get_transport_state(control_url, timeout=4):
    """Consulta GetTransportInfo — el estado REAL del renderer (PLAYING,
    TRANSITIONING, STOPPED, NO_MEDIA_PRESENT…), no solo si el SOAP call
    anterior devolvió 200. Un renderer puede aceptar SetAVTransportURI+Play
    (200 los dos) y sin embargo nunca arrancar a sonar si no reconoce el
    formato — esto es lo único que lo detecta de verdad. Devuelve el string
    de estado, o None si no se pudo consultar."""
    status, body = _cast_soap_call(
        control_url, 'GetTransportInfo',
        '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:GetTransportInfo>', timeout=timeout)
    if status != 200:
        return None
    m = re.search(r'<CurrentTransportState>([^<]*)</CurrentTransportState>', body)
    return m.group(1) if m else None

def _cast_set_volume(rendering_control_url, volume_0_100, timeout=4):
    body = ('<u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            '<InstanceID>0</InstanceID><Channel>Master</Channel>'
            f'<DesiredVolume>{int(volume_0_100)}</DesiredVolume></u:SetVolume>')
    return _cast_soap_call(rendering_control_url, 'SetVolume', body, timeout=timeout, service='RenderingControl')

def _cast_get_volume(rendering_control_url, timeout=4):
    """GetVolume — el volumen que el dispositivo YA tiene configurado (perilla
    física, control remoto, o lo que haya quedado de antes). Se usa al
    conectar para reflejarlo en el slider local en vez de imponerle el valor
    del slider al dispositivo (que podía dejarlo sonando muy fuerte o muy
    bajo la primera vez). Devuelve un int 0-100, o None si no se pudo leer
    (dispositivo sin RenderingControl, o que no responde GetVolume)."""
    body = ('<u:GetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            '<InstanceID>0</InstanceID><Channel>Master</Channel></u:GetVolume>')
    status, resp_body = _cast_soap_call(rendering_control_url, 'GetVolume', body, timeout=timeout, service='RenderingControl')
    if status != 200:
        return None
    m = re.search(r'<CurrentVolume>(\d+)</CurrentVolume>', resp_body)
    return max(0, min(100, int(m.group(1)))) if m else None

def _cast_send_track(control_url, media_url, didl):
    status, body = _cast_soap_call(control_url, 'SetAVTransportURI',
        f'<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        f'<InstanceID>0</InstanceID><CurrentURI>{_xml_escape(media_url)}</CurrentURI>'
        f'<CurrentURIMetaData>{_xml_escape(didl)}</CurrentURIMetaData></u:SetAVTransportURI>')
    if status != 200:
        return status, body
    return _cast_soap_call(control_url, 'Play',
        '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>')


# Ticket D-07: OP=01 -> seek por rango de bytes; FLAGS = streaming + background
# transfer + DLNA v1.5 (los mismos que anuncian Gerbera/MiniDLNA).
_CAST_DLNA_FLAGS = 'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01500000000000000000000000000000'
# Ticket D-08: cuanto se espera a que el renderer pase a PLAYING tras cargar una pista.
_CAST_ARRANQUE_S = 4.0


# ── Ticket D-14 (pedido por Niko): preparar receptores Onkyo/Integra (eISCP) ─
# El TX-8050 despertado por SetAVTransportURI se enciende en NET con el ULTIMO
# servicio usado (ej. radio por internet) y no reproduce nada. Antes de mandar
# una pista: si el equipo habla eISCP (puerto 60128), se enciende si esta en
# standby (PWR01), se pasa a la entrada NET (SLI2B) y al servicio DLNA (NSV000)
# - los comandos que Niko confirmo a mano. Cualquier otro renderer (Yamaha,
# etc.) no tiene ese puerto abierto: se detecta en < 1 s y se sigue igual que
# siempre. Solo RAM, sin columnas nuevas.
_EISCP_PORT = 60128
_eiscp_listo = {}  # host -> monotonic del ultimo envio que sono (cola en curso: no repetir)


def _eiscp_paquete(cmd):
    data = f"!1{cmd}\r".encode('ascii')
    return b"ISCP" + struct.pack(">IIB3x", 16, len(data), 1) + data


def _eiscp(host, cmd, consulta=None, timeout=2.0):
    """Manda un comando eISCP. Con consulta='PWR' (p. ej.) devuelve el valor de
    la primera respuesta '!1PWRxx' ('01', '00', '2B'...). None si no hubo
    respuesta; lanza OSError si el puerto no esta abierto (no es eISCP)."""
    with socket.create_connection((host, _EISCP_PORT), timeout=0.8) as s:
        s.settimeout(timeout)
        s.sendall(_eiscp_paquete(cmd))
        if not consulta:
            return None
        buf, fin = b"", time.monotonic() + timeout
        while time.monotonic() < fin:
            try:
                trozo = s.recv(1024)
            except socket.timeout:
                break
            if not trozo:
                break
            buf += trozo
            for m in re.finditer(rb"!1" + consulta.encode() + rb"([0-9A-Za-z]+)", buf):
                return m.group(1).decode('ascii', 'ignore').upper()
        return None


def _cast_preparar_eiscp(control_url, estado_actual):
    """Deja un Onkyo/Integra listo para recibir por DLNA. No hace nada si el
    renderer ya esta reproduciendo/pausado o si sono hace poco (cola en curso)."""
    host = urlparse(control_url).hostname
    if not host or estado_actual in ('PLAYING', 'PAUSED_PLAYBACK', 'TRANSITIONING'):
        return
    # Ticket D-15: el atajo "sono hace poco" solo vale si el renderer RESPONDE.
    # estado None = no contesta UPnP (standby): hay que despertarlo aunque haya
    # sonado hace 1 minuto (Niko lo apago entre dos pistas y se salteaba todo).
    if estado_actual is not None and time.monotonic() - _eiscp_listo.get(host, 0) < 120:
        return
    try:
        pwr = _eiscp(host, 'PWRQSTN', consulta='PWR')
    except OSError:
        if estado_actual is None:
            # Ticket D-15: ni UPnP ni eISCP -> apagado del todo, o un Onkyo con
            # "Network Standby" desactivado (en standby no escucha la red).
            app.logger.info(f"[cast eiscp] {host} no responde (ni UPnP ni eISCP)")
        return  # no habla eISCP -> no es Onkyo/Integra
    try:
        if pwr != '01':
            app.logger.info(f"[cast eiscp] {host} en standby (PWR={pwr}) -> encendiendo")
            _eiscp(host, 'PWR01')
            fin = time.monotonic() + 12
            while time.monotonic() < fin:
                time.sleep(1)
                if _eiscp(host, 'PWRQSTN', consulta='PWR') == '01':
                    break
            time.sleep(3)  # el modulo de red tarda un poco mas que la pantalla
        sli = _eiscp(host, 'SLIQSTN', consulta='SLI')
        if sli != '2B':
            app.logger.info(f"[cast eiscp] {host} entrada {sli} -> NET (SLI2B)")
            _eiscp(host, 'SLI2B')
            time.sleep(1.5)
        app.logger.info(f"[cast eiscp] {host} -> servicio DLNA (NSV000)")
        _eiscp(host, 'NSV000')
        time.sleep(2.5)
        # Ticket D-15: recien despertado, el renderer DLNA tarda en volver a
        # contestar UPnP; esperar hasta 15 s antes de mandarle la pista.
        if estado_actual is None:
            fin = time.monotonic() + 15
            while time.monotonic() < fin and _cast_get_transport_state(control_url) is None:
                time.sleep(1)
            app.logger.info(f"[cast eiscp] {host} UPnP {'listo' if time.monotonic() < fin else 'sin respuesta tras 15 s'}")
    except OSError as e:
        app.logger.warning(f"[cast eiscp] {host}: no se pudo preparar ({e}) — se intenta igual")


def _cast_try_send_track(control_url, track, media_url, cover_url, file_path, file_size, mime_candidates):
    """Prueba cada mime de mime_candidates hasta que el renderer EFECTIVAMENTE
    arranca a reproducir (confirmado con GetTransportInfo — un renderer puede
    aceptar SetAVTransportURI+Play con 200 en los dos y jamás sonar si no
    reconoce el formato, es justo lo que pasaba con DSD). Para archivos no-DSD
    mime_candidates trae un solo elemento, así que el comportamiento es
    idéntico al de antes, solo que ahora también queda confirmado por estado
    real en vez de confiar ciegamente en el 200.
    Devuelve (ok: bool, mime_usado: str|None, motivo_si_falló: str|None)."""
    last_reason = 'El dispositivo no respondió'
    # Ticket D-07: primero con flags DLNA que anuncian "se puede saltar por
    # rangos de bytes" (DLNA.ORG_OP=01). El TX-8050 respondia HTTP 500 a
    # Seek/Pause con el protocolInfo pelado ('*'): muchos renderers DLNA
    # solo habilitan Seek si el recurso lo declara. Si un renderer rechaza
    # los flags, se reintenta con '*' (comportamiento de siempre).
    intentos = [(m, f) for m in mime_candidates for f in (_CAST_DLNA_FLAGS, '*')]
    # Ticket D-08: si el renderer esta sonando, Stop ANTES de cargar la pista
    # nueva. Visto en el TX-8050: SetAVTransportURI+Play sobre una pista en
    # curso lo deja en STOPPED durante la transicion, y el chequeo lo daba
    # por rechazado (pasaba al cambiar de pista a mano, no al terminar sola).
    estado_previo = _cast_get_transport_state(control_url)
    _cast_preparar_eiscp(control_url, estado_previo)  # Ticket D-14
    if estado_previo in ('PLAYING', 'PAUSED_PLAYBACK', 'TRANSITIONING'):
        _cast_soap_call(control_url, 'Stop',
            '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID></u:Stop>', timeout=4)
        time.sleep(0.5)
    for mime, flags in intentos:
        protocol_info = f'http-get:*:{mime}:{flags}'
        didl = _cast_build_didl(track['id'], track['title'], track['artist'], protocol_info,
                                 media_url, file_size, album=track['album_name'], genre=track['genre'],
                                 track_number=track['track_number'], cover_url=cover_url)
        status, body = _cast_send_track(control_url, media_url, didl)
        app.logger.info(f"[cast] SetAVTransportURI+Play mime={mime} dlna={'si' if flags != '*' else 'no'} -> HTTP {status}")
        if status != 200:
            last_reason = ('El equipo no responde (su servicio de red DLNA no contesta)' if status is None
                           else f'El dispositivo devolvió HTTP {status} para {mime}')
            continue
        # Ticket D-08: en vez de UNA consulta a los 1.2 s, se consulta cada
        # 0.5 s hasta _CAST_ARRANQUE_S: un FLAC hi-res o un renderer lento
        # puede pasar por STOPPED/TRANSITIONING antes de sonar.
        deadline = time.monotonic() + _CAST_ARRANQUE_S
        time.sleep(0.8)
        state = _cast_get_transport_state(control_url)
        while state != 'PLAYING' and time.monotonic() < deadline:
            time.sleep(0.5)
            state = _cast_get_transport_state(control_url)
        app.logger.info(f"[cast] estado tras mime={mime}: {state}")
        if state in ('STOPPED', 'NO_MEDIA_PRESENT', None):
            last_reason = f'El dispositivo no aceptó el formato {mime} (estado: {state or "no se pudo consultar"})'
            continue
        _eiscp_listo[urlparse(control_url).hostname] = time.monotonic()  # Ticket D-14
        return True, mime, None
    return False, None, last_reason


@app.route('/api/admin/cast/discover', methods=['POST'])
@admin_required
def api_admin_cast_discover():
    """Escanea la LAN (SSDP) y guarda/actualiza los dispositivos con
    AVTransport encontrados. Puede tardar unos segundos — es sincrónico a
    propósito, así el botón "Buscar dispositivos" del panel sabe cuándo
    terminó."""
    locations = _cast_ssdp_discover(timeout=4)
    conn = get_db_connection()
    found = 0
    try:
        for location in locations:
            info = _cast_fetch_device_info(location)
            if not info:
                continue
            name, manufacturer, model_name, av_transport_url, rendering_control_url, ip = info
            conn.execute('''
                INSERT INTO cast_targets (name, control_url, rendering_control_url, ip, manufacturer, model_name, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(control_url) DO UPDATE SET
                    name=excluded.name, ip=excluded.ip, rendering_control_url=excluded.rendering_control_url,
                    manufacturer=excluded.manufacturer, model_name=excluded.model_name
            ''', (name, av_transport_url, rendering_control_url, ip, manufacturer, model_name, _utcnow_iso()))
            found += 1
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok', 'found': found})


@app.route('/api/admin/cast/targets')
@admin_required
def api_admin_cast_targets():
    conn = get_db_connection()
    try:
        rows = conn.execute('SELECT * FROM cast_targets ORDER BY name').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/admin/cast/targets/<int:target_id>', methods=['DELETE'])
@admin_required
def api_admin_cast_target_delete(target_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM cast_targets WHERE id=?', (target_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok'})


# ── Estado en memoria de "qué dispositivo está transmitiendo ahora" ────────
# Solo vive en RAM mientras vive el proceso (no es una columna nueva de la
# base — ver regla 2 de AGENTE.md). Sirve para un único propósito: si el
# server se apaga de forma PROLIJA (systemctl stop, Ctrl+C, redeploy, o el
# propio auto-reload del modo debug de Flask), poder avisarle "Stop" al
# dispositivo ANTES de morir, en vez de dejarlo sonando indefinidamente sin
# que nadie lo controle (ver _cast_stop_active_on_shutdown más abajo).
_cast_active_target = None  # {'id', 'name', 'control_url'} o None


def _cast_stop_active_on_shutdown(*_args):
    """Le manda Stop al dispositivo activo antes de que el proceso muera.
    OJO — esto SOLO puede cubrir un apagado prolijo: un crash real (kill -9,
    segfault, corte de luz) no ejecuta ni una línea de Python, así que
    ningún hook de este lado puede interceptarlo. Para ese caso, el
    respaldo es el heartbeat del propio navegador (ver static/cast.js):
    detecta que el server dejó de responder y corta la transmisión desde
    ahí, aunque no pueda garantizar el Stop en el dispositivo mismo."""
    global _cast_active_target
    if not _cast_active_target:
        return
    target = _cast_active_target
    _cast_active_target = None
    try:
        app.logger.info(f"[cast] apagado del server — mandando Stop a {target['name']} antes de salir")
        _cast_soap_call(
            target['control_url'], 'Stop',
            '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID></u:Stop>', timeout=3)
    except Exception as e:
        app.logger.warning(f"[cast] no se pudo avisar Stop al apagar: {e}")


atexit.register(_cast_stop_active_on_shutdown)
try:
    # signal.signal solo se puede llamar desde el hilo principal — si en algún
    # momento esto corre bajo un servidor WSGI que lo importa desde otro
    # hilo, se degrada solo al atexit.register de arriba (que sigue andando).
    signal.signal(signal.SIGTERM, lambda signum, frame: (_cast_stop_active_on_shutdown(), os._exit(0)))
except (ValueError, OSError):
    pass


@app.route('/api/admin/cast/play', methods=['POST'])
@admin_required
def api_admin_cast_play():
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    track_id = data.get('track_id')
    if not target_id or not track_id:
        return jsonify({'status': 'error', 'message': 'Falta target_id o track_id'}), 400

    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
        track = conn.execute('''
            SELECT t.id, t.title, t.artist, t.file_path, t.genre, t.track_number,
                   al.name AS album_name, al.cover_path
            FROM tracks t LEFT JOIN albums al ON t.album_id = al.id
            WHERE t.id=?''', (track_id,)).fetchone()
        if not target:
            return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado — volvé a buscar'}), 404
        if not track:
            return jsonify({'status': 'error', 'message': 'Pista no encontrada'}), 404

        file_path = clean_db_path(track['file_path'])
        if not os.path.isfile(file_path):
            return jsonify({'status': 'error', 'message': 'El archivo no está en disco'}), 404

        token = cast_token_for_track(track['id'])
        base = request.host_url.rstrip('/')
        media_url = f"{base}/cast-audio/{track['id']}?token={token}"
        cover_url = f"{base}/cast-cover/{track['id']}?token={token}" if track['cover_path'] else None
        file_size = os.path.getsize(file_path)

        ext = os.path.splitext(file_path)[1].lower()
        if ext in ('.dsf', '.dff'):
            mime_candidates = _CAST_DSD_MIME_CANDIDATES
        else:
            mime_candidates = [_CAST_MIME_BY_EXT.get(ext) or mimetypes.guess_type(file_path)[0] or 'application/octet-stream']

        ok, mime_used, reason = _cast_try_send_track(
            target['control_url'], track, media_url, cover_url, file_path, file_size, mime_candidates)

        if ok:
            app.logger.info(f"[cast] '{track['title']}' -> {target['name']} OK (mime={mime_used})")
            conn.execute('UPDATE cast_targets SET last_used_at=? WHERE id=?', (_utcnow_iso(), target_id))
            conn.commit()
            global _cast_active_target
            _cast_active_target = {'id': target['id'], 'name': target['name'], 'control_url': target['control_url']}
            return jsonify({'status': 'ok', 'device': target['name']})

        app.logger.warning(f"[cast] '{track['title']}' -> {target['name']} FALLÓ: {reason}")
        return jsonify({'status': 'error', 'message': reason}), 502
    finally:
        conn.close()


@app.route('/api/admin/cast/transport', methods=['POST'])
@admin_required
def api_admin_cast_transport():
    """Play/Pause/Stop sobre lo que el renderer YA tiene cargado — no hace
    falta reenviar la pista, AVTransport lo maneja como cualquier control
    remoto. Usado para que pausar/reanudar en el reproductor local también
    pause/reanude en el dispositivo transmitiendo (ver static/cast.js)."""
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    action = data.get('action')
    if action not in ('Play', 'Pause', 'Stop'):
        return jsonify({'status': 'error', 'message': 'Acción inválida'}), 400
    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
    finally:
        conn.close()
    if not target:
        return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
    body = (f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f'<InstanceID>0</InstanceID>{"<Speed>1</Speed>" if action == "Play" else ""}</u:{action}>')
    status, body_resp = _cast_soap_call(target['control_url'], action, body)
    app.logger.info(f"[cast] {action} -> {target['name']}: HTTP {status}")
    if status == 200:
        global _cast_active_target
        if action == 'Stop':
            if _cast_active_target and _cast_active_target.get('id') == target['id']:
                _cast_active_target = None
        else:  # Play / Pause — sigue "activo" para el hook de apagado prolijo
            _cast_active_target = {'id': target['id'], 'name': target['name'], 'control_url': target['control_url']}
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': f'HTTP {status}'}), 502


@app.route('/api/admin/cast/seek', methods=['POST'])
@admin_required
def api_admin_cast_seek():
    """Mueve la posición de reproducción en el renderer — usado cuando se
    arrastra la barra de progreso del reproductor local."""
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    seconds = data.get('seconds')
    if target_id is None or seconds is None:
        return jsonify({'status': 'error', 'message': 'Faltan parámetros'}), 400
    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
    finally:
        conn.close()
    if not target:
        return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
    seconds = max(0, int(seconds))
    target_time = f'{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}'
    body = ('<u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID><Unit>REL_TIME</Unit>'
            f'<Target>{target_time}</Target></u:Seek>')
    status, body_resp = _cast_soap_call(target['control_url'], 'Seek', body)
    app.logger.info(f"[cast] Seek {seconds}s -> {target['name']}: HTTP {status}")
    if status == 200:
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': f'HTTP {status}'}), 502


@app.route('/api/admin/cast/volume', methods=['GET', 'POST'])
@admin_required
def api_admin_cast_volume():
    """Volumen del DISPOSITIVO — servicio UPnP separado (RenderingControl,
    no AVTransport). Si el renderer no lo expone, se degrada solo (ver
    _cast_fetch_device_info) en vez de romper el resto del cast.

    GET  ?target_id=N  -> consulta el volumen ACTUAL del dispositivo
                          (GetVolume) — usado al conectar, para reflejarlo
                          en el slider local en vez de imponerle el valor
                          que tuviera el slider (ver static/cast.js).
    POST {target_id, volume} -> fija el volumen (SetVolume, como antes)."""
    if request.method == 'GET':
        target_id = request.args.get('target_id', type=int)
        if target_id is None:
            return jsonify({'status': 'error', 'message': 'Falta target_id'}), 400
        conn = get_db_connection()
        try:
            target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
        finally:
            conn.close()
        if not target:
            return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
        if not target['rendering_control_url']:
            return jsonify({'status': 'error', 'message': 'Este dispositivo no expone control de volumen por UPnP'}), 501
        volume = _cast_get_volume(target['rendering_control_url'])
        if volume is None:
            return jsonify({'status': 'error', 'message': 'El dispositivo no respondió GetVolume'}), 502
        return jsonify({'status': 'ok', 'volume': volume})

    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    volume = data.get('volume')  # 0-100
    if target_id is None or volume is None:
        return jsonify({'status': 'error', 'message': 'Faltan parámetros'}), 400
    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
    finally:
        conn.close()
    if not target:
        return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
    if not target['rendering_control_url']:
        return jsonify({'status': 'error', 'message': 'Este dispositivo no expone control de volumen por UPnP'}), 501
    status, body_resp = _cast_set_volume(target['rendering_control_url'], max(0, min(100, int(volume))))
    app.logger.info(f"[cast] SetVolume {volume} -> {target['name']}: HTTP {status}")
    if status == 200:
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': f'HTTP {status}'}), 502


# ── /api/v1/cast/* — espejo nativo de /api/admin/cast/* de arriba (Ticket 30) ──
# Decision explicita de Niko: DLNA/UPnP abierto a CUALQUIER usuario logueado
# desde la app nativa, no solo admin (a diferencia de la version web, que
# sigue admin-only - ver {% if current_user.is_admin %} en base.html, sin
# tocar). Mismo patron ya establecido en este archivo para otros mirrors
# nativos de rutas web con distinto gate de auth (_lyrics_payload/
# api_v1_lyrics, _lookup_artist_website/api_v1_artist_website): TODA la
# logica real (_cast_ssdp_discover/_cast_soap_call/_cast_try_send_track/
# _cast_get_volume/_cast_set_volume/cast_token_for_track) se reusa tal
# cual, cero duplicacion - el unico cambio real por endpoint es el
# decorator, api_login_required (Bearer token O cookie de sesion de
# cuenta real, cualquier usuario aprobado) en vez de admin_required
# (cookie de sesion + is_admin). No hace falta agregar estos nombres a
# _COLLAB_GUEST_BLOCKED_ENDPOINTS: un invitado de sesion colaborativa
# nunca tiene session['user_id'] seteado (ver collab_join(), hace
# session.clear() antes de armar su propia sesion con is_collab_guest),
# asi que api_login_required lo rechaza igual sin necesitar la lista -
# ademas, /api/v1/* esta completamente exento del gate de
# _require_login() que es lo unico que consulta esa lista (linea
# "if request.path.startswith('/api/v1/'): return" mas arriba en este
# archivo). Confirmado leyendo _require_login()/api_login_required()/
# collab_join() reales antes de asumir que esto era seguro.

@app.route('/api/v1/cast/discover', methods=['POST'])
@api_login_required
def api_v1_cast_discover():
    locations = _cast_ssdp_discover(timeout=4)
    conn = get_db_connection()
    found = 0
    try:
        for location in locations:
            info = _cast_fetch_device_info(location)
            if not info:
                continue
            name, manufacturer, model_name, av_transport_url, rendering_control_url, ip = info
            conn.execute('''
                INSERT INTO cast_targets (name, control_url, rendering_control_url, ip, manufacturer, model_name, discovered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(control_url) DO UPDATE SET
                    name=excluded.name, ip=excluded.ip, rendering_control_url=excluded.rendering_control_url,
                    manufacturer=excluded.manufacturer, model_name=excluded.model_name
            ''', (name, av_transport_url, rendering_control_url, ip, manufacturer, model_name, _utcnow_iso()))
            found += 1
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok', 'found': found})


@app.route('/api/v1/cast/targets')
@api_login_required
def api_v1_cast_targets():
    conn = get_db_connection()
    try:
        rows = conn.execute('SELECT * FROM cast_targets ORDER BY name').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/v1/cast/targets/<int:target_id>', methods=['DELETE'])
@api_login_required
def api_v1_cast_target_delete(target_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM cast_targets WHERE id=?', (target_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'ok'})


# ── Ticket D-03 (reportado por Niko): DSD -> PCM para cast ─────────────────
# Renderers sin soporte DSD (el Onkyo TX-8050) rechazan los cuatro mime de
# _CAST_DSD_MIME_CANDIDATES y, peor, al recibir Play sin una URI nueva
# valida REPITEN la ultima pista que tenian cargada. La conversion a PCM
# de iOS/Android ocurre en el telefono, asi que no sirve cuando el audio
# va directo del server al renderer: hay que convertir aca.
#
# Mismo criterio ya probado en /stream-dsd y /api/v1/stream-pcm: transcode
# COMPLETO a disco antes de servir (nunca en vivo), mismo directorio de
# cache (_DSD_CACHE_DIR, misma limpieza _dsd_cache_cleanup), servido con
# _serve_audio (Content-Length + Range). Diferencia: 88.2kHz/24-bit en vez
# de 176.4kHz/32-bit, porque un renderer DLNA de esa epoca casi seguro no
# acepta FLAC por encima de 96kHz/24-bit. 88.2k es divisor exacto de
# DSD64 (2.8224 MHz / 32). Archivo de cache distinto (sufijo .cast.flac)
# para no pisar el de la PWA/iOS, que tiene otros parametros.
# Ticket D-04: perfiles en cascada, del mejor al mas compatible. 'cd' es el
# mismo formato que el TX-8050 ya reproduce sin problema (FLAC 16/44.1).
_CAST_PCM_PERFILES = {
    # Ticket D-05: 'max' = misma resolucion que ya usan /stream-dsd y
    # /api/v1/stream-pcm (176.4k), pero a 24-bit (techo real de un DAC DLNA).
    'max': {'rate': '176400', 'fmt': ['-sample_fmt', 's32', '-bits_per_raw_sample', '24']},
    'hi': {'rate': '88200', 'fmt': ['-sample_fmt', 's32', '-bits_per_raw_sample', '24']},
    # Ticket D-09: familia 48k (192k/384k -> 96k, division exacta) para PCM hi-res.
    'hi48': {'rate': '96000', 'fmt': ['-sample_fmt', 's32', '-bits_per_raw_sample', '24']},
    'cd': {'rate': '44100', 'fmt': ['-sample_fmt', 's16']},
}
# Ticket D-06: 'max' FUERA de la cascada. Probado en el TX-8050: rechaza
# audio/flac a 176.4k (STOPPED) pero con audio/x-flac responde PLAYING y NO
# suena -> falso positivo imposible de detectar por UPnP (el renderer dice
# PLAYING igual). 88.2k/24 ('hi') si suena. 'max' queda definido por si un
# renderer futuro lo soporta, pero solo se usaria agregandolo aca a mano.
_CAST_PCM_ORDEN = ['hi', 'cd']
# Ticket D-09: PCM por encima de esto (24/192, 24/176.4, DXD) se baja al perfil
# de su familia si el renderer lo rechaza. 96k/24 confirmado en el TX-8050.
_CAST_PCM_MAX_RATE = 96000


def _cast_perfil_pcm_para(sample_rate):
    """Perfil de la MISMA familia de frecuencia (division exacta, sin
    remuestreo fraccional): 44.1k/88.2k/176.4k -> 'hi'; 48k/96k/192k -> 'hi48'."""
    try:
        return 'hi' if int(sample_rate) % 44100 == 0 else 'hi48'
    except (TypeError, ValueError):
        return 'hi'
_CAST_PCM_MIME_CANDIDATES = ['audio/flac', 'audio/x-flac']
# Ultimo perfil que SI sono por renderer (RAM, igual que _cast_targets_sin_dsd):
# se prueba primero y no se repite el que ya fallo.
_cast_pcm_perfil_ok = {}

# Renderers que ya rechazaron DSD nativo en esta vida del proceso: se les
# manda PCM directo y no se pierden ~6s probando los cuatro mime DSD cada
# vez. Solo RAM (regla 2 de AGENTE.md: nada de columnas nuevas); se
# reaprende solo tras un reinicio.
_cast_targets_sin_dsd = set()
# Ticket D-09: renderers que rechazaron PCM > _CAST_PCM_MAX_RATE (misma logica, RAM).
_cast_targets_max96 = set()


def _dsd_cast_cache_path(absolute_path, perfil='hi'):
    """Igual que _dsd_cache_path pero con sufijo propio por perfil de cast."""
    return _dsd_cache_path(absolute_path)[:-len('.flac')] + f'.cast-{perfil}.flac'


def _dsd_transcode_para_cast(absolute_path, perfil='hi'):
    """Devuelve la ruta del FLAC del perfil pedido (_CAST_PCM_PERFILES) en
    cache (lo crea si no existe), o None si ffmpeg fallo."""
    _dsd_cache_cleanup()
    cache_path = _dsd_cast_cache_path(absolute_path, perfil)
    if os.path.isfile(cache_path):
        return cache_path
    tmp_path = f'{cache_path}.{os.getpid()}.tmp'
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
        '-i', absolute_path,
        '-vn',
        '-ar', _CAST_PCM_PERFILES[perfil]['rate'],
        *_CAST_PCM_PERFILES[perfil]['fmt'],
        '-c:a', 'flac',
        '-compression_level', '0',
        '-f', 'flac',
        tmp_path,
    ]
    # Ticket D-09: remuestreo con SoX (mejor calidad) si este ffmpeg lo trae;
    # si no, el remuestreador por defecto de siempre.
    idx = cmd.index('-ar')
    cmd_soxr = cmd[:idx] + ['-af', 'aresample=resampler=soxr:precision=28'] + cmd[idx:]
    try:
        result = subprocess.run(cmd_soxr, capture_output=True, timeout=300)
        if result.returncode != 0 and b'soxr' in result.stderr.lower():
            result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not os.path.isfile(tmp_path):
            app.logger.error(f"[cast dsd->pcm] ffmpeg fallo para {absolute_path}: "
                             f"{result.stderr.decode(errors='replace')[:500]}")
            return None
        os.replace(tmp_path, cache_path)
        return cache_path
    except subprocess.TimeoutExpired:
        app.logger.error(f"[cast dsd->pcm] ffmpeg timeout para {absolute_path}")
        return None
    finally:
        if os.path.isfile(tmp_path):
            try: os.remove(tmp_path)
            except OSError: pass


def _cast_stop(control_url):
    """Stop best-effort. Se usa cuando un envio falla del todo, para que el
    renderer no se quede repitiendo la pista anterior como si fuera la nueva."""
    _cast_soap_call(control_url, 'Stop',
        '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:Stop>', timeout=3)


@app.route('/api/v1/cast/play', methods=['POST'])
@api_login_required
def api_v1_cast_play():
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    track_id = data.get('track_id')
    if not target_id or not track_id:
        return jsonify({'status': 'error', 'message': 'Falta target_id o track_id'}), 400
    # Ticket D-18: otro cliente (Desktop, iPhone) toma el equipo -> la cola
    # del servidor deja de avanzar para no pisarle lo que mando.
    _cast_cola_soltar(int(target_id), 'cast/play directo')
    _cast_duenos[int(target_id)] = _api_dispositivo()   # C-07j
    payload, codigo = _cast_reproducir(int(target_id), int(track_id),
                                       request.host_url.rstrip('/'), g.api_user['id'])
    return jsonify(payload), codigo


_cast_envio_locks = {}            # Ticket D-20: target_id -> Lock
_cast_envio_locks_guard = threading.Lock()
_cast_envio_gen = {}              # Ticket D-22: target_id -> n° del ultimo pedido
_cast_ultimo_envio = {}           # Ticket D-22: target_id -> monotonic de la ultima carga
_CAST_ENVIO_MIN_S = 5.0           # Ticket D-22: separacion minima entre cargas al mismo equipo


def _cast_reproducir(target_id, track_id, base, user_id):
    """Ticket D-18: cuerpo de /api/v1/cast/play sin depender de la request,
    para que la cola del servidor pueda avanzar sola. Devuelve (dict, http).
    Ticket D-20: un solo envio a la vez por equipo — antes un toque del
    usuario y un avance automatico podian mandarle dos pistas al mismo tiempo
    (Stop/SetAVTransportURI/Play cruzados)."""
    # Ticket D-22 (log real de Niko, 10/10): en las dos caidas del TX-8050 le
    # llego Stop + pista nueva mientras aun cargaba la anterior (18:51) o a la
    # decima de segundo de empezar a sonar (20:09); ahi su servicio UPnP dejo
    # de responder (en el 2do caso rechazo la conexion en 2 ms). Por eso:
    # 1) si mientras se espera llega un pedido mas nuevo, este se descarta
    #    (no se encadenan cargas viejas); 2) entre una carga y la siguiente al
    #    mismo equipo pasan al menos _CAST_ENVIO_MIN_S segundos.
    with _cast_envio_locks_guard:
        lock = _cast_envio_locks.setdefault(target_id, threading.Lock())
        gen = _cast_envio_gen.get(target_id, 0) + 1
        _cast_envio_gen[target_id] = gen
    with lock:
        if _cast_envio_gen.get(target_id) != gen:
            app.logger.info(f"[cast] pedido de la pista {track_id} descartado: llegó otro más nuevo")
            return {'status': 'error', 'code': 'superseded',
                    'message': 'Se reemplazó por un pedido más nuevo'}, 409
        espera = _cast_ultimo_envio.get(target_id, 0) + _CAST_ENVIO_MIN_S - time.monotonic()
        if espera > 0:
            app.logger.info(f"[cast] esperando {espera:.1f}s antes de cargar otra pista (el equipo recién cargó una)")
            time.sleep(espera)
            if _cast_envio_gen.get(target_id) != gen:
                app.logger.info(f"[cast] pedido de la pista {track_id} descartado: llegó otro más nuevo")
                return {'status': 'error', 'code': 'superseded',
                        'message': 'Se reemplazó por un pedido más nuevo'}, 409
        try:
            return _cast_reproducir_sin_lock(target_id, track_id, base, user_id)
        finally:
            _cast_ultimo_envio[target_id] = time.monotonic()


def _cast_reproducir_sin_lock(target_id, track_id, base, user_id):
    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
        track = conn.execute('''
            SELECT t.id, t.title, t.artist, t.file_path, t.genre, t.track_number,
                   t.sample_rate_real, al.name AS album_name, al.cover_path
            FROM tracks t LEFT JOIN albums al ON t.album_id = al.id
            WHERE t.id=?''', (track_id,)).fetchone()
        if not target:
            return {'status': 'error', 'message': 'Dispositivo no encontrado — volvé a buscar'}, 404
        if not track:
            return {'status': 'error', 'message': 'Pista no encontrada'}, 404

        # Ticket D-09: 'code' para que el cliente distinga "el ARCHIVO no sirve"
        # (saltar a la siguiente) de "el receptor fallo" (mostrar error).
        file_path = clean_db_path(track['file_path'])
        if not os.path.isfile(file_path):
            return {'status': 'error', 'code': 'file_missing', 'message': 'El archivo no está en disco'}, 404
        if os.path.getsize(file_path) == 0:
            app.logger.warning(f"[cast v1] '{track['title']}' archivo vacío (0 bytes): {file_path}")
            return {'status': 'error', 'code': 'empty_file', 'message': 'El archivo está vacío (0 bytes)'}, 422

        token = cast_token_for_track(track['id'])
        media_url = f"{base}/cast-audio/{track['id']}?token={token}"
        cover_url = f"{base}/cast-cover/{track['id']}?token={token}" if track['cover_path'] else None
        file_size = os.path.getsize(file_path)

        _cast_pausas.pop(target['id'], None)  # Ticket D-05: pista nueva anula pausa emulada
        ext = os.path.splitext(file_path)[1].lower()
        es_dsd = ext in ('.dsf', '.dff')
        # Ticket D-09: PCM por encima de 96k (24/192...) - si el renderer ya
        # demostro que no lo acepta, directo al perfil de su familia.
        rate = track['sample_rate_real'] or 0
        es_hires = (not es_dsd) and rate > _CAST_PCM_MAX_RATE
        transcodificado = False
        if es_dsd and target['id'] in _cast_targets_sin_dsd:
            ok, mime_used, reason = False, None, 'renderer sin DSD (ya conocido)'
        elif es_hires and target['id'] in _cast_targets_max96:
            ok, mime_used, reason = False, None, f'renderer sin PCM > {_CAST_PCM_MAX_RATE} Hz (ya conocido)'
        else:
            if es_dsd:
                mime_candidates = _CAST_DSD_MIME_CANDIDATES
            else:
                mime_candidates = [_CAST_MIME_BY_EXT.get(ext) or mimetypes.guess_type(file_path)[0] or 'application/octet-stream']
            ok, mime_used, reason = _cast_try_send_track(
                target['control_url'], track, media_url, cover_url, file_path, file_size, mime_candidates)

        # Ticket D-03/D-04: DSD rechazado -> FLAC en cascada de perfiles (hi 88.2k/24,
        # luego cd 44.1k/16), URL por path sin '&'. Primero el ultimo perfil que
        # ya funciono en este renderer.
        perfil_usado = None
        if not ok and (es_dsd or es_hires):
            if es_dsd:
                previo = _cast_pcm_perfil_ok.get(target['id'])
                if previo not in _CAST_PCM_ORDEN:
                    previo = None
                orden = ([previo] if previo else []) + [x for x in _CAST_PCM_ORDEN if x != previo]
            else:
                orden = [_cast_perfil_pcm_para(rate), 'cd']
            for perfil in orden:
                pcm_path = _dsd_transcode_para_cast(file_path, perfil)
                if not pcm_path:
                    reason = 'No se pudo convertir el archivo a PCM'
                    break
                ok, mime_used, reason = _cast_try_send_track(
                    target['control_url'], track,
                    f"{base}/cast-audio/{track['id']}/pcm/{perfil}?token={token}", cover_url,
                    pcm_path, os.path.getsize(pcm_path), _CAST_PCM_MIME_CANDIDATES)
                app.logger.info(f"[cast v1] PCM perfil={perfil} -> {target['name']}: {'OK' if ok else reason}")
                if ok:
                    perfil_usado = perfil
                    break
            transcodificado = ok
            # Solo se recuerda si el PCM SI sonó: prueba que el renderer estaba
            # alcanzable y rechazó el formato (un corte de red no lo marca).
            if ok and es_hires:
                if target['id'] not in _cast_targets_max96:
                    _cast_targets_max96.add(target['id'])
                    app.logger.info(f"[cast v1] {target['name']} no acepta PCM > {_CAST_PCM_MAX_RATE} Hz — se recuerda, se convierte ({perfil_usado})")
            elif ok:
                _cast_pcm_perfil_ok[target['id']] = perfil_usado
                if target['id'] not in _cast_targets_sin_dsd:
                    _cast_targets_sin_dsd.add(target['id'])
                    app.logger.info(f"[cast v1] {target['name']} no acepta DSD nativo — se recuerda, se usa PCM ({perfil_usado})")

        if ok:
            app.logger.info(f"[cast v1] '{track['title']}' -> {target['name']} OK (mime={mime_used}, pcm={perfil_usado or False}, user={user_id})")
            conn.execute('UPDATE cast_targets SET last_used_at=? WHERE id=?', (_utcnow_iso(), target_id))
            conn.commit()
            global _cast_active_target
            _cast_active_target = {'id': target['id'], 'name': target['name'], 'control_url': target['control_url']}
            return {'status': 'ok', 'device': target['name'], 'transcoded': transcodificado,
                    'pcm_profile': perfil_usado}, 200

        # Ticket D-03: sin esto el renderer repetia la pista ANTERIOR y el
        # cliente creia que sonaba la nueva.
        _cast_stop(target['control_url'])
        app.logger.warning(f"[cast v1] '{track['title']}' -> {target['name']} FALLÓ: {reason} (se mandó Stop)")
        return {'status': 'error', 'message': reason}, 502
    finally:
        conn.close()


# ── Ticket D-05 (pedido por Niko): pausa emulada para renderers sin Pause ──
# El TX-8050 responde HTTP 500 a Pause (visto en el log real). Para esos
# renderers: al pausar se guarda en RAM la pista + segundo actual y se manda
# Stop; al reanudar (Play) se vuelve a mandar la misma pista con un token
# NUEVO (los de cast duran 10 min, una pausa larga los venceria) y se hace
# Seek al segundo guardado. Solo RAM, mismo criterio que _cast_active_target.
_cast_pausas = {}  # target_id -> {'track_id', 'perfil', 'position_s'}


def _cast_uri_a_pista(uri):
    """'/cast-audio/123' o '/cast-audio/123/pcm/hi' -> (123, 'hi'|None), o (None, None)."""
    m = re.search(r'/cast-audio/(\d+)(?:/pcm/([a-z]+))?', uri or '')
    return (int(m.group(1)), m.group(2)) if m else (None, None)


def _cast_reenviar_pista(target, track_id, perfil, base):
    """Vuelve a cargar una pista en el renderer (misma version: original o PCM
    del perfil indicado). Devuelve (ok, motivo)."""
    conn = get_db_connection()
    try:
        track = conn.execute('''
            SELECT t.id, t.title, t.artist, t.file_path, t.genre, t.track_number,
                   al.name AS album_name, al.cover_path
            FROM tracks t LEFT JOIN albums al ON t.album_id = al.id
            WHERE t.id=?''', (track_id,)).fetchone()
    finally:
        conn.close()
    if not track:
        return False, 'Pista no encontrada'
    file_path = clean_db_path(track['file_path'])
    token = cast_token_for_track(track['id'])
    cover_url = f"{base}/cast-cover/{track['id']}?token={token}" if track['cover_path'] else None
    if perfil:
        ruta = _dsd_transcode_para_cast(file_path, perfil)
        if not ruta:
            return False, 'No se pudo convertir el DSD a PCM'
        media_url = f"{base}/cast-audio/{track['id']}/pcm/{perfil}?token={token}"
        mimes = _CAST_PCM_MIME_CANDIDATES
    else:
        ruta = file_path
        media_url = f"{base}/cast-audio/{track['id']}?token={token}"
        ext = os.path.splitext(file_path)[1].lower()
        mimes = [_CAST_MIME_BY_EXT.get(ext) or mimetypes.guess_type(file_path)[0] or 'application/octet-stream']
    if not os.path.isfile(ruta):
        return False, 'El archivo no está en disco'
    ok, _mime, motivo = _cast_try_send_track(
        target['control_url'], track, media_url, cover_url, ruta, os.path.getsize(ruta), mimes)
    return ok, motivo


def _cast_seek_segundos(control_url, seconds):
    t = f'{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}'
    status, _ = _cast_soap_call(control_url, 'Seek',
        '<u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID><Unit>REL_TIME</Unit>'
        f'<Target>{t}</Target></u:Seek>')
    return status


@app.route('/api/v1/cast/transport', methods=['POST'])
@api_login_required
def api_v1_cast_transport():
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    action = data.get('action')
    if action not in ('Play', 'Pause', 'Stop'):
        return jsonify({'status': 'error', 'message': 'Acción inválida'}), 400
    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
    finally:
        conn.close()
    if not target:
        return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
    global _cast_active_target
    base = request.host_url.rstrip('/')

    # Ticket D-05: reanudar una pausa emulada -> recargar pista + Seek
    if action == 'Play' and target['id'] in _cast_pausas:
        pausa = _cast_pausas.pop(target['id'])
        ok, motivo = _cast_reenviar_pista(target, pausa['track_id'], pausa['perfil'], base)
        if not ok:
            app.logger.warning(f"[cast v1] reanudar -> {target['name']} FALLÓ: {motivo}")
            return jsonify({'status': 'error', 'message': motivo}), 502
        pos = pausa['position_s'] or 0
        seek_ok = pos <= 0 or _cast_seek_segundos(target['control_url'], pos) == 200
        app.logger.info(f"[cast v1] reanudar -> {target['name']} desde {pos}s (seek={'OK' if seek_ok else 'NO'})")
        _cast_active_target = {'id': target['id'], 'name': target['name'], 'control_url': target['control_url']}
        return jsonify({'status': 'ok', 'emulated': True, 'resumed_from': pos if seek_ok else 0})

    if action == 'Stop':
        _cast_pausas.pop(target['id'], None)
        _cast_cola_soltar(target['id'], 'Stop')   # Ticket D-18

    body = (f'<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            f'<InstanceID>0</InstanceID>{"<Speed>1</Speed>" if action == "Play" else ""}</u:{action}>')
    status, body_resp = _cast_soap_call(target['control_url'], action, body)
    app.logger.info(f"[cast v1] {action} -> {target['name']}: HTTP {status}")

    # Ticket D-05: el renderer no soporta Pause -> guardar posicion y Stop
    if action == 'Pause' and status != 200:
        pos = _cast_get_position_info(target['control_url']) or {}
        track_id, perfil = _cast_uri_a_pista(pos.get('track_uri'))
        if track_id:
            stop_status, _ = _cast_soap_call(target['control_url'], 'Stop',
                '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                '<InstanceID>0</InstanceID></u:Stop>')
            if stop_status == 200:
                _cast_pausas[target['id']] = {'track_id': track_id, 'perfil': perfil,
                                              'position_s': pos.get('position_s') or 0}
                app.logger.info(f"[cast v1] Pause emulado -> {target['name']} en {pos.get('position_s')}s")
                return jsonify({'status': 'ok', 'emulated': True})

    if status == 200:
        if action == 'Stop':
            if _cast_active_target and _cast_active_target.get('id') == target['id']:
                _cast_active_target = None
        else:
            _cast_active_target = {'id': target['id'], 'name': target['name'], 'control_url': target['control_url']}
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'El equipo no responde' if status is None else f'HTTP {status}'}), 502


@app.route('/api/v1/cast/seek', methods=['POST'])
@api_login_required
def api_v1_cast_seek():
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    seconds = data.get('seconds')
    if target_id is None or seconds is None:
        return jsonify({'status': 'error', 'message': 'Faltan parámetros'}), 400
    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
    finally:
        conn.close()
    if not target:
        return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
    seconds = max(0, int(seconds))
    target_time = f'{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}'
    body = ('<u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
            '<InstanceID>0</InstanceID><Unit>REL_TIME</Unit>'
            f'<Target>{target_time}</Target></u:Seek>')
    status, body_resp = _cast_soap_call(target['control_url'], 'Seek', body)
    app.logger.info(f"[cast v1] Seek {seconds}s -> {target['name']}: HTTP {status}")
    if status == 200:
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'El equipo no responde' if status is None else f'HTTP {status}'}), 502


@app.route('/api/v1/cast/volume', methods=['GET', 'POST'])
@api_login_required
def api_v1_cast_volume():
    if request.method == 'GET':
        target_id = request.args.get('target_id', type=int)
        if target_id is None:
            return jsonify({'status': 'error', 'message': 'Falta target_id'}), 400
        conn = get_db_connection()
        try:
            target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
        finally:
            conn.close()
        if not target:
            return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
        if not target['rendering_control_url']:
            return jsonify({'status': 'error', 'message': 'Este dispositivo no expone control de volumen por UPnP'}), 501
        volume = _cast_get_volume(target['rendering_control_url'])
        if volume is None:
            return jsonify({'status': 'error', 'message': 'El dispositivo no respondió GetVolume'}), 502
        return jsonify({'status': 'ok', 'volume': volume})

    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    volume = data.get('volume')
    if target_id is None or volume is None:
        return jsonify({'status': 'error', 'message': 'Faltan parámetros'}), 400
    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
    finally:
        conn.close()
    if not target:
        return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
    if not target['rendering_control_url']:
        return jsonify({'status': 'error', 'message': 'Este dispositivo no expone control de volumen por UPnP'}), 501
    status, body_resp = _cast_set_volume(target['rendering_control_url'], max(0, min(100, int(volume))))
    app.logger.info(f"[cast v1] SetVolume {volume} -> {target['name']}: HTTP {status}")
    if status == 200:
        return jsonify({'status': 'ok'})
    return jsonify({'status': 'error', 'message': 'El equipo no responde' if status is None else f'HTTP {status}'}), 502


# ── Ticket D-01 (pedido por Niko): estado del renderer para el mini control
# del dashboard de Home Assistant (iPad) ──────────────────────────────────
# Hasta ahora /api/v1/cast/* solo podia MANDAR ordenes (play/transport/seek/
# volume) pero nunca PREGUNTAR que esta pasando en el dispositivo: el avance
# de cola y la barra de progreso dependian 100% del reloj local del cliente
# que inicio la transmision. Un segundo cliente (el iPad del dashboard) no
# tenia forma de saber si el Onkyo esta sonando, en que segundo va, ni que
# pista es. Este endpoint solo LEE el renderer (GetTransportInfo +
# GetPositionInfo) - no cambia nada del flujo de cast existente.

def _cast_hms_a_segundos(valor):
    """'H:MM:SS' o 'H:MM:SS.mmm' (formato UPnP) -> int segundos. Devuelve
    None para valores vacios o 'NOT_IMPLEMENTED' que algunos renderers
    mandan cuando no saben la duracion."""
    if not valor:
        return None
    m = re.match(r'^(\d+):(\d{1,2}):(\d{1,2})', valor.strip())
    if not m:
        return None
    h, mi, se = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + se


def _cast_get_position_info(control_url, timeout=4):
    """GetPositionInfo - posicion real, duracion y URI de lo que esta
    sonando en el renderer. Devuelve dict {position_s, duration_s,
    track_uri} o None si el dispositivo no respondio."""
    status, body = _cast_soap_call(
        control_url, 'GetPositionInfo',
        '<u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:GetPositionInfo>', timeout=timeout)
    if status != 200:
        return None

    def _tag(nombre):
        m = re.search(rf'<{nombre}>([^<]*)</{nombre}>', body)
        return m.group(1) if m else None

    # La URI viene XML-escapada dentro del SOAP (&amp; en el querystring)
    uri = (_tag('TrackURI') or '').replace('&amp;', '&')
    return {
        'position_s': _cast_hms_a_segundos(_tag('RelTime')),
        'duration_s': _cast_hms_a_segundos(_tag('TrackDuration')),
        'track_uri':  uri,
    }


@app.route('/api/v1/cast/status')
@api_login_required
def api_v1_cast_status():
    """Estado actual del renderer. target_id opcional: si no viene, usa el
    dispositivo activo en memoria (_cast_active_target); si tampoco hay,
    responde state=null sin error (nada transmitiendo).

    track viene solo si la URI que esta sonando es una /cast-audio/<id> de
    ESTE server - si el renderer reproduce otra cosa (radio, otra app),
    track=null y el cliente no debe asumir que es suya."""
    target_id = request.args.get('target_id', type=int)
    if target_id is None:
        if not _cast_active_target:
            return jsonify({'status': 'ok', 'target': None, 'state': None,
                            'position_s': None, 'duration_s': None, 'track': None})
        target_id = _cast_active_target['id']

    conn = get_db_connection()
    try:
        target = conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
        if not target:
            return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404

        state = _cast_get_transport_state(target['control_url'])
        if state is None:
            return jsonify({'status': 'error', 'message': 'El dispositivo no respondió'}), 502
        pos = _cast_get_position_info(target['control_url']) or {}

        # Ticket D-05: con pausa emulada el renderer dice STOPPED; se informa
        # PAUSED_PLAYBACK con la pista y el segundo guardados.
        pausa = _cast_pausas.get(target['id'])
        if pausa and state in ('STOPPED', 'NO_MEDIA_PRESENT'):
            state = 'PAUSED_PLAYBACK'
            pos = {'position_s': pausa['position_s'], 'duration_s': None,
                   'track_uri': f"/cast-audio/{pausa['track_id']}"}

        track = None
        m = re.search(r'/cast-audio/(\d+)', pos.get('track_uri') or '')
        if m:
            row = conn.execute(
                'SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.dsd_rate, '
                '       t.bit_depth, t.is_mqa, t.codec, t.bitrate, t.compression_mode, t.duration, t.sample_rate_real, '
                '       a.id AS album_id, a.name AS album_name, a.cover_path '
                'FROM tracks t LEFT JOIN albums a ON t.album_id = a.id '
                'WHERE t.id=?', (int(m.group(1)),)).fetchone()
            if row:
                track = dict(row)
                fmt, led = _fmt_format(track)
                track['format_display'] = fmt
                track['format_color']   = led
                track['cover_url']      = cover_url_filter(track.pop('cover_path'))

        # Ticket D-05: que version esta sonando ('max'/'hi'/'cd' = DSD convertido a PCM)
        _tid, pcm_perfil = _cast_uri_a_pista(pos.get('track_uri'))
        if pausa and state == 'PAUSED_PLAYBACK':
            pcm_perfil = pausa['perfil']

        return jsonify({
            'status':     'ok',
            'target':     {'id': target['id'], 'name': target['name']},
            'state':      state,
            'pcm_profile': pcm_perfil,
            'position_s': pos.get('position_s'),
            'duration_s': pos.get('duration_s') or (track or {}).get('duration'),
            'track':      track,
            'queue':      _cast_cola_estado(target['id']),   # Ticket D-18
        })
    finally:
        conn.close()


# ── Ticket D-18 (pedido por Niko): cola de reproduccion EN EL SERVIDOR ──────
# Hasta ahora la cola del mini control (/mini) la avanzaba la pagina del iPad:
# solo funcionaba mientras esa vista estaba visible y con la pantalla
# encendida (D-13 corta los sondeos de vistas ocultas), asi que al cambiar de
# vista en HA o al dormirse el iPad la musica se detenia tras la pista actual.
# Ahora el cliente entrega la lista de ids y el servidor la recorre solo:
# un hilo vigila el renderer cada _CAST_COLA_TICK s y, cuando la pista que
# mando termina (STOPPED cerca del final), manda la siguiente. Con aleatorio
# y repetir (off / all / one). Solo RAM, mismo criterio que _cast_pausas:
# un reinicio la olvida y el cliente la vuelve a mandar al tocar play.
# Si otro cliente usa /api/v1/cast/play en ese equipo, o se manda Stop, la
# cola se "suelta" (deja de avanzar) para no pisar a nadie.
_CAST_COLA_TICK = 2.0
_CAST_COLA_FIN_S = 8        # margen para dar por terminada una pista
_CAST_COLA_GRACIA_S = 8     # Ticket D-20: tras cargar una pista, no decidir nada
_CAST_COLA_PREMATURO_S = 20 # Ticket D-20: fin antes de esto = sospechoso (freno)
_cast_colas = {}            # target_id -> dict (ver _cast_cola_nueva)
_cast_colas_lock = threading.RLock()
_cast_cola_hilo = None


def _cast_cola_orden(n, shuffle, primero=None):
    """Orden de reproduccion (indices sobre ids). Con aleatorio, 'primero'
    va adelante y el resto mezclado."""
    orden = list(range(n))
    if shuffle:
        resto = [i for i in orden if i != primero]
        random.shuffle(resto)
        orden = ([primero] if primero is not None else []) + resto
    return orden


def _cast_cola_publica(cola):
    if not cola:
        return None
    return {
        'ids': cola['ids'],
        'index': cola['orden'][cola['pos']] if cola['ids'] else -1,
        'shuffle': cola['shuffle'],
        'repeat': cola['repeat'],
        'active': cola['activa'],
        'stop_after': cola['solo'],
        'version': cola['version'],
    }


def _cast_cola_estado(target_id):
    with _cast_colas_lock:
        return _cast_cola_publica(_cast_colas.get(target_id))


_cast_duenos = {}   # C-07j: target_id -> dispositivo que lo está usando


def _cast_soltar_equipo(target):
    """C-07j: la app que usaba este equipo se cerró (o se volvió a abrir):
    se detiene y se descarta su cola; la app vuelve a su salida propia."""
    tid = target['id']
    _cast_duenos.pop(tid, None)
    _cast_pausas.pop(tid, None)
    _cast_cola_soltar(tid, 'la app se cerró')
    with _cast_colas_lock:
        _cast_colas.pop(tid, None)
    try:
        status, _ = _cast_soap_call(target['control_url'], 'Stop',
                                    '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                                    '<InstanceID>0</InstanceID></u:Stop>')
        app.logger.info(f"[cast v1] liberar -> {target['name']}: HTTP {status}")
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"[cast v1] liberar -> {target['name']}: {e}")
    global _cast_active_target
    if _cast_active_target and _cast_active_target.get('id') == tid:
        _cast_active_target = None


def _cast_cola_soltar(target_id, motivo):
    with _cast_colas_lock:
        cola = _cast_colas.get(target_id)
        if cola and cola['activa']:
            cola['activa'] = False
            cola['version'] += 1
            app.logger.info(f"[cast cola] equipo {target_id}: deja de avanzar ({motivo})")


def _cast_cola_tocar(target_id, pos, solo=False):
    """Manda la pista de la posicion 'pos' (sobre el orden). Si el archivo
    esta roto (vacio / no existe) salta a la siguiente, con tope. Devuelve
    (payload, http, saltadas)."""
    saltadas = []
    with _cast_colas_lock:
        cola = _cast_colas.get(target_id)
        if not cola or not cola['ids']:
            return {'status': 'error', 'message': 'Cola vacía'}, 409, saltadas
        cola['enviando'] = True
        n = len(cola['ids'])
    try:
        for _ in range(n):
            with _cast_colas_lock:
                track_id = cola['ids'][cola['orden'][pos]]
                cola['pos'] = pos
                cola['actual'] = track_id
                cola['solo'] = solo
                cola['visto'] = False
                cola['ultimo_pos'] = 0
                cola['jugado_s'] = 0.0
                cola['rel_previo'] = None
                cola['fin_previsto'] = None
                cola['activa'] = True
                cola['version'] += 1
            _cast_pausas.pop(target_id, None)
            payload, codigo = _cast_reproducir(target_id, track_id, cola['base'], cola['user_id'])
            cola['inicio'] = time.monotonic()   # Ticket D-20: ventana de gracia
            if codigo == 200 or payload.get('code') not in ('empty_file', 'file_missing'):
                if codigo != 200:
                    with _cast_colas_lock:
                        cola['activa'] = False
                        cola['version'] += 1
                payload['skipped'] = saltadas
                return payload, codigo, saltadas
            saltadas.append({'track_id': track_id, 'reason': payload.get('message')})
            sig = _cast_cola_siguiente_pos(cola, natural=False)
            if sig is None:
                break
            pos = sig
        with _cast_colas_lock:
            cola['activa'] = False
            cola['version'] += 1
        return {'status': 'error', 'code': 'all_skipped', 'message': 'Ninguna pista se pudo reproducir',
                'skipped': saltadas}, 422, saltadas
    finally:
        cola['enviando'] = False
        cola['ultimo_tick'] = time.monotonic()


def _cast_cola_siguiente_pos(cola, natural):
    """Posicion siguiente segun repetir/aleatorio, o None si se termina.
    natural=True: la pista acabo sola (ahi aplica 'repetir una')."""
    if natural and cola['repeat'] == 'one':
        return cola['pos']
    if cola['pos'] + 1 < len(cola['orden']):
        return cola['pos'] + 1
    if cola['repeat'] in ('all', 'one'):
        if cola['shuffle']:
            # nueva vuelta mezclada, sin repetir de entrada la que acaba de sonar
            ultimo = cola['orden'][cola['pos']]
            cola['orden'] = _cast_cola_orden(len(cola['ids']), True)
            if len(cola['orden']) > 1 and cola['orden'][0] == ultimo:
                cola['orden'].append(cola['orden'].pop(0))
        return 0
    return None


def _cast_cola_vigilar():
    """Hilo: detecta fin de pista y avanza. Nunca muere por una excepcion.
    Ticket D-19: duerme menos cuando una pista esta por terminar, para
    mandar la siguiente justo a tiempo (no hasta 2 s tarde)."""
    while True:
        try:
            ahora = time.monotonic()
            with _cast_colas_lock:
                activas = [(tid, c) for tid, c in _cast_colas.items() if c['activa'] and not c['enviando']]
            espera = _CAST_COLA_TICK
            for _tid, c in activas:
                fin = c.get('fin_previsto')
                if fin:
                    # Ticket D-20: nunca menos de 1 s (antes 0.2 s) — el TX-8050
                    # tiene un servidor UPnP muy limitado.
                    espera = min(espera, max(1.0, fin - ahora))
            time.sleep(espera)
            with _cast_colas_lock:
                activas = [(tid, c) for tid, c in _cast_colas.items() if c['activa'] and not c['enviando']]
            for target_id, cola in activas:
                if time.monotonic() < cola.get('pausa_hasta', 0):
                    continue
                _cast_cola_revisar(target_id, cola)
        except Exception as e:  # noqa: BLE001
            app.logger.error(f"[cast cola] error en el vigilante: {e}")
            time.sleep(_CAST_COLA_TICK)


def _cast_cola_avanzar(target_id, cola, motivo):
    # Ticket D-20: freno de seguridad. Un avance "prematuro" = una pista de
    # mas de 30 s dada por terminada a los menos de 20 s de cargarla (posicion
    # o duracion erronea del receptor). Dos en un minuto -> se detiene la cola
    # en vez de seguir encadenando cargas contra el equipo.
    ahora = time.monotonic()
    dur_db = cola.get('duraciones', {}).get(cola['actual']) or 0
    if dur_db > 30 and ahora - cola.get('inicio', 0) < _CAST_COLA_PREMATURO_S:
        recientes = [t for t in cola.get('prematuros', []) if ahora - t < 60] + [ahora]
        cola['prematuros'] = recientes
        app.logger.warning(f"[cast cola] equipo {target_id}: avance prematuro de la pista {cola['actual']} "
                           f"a los {int(ahora - cola.get('inicio', 0))}s de {int(dur_db)}s ({motivo})")
        if len(recientes) >= 2:
            return _cast_cola_soltar(target_id, 'avances prematuros en cadena (freno de seguridad)')
    with _cast_colas_lock:
        cola['fin_previsto'] = None
        if cola['solo']:
            cola['activa'] = False
            cola['version'] += 1
            app.logger.info(f"[cast cola] equipo {target_id}: fin de 'solo esta pista' ({motivo})")
            return
        sig = _cast_cola_siguiente_pos(cola, natural=True)
        if sig is None:
            cola['activa'] = False
            cola['version'] += 1
            app.logger.info(f"[cast cola] equipo {target_id}: fin de la cola ({motivo})")
            return
    app.logger.info(f"[cast cola] equipo {target_id}: pista {cola['actual']} terminada ({motivo}), sigue posicion {sig}")
    payload, codigo, _ = _cast_cola_tocar(target_id, sig)
    if codigo != 200:
        app.logger.warning(f"[cast cola] equipo {target_id}: no se pudo seguir: {payload.get('message')}")


def _cast_cola_revisar(target_id, cola):
    """Ticket D-19: mismo criterio que Orbyte-Desktop (Lote 42bi), que con el
    TX-8050 avanza sin problemas: un reloj propio de la pista, alineado con
    la posicion que informa el receptor; al llegar a la duracion se manda la
    siguiente, SIN esperar a que el receptor pase a STOPPED (D-18 dependia de
    eso y a veces el equipo no lo informaba -> la cola no seguia). STOPPED
    cerca del final tambien cuenta como fin; STOPPED a mitad = lo detuvieron."""
    control_url = cola['control_url']
    estado = _cast_get_transport_state(control_url, timeout=3)
    ahora = time.monotonic()
    dt = ahora - cola.get('ultimo_tick', ahora)
    cola['ultimo_tick'] = ahora
    if estado is None:
        # Ticket D-20: renderer que no responde -> no insistir cada 2 s
        cola['sin_respuesta'] = cola.get('sin_respuesta', 0) + 1
        if cola['sin_respuesta'] >= 2:
            cola['pausa_hasta'] = ahora + 10
            if cola['sin_respuesta'] == 2:
                app.logger.warning(f"[cast cola] equipo {target_id}: no responde, se espacian las consultas")
        return  # renderer no responde (apagado/red): no se decide nada
    cola['sin_respuesta'] = 0
    # Ticket D-20: los primeros segundos tras cargar una pista el receptor
    # puede seguir informando la posicion/duracion/estado de la ANTERIOR.
    # Antes eso podia dar la pista nueva por terminada al instante y encadenar
    # cargas cada 1-2 s hasta colgar el servicio UPnP del TX-8050. En esa
    # ventana no se decide nada ni se usa la posicion del receptor.
    en_gracia = ahora - cola.get('inicio', 0) < _CAST_COLA_GRACIA_S
    pos = _cast_get_position_info(control_url, timeout=3) or {}
    sonando_id, _perfil = _cast_uri_a_pista(pos.get('track_uri'))
    dur_rx = pos.get('duration_s') or 0
    dur_db = cola.get('duraciones', {}).get(cola['actual']) or 0
    dur = dur_rx or dur_db   # igual que Desktop: manda la duracion que informa el receptor
    rel = pos.get('position_s')

    if estado in ('PLAYING', 'TRANSITIONING'):
        if sonando_id and sonando_id != cola['actual']:
            return _cast_cola_soltar(target_id, f'el equipo reproduce otra pista ({sonando_id})')
        if estado != 'PLAYING':
            cola['fin_previsto'] = None
            return
        cola['visto'] = True
        # reloj de la pista: avanza con el tiempo real y se alinea con el
        # receptor cuando este informa una posicion que se mueve
        cola['jugado_s'] += dt
        if en_gracia:
            cola['rel_previo'] = rel
            return
        if rel is not None and rel > 0 and rel != cola.get('rel_previo'):
            cola['jugado_s'] = float(rel)
        cola['rel_previo'] = rel
        cola['ultimo_pos'] = cola['jugado_s']
        if dur:
            resto = dur - cola['jugado_s']
            cola['fin_previsto'] = ahora + resto
            if resto <= 0.5:
                return _cast_cola_avanzar(target_id, cola, f'reloj {int(cola["jugado_s"])}s de {int(dur)}s')
        return

    if estado not in ('STOPPED', 'NO_MEDIA_PRESENT') or not cola['visto']:
        cola['fin_previsto'] = None   # PAUSED_PLAYBACK u otro: esperar sin sondear de mas
        return
    if target_id in _cast_pausas:
        return  # pausa emulada (D-05): el equipo dice STOPPED pero esta en pausa
    if en_gracia:
        return  # STOPPED transitorio mientras el receptor carga la pista nueva
    cola['fin_previsto'] = None
    llevado = cola['jugado_s'] + min(dt, _CAST_COLA_TICK)
    if dur and llevado < dur - _CAST_COLA_FIN_S:
        app.logger.info(f"[cast cola] equipo {target_id}: {estado} en {int(llevado)}s de {int(dur)}s "
                        f"(rx={rel}, dur_rx={dur_rx}, dur_db={dur_db})")
        return _cast_cola_soltar(target_id, f'detenido a mitad ({int(llevado)}s de {int(dur)}s)')
    return _cast_cola_avanzar(target_id, cola, f'{estado} en {int(llevado)}s de {int(dur)}s')


def _cast_cola_arrancar_hilo():
    global _cast_cola_hilo
    with _cast_colas_lock:
        if _cast_cola_hilo is None or not _cast_cola_hilo.is_alive():
            _cast_cola_hilo = threading.Thread(target=_cast_cola_vigilar, name='cast-cola', daemon=True)
            _cast_cola_hilo.start()


def _cast_cola_target(target_id):
    conn = get_db_connection()
    try:
        return conn.execute('SELECT * FROM cast_targets WHERE id=?', (target_id,)).fetchone()
    finally:
        conn.close()


@app.route('/api/v1/cast/queue', methods=['GET', 'POST'])
@api_login_required
def api_v1_cast_queue():
    """GET ?target_id= -> cola actual (o null).
    POST {target_id, track_ids:[...], index, shuffle, repeat, stop_after, play}
      index: posicion en track_ids a reproducir (-1 = al azar si shuffle).
      stop_after: true = "solo esta pista" (suena y se detiene; la cola queda).
      play: false = solo actualizar la lista (agregar/quitar) sin tocar el equipo."""
    if request.method == 'GET':
        target_id = request.args.get('target_id', type=int)
        with _cast_colas_lock:
            return jsonify({'status': 'ok', 'queue': _cast_cola_publica(_cast_colas.get(target_id))})

    data = request.get_json(silent=True) or {}
    try:
        target_id = int(data.get('target_id'))
        ids = [int(x) for x in (data.get('track_ids') or [])]
        index = int(data.get('index', 0))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Parámetros inválidos'}), 400
    if not ids:
        with _cast_colas_lock:
            _cast_colas.pop(target_id, None)
        return jsonify({'status': 'ok', 'queue': None})
    target = _cast_cola_target(target_id)
    if not target:
        return jsonify({'status': 'error', 'message': 'Dispositivo no encontrado'}), 404
    repeat = data.get('repeat') if data.get('repeat') in ('off', 'all', 'one') else 'off'
    shuffle = bool(data.get('shuffle'))
    if index < 0 or index >= len(ids):
        index = random.randrange(len(ids)) if shuffle else 0

    conn = get_db_connection()
    try:
        marcas = ','.join('?' * len(ids))
        duraciones = {r['id']: r['duration'] or 0 for r in conn.execute(
            f'SELECT id, duration FROM tracks WHERE id IN ({marcas})', ids)}
    finally:
        conn.close()

    tocar = data.get('play', True) is not False
    if tocar:
        _cast_duenos[target_id] = _api_dispositivo()   # C-07j
    if tocar:
        # Ticket D-22: diagnostico — que accion de la pantalla pidio reproducir
        app.logger.info(f"[cast cola] pedido de reproducción equipo {target_id} pos {index} "
                        f"solo={bool(data.get('stop_after'))} desde {request.remote_addr} "
                        f"origen={str(data.get('origen') or '-')[:160]}")
    with _cast_colas_lock:
        previa = _cast_colas.get(target_id)
        cola = {
            'ids': ids, 'shuffle': shuffle, 'repeat': repeat,
            'orden': _cast_cola_orden(len(ids), shuffle, index), 'pos': 0,
            'duraciones': duraciones, 'control_url': target['control_url'],
            'base': request.host_url.rstrip('/'), 'user_id': g.api_user['id'],
            'actual': None, 'solo': bool(data.get('stop_after')), 'visto': False,
            'ultimo_pos': 0, 'jugado_s': 0.0, 'activa': False, 'enviando': False,
            'version': (previa['version'] + 1) if previa else 1,
        }
        if not shuffle:
            cola['pos'] = index
        if not tocar and previa:
            # actualizar la lista sin cortar lo que suena: conservar seguimiento
            for k in ('actual', 'visto', 'ultimo_pos', 'jugado_s', 'activa', 'solo', 'ultimo_tick',
                      'rel_previo', 'fin_previsto'):
                if k in previa:
                    cola[k] = previa[k]
        _cast_colas[target_id] = cola
    _cast_cola_arrancar_hilo()
    if not tocar:
        return jsonify({'status': 'ok', 'queue': _cast_cola_publica(cola)})
    payload, codigo, _ = _cast_cola_tocar(target_id, cola['pos'], solo=cola['solo'])
    payload['queue'] = _cast_cola_publica(cola)
    return jsonify(payload), codigo


@app.route('/api/v1/cast/queue/step', methods=['POST'])
@api_login_required
def api_v1_cast_queue_step():
    """{target_id, delta: 1|-1} -> siguiente / anterior segun el orden de la cola."""
    data = request.get_json(silent=True) or {}
    target_id = data.get('target_id')
    try:
        target_id = int(target_id)
        delta = 1 if int(data.get('delta', 1)) >= 0 else -1
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Parámetros inválidos'}), 400
    with _cast_colas_lock:
        cola = _cast_colas.get(target_id)
        if not cola or not cola['ids']:
            return jsonify({'status': 'error', 'code': 'no_queue', 'message': 'No hay cola en el servidor'}), 409
        if delta > 0:
            pos = _cast_cola_siguiente_pos(cola, natural=False)
        else:
            pos = cola['pos'] - 1 if cola['pos'] > 0 else (len(cola['orden']) - 1 if cola['repeat'] != 'off' else None)
        if pos is None:
            return jsonify({'status': 'error', 'code': 'queue_end', 'message': 'No hay más pistas'}), 409
    payload, codigo, _ = _cast_cola_tocar(target_id, pos)
    payload['queue'] = _cast_cola_publica(cola)
    return jsonify(payload), codigo


@app.route('/api/v1/cast/queue/mode', methods=['POST'])
@api_login_required
def api_v1_cast_queue_mode():
    """{target_id, shuffle?, repeat?} -> cambia aleatorio / repetir sin cortar lo que suena."""
    data = request.get_json(silent=True) or {}
    try:
        target_id = int(data.get('target_id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Parámetros inválidos'}), 400
    with _cast_colas_lock:
        cola = _cast_colas.get(target_id)
        if not cola:
            return jsonify({'status': 'ok', 'queue': None})
        if data.get('repeat') in ('off', 'all', 'one'):
            cola['repeat'] = data['repeat']
        if 'shuffle' in data and bool(data['shuffle']) != cola['shuffle']:
            actual_idx = cola['orden'][cola['pos']]
            cola['shuffle'] = bool(data['shuffle'])
            cola['orden'] = _cast_cola_orden(len(cola['ids']), cola['shuffle'], actual_idx)
            cola['pos'] = 0 if cola['shuffle'] else actual_idx
        cola['version'] += 1
        return jsonify({'status': 'ok', 'queue': _cast_cola_publica(cola)})


def parse_range_header(rh, file_size):
    if not rh.startswith('bytes='): return (0, None)
    parts = rh[6:].split('-')
    start = int(parts[0])
    end   = int(parts[1]) if len(parts) == 2 and parts[1] else file_size - 1
    return (start, end)

COVER_NAMES = [
    'Cover.jpg','cover.jpg','folder.jpg','Folder.jpg',
    'Front.jpg','front.jpg','AlbumArt.jpg','albumart.jpg',
    'Artwork.jpg','artwork.jpg','Art.jpg','art.jpg',
    'Cover.png','cover.png','Cover.webp','cover.webp',
    'thumb.jpg','Thumb.jpg','back.jpg','Back.jpg',
]

# Ticket D-13: miniaturas de portada. /cover/<ruta>?w=N (N = 64..1024) sirve
# una copia reducida en cache (JPEG) en vez del archivo original, que puede
# pesar varios MB y medir 3000 px. Sin ?w (todos los clientes de siempre) o
# sin Pillow instalado: el original, igual que antes.
_COVER_THUMB_DIR = os.path.join(tempfile.gettempdir(), 'orbyte_cover_thumbs')


def _send_cover(path):
    w = request.args.get('w', type=int)
    if w:
        w = max(64, min(1024, w))
        try:
            from PIL import Image
            os.makedirs(_COVER_THUMB_DIR, exist_ok=True)
            key = hashlib.sha1(f'{path}:{os.path.getmtime(path)}:{w}'.encode('utf-8')).hexdigest()
            thumb = os.path.join(_COVER_THUMB_DIR, f'{key}.jpg')
            if not os.path.isfile(thumb):
                with Image.open(path) as im:
                    im = im.convert('RGB')
                    im.thumbnail((w, w), Image.LANCZOS)
                    tmp = f'{thumb}.{os.getpid()}.tmp'
                    im.save(tmp, 'JPEG', quality=85, optimize=True)
                    os.replace(tmp, thumb)
            return send_file(thumb, mimetype='image/jpeg', max_age=86400)
        except Exception as e:  # sin Pillow o imagen rara -> original
            app.logger.debug(f"[cover] miniatura no disponible para {path}: {e}")
    mime, _ = mimetypes.guess_type(path)
    return send_file(path, mimetype=mime or 'image/jpeg', max_age=86400)


@app.route('/cover/<path:filepath>')
def cover_file(filepath):
    # Try exact path first
    absolute_path = os.path.join(MUSIC_ROOT, filepath.lstrip('/'))
    if os.path.isfile(absolute_path):
        return _send_cover(absolute_path)

    # Fallback: try common cover filenames in same directory
    directory = os.path.dirname(absolute_path)
    if os.path.isdir(directory):
        for name in COVER_NAMES:
            alt = os.path.join(directory, name)
            if os.path.isfile(alt):
                return _send_cover(alt)
        # Last resort: first image file found in directory
        try:
            for fname in sorted(os.listdir(directory)):
                if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    alt = os.path.join(directory, fname)
                    if os.path.isfile(alt):
                        return _send_cover(alt)
        except PermissionError:
            pass

    # Try parent directory (multi-disc albums store cover one level up)
    parent = os.path.dirname(directory)
    if parent != directory and os.path.isdir(parent):
        for name in COVER_NAMES:
            alt = os.path.join(parent, name)
            if os.path.isfile(alt):
                return _send_cover(alt)

    return "Cover not found", 404

# ── MPD helpers ───────────────────────────────────────────────────────────────

MPD_HOST = os.environ.get('MPD_HOST', 'localhost')
MPD_PORT = int(os.environ.get('MPD_PORT', 6600))
MPD_PASSWORD = os.environ.get('MPD_PASSWORD', None)

def _mpd_connect():
    """Return a connected MPDClient or raise ConnectionRefusedError."""
    client = _MPDClient()
    client.timeout = 5
    client.connect(MPD_HOST, MPD_PORT)
    if MPD_PASSWORD:
        client.password(MPD_PASSWORD)
    return client

def _to_mpd_relative(filepath):
    """Strip MUSIC_ROOT prefix to get the path relative to MPD's music_directory."""
    p = clean_db_path(filepath or '').strip()
    if p.startswith(MUSIC_ROOT):
        return p[len(MUSIC_ROOT):].lstrip('/')
    p = p.lstrip('/')
    for prefix in ('mnt/musica/', 'mnt/musica'):
        if p.startswith(prefix):
            return p[len(prefix):].lstrip('/')
    return p

@app.route('/play-mpd', methods=['POST'])
@app.route('/play-dsd', methods=['POST'])
def play_mpd():
    """
    Send a track to MPD for native hardware DSD playback.
    Uses python-mpd2 (socket protocol) for reliability.
    Falls back to mpc CLI if python-mpd2 is unavailable.
    Returns JSON so the client can decide whether to show/hide the MPD badge.
    """
    data     = request.get_json() or {}
    filepath = clean_db_path(data.get('path', ''))
    if not filepath:
        return jsonify({'status': 'error', 'message': 'No path provided'}), 400

    relative = _to_mpd_relative(filepath)
    app.logger.debug(f"[MPD] relative path: {relative!r}")

    # ── Strategy 1: python-mpd2 via socket ────────────────────────────────────
    if _MPD_AVAILABLE:
        try:
            client = _mpd_connect()
            try:
                client.clear()
                client.add(relative)
                client.play(0)
                status = client.status()
                return jsonify({
                    'status':  'ok',
                    'message': 'Playing via MPD (native)',
                    'path':    relative,
                    'mpd_state': status.get('state'),
                })
            except Exception as e:
                err = str(e)
                app.logger.warning(f"[MPD] add failed for {relative!r}: {err}")
                # Try to update the library then retry once
                try:
                    client.update(relative)
                    client.clear()
                    client.add(relative)
                    client.play(0)
                    return jsonify({'status': 'ok', 'message': 'Playing via MPD (after update)', 'path': relative})
                except Exception as e2:
                    return jsonify({'status': 'error', 'message': str(e2), 'path': relative}), 200
            finally:
                try: client.disconnect()
                except: pass
        except ConnectionRefusedError:
            return jsonify({'status': 'error', 'message': 'MPD not running', 'path': relative}), 200
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e), 'path': relative}), 200

    # ── Strategy 2: mpc CLI fallback ──────────────────────────────────────────
    try:
        subprocess.run(['mpc', 'clear'], capture_output=True, text=True, timeout=5)
        r_add = subprocess.run(['mpc', 'add', relative], capture_output=True, text=True, timeout=5)
        if r_add.returncode != 0 or 'error' in r_add.stderr.lower():
            subprocess.run(['mpc', 'update', '--wait'], capture_output=True, timeout=30)
            subprocess.run(['mpc', 'clear'], capture_output=True, timeout=5)
            subprocess.run(['mpc', 'add', relative], capture_output=True, text=True, check=True, timeout=5)
        subprocess.run(['mpc', 'play'], capture_output=True, check=True, timeout=5)
        return jsonify({'status': 'ok', 'message': 'Playing via MPD (mpc)', 'path': relative})
    except FileNotFoundError:
        return jsonify({'status': 'error', 'message': 'MPD not available', 'path': relative}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({'status': 'error', 'message': getattr(e, 'stderr', str(e)), 'path': relative}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'path': relative}), 200

# ── DSD transcode cache ───────────────────────────────────────────────────────
# The previous version of this endpoint computed an exact Content-Length for
# a synthetic WAV and padded it with silence if ffmpeg came up short — that
# solved "browser doesn't know the length" but NOT "browser can't
# resume/seek", because the response still declared Accept-Ranges: none. To
# iOS's background media pipeline that's still indistinguishable from a
# live/infinite stream: it periodically aborts and reconnects the request
# (visible in server logs as repeated restarts from start=0 every few
# seconds), and each reconnect threw away all progress on the track. The
# player's retry budget ran out long before a multi-minute track could get
# through like that, so playback on a DSD track reached mid-playlist would
# just stop.
#
# Regular (non-DSD) files never had this problem because they're already
# served through _serve_audio() with real Content-Length + byte-range
# support — exactly like every other progressively downloadable audio file
# iOS expects.
#
# Fix: transcode the whole track to a cached .flac file on disk first, then
# serve that file through the same _serve_audio() path used for everything
# else. First playback of a given DSD track pays the one-time transcode cost
# (ffmpeg at -compression_level 0 is much faster than realtime); every
# repeat request — including the reconnect-from-lastPos calls the player
# already makes on a network hiccup, and the watchdog's stall recovery —
# hits the cache and is served instantly.
_DSD_CACHE_DIR      = os.path.join(tempfile.gettempdir(), 'orbyte_dsd_cache')
_DSD_CACHE_MAX_AGE  = 4 * 3600   # seconds — long enough for a listening session

def _dsd_cache_path(absolute_path):
    """Deterministic cache filename for a DSD source: hash of path + mtime."""
    os.makedirs(_DSD_CACHE_DIR, exist_ok=True)
    try:
        mtime = os.path.getmtime(absolute_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f'{absolute_path}:{mtime}'.encode('utf-8')).hexdigest()
    return os.path.join(_DSD_CACHE_DIR, f'{key}.flac')

def _dsd_cache_cleanup():
    """Best-effort removal of stale cache entries. Cheap enough to run per-request
    at personal-library scale; swap for a cron job if the library gets huge."""
    try:
        now = time.time()
        for name in os.listdir(_DSD_CACHE_DIR):
            fp = os.path.join(_DSD_CACHE_DIR, name)
            try:
                if now - os.path.getmtime(fp) > _DSD_CACHE_MAX_AGE:
                    os.remove(fp)
            except OSError:
                pass
    except FileNotFoundError:
        pass

# ── Ticket 42: caché de transcodeo MP3 para la skill de Alexa ──────────────
# Mismo criterio que _DSD_CACHE_DIR de arriba: directorio, hash y limpieza
# propios, completamente separados del caché DSD — cero riesgo de colisión
# o de tocar algo que ya usan iOS/Android.
_ALEXA_CACHE_DIR      = os.path.join(tempfile.gettempdir(), 'orbyte_alexa_cache')
_ALEXA_CACHE_MAX_AGE  = 24 * 3600   # seconds — un día alcanza de sobra para uso personal
# Ticket 42, Lote 13: tope adicional por TAMAÑO total, no solo por edad.
# tempfile.gettempdir() suele ser /tmp, que en muchas distros es tmpfs
# (RAM, no disco) — un caché que crece sin límite ahí puede competir por
# memoria real con el resto del sistema. 300MB son unas ~50 canciones a
# 192kbps, de sobra para uso personal.
_ALEXA_CACHE_MAX_BYTES = 300 * 1024 * 1024

def _alexa_cache_path(absolute_path):
    """Nombre de caché determinístico: hash de ruta + mtime, igual que _dsd_cache_path."""
    os.makedirs(_ALEXA_CACHE_DIR, exist_ok=True)
    try:
        mtime = os.path.getmtime(absolute_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f'{absolute_path}:{mtime}'.encode('utf-8')).hexdigest()
    return os.path.join(_ALEXA_CACHE_DIR, f'{key}.mp3')

def _alexa_cache_cleanup():
    """Limpieza best-effort, igual que _dsd_cache_cleanup — por edad, y
    ahora también por tamaño total (Ticket 42, Lote 13): si el caché entero
    supera _ALEXA_CACHE_MAX_BYTES, borra primero los archivos más viejos
    hasta bajar del límite, sin esperar a que cumplan las 24hs."""
    try:
        now = time.time()
        entries = []
        for name in os.listdir(_ALEXA_CACHE_DIR):
            fp = os.path.join(_ALEXA_CACHE_DIR, name)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if now - st.st_mtime > _ALEXA_CACHE_MAX_AGE:
                try:
                    os.remove(fp)
                except OSError:
                    pass
                continue
            entries.append((st.st_mtime, st.st_size, fp))

        total = sum(size for _, size, _ in entries)
        if total > _ALEXA_CACHE_MAX_BYTES:
            entries.sort(key=lambda e: e[0])  # más viejos primero
            for _, size, fp in entries:
                if total <= _ALEXA_CACHE_MAX_BYTES:
                    break
                try:
                    os.remove(fp)
                    total -= size
                except OSError:
                    pass
    except FileNotFoundError:
        pass

@app.route('/stream-dsd/<path:filepath>')
def stream_dsd(filepath):
    """
    Transcode a DSD file (DSF/DFF) to FLAC and serve it exactly like a normal
    audio file (Content-Length + Accept-Ranges: bytes via _serve_audio), so
    iOS Safari can buffer/seek/resume it in the background the same way it
    already does for every non-DSD track. See _DSD_CACHE_DIR comment above.
    """
    absolute_path = os.path.join(MUSIC_ROOT, filepath.lstrip('/'))
    if not os.path.isfile(absolute_path):
        app.logger.warning(f"stream-dsd 404: {absolute_path}")
        return "File not found", 404

    _dsd_cache_cleanup()
    cache_path = _dsd_cache_path(absolute_path)
    cache_hit  = os.path.isfile(cache_path)

    if not cache_hit:
        tmp_path = f'{cache_path}.{os.getpid()}.tmp'
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', absolute_path,
            '-vn',                        # drop embedded cover art (DSF stores album art)
            '-ar', '176400',              # 176.4 kHz — universal browser FLAC support
            '-sample_fmt', 's32',         # 32-bit integer, full DSD dynamic range
            '-c:a', 'flac',
            '-compression_level', '0',    # fastest encode
            '-f', 'flac',                 # explicit — tmp_path ends in .tmp, not .flac,
                                           # so ffmpeg can't infer the muxer from the extension
            tmp_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0 or not os.path.isfile(tmp_path):
                app.logger.error(
                    f"[stream-dsd] ffmpeg failed for {absolute_path}: "
                    f"{result.stderr.decode(errors='replace')[:500]}"
                )
                return "Transcode failed", 500
            os.replace(tmp_path, cache_path)
        finally:
            if os.path.isfile(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass

    resp = _serve_audio(cache_path)
    resp.headers['Content-Type']  = 'audio/flac'
    resp.headers['X-DSD-Source']  = os.path.basename(absolute_path)
    resp.headers['X-DSD-Rate']    = '176400'
    resp.headers['X-DSD-Cache']   = 'hit' if cache_hit else 'miss'
    return resp


@app.route('/api/favorites/rebuild-cache', methods=['POST'])
def api_rebuild_pop_cache():
    """Rebuild popularity cache — call after bulk metadata updates."""
    try:
        conn = get_db_connection()
        conn.execute('DELETE FROM album_pop_cache')
        conn.execute('''INSERT INTO album_pop_cache (album_id, pop_score)
            SELECT al.id,
              COALESCE((SELECT CASE MAX(
                CASE led_color WHEN "red" THEN 700 WHEN "cyan" THEN 680 WHEN "white" THEN 600
                  WHEN "blue" THEN 500 WHEN "green" THEN 440 WHEN "magenta" THEN 400 ELSE 200 END)
                WHEN 700 THEN 40 WHEN 680 THEN 38 WHEN 600 THEN 30 WHEN 500 THEN 25
                WHEN 440 THEN 22 WHEN 400 THEN 20 ELSE 10 END FROM tracks WHERE album_id=al.id), 10)
              + COALESCE((SELECT AVG(CASE tm.tier WHEN "silver" THEN 30 WHEN "bronze" THEN 20 ELSE 8 END)
                FROM tracks t JOIN track_meta tm ON tm.track_id=t.id WHERE t.album_id=al.id), 8)
              + COALESCE((SELECT 10 + ROUND(10.0 * am.tracks_con_letra / NULLIF(am.tracks_procesados,0))
                FROM album_meta am WHERE am.album_id=al.id), 0)
            FROM albums al''')
        conn.execute('DELETE FROM track_pop_cache')
        conn.execute('''INSERT INTO track_pop_cache (track_id, pop_score)
            SELECT t.id,
              CASE t.led_color WHEN "red" THEN 40 WHEN "cyan" THEN 38 WHEN "white" THEN 30
                WHEN "blue" THEN 25 WHEN "green" THEN 22 WHEN "magenta" THEN 20 ELSE 10 END
              + COALESCE(CASE tm.tier WHEN "silver" THEN 30 WHEN "bronze" THEN 20 ELSE 8 END, 8)
              + COALESCE(CASE WHEN tm.has_synced_lrc=1 THEN 20 WHEN tm.has_lyrics=1 THEN 12 ELSE 0 END, 0)
              + COALESCE(ROUND(tm.mood_confidence * 10), 0)
            FROM tracks t LEFT JOIN track_meta tm ON tm.track_id=t.id''')
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/mini')
def mini_dashboard():
    """Ticket D-02 (pedido por Niko): mini control para el dashboard de Home
    Assistant (iPad, iframe). Pagina suelta - no extiende base.html (sin
    reproductor local ni SPA): todo va al receptor DLNA via /api/v1/cast/*,
    con la cookie de sesion. Protegida por el _require_login de siempre."""
    return render_template('mini.html')


@app.route('/favorites')
def favorites_page():
    """Favorites page — shows all bookmarked tracks (Ticket 10: ahora
    per-usuario, contra user_item_favorites en vez de la vieja tabla
    global `favorites`)."""
    try:
        user_id = session.get('user_id')
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT t.id, t.title, t.artist, t.duration, t.led_color, t.file_path,
                   t.codec, t.bitrate, t.compression_mode,
                   t.is_dsd, t.dsd_rate, t.is_mqa, t.sample_rate_real,
                   al.id as album_id, al.name as album_name, al.cover_path,
                   al.artist_id,
                   CASE WHEN t.is_dsd=1 THEN COALESCE(t.dsd_rate,'DSD')
                        WHEN t.is_mqa=1 THEN 'MQA'
                        ELSE UPPER(COALESCE(t.codec,'FLAC')) END as format_display,
                   uif.added_at
            FROM user_item_favorites uif
            JOIN tracks t ON t.id=uif.item_id
            JOIN albums al ON al.id=t.album_id
            WHERE uif.user_id=? AND uif.item_type='track'
            ORDER BY uif.added_at DESC
        ''', (user_id,)).fetchall()
        conn.close()
        tracks = [dict(r) for r in rows]
        for t in tracks:
            # Con Pérdida: etiqueta "MP3 · 320 kbps" y distintivo titanio.
            if _es_con_perdida(t):
                t['format_display'], t['format_color'] = _fmt_format(t)
        # Build audio_url for each track
        for t in tracks:
            t['audio_url'] = audio_url_filter(t['file_path'])
            t['cover_url'] = cover_url_filter(t['cover_path'] or '')
        return render_template('favorites.html',
                               tracks=tracks,
                               tracks_json=json.dumps(tracks),
                               fav_ids={t['id'] for t in tracks})
    except Exception as e:
        app.logger.error(f'favorites_page error: {e}')
        return render_template('favorites.html', tracks=[], tracks_json='[]', fav_ids=set())


@app.route('/api/debug-dsd')
def api_debug_dsd():
    """Diagnostic endpoint: shows what the server would do for a DSD track."""
    import shutil
    conn = get_db_connection()
    try:
        sample = conn.execute(
            "SELECT id, title, file_path, is_dsd, dsd_rate, sample_rate_real FROM tracks WHERE is_dsd=1 LIMIT 1"
        ).fetchone()
        result = {
            'ffmpeg_available':    bool(shutil.which('ffmpeg')),
            'mpc_available':       bool(shutil.which('mpc')),
            'python_mpd2':         _MPD_AVAILABLE,
            'music_root':          MUSIC_ROOT,
            'music_root_exists':   os.path.isdir(MUSIC_ROOT),
            'sample_dsd_track':    dict(sample) if sample else None,
        }
        if sample:
            fp = sample['file_path']
            result['sample_audio_url']  = audio_url_filter(fp)
            result['sample_file_exists'] = os.path.isfile(fp)
            result['mpd_relative']       = _to_mpd_relative(fp)
        return jsonify(result)
    finally:
        conn.close()



# ══ Ticket C-07 (pedido por Niko): Modo TV ═══════════════════════════════════
# La tele LG del primer piso muestra lo que suena (y en una playlist
# colaborativa: quién lo agregó, lo que viene, avisos y estadísticas) sin
# espejar el teléfono. La música sigue saliendo por el Yamaha desde la app.
# El servidor enciende la tele por red, abre su navegador en /tv/<clave> y,
# al terminar, la devuelve a lo que tenía. Especificación:
# claude/especificacion-c07-modo-tv.md (proyecto).

_tv_lock = threading.Lock()
_tv_progreso = {'estado': 'inactivo', 'mensaje': '', 'at': 0.0}
_tv_sonando = {}   # dispositivo -> lo que suena en su app SIN playlist colaborativa (C-07j)
_TV_EN_CURSO = ('conectando', 'encendiendo', 'emparejando', 'quitando')


def _tv_set_progreso(estado, mensaje):
    _tv_progreso.update({'estado': estado, 'mensaje': mensaje, 'at': time.time()})


def _tv_hay_activa(conn=None):
    propia = conn is None
    if propia:
        conn = get_db_connection()
    try:
        return conn.execute('SELECT 1 FROM tv_pantallas WHERE activa=1 LIMIT 1').fetchone() is not None
    finally:
        if propia:
            conn.close()


def _tv_candidatas(conn):
    """Teles LG conocidas: las ya usadas + las LG que encontró "Reproducir en…"."""
    out, vistas = [], set()
    for r in conn.execute('SELECT ip, nombre, alias, client_key, activa FROM tv_pantallas ORDER BY usada_at DESC'):
        vistas.add(r['ip'])
        out.append({'ip': r['ip'], 'nombre': r['alias'] or r['nombre'] or r['ip'], 'emparejada': bool(r['client_key']),
                    'activa': bool(r['activa'])})
    for r in conn.execute("SELECT name, ip, manufacturer, model_name FROM cast_targets "
                          "WHERE ip IS NOT NULL AND (manufacturer LIKE '%LG%' OR name LIKE '%LG%' OR model_name LIKE '%webOS%') "
                          "ORDER BY name"):
        if r['ip'] and r['ip'] not in vistas:
            vistas.add(r['ip'])
            out.append({'ip': r['ip'], 'nombre': r['name'] or r['ip'], 'emparejada': False, 'activa': False})
    return out


_tv_descubiertas = {}   # ip -> {ip, nombre, modelo, udn, at} de la última búsqueda


def _tv_ip_valida(ip):
    if str(ip).startswith('app:'):
        return True   # Orbyte TV (C-08): su existencia se valida contra la lista
    try:
        return ipaddress.ip_address(str(ip)).is_private
    except ValueError:
        return False


def _tv_buscar(conn):
    """Busca teles LG en la red y arma la lista para elegir (C-07c).
    Incluye las ya conocidas aunque estén apagadas (si se sabe su MAC,
    se pueden encender por red)."""
    encontradas = lg_webos.descubrir(3.0, log=app.logger.info) if lg_webos.disponible() else []
    ahora = time.time()
    for t in encontradas:
        _tv_descubiertas[t['ip']] = dict(t, at=ahora)
        # Si es una tele conocida que cambió de IP (DHCP), se le actualiza
        # (se reconoce por su identificador UPnP o por su MAC).
        mac = lg_webos.mac_desde_arp(t['ip'])
        vieja = None
        if t.get('udn'):
            vieja = conn.execute('SELECT id, ip FROM tv_pantallas WHERE udn=? AND ip<>?', (t['udn'], t['ip'])).fetchone()
        if not vieja and mac:
            vieja = conn.execute('SELECT id, ip FROM tv_pantallas WHERE lower(mac)=lower(?) AND ip<>?', (mac, t['ip'])).fetchone()
        if vieja and not conn.execute('SELECT 1 FROM tv_pantallas WHERE ip=?', (t['ip'],)).fetchone():
            app.logger.info('Modo TV: la tele %s ahora está en %s', vieja['ip'], t['ip'])
            conn.execute('UPDATE tv_pantallas SET ip=? WHERE id=?', (t['ip'], vieja['id']))
        conn.execute('UPDATE tv_pantallas SET nombre=?, udn=COALESCE(?, udn) WHERE ip=?',
                     (t['nombre'], t.get('udn'), t['ip']))
    conn.commit()
    por_ip = {t['ip']: t for t in encontradas}
    for c in _tv_candidatas(conn):   # LG vistas por «Reproducir en…»
        if c['ip'] not in por_ip and not conn.execute('SELECT 1 FROM tv_pantallas WHERE ip=?', (c['ip'],)).fetchone():
            por_ip[c['ip']] = {'ip': c['ip'], 'nombre': c['nombre'], 'modelo': None, 'udn': None}
    conocidas = {r['ip']: dict(r) for r in conn.execute('SELECT * FROM tv_pantallas')}
    ultima = conn.execute('SELECT ip FROM tv_pantallas WHERE usada_at IS NOT NULL ORDER BY usada_at DESC LIMIT 1').fetchone()
    ips = list(dict.fromkeys(list(por_ip) + list(conocidas)))
    encendida = {}
    def _probar(ip):
        if ip.startswith(_TVAPP_PREFIJO):   # Orbyte TV: conectada si consultó hace poco
            encendida[ip] = _tvapp_conectada(ip[len(_TVAPP_PREFIJO):])
            return
        encendida[ip] = ip in {t['ip'] for t in encontradas} or lg_webos.puerto_abierto(ip, timeout=1.0)
    hilos = [threading.Thread(target=_probar, args=(ip,), daemon=True) for ip in ips]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(3)
    out = []
    for ip in ips:
        k, d = conocidas.get(ip) or {}, por_ip.get(ip) or {}
        es_app = ip.startswith(_TVAPP_PREFIJO)
        if not encendida.get(ip) and not k.get('mac') and not es_app:
            # Apagada y sin MAC = no se puede encender ni usar; suele ser una
            # IP vieja de la misma tele (p. ej. la que guardó «Reproducir en…»).
            continue
        nombre_tele = d.get('nombre') or k.get('nombre') or ip
        out.append({
            'ip': ip,
            'nombre': k.get('alias') or nombre_tele,
            'nombre_tele': nombre_tele,
            'alias': k.get('alias'),
            'modelo': d.get('modelo') or k.get('app_modelo'),
            'encendida': bool(encendida.get(ip)),
            'emparejada': bool(k.get('client_key')),
            'puede_encender': bool(k.get('mac')),
            'activa': bool(k.get('activa')),
            'ultima': bool(ultima and ultima['ip'] == ip),
            'tipo': 'app' if es_app else 'lg',
            'modo': k.get('modo') or 'normal',
        })
    out.sort(key=lambda t: (not t['activa'], not t['ultima'], not t['encendida'], t['nombre'].lower()))
    app.logger.info('Modo TV: lista para elegir — %s', '; '.join(
        f"{t['ip']} «{t['nombre']}» {'encendida' if t['encendida'] else 'apagada'}"
        f"{' emparejada' if t['emparejada'] else ''}" for t in out) or 'vacía')
    return out


def _tv_fila(conn, ip, nombre=None):
    row = conn.execute('SELECT * FROM tv_pantallas WHERE ip=?', (ip,)).fetchone()
    if row:
        return dict(row)
    conn.execute('INSERT INTO tv_pantallas (ip, nombre, clave) VALUES (?, ?, ?)',
                 (ip, nombre or ip, secrets.token_urlsafe(18)))
    conn.commit()
    return dict(conn.execute('SELECT * FROM tv_pantallas WHERE ip=?', (ip,)).fetchone())


def _tv_url(clave, modo='normal'):
    return _COLLAB_LAN_URL + f'/tv/{clave}' + ('?modo=oled' if modo == 'oled' else '')


# ══ Ticket C-08 (pedido por Niko): Orbyte TV — app de solo visualización ══════
# para Android TV (NVIDIA Shield, Xiaomi Mi Box S). La app (módulo `tv/` de
# Orbyte-Android) se anuncia con /tvapp/hola y queda escuchando órdenes con
# /tvapp/orden (espera larga). "Mostrar en TV" sobre una de estas cajas le
# manda: permiso (la primera vez, «Permitir» con el control), mostrar
# (ruta de /tv/<clave>, la app arma la URL con la dirección del servidor que
# le funcione: casa o Tailscale) y quitar. En tv_pantallas: tipo='app',
# ip='app:<id>' (identificador para las apps del móvil), client_key='permitido'
# cuando se aceptó «Permitir», app_secret = hash del secreto de la app.

_TVAPP_PREFIJO = 'app:'
_TVAPP_VIGENCIA_S = 50          # sin consultas en este lapso = desconectada
_tvapp = {}                     # app_id -> {'visto', 'ip', 'ordenes', 'cond', 'respuesta', 'mostrando', 'mostrando_at'}
_tvapp_lock = threading.Lock()


def _tvapp_estado(app_id):
    with _tvapp_lock:
        st = _tvapp.get(app_id)
        if st is None:
            st = {'visto': 0.0, 'ip': None, 'ordenes': [], 'cond': threading.Condition(),
                  'respuesta': None, 'mostrando': False, 'mostrando_at': 0.0}
            _tvapp[app_id] = st
        return st


def _tvapp_conectada(app_id):
    st = _tvapp.get(app_id)
    return bool(st and time.time() - st['visto'] <= _TVAPP_VIGENCIA_S)


def _tvapp_enviar(app_id, orden):
    st = _tvapp_estado(app_id)
    with st['cond']:
        # Una orden nueva reemplaza a las del mismo tipo que no se leyeron.
        st['ordenes'] = [o for o in st['ordenes'] if o.get('tipo') != orden.get('tipo')] + [orden]
        st['cond'].notify_all()


def _tvapp_red_permitida():
    """Casa (LAN), el propio servidor o Tailscale (100.64.0.0/10)."""
    try:
        ip = ipaddress.ip_address(request.remote_addr or '')
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10')


def _tvapp_hash(secreto):
    return hashlib.sha256(('orbyte-tv:' + str(secreto)).encode('utf-8')).hexdigest()


def _tvapp_autenticar(conn, datos):
    """(fila, error_response). La app se identifica con {id, secret}."""
    if not _tvapp_red_permitida():
        return None, (jsonify({'status': 'error', 'message': 'Solo desde la red de la casa o Tailscale.'}), 403)
    app_id = str(datos.get('id') or '').strip()
    secreto = str(datos.get('secret') or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', app_id) or len(secreto) < 16:
        return None, (jsonify({'status': 'error', 'message': 'Identificación inválida.'}), 400)
    fila = conn.execute('SELECT * FROM tv_pantallas WHERE ip=?', (_TVAPP_PREFIJO + app_id,)).fetchone()
    if fila and fila['app_secret'] and not hmac.compare_digest(fila['app_secret'], _tvapp_hash(secreto)):
        return None, (jsonify({'status': 'error', 'message': 'Esta caja ya está registrada con otra clave. '
                               'Bórrala desde la app del móvil o reinstala Orbyte TV.'}), 403)
    return (dict(fila) if fila else None), None


@app.route('/api/v1/tvapp/hola', methods=['POST'])
def api_v1_tvapp_hola():
    """La app Orbyte TV se anuncia (al abrir y cada vez que reconecta).
    Body: {id, secret, nombre, modelo, version}."""
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        fila, err = _tvapp_autenticar(conn, datos)
        if err:
            return err
        app_id = str(datos['id']).strip()
        nombre = ' '.join(str(datos.get('nombre') or '').split())[:40] or 'Orbyte TV'
        modelo = ' '.join(str(datos.get('modelo') or '').split())[:60] or None
        if fila is None:
            conn.execute('INSERT INTO tv_pantallas (ip, nombre, clave, tipo, app_secret, app_modelo) '
                         'VALUES (?, ?, ?, ?, ?, ?)',
                         (_TVAPP_PREFIJO + app_id, nombre, secrets.token_urlsafe(18), 'app',
                          _tvapp_hash(datos['secret']), modelo))
            app.logger.info('Orbyte TV: caja nueva «%s» (%s) desde %s', nombre, modelo, request.remote_addr)
        else:
            conn.execute('UPDATE tv_pantallas SET nombre=?, app_modelo=COALESCE(?, app_modelo), tipo=? WHERE id=?',
                         (nombre, modelo, 'app', fila['id']))
        conn.commit()
        st = _tvapp_estado(app_id)
        st['visto'], st['ip'] = time.time(), request.remote_addr
        fila = conn.execute('SELECT client_key FROM tv_pantallas WHERE ip=?', (_TVAPP_PREFIJO + app_id,)).fetchone()
        return jsonify({'status': 'ok', 'emparejada': bool(fila and fila['client_key'])})
    finally:
        conn.close()


@app.route('/api/v1/tvapp/orden', methods=['POST'])
def api_v1_tvapp_orden():
    """Espera larga: la app pregunta "¿hay algo para mí?" y el servidor
    responde al tiro si hay una orden, o a los `espera` s (máx. 25) con
    {orden: null}. Body: {id, secret, espera}."""
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        fila, err = _tvapp_autenticar(conn, datos)
        if err:
            return err
        if fila is None:
            return jsonify({'status': 'error', 'message': 'Primero /tvapp/hola.'}), 404
    finally:
        conn.close()
    app_id = str(datos['id']).strip()
    try:
        espera = max(0.0, min(25.0, float(datos.get('espera', 20))))
    except (TypeError, ValueError):
        espera = 20.0
    st = _tvapp_estado(app_id)
    st['visto'], st['ip'] = time.time(), request.remote_addr
    with st['cond']:
        if not st['ordenes']:
            st['cond'].wait(espera)
        orden = st['ordenes'].pop(0) if st['ordenes'] else None
    st['visto'] = time.time()
    return jsonify({'status': 'ok', 'orden': orden})


@app.route('/api/v1/tvapp/respuesta', methods=['POST'])
def api_v1_tvapp_respuesta():
    """La app responde una orden. Body: {id, secret, tipo: 'permiso', permitido}
    o {id, secret, tipo: 'mostrando', valor: bool} (abrió / se cerró la pantalla)."""
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        fila, err = _tvapp_autenticar(conn, datos)
        if err:
            return err
        if fila is None:
            return jsonify({'status': 'error', 'message': 'Primero /tvapp/hola.'}), 404
        app_id = str(datos['id']).strip()
        st = _tvapp_estado(app_id)
        st['visto'] = time.time()
        tipo = datos.get('tipo')
        if tipo == 'permiso':
            ok = bool(datos.get('permitido'))
            if ok:
                conn.execute("UPDATE tv_pantallas SET client_key='permitido' WHERE id=?", (fila['id'],))
                conn.commit()
            with st['cond']:
                st['respuesta'] = {'tipo': 'permiso', 'permitido': ok}
                st['cond'].notify_all()
        elif tipo == 'mostrando':
            valor = bool(datos.get('valor'))
            with st['cond']:
                st['mostrando'], st['mostrando_at'] = valor, time.time()
                st['cond'].notify_all()
            if not valor and fila['activa']:
                # Se cerró en la caja (botón Atrás): deja de contar como activa.
                conn.execute('UPDATE tv_pantallas SET activa=0 WHERE id=?', (fila['id'],))
                conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


def _tvapp_esperar(st, condicion, segundos):
    limite = time.time() + segundos
    with st['cond']:
        while not condicion():
            resto = limite - time.time()
            if resto <= 0:
                return False
            st['cond'].wait(resto)
    return True


def _tvapp_mostrar(conn, fila, modo):
    """Parte de _tv_mostrar_worker para una caja con Orbyte TV."""
    app_id = fila['ip'][len(_TVAPP_PREFIJO):]
    nombre = fila.get('alias') or fila.get('nombre') or 'la caja'
    if not _tvapp_conectada(app_id):
        raise lg_webos.WebOsError(f'Orbyte TV no está conectada en «{nombre}». Enciende la caja y abre Orbyte TV '
                                  '(o revisa en sus ajustes la dirección del servidor).')
    st = _tvapp_estado(app_id)
    if not fila.get('client_key'):
        st['respuesta'] = None
        _tvapp_enviar(app_id, {'tipo': 'permiso'})
        _tv_set_progreso('emparejando', 'Acepta «Permitir» en la tele con el control (solo esta vez).')
        if not _tvapp_esperar(st, lambda: st['respuesta'] is not None, 70):
            raise lg_webos.WebOsError('No se aceptó «Permitir» en la tele a tiempo. Vuelve a intentarlo.')
        if not st['respuesta'].get('permitido'):
            raise lg_webos.WebOsError('En la tele se eligió no permitir. Vuelve a intentarlo y elige «Permitir».')
    _tv_set_progreso('conectando', f'Abriendo Orbyte en «{nombre}»…')
    desde = time.time()
    ruta = f'/tv/{fila["clave"]}' + ('?modo=oled' if modo == 'oled' else '')
    _tvapp_enviar(app_id, {'tipo': 'mostrar', 'ruta': ruta})
    if not _tvapp_esperar(st, lambda: st['mostrando'] and st['mostrando_at'] >= desde, 25):
        raise lg_webos.WebOsError(f'«{nombre}» recibió la orden pero Orbyte TV no se abrió. En el Shield, dale a '
                                  'Orbyte TV el permiso «Mostrar sobre otras apps» (lo explica la propia app en sus ajustes).')


def _tv_mostrar_worker(ip, nombre, admin_id, modo='normal', dispositivo=None):
    """En un hilo: enciende (si hace falta), empareja, abre /tv/<clave>."""
    with _tv_lock:
        conn = get_db_connection()
        try:
            fila = _tv_fila(conn, ip, nombre)
            # Una tele a la vez (C-07c): si otra estaba mostrando Orbyte, vuelve a lo suyo.
            otras = [dict(r) for r in conn.execute('SELECT * FROM tv_pantallas WHERE activa=1 AND ip<>?', (ip,))]
            if otras:
                conn.execute('UPDATE tv_pantallas SET activa=0 WHERE ip<>?', (ip,))
                conn.commit()
                for f in otras:
                    _tv_devolver(f)
            conn.execute('UPDATE tv_pantallas SET modo=? WHERE id=?', (modo, fila['id']))
            conn.commit()
            if fila.get('tipo') == 'app' or ip.startswith(_TVAPP_PREFIJO):
                _tvapp_mostrar(conn, fila, modo)
                conn.execute('UPDATE tv_pantallas SET activa=0')
                conn.execute('UPDATE tv_pantallas SET activa=1, admin_id=?, dispositivo=?, usada_at=? WHERE id=?',
                             (admin_id, dispositivo, _utcnow_iso(), fila['id']))
                conn.commit()
                _tv_set_progreso('lista', 'Listo: Orbyte está en la tele.')
                return
            if not lg_webos.disponible():
                raise lg_webos.WebOsError('Falta instalar websocket-client en el servidor '
                                          '(venv/bin/pip install websocket-client) y reiniciar Orbyte.')
            _tv_set_progreso('conectando', 'Conectando con la tele…')
            if not lg_webos.puerto_abierto(ip):
                mac = fila.get('mac') or lg_webos.mac_desde_arp(ip)
                if not mac:
                    raise lg_webos.WebOsError('La tele está apagada y todavía no sé cómo encenderla por red: '
                                              'enciéndela con el control esta primera vez (después la enciendo yo).')
                _tv_set_progreso('encendiendo', 'Encendiendo la tele…')
                lg_webos.wake_on_lan(mac, ip)
                limite = time.time() + 35
                while time.time() < limite and not lg_webos.puerto_abierto(ip):
                    time.sleep(1)
                if not lg_webos.puerto_abierto(ip):
                    raise lg_webos.WebOsError('La tele no se encendió por red. Revisa que tenga activado '
                                              '«Encender con móvil / por red» o enciéndela con el control.')
                time.sleep(4)   # webOS tarda un poco en aceptar órdenes después de despertar

            def _aviso():
                _tv_set_progreso('emparejando', 'Acepta «Permitir» en la tele con el control (solo esta vez).')

            with lg_webos.WebOsTv(ip, fila.get('client_key'), on_prompt=_aviso, log=app.logger.info) as tv:
                cambios = {'client_key': tv.client_key}
                mac = tv.mac() or lg_webos.mac_desde_arp(ip)
                if mac:
                    cambios['mac'] = mac
                previa = tv.app_en_primer_plano()
                if previa and previa != lg_webos.BROWSER_APP:
                    cambios['prev_app'] = previa
                _tv_set_progreso('conectando', 'Abriendo Orbyte en la tele…')
                tv.abrir_url(_tv_url(fila['clave'], modo))
                # Pantalla completa: la página la pide con la primera tecla del
                # control, así que se "pulsa OK" cuando ya cargó (C-07g).
                time.sleep(6)
                try:
                    tv.pulsar('ENTER')
                except Exception as e:  # noqa: BLE001 - si falla, la página pide pulsar OK
                    app.logger.info('Modo TV: no se pudo pulsar OK para pantalla completa (%s)', e)
            conn.execute('UPDATE tv_pantallas SET activa=0')
            conn.execute('UPDATE tv_pantallas SET activa=1, admin_id=?, dispositivo=?, usada_at=?, client_key=?, '
                         'mac=COALESCE(?, mac), prev_app=COALESCE(?, prev_app) WHERE id=?',
                         (admin_id, dispositivo, _utcnow_iso(), cambios['client_key'], cambios.get('mac'),
                          cambios.get('prev_app'), fila['id']))
            conn.commit()
            _tv_set_progreso('lista', 'Listo: Orbyte está en la tele.')
        except lg_webos.WebOsError as e:
            app.logger.warning('Modo TV: error al mostrar en %s: %s', ip, e)
            _tv_set_progreso('error', str(e))
        except Exception as e:  # noqa: BLE001
            app.logger.exception('Modo TV: no se pudo mostrar en la tele')
            _tv_set_progreso('error', f'No se pudo abrir en la tele: {e}')
        finally:
            conn.close()


def _tv_devolver(f):
    """La tele `f` (fila de tv_pantallas) vuelve a la app que tenía."""
    if f.get('tipo') == 'app' or str(f.get('ip', '')).startswith(_TVAPP_PREFIJO):
        _tvapp_enviar(f['ip'][len(_TVAPP_PREFIJO):], {'tipo': 'quitar'})   # Orbyte TV se cierra sola
        return
    if not lg_webos.disponible() or not f.get('client_key') or not lg_webos.puerto_abierto(f['ip']):
        return
    _tv_set_progreso('quitando', 'Quitando Orbyte de la tele…')
    try:
        with lg_webos.WebOsTv(f['ip'], f['client_key'], timeout_emparejar=8, log=app.logger.info) as tv:
            if f.get('prev_app'):
                tv.abrir_app(f['prev_app'])
            else:
                tv.cerrar_app(lg_webos.BROWSER_APP)
    except lg_webos.WebOsError as e:
        app.logger.warning('Modo TV: no se pudo devolver la tele a su app (%s)', e)


def _tv_quitar_worker():
    """En un hilo: la tele vuelve a la app que tenía (o se cierra el navegador)."""
    with _tv_lock:
        conn = get_db_connection()
        try:
            filas = [dict(r) for r in conn.execute('SELECT * FROM tv_pantallas WHERE activa=1')]
            conn.execute('UPDATE tv_pantallas SET activa=0, dispositivo=NULL')
            conn.commit()
            _tv_sonando.clear()
            for f in filas:
                _tv_devolver(f)
            _tv_set_progreso('inactivo', 'Se quitó Orbyte de la tele.')
        except Exception:  # noqa: BLE001
            app.logger.exception('Modo TV: error al quitar')
            _tv_set_progreso('inactivo', 'Se quitó Orbyte de la tele.')
        finally:
            conn.close()


def _tv_soltar_ya():
    """Marca la tele como libre AL INSTANTE (la app que vuelve a abrirse ya
    la ve en OFF) y la devuelve a su app en segundo plano."""
    conn = get_db_connection()
    try:
        filas = [dict(r) for r in conn.execute('SELECT * FROM tv_pantallas WHERE activa=1')]
        conn.execute('UPDATE tv_pantallas SET activa=0, dispositivo=NULL')
        conn.commit()
    finally:
        conn.close()
    _tv_sonando.clear()

    def _devolver():
        with _tv_lock:
            for f in filas:
                try:
                    _tv_devolver(f)
                except Exception:  # noqa: BLE001
                    app.logger.exception('Modo TV: error al devolver la tele')
            _tv_set_progreso('inactivo', 'Se quitó Orbyte de la tele.')
    threading.Thread(target=_devolver, daemon=True).start()


def _tv_quitar_si_activa():
    try:
        if _tv_hay_activa():
            threading.Thread(target=_tv_quitar_worker, daemon=True).start()
    except Exception:  # noqa: BLE001 - nunca romper el cierre de la sesión
        app.logger.exception('Modo TV: no se pudo programar el quitar')


@app.route('/api/v1/admin/tv')
@api_admin_required
def api_v1_admin_tv():
    """Estado del Modo TV para la app del admin (botón y avisos de progreso)."""
    conn = get_db_connection()
    try:
        cands = _tv_candidatas(conn)
        activa = next((c for c in cands if c['activa']), None)
        return jsonify({'disponible': lg_webos.disponible(), 'pantallas': cands,
                        'activa': activa is not None, 'ip': activa['ip'] if activa else None,
                        'nombre': activa['nombre'] if activa else None,
                        'es_mia': bool(activa) and conn.execute(
                            'SELECT 1 FROM tv_pantallas WHERE activa=1 AND dispositivo=?', (_api_dispositivo(),)).fetchone() is not None,
                        'progreso': dict(_tv_progreso)})
    finally:
        conn.close()


@app.route('/api/v1/admin/tv/mostrar', methods=['POST'])
@api_admin_required
def api_v1_admin_tv_mostrar():
    """"Mostrar en TV": enciende la tele (si hace falta), la empareja la primera
    vez y abre el Modo TV. Corre en segundo plano; la app consulta
    /api/v1/admin/tv para ir mostrando el progreso. Body opcional: {ip}."""
    data = request.get_json(silent=True) or {}
    if _tv_progreso['estado'] in _TV_EN_CURSO and time.time() - _tv_progreso['at'] < 90:
        return jsonify({'status': 'ok', 'progreso': dict(_tv_progreso)})
    conn = get_db_connection()
    try:
        cands = _tv_candidatas(conn)
    finally:
        conn.close()
    elegida = None
    if data.get('ip'):
        ip = str(data['ip']).strip()
        elegida = next((c for c in cands if c['ip'] == ip), None)
        if not elegida and ip in _tv_descubiertas:
            elegida = {'ip': ip, 'nombre': _tv_descubiertas[ip]['nombre']}
        if not elegida or not _tv_ip_valida(ip):
            return jsonify({'status': 'error', 'message': 'Esa tele no está en la lista. Busca las teles de nuevo.'}), 404
    elif cands:
        elegida = cands[0]   # la última usada, o la primera LG encontrada
    if not elegida:
        return jsonify({'status': 'error', 'message': 'No encontré una tele LG en la red. Con la tele encendida, '
                        'usa «Buscar equipos» en «Reproducir en…» y vuelve a intentarlo.'}), 404
    modo = 'oled' if str(data.get('modo') or '').lower() == 'oled' else 'normal'
    _tv_set_progreso('conectando', 'Conectando con la tele…')
    # C-07j: la tele muestra solo lo que suena en ESTE dispositivo.
    threading.Thread(target=_tv_mostrar_worker,
                     args=(elegida['ip'], elegida.get('nombre'), g.api_user['id'], modo, _api_dispositivo()),
                     daemon=True).start()
    return jsonify({'status': 'ok', 'progreso': dict(_tv_progreso)})


_tv_busqueda = {'buscando': False, 'at': 0.0, 'pantallas': None, 'error': None}


def _tv_buscar_worker():
    conn = get_db_connection()
    try:
        _tv_busqueda['pantallas'] = _tv_buscar(conn)
        _tv_busqueda['error'] = None
    except Exception as e:  # noqa: BLE001
        app.logger.exception('Modo TV: error al buscar teles')
        _tv_busqueda['error'] = f'No se pudo buscar las teles: {e}'
    finally:
        conn.close()
        _tv_busqueda['buscando'] = False
        _tv_busqueda['at'] = time.time()


def _tv_busqueda_json():
    return jsonify({'status': 'ok', 'disponible': lg_webos.disponible(), 'buscando': _tv_busqueda['buscando'],
                    'pantallas': _tv_busqueda['pantallas'] or [], 'error': _tv_busqueda['error']})


@app.route('/api/v1/admin/tv/buscar', methods=['GET', 'POST'])
@api_admin_required
def api_v1_admin_tv_buscar():
    """C-07c/d: teles LG de la red para elegir dónde mostrar.
    POST arranca una búsqueda en segundo plano (~3-6 s) y responde al tiro;
    GET devuelve el estado: {buscando, pantallas, error}. La app consulta
    hasta que buscando=false (las apps cortan a los 5 s por LAN, por eso no
    se espera la búsqueda dentro del request).
    Cada tele: {ip, nombre, nombre_tele, alias, modelo, encendida, emparejada,
    puede_encender, activa, ultima}. Primero la activa y la última usada."""
    if request.method == 'POST' and not (_tv_busqueda['buscando'] and time.time() - _tv_busqueda['at'] < 60):
        _tv_busqueda.update({'buscando': True, 'at': time.time(), 'error': None})
        threading.Thread(target=_tv_buscar_worker, daemon=True).start()
    return _tv_busqueda_json()


@app.route('/api/v1/admin/tv/renombrar', methods=['POST'])
@api_admin_required
def api_v1_admin_tv_renombrar():
    """C-07c: nombre propio para una tele (ej. «Living», «Quincho»).
    Body: {ip, alias}. alias vacío = volver al nombre que trae la tele."""
    data = request.get_json(silent=True) or {}
    ip = str(data.get('ip') or '').strip()
    alias = ' '.join(str(data.get('alias') or '').split())[:40] or None
    conn = get_db_connection()
    try:
        conocida = conn.execute('SELECT 1 FROM tv_pantallas WHERE ip=?', (ip,)).fetchone()
        if not _tv_ip_valida(ip) or not (conocida or ip in _tv_descubiertas
                                          or any(c['ip'] == ip for c in _tv_candidatas(conn))):
            return jsonify({'status': 'error', 'message': 'Esa tele no está en la lista.'}), 404
        nombre = (_tv_descubiertas.get(ip) or {}).get('nombre')
        _tv_fila(conn, ip, nombre)
        conn.execute('UPDATE tv_pantallas SET alias=? WHERE ip=?', (alias, ip))
        conn.commit()
        fila = conn.execute('SELECT nombre, alias FROM tv_pantallas WHERE ip=?', (ip,)).fetchone()
        return jsonify({'status': 'ok', 'ip': ip, 'alias': fila['alias'],
                        'nombre': fila['alias'] or fila['nombre'] or ip})
    finally:
        conn.close()


# ── Actualización de la app Android desde el propio servidor ─────────────────
# tools/publicar_apk.py deja en apk/ el APK más nuevo (orbyte.apk) y su ficha
# (android.json). La app pregunta al abrirse; si hay una versión mayor que la
# suya, la descarga (con su sesión, por LAN o Tailscale) y abre el instalador.
APK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'apk')


def _apk_android_info():
    try:
        with open(os.path.join(APK_DIR, 'android.json'), encoding='utf-8') as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(os.path.join(APK_DIR, 'orbyte.apk')):
        return None
    return info


@app.route('/api/v1/app/android', methods=['GET'])
@api_login_required
def api_v1_app_android():
    info = _apk_android_info()
    if not info:
        return jsonify({'disponible': False})
    return jsonify({
        'disponible': True,
        'version_code': int(info.get('version_code') or 0),
        'version_name': info.get('version_name') or '',
        'notas': info.get('notas') or '',
        'tamano': int(info.get('tamano') or 0),
        'sha256': info.get('sha256') or '',
        'publicado_at': info.get('publicado_at') or '',
    })


@app.route('/api/v1/app/android/apk', methods=['GET'])
@api_login_required
def api_v1_app_android_apk():
    info = _apk_android_info()
    if not info:
        return jsonify({'error': 'sin_apk'}), 404
    return send_file(os.path.join(APK_DIR, 'orbyte.apk'),
                     mimetype='application/vnd.android.package-archive',
                     as_attachment=True, conditional=True,
                     download_name=f"orbyte-{info.get('version_name') or 'android'}.apk")


# ── Actualización de la app iPhone (SideStore) ──────────────────────────────
# tools/publicar_ipa.py deja en apk/ el IPA más nuevo (orbyte.ipa) y su ficha
# (ios.json, con un enlace_token al azar que cambia en cada publicación).
# La app pregunta al abrirse; si hay una versión mayor, abre SideStore con
# sidestore://install?url=<este servidor>/descargas/orbyte.ipa?t=<token>.
# SideStore descarga sin la sesión de Orbyte: por eso el enlace lleva token.
def _ipa_ios_info():
    try:
        with open(os.path.join(APK_DIR, 'ios.json'), encoding='utf-8') as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(os.path.join(APK_DIR, 'orbyte.ipa')) or not info.get('enlace_token'):
        return None
    return info


@app.route('/api/v1/app/ios', methods=['GET'])
@api_login_required
def api_v1_app_ios():
    info = _ipa_ios_info()
    if not info:
        return jsonify({'disponible': False})
    from urllib.parse import quote
    # Con ORBYTE_URL_PUBLICA (la dirección de Tailscale Funnel, en el .env) el
    # IPA se baja por internet: fuera de casa no se puede tener Tailscale y la
    # VPN de SideStore encendidas a la vez (iOS permite una sola VPN).
    publica = os.environ.get('ORBYTE_URL_PUBLICA', '').strip().rstrip('/')
    base = publica or request.host_url.rstrip('/')
    descarga = f"{base}/descargas/orbyte.ipa?t={info['enlace_token']}"
    return jsonify({
        'disponible': True,
        'version': info.get('version') or '',
        'build': int(info.get('build') or 0),
        'notas': info.get('notas') or '',
        'tamano': int(info.get('tamano') or 0),
        'publicado_at': info.get('publicado_at') or '',
        'descarga_url': descarga,
        'instalar_url': 'sidestore://install?url=' + quote(descarga, safe=''),
    })


@app.route('/descargas/orbyte.ipa', methods=['GET'])
def descargas_orbyte_ipa():
    info = _ipa_ios_info()
    token = request.args.get('t', '')
    if not info or not token or not hmac.compare_digest(token, str(info['enlace_token'])):
        return jsonify({'error': 'enlace_invalido'}), 404
    app.logger.info(f"[actualizador ios] descarga del IPA {info.get('version')} ({info.get('build')}) "
                    f"desde {request.remote_addr}")
    return send_file(os.path.join(APK_DIR, 'orbyte.ipa'), mimetype='application/octet-stream',
                     as_attachment=True, conditional=True, download_name='Orbyte.ipa')


# ── Actualización de Orbyte Desktop (Windows instalador / Linux AppImage) ────
# tools/publicar_desktop.py deja en apk/ el paquete más nuevo de cada sistema
# (desktop-<plataforma>.json + el archivo). La app lo baja con su sesión.
_DESKTOP_PLATAFORMAS = ('windows', 'linux')


def _desktop_info(plataforma):
    if plataforma not in _DESKTOP_PLATAFORMAS:
        return None
    try:
        with open(os.path.join(APK_DIR, f'desktop-{plataforma}.json'), encoding='utf-8') as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        return None
    archivo = os.path.join(APK_DIR, os.path.basename(info.get('archivo') or ''))
    if not info.get('archivo') or not os.path.isfile(archivo):
        return None
    info['_ruta'] = archivo
    return info


@app.route('/api/v1/app/desktop', methods=['GET'])
@api_login_required
def api_v1_app_desktop():
    plataforma = (request.args.get('plataforma') or '').strip().lower()
    info = _desktop_info(plataforma)
    if not info:
        return jsonify({'disponible': False})
    return jsonify({
        'disponible': True,
        'plataforma': plataforma,
        'version': info.get('version') or '',
        'numero': info.get('numero') or [],
        'notas': info.get('notas') or '',
        'tamano': int(info.get('tamano') or 0),
        'sha256': info.get('sha256') or '',
        'archivo': info.get('archivo'),
        'publicado_at': info.get('publicado_at') or '',
        'descarga': f'/api/v1/app/desktop/descargar?plataforma={plataforma}',
    })


@app.route('/api/v1/app/desktop/descargar', methods=['GET'])
@api_login_required
def api_v1_app_desktop_descargar():
    plataforma = (request.args.get('plataforma') or '').strip().lower()
    info = _desktop_info(plataforma)
    if not info:
        return jsonify({'error': 'sin_paquete'}), 404
    return send_file(info['_ruta'], mimetype='application/octet-stream', as_attachment=True,
                     conditional=True, download_name=info['archivo'])


@app.route('/api/v1/dispositivo/liberar', methods=['POST'])
@api_login_required
def api_v1_dispositivo_liberar():
    """C-07j (pedido por Niko): al cerrar la app y al volver a abrirla todo
    parte en OFF. Suelta lo que ESTE dispositivo dejó tomado: la tele del
    Modo TV (si la mandó él) y los equipos de «Reproducir en…» que estaba
    usando (se detienen y se descarta su cola). No toca lo de otros
    dispositivos de la misma cuenta."""
    disp = _api_dispositivo()
    tv = False
    conn = get_db_connection()
    try:
        # (una tele encendida antes de esta versión no tiene dispositivo: se
        # suelta si la mandó esta misma cuenta)
        tv = conn.execute('SELECT 1 FROM tv_pantallas WHERE activa=1 AND (dispositivo=? OR '
                          '(dispositivo IS NULL AND admin_id=?))', (disp, g.api_user['id'])).fetchone() is not None
        targets = [dict(r) for r in conn.execute('SELECT * FROM cast_targets').fetchall()
                   if _cast_duenos.get(r['id']) == disp]
    finally:
        conn.close()
    if tv:
        _tv_soltar_ya()
    for t in targets:
        _cast_soltar_equipo(t)
    return jsonify({'status': 'ok', 'tv': tv, 'equipos': len(targets)})


@app.route('/api/v1/admin/tv/quitar', methods=['POST'])
@api_admin_required
def api_v1_admin_tv_quitar():
    threading.Thread(target=_tv_quitar_worker, daemon=True).start()
    return jsonify({'status': 'ok'})


def _tv_fila_por_clave(clave):
    """Fila de la tele si la clave es válida y el pedido viene de la red de
    la casa (LAN). None si no."""
    if not _tvapp_red_permitida():   # casa o Tailscale (Orbyte TV puede usar Tailscale)
        return None
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM tv_pantallas WHERE clave=?', (clave,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@app.route('/tv/<clave>')
def tv_pantalla(clave):
    if not _tv_fila_por_clave(clave):
        return 'Pantalla no encontrada', 404
    if request.args.get('modo') == 'oled':   # C-08: diseño optimizado para OLED
        return render_template('tv_oled.html', clave=clave)
    return render_template('tv.html', clave=clave)


@app.route('/tv/<clave>/qr.png')
def tv_qr(clave):
    if not _tv_fila_por_clave(clave) or not _QRCODE_AVAILABLE:
        return '', 404
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
    finally:
        conn.close()
    if not sess:
        return '', 404
    img = qrcode.make(_collab_join_url(sess['token']), border=1)
    buf = BytesIO()
    img.save(buf, format='PNG')
    resp = Response(buf.getvalue(), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def _tv_pos(st, dur):
    pos = st['position'] + ((time.time() - st['at']) if st['playing'] else 0)
    return round(min(pos, dur), 1) if dur else round(pos, 1)


_TV_SQL_RANKING = (
    "SELECT cp.*, " + _COLLAB_SQL_USER_COLS + ", COUNT(cqi.id) AS n "
    "FROM collab_queue_items cqi JOIN collab_participants cp ON cp.id = cqi.participant_id "
    + _COLLAB_SQL_USER_JOIN +
    " WHERE cqi.session_id=? AND cp.device_key != ? "
    "GROUP BY cp.id ORDER BY n DESC, MIN(cqi.id) ASC LIMIT 5"
)
_TV_SQL_EVENTOS = (
    "SELECT cqi.id AS item_id, t.title AS t_title, ar.name AS t_artist, cp.*, " + _COLLAB_SQL_USER_COLS + " "
    "FROM collab_queue_items cqi "
    "JOIN tracks t ON t.id = cqi.track_id "
    "LEFT JOIN albums al ON al.id = t.album_id "
    "LEFT JOIN artists ar ON ar.id = al.artist_id "
    "JOIN collab_participants cp ON cp.id = cqi.participant_id "
    + _COLLAB_SQL_USER_JOIN +
    " WHERE cqi.session_id=? AND cqi.id>? ORDER BY cqi.id LIMIT 8"
)


@app.route('/tv/<clave>/estado')
def tv_estado(clave):
    """Lo que muestra la tele (consulta cada ~2,5 s). ?desde=<item_id> = último
    aviso ya mostrado (los avisos son las pistas agregadas después)."""
    fila = _tv_fila_por_clave(clave)
    if not fila:
        return jsonify({'error': 'not_found'}), 404
    try:
        desde = int(request.args.get('desde', ''))
    except ValueError:
        desde = None
    conn = get_db_connection()
    try:
        out = {'activa': bool(fila['activa']), 'modo': 'nada', 'sonando': None, 'siguientes': [], 'colab': None}
        sess = _collab_active_session(conn)
        if sess:
            out['modo'] = 'colab'
            p = _collab_sonando_payload(conn, sess, fila.get('dispositivo'))
            if p.get('track'):
                out['sonando'] = {'track': p['track'], 'position': p['position'], 'duration': p['duration'],
                                  'playing': p['playing']}
                out['siguientes'] = (p.get('next') or [])[:3]
            sid = sess['id']
            ranking = []
            for r in conn.execute(_TV_SQL_RANKING, (sid, _COLLAB_INFINITO_KEY)):
                nombre, av = _collab_part_view(r)
                ranking.append({'name': nombre, 'avatar': av, 'total': r['n']})
            total = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE session_id=?', (sid,)).fetchone()[0]
            sonaron = conn.execute('SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND played_at IS NOT NULL',
                                   (sid,)).fetchone()[0]
            ultimo = conn.execute('SELECT MAX(id) FROM collab_queue_items WHERE session_id=?', (sid,)).fetchone()[0] or 0
            eventos = []
            if desde is not None and desde < ultimo:
                for r in conn.execute(_TV_SQL_EVENTOS, (sid, desde)):
                    nombre, av = _collab_part_view(r)
                    eventos.append({'item_id': r['item_id'], 'by': nombre, 'avatar': av,
                                    'title': r['t_title'], 'artist': r['t_artist']})
            out['colab'] = {'code': _collab_codigo_display(sess.get('code')), 'join_url': _collab_join_url(sess['token']),
                            'total': total, 'sonaron': sonaron, 'ranking': ranking,
                            'eventos': eventos, 'ultimo_item': ultimo}
        elif fila.get('dispositivo') and fila['activa']:
            st = _tv_sonando.get(fila['dispositivo'])
            if st and st['track_id'] and time.time() - st['at'] <= _COLLAB_SONANDO_VIGENCIA_S:
                falso = {'id': -1, 'created_by': fila['admin_id']}
                detalle = _collab_pistas_detalle(conn, falso, [st['track_id']] + st['next_ids'][:3])
                if detalle and detalle[0]['id'] == st['track_id']:
                    out['modo'] = 'solo'
                    dur = st['duration'] or (detalle[0]['duration'] or 0)
                    out['sonando'] = {'track': detalle[0], 'position': _tv_pos(st, dur), 'duration': dur,
                                      'playing': st['playing']}
                    out['siguientes'] = detalle[1:4]
        resp = jsonify(out)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    finally:
        conn.close()


# ══ Ticket C-10 (pedido por Niko): Orbyte social — etapa 1 ════════════════════
# Amigos con amistad mutua (solicitud → aceptar), silenciar a un amigo, centro
# de notificaciones (campana) y ajustes de privacidad/avisos por usuario.
# Toda interacción es solo entre amigos. Especificación:
# claude/especificacion-c10-social.md (proyecto).

_SOCIAL_TIPOS = {
    # tipo: (descripción para ajustes, activo por defecto, fuera de la app por defecto)
    'amistad_solicitud': ('Solicitudes de amistad', True, True),
    'amistad_aceptada':  ('Solicitud aceptada', True, True),
    'sistema':           ('Avisos de Orbyte', True, True),
    # Etapa 2 (playlists compartidas): los cambios, por defecto solo en la campana.
    'playlist_compartida': ('Playlists compartidas contigo', True, True),
    'playlist_cambios':    ('Cambios en playlists compartidas', True, False),
    # Etapa 3 (actividad entre amigos).
    'recomendacion':       ('Recomendaciones de amigos', True, True),
    'toque':               ('Toques 🔥', True, True),
    'like':                ('Likes ❤️', True, False),
    'respect':             ('Respect', True, True),
    'playlist_comentario': ('Comentarios en playlists', True, False),
    # Ticket C-11 (Descubrir).
    'descubrir':           ('Descubrir: novedades de tus favoritos', True, True),
    'pedido_subido':       ('Material que pediste ya está en Orbyte', True, True),
}
_SOCIAL_PRIVACIDAD_DEFECTO = {
    'buscable': True,              # encontrarme por nickname o correo (el QR siempre funciona)
    'mostrar_conectado': True,
    'mostrar_escuchando': False,
    'actividad': 'amigos',         # 'amigos' | 'nadie'
}
_NOTIF_VIGENCIA_DIAS = 90


def _social_ajustes(conn, user_id):
    row = conn.execute('SELECT settings_json FROM users WHERE id=?', (user_id,)).fetchone()
    try:
        guardado = (json.loads(row['settings_json']) if row and row['settings_json'] else {}).get('social') or {}
    except (TypeError, ValueError):
        guardado = {}
    priv = dict(_SOCIAL_PRIVACIDAD_DEFECTO)
    priv.update({k: v for k, v in (guardado.get('privacidad') or {}).items() if k in priv})
    avisos = {}
    for tipo, (nombre, activo, fuera) in _SOCIAL_TIPOS.items():
        g_ = (guardado.get('avisos') or {}).get(tipo) or {}
        avisos[tipo] = {'nombre': nombre, 'activo': bool(g_.get('activo', activo)), 'fuera': bool(g_.get('fuera', fuera))}
    return {'privacidad': priv, 'avisos': avisos}


def _social_usuario(conn, user_id):
    row = conn.execute('SELECT id, email, nickname, avatar FROM users WHERE id=?', (user_id,)).fetchone()
    if not row:
        return None
    nombre = _collab_nombre_usuario(row['nickname'], row['email'])
    return {'id': row['id'], 'nombre': nombre, 'avatar': _collab_avatar_usuario(row['avatar'], nombre)}


def _social_par(a, b):
    a, b = int(a), int(b)
    return (a, b) if a < b else (b, a)


def _social_amistad(conn, a, b):
    x, y = _social_par(a, b)
    row = conn.execute('SELECT * FROM amistades WHERE usuario_a=? AND usuario_b=?', (x, y)).fetchone()
    return dict(row) if row else None


def _social_relacion(conn, yo, otro):
    """'amigo' | 'enviada' (yo pedí) | 'recibida' (me pidió) | 'ninguna'."""
    am = _social_amistad(conn, yo, otro)
    if not am:
        return 'ninguna'
    if am['estado'] == 'aceptada':
        return 'amigo'
    return 'enviada' if am['solicitante'] == yo else 'recibida'


def _social_codigo(conn, user_id):
    row = conn.execute('SELECT amigo_codigo FROM users WHERE id=?', (user_id,)).fetchone()
    if row and row['amigo_codigo']:
        return row['amigo_codigo']
    while True:
        codigo = ''.join(secrets.choice(_COLLAB_CODE_ALFABETO) for _ in range(8))
        if not conn.execute('SELECT 1 FROM users WHERE amigo_codigo=?', (codigo,)).fetchone():
            break
    conn.execute('UPDATE users SET amigo_codigo=? WHERE id=?', (codigo, user_id))
    conn.commit()
    return codigo


def _social_link_codigo(codigo):
    return _COLLAB_LAN_URL + f'/amigo/{codigo}'


def _notificar(conn, usuario_id, tipo, texto, actor_id=None, datos=None):
    """Crea una notificación respetando los ajustes del destinatario (tipo
    desactivado → no se crea) y los silenciados (amigo silenciado → no se crea).
    Devuelve el id o None."""
    if actor_id is not None and conn.execute(
            'SELECT 1 FROM amigos_silenciados WHERE usuario_id=? AND amigo_id=?', (usuario_id, actor_id)).fetchone():
        return None
    pref = _social_ajustes(conn, usuario_id)['avisos'].get(tipo) or {'activo': True, 'fuera': True}
    if not pref['activo']:
        return None
    cur = conn.execute('INSERT INTO notificaciones (usuario_id, tipo, actor_id, texto, datos_json, fuera, creada_at) '
                       'VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (usuario_id, tipo, actor_id, texto[:300], json.dumps(datos or {}, ensure_ascii=False),
                        1 if pref['fuera'] else 0, _utcnow_iso()))
    return cur.lastrowid


def _notif_json(conn, row, cache):
    r = dict(row)
    actor = None
    if r['actor_id']:
        if r['actor_id'] not in cache:
            cache[r['actor_id']] = _social_usuario(conn, r['actor_id'])
        actor = cache[r['actor_id']]
    try:
        datos = json.loads(r['datos_json'] or '{}')
    except ValueError:
        datos = {}
    return {'id': r['id'], 'tipo': r['tipo'], 'texto': r['texto'], 'actor': actor, 'datos': datos,
            'fuera': bool(r['fuera']), 'leida': bool(r['leida_at']), 'creada': r['creada_at']}


def _social_limpiar(conn, user_id):
    limite = (datetime.now(timezone.utc) - timedelta(days=_NOTIF_VIGENCIA_DIAS)).strftime('%Y-%m-%dT%H:%M:%S')
    conn.execute('DELETE FROM notificaciones WHERE usuario_id=? AND creada_at < ?', (user_id, limite))


def _social_marcar_solicitud(conn, receptor, solicitante, estado):
    """La notificación de solicitud queda resuelta (las apps ocultan los botones)."""
    for r in conn.execute("SELECT id, datos_json FROM notificaciones WHERE usuario_id=? AND tipo='amistad_solicitud' "
                          "AND actor_id=?", (receptor, solicitante)).fetchall():
        try:
            d = json.loads(r['datos_json'] or '{}')
        except ValueError:
            d = {}
        d['estado'] = estado
        conn.execute('UPDATE notificaciones SET datos_json=?, leida_at=COALESCE(leida_at, ?) WHERE id=?',
                     (json.dumps(d, ensure_ascii=False), _utcnow_iso(), r['id']))


def _social_lista_amigos(conn, yo):
    silenciados = {r['amigo_id'] for r in conn.execute(
        'SELECT amigo_id FROM amigos_silenciados WHERE usuario_id=?', (yo,))}
    amigos, recibidas, enviadas = [], [], []
    for am in conn.execute('SELECT * FROM amistades WHERE usuario_a=? OR usuario_b=? ORDER BY COALESCE(aceptada_at, creada_at) DESC',
                           (yo, yo)).fetchall():
        otro = am['usuario_b'] if am['usuario_a'] == yo else am['usuario_a']
        u = _social_usuario(conn, otro)
        if not u:
            continue
        if am['estado'] == 'aceptada':
            u.update({'silenciado': otro in silenciados, 'desde': am['aceptada_at'],
                      'estado': _social_estado(conn, otro)})   # etapa 3
            amigos.append(u)
        elif am['solicitante'] == yo:
            u['enviada_at'] = am['creada_at']
            enviadas.append(u)
        else:
            u['recibida_at'] = am['creada_at']
            recibidas.append(u)
    amigos.sort(key=lambda x: x['nombre'].lower())
    return amigos, recibidas, enviadas


@app.route('/api/v1/social/amigos')
@api_login_required
def api_v1_social_amigos():
    """Amigos, solicitudes recibidas y enviadas, y mi código/QR para que me agreguen."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        amigos, recibidas, enviadas = _social_lista_amigos(conn, yo)
        codigo = _social_codigo(conn, yo)
        return jsonify({'status': 'ok', 'amigos': amigos, 'recibidas': recibidas, 'enviadas': enviadas,
                        'mi_codigo': codigo, 'mi_link': _social_link_codigo(codigo)})
    finally:
        conn.close()


@app.route('/api/v1/social/buscar', methods=['POST'])
@api_login_required
def api_v1_social_buscar():
    """Buscar usuarios para agregar. {q}: con '@' busca el correo EXACTO; si
    no, nicknames que empiecen con q (mín. 2 letras). Respeta 'buscable'."""
    yo = g.api_user['id']
    q = ' '.join(str((request.get_json(silent=True) or {}).get('q') or '').split())[:80]
    if len(q) < 2:
        return jsonify({'status': 'ok', 'resultados': []})
    conn = get_db_connection()
    try:
        if '@' in q:
            filas = conn.execute('SELECT id FROM users WHERE lower(email)=lower(?) AND is_approved=1 AND id<>?',
                                 (q, yo)).fetchall()
        else:
            like = q.lower().replace('\\', '').replace('%', '').replace('_', '') + '%'
            filas = conn.execute("SELECT id FROM users WHERE lower(nickname) LIKE ? AND is_approved=1 AND id<>? "
                                 "ORDER BY nickname LIMIT 10", (like, yo)).fetchall()
        out = []
        for r in filas:
            if not _social_ajustes(conn, r['id'])['privacidad']['buscable']:
                continue
            u = _social_usuario(conn, r['id'])
            u['relacion'] = _social_relacion(conn, yo, r['id'])
            out.append(u)
        return jsonify({'status': 'ok', 'resultados': out})
    finally:
        conn.close()


@app.route('/api/v1/social/codigo/<codigo>')
@api_login_required
def api_v1_social_codigo(codigo):
    """Quién es el dueño de un código/QR de cuenta (para confirmar antes de agregar)."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT id FROM users WHERE amigo_codigo=? AND is_approved=1',
                           (str(codigo).strip().upper(),)).fetchone()
        if not row:
            return jsonify({'status': 'error', 'message': 'Ese código no corresponde a ninguna cuenta.'}), 404
        u = _social_usuario(conn, row['id'])
        u['relacion'] = 'yo' if row['id'] == yo else _social_relacion(conn, yo, row['id'])
        return jsonify({'status': 'ok', 'usuario': u})
    finally:
        conn.close()


def _social_destino(conn, datos, yo):
    """user_id destino a partir de {user_id} o {codigo}."""
    if datos.get('codigo'):
        row = conn.execute('SELECT id FROM users WHERE amigo_codigo=? AND is_approved=1',
                           (str(datos['codigo']).strip().upper(),)).fetchone()
        return row['id'] if row else None
    try:
        uid = int(datos.get('user_id'))
    except (TypeError, ValueError):
        return None
    row = conn.execute('SELECT id FROM users WHERE id=? AND is_approved=1', (uid,)).fetchone()
    return row['id'] if row else None


@app.route('/api/v1/social/solicitar', methods=['POST'])
@api_login_required
def api_v1_social_solicitar():
    """Enviar solicitud de amistad. {user_id} o {codigo}. Si el otro ya me
    había pedido, se acepta directo."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        otro = _social_destino(conn, datos, yo)
        if not otro or otro == yo:
            return jsonify({'status': 'error', 'message': 'No encontré esa cuenta.'}), 404
        am = _social_amistad(conn, yo, otro)
        if am and am['estado'] == 'aceptada':
            return jsonify({'status': 'ok', 'relacion': 'amigo'})
        if am and am['solicitante'] != yo:   # me había pedido: aceptar
            return _social_aceptar(conn, yo, otro)
        if am:
            return jsonify({'status': 'ok', 'relacion': 'enviada'})
        x, y = _social_par(yo, otro)
        conn.execute('INSERT INTO amistades (usuario_a, usuario_b, solicitante, creada_at) VALUES (?, ?, ?, ?)',
                     (x, y, yo, _utcnow_iso()))
        yo_u = _social_usuario(conn, yo)
        _notificar(conn, otro, 'amistad_solicitud', f'{yo_u["nombre"]} quiere ser tu amigo en Orbyte.',
                   actor_id=yo, datos={'user_id': yo, 'estado': 'pendiente'})
        conn.commit()
        return jsonify({'status': 'ok', 'relacion': 'enviada'})
    finally:
        conn.close()


def _social_aceptar(conn, yo, otro):
    x, y = _social_par(yo, otro)
    conn.execute("UPDATE amistades SET estado='aceptada', aceptada_at=? WHERE usuario_a=? AND usuario_b=?",
                 (_utcnow_iso(), x, y))
    _social_marcar_solicitud(conn, yo, otro, 'aceptada')
    yo_u = _social_usuario(conn, yo)
    _notificar(conn, otro, 'amistad_aceptada', f'{yo_u["nombre"]} aceptó tu solicitud: ahora son amigos.',
               actor_id=yo, datos={'user_id': yo})
    conn.commit()
    return jsonify({'status': 'ok', 'relacion': 'amigo'})


@app.route('/api/v1/social/responder', methods=['POST'])
@api_login_required
def api_v1_social_responder():
    """Responder una solicitud recibida. {user_id, aceptar: bool}."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        otro = _social_destino(conn, datos, yo)
        am = _social_amistad(conn, yo, otro) if otro else None
        if not am or am['estado'] != 'pendiente' or am['solicitante'] == yo:
            return jsonify({'status': 'error', 'message': 'Esa solicitud ya no está pendiente.'}), 409
        if datos.get('aceptar'):
            return _social_aceptar(conn, yo, otro)
        conn.execute('DELETE FROM amistades WHERE id=?', (am['id'],))
        _social_marcar_solicitud(conn, yo, otro, 'rechazada')
        conn.commit()
        return jsonify({'status': 'ok', 'relacion': 'ninguna'})
    finally:
        conn.close()


@app.route('/api/v1/social/quitar', methods=['POST'])
@api_login_required
def api_v1_social_quitar():
    """Quitar amistad o cancelar una solicitud enviada. {user_id}."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        otro = _social_destino(conn, datos, yo)
        am = _social_amistad(conn, yo, otro) if otro else None
        if am:
            conn.execute('DELETE FROM amistades WHERE id=?', (am['id'],))
            conn.execute('DELETE FROM amigos_silenciados WHERE (usuario_id=? AND amigo_id=?) OR (usuario_id=? AND amigo_id=?)',
                         (yo, otro, otro, yo))
            # C-10 etapa 2: se deja de compartir todo lo que había entre los dos.
            conn.execute('DELETE FROM playlist_compartidas WHERE (usuario_id=? AND playlist_id IN '
                         '(SELECT id FROM playlists WHERE user_id=?)) OR (usuario_id=? AND playlist_id IN '
                         '(SELECT id FROM playlists WHERE user_id=?))', (otro, yo, yo, otro))
            if am['estado'] == 'pendiente' and am['solicitante'] == yo:
                # Solicitud cancelada: se borra el aviso que le llegó al otro.
                conn.execute("DELETE FROM notificaciones WHERE usuario_id=? AND tipo='amistad_solicitud' AND actor_id=?",
                             (otro, yo))
            conn.commit()
        return jsonify({'status': 'ok', 'relacion': 'ninguna'})
    finally:
        conn.close()


@app.route('/api/v1/social/silenciar', methods=['POST'])
@api_login_required
def api_v1_social_silenciar():
    """{user_id, silenciar: bool}: sigue siendo amigo, pero sus avisos no llegan."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        otro = _social_destino(conn, datos, yo)
        if not otro or _social_relacion(conn, yo, otro) != 'amigo':
            return jsonify({'status': 'error', 'message': 'Solo puedes silenciar a un amigo.'}), 409
        if datos.get('silenciar'):
            conn.execute('INSERT OR IGNORE INTO amigos_silenciados (usuario_id, amigo_id) VALUES (?, ?)', (yo, otro))
        else:
            conn.execute('DELETE FROM amigos_silenciados WHERE usuario_id=? AND amigo_id=?', (yo, otro))
        conn.commit()
        return jsonify({'status': 'ok', 'silenciado': bool(datos.get('silenciar'))})
    finally:
        conn.close()


@app.route('/api/v1/social/notificaciones')
@api_login_required
def api_v1_social_notificaciones():
    """Centro de notificaciones: las últimas (?limite=, máx. 100; ?antes_de=<id>
    para paginar) + no leídas."""
    yo = g.api_user['id']
    limite = max(1, min(100, request.args.get('limite', 50, type=int)))
    antes = request.args.get('antes_de', type=int)
    conn = get_db_connection()
    try:
        _social_limpiar(conn, yo)
        conn.commit()
        sql = 'SELECT * FROM notificaciones WHERE usuario_id=?' + (' AND id<?' if antes else '') + ' ORDER BY id DESC LIMIT ?'
        args = (yo, antes, limite) if antes else (yo, limite)
        cache = {}
        items = [_notif_json(conn, r, cache) for r in conn.execute(sql, args).fetchall()]
        no_leidas = conn.execute('SELECT COUNT(*) FROM notificaciones WHERE usuario_id=? AND leida_at IS NULL',
                                 (yo,)).fetchone()[0]
        return jsonify({'status': 'ok', 'items': items, 'no_leidas': no_leidas})
    finally:
        conn.close()


@app.route('/api/v1/social/notificaciones/contador')
@api_login_required
def api_v1_social_contador():
    """Para la campana (consulta liviana): no leídas, el id más nuevo y las
    nuevas para avisar fuera de la app (?despues_de=<id>, solo fuera=1)."""
    yo = g.api_user['id']
    despues = request.args.get('despues_de', type=int)
    conn = get_db_connection()
    try:
        no_leidas = conn.execute('SELECT COUNT(*) FROM notificaciones WHERE usuario_id=? AND leida_at IS NULL',
                                 (yo,)).fetchone()[0]
        ultima = conn.execute('SELECT MAX(id) FROM notificaciones WHERE usuario_id=?', (yo,)).fetchone()[0] or 0
        nuevas = []
        if despues is not None and despues < ultima:
            cache = {}
            nuevas = [_notif_json(conn, r, cache) for r in conn.execute(
                'SELECT * FROM notificaciones WHERE usuario_id=? AND id>? AND fuera=1 AND leida_at IS NULL '
                'ORDER BY id LIMIT 10', (yo, despues)).fetchall()]
        return jsonify({'status': 'ok', 'no_leidas': no_leidas, 'ultima_id': ultima, 'nuevas': nuevas})
    finally:
        conn.close()


@app.route('/api/v1/social/notificaciones/leer', methods=['POST'])
@api_login_required
def api_v1_social_leer():
    """{ids: [...]} o {todas: true}."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        if datos.get('todas'):
            conn.execute('UPDATE notificaciones SET leida_at=? WHERE usuario_id=? AND leida_at IS NULL', (_utcnow_iso(), yo))
        else:
            ids = [int(x) for x in (datos.get('ids') or []) if str(x).isdigit()][:200]
            if ids:
                conn.execute(f'UPDATE notificaciones SET leida_at=COALESCE(leida_at, ?) WHERE usuario_id=? '
                             f'AND id IN ({",".join("?" * len(ids))})', [_utcnow_iso(), yo] + ids)
        conn.commit()
        no_leidas = conn.execute('SELECT COUNT(*) FROM notificaciones WHERE usuario_id=? AND leida_at IS NULL',
                                 (yo,)).fetchone()[0]
        return jsonify({'status': 'ok', 'no_leidas': no_leidas})
    finally:
        conn.close()


@app.route('/api/v1/social/notificaciones/borrar', methods=['POST'])
@api_login_required
def api_v1_social_borrar():
    """{id}."""
    yo = g.api_user['id']
    try:
        nid = int((request.get_json(silent=True) or {}).get('id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Falta id.'}), 400
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM notificaciones WHERE id=? AND usuario_id=?', (nid, yo))
        conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


@app.route('/api/v1/social/ajustes', methods=['GET', 'POST'])
@api_login_required
def api_v1_social_ajustes():
    """Privacidad y avisos. POST parcial: {privacidad: {...}, avisos: {tipo: {activo, fuera}}}."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            datos = request.get_json(silent=True) or {}
            actual = _social_ajustes(conn, yo)
            priv = actual['privacidad']
            for k, v in (datos.get('privacidad') or {}).items():
                if k in ('buscable', 'mostrar_conectado', 'mostrar_escuchando'):
                    priv[k] = bool(v)
                elif k == 'actividad' and v in ('amigos', 'nadie'):
                    priv[k] = v
            avisos = {t: {'activo': a['activo'], 'fuera': a['fuera']} for t, a in actual['avisos'].items()}
            for t, v in (datos.get('avisos') or {}).items():
                if t in avisos and isinstance(v, dict):
                    if 'activo' in v:
                        avisos[t]['activo'] = bool(v['activo'])
                    if 'fuera' in v:
                        avisos[t]['fuera'] = bool(v['fuera'])
            row = conn.execute('SELECT settings_json FROM users WHERE id=?', (yo,)).fetchone()
            try:
                todo = json.loads(row['settings_json']) if row and row['settings_json'] else {}
            except ValueError:
                todo = {}
            todo['social'] = {'privacidad': priv, 'avisos': avisos}
            conn.execute('UPDATE users SET settings_json=? WHERE id=?', (json.dumps(todo), yo))
            conn.commit()
        return jsonify(dict(_social_ajustes(conn, yo), status='ok'))
    finally:
        conn.close()


@app.route('/api/v1/admin/social/anuncio', methods=['POST'])
@api_admin_required
def api_v1_admin_social_anuncio():
    """Aviso del sistema para todos los usuarios aprobados. {texto}."""
    texto = ' '.join(str((request.get_json(silent=True) or {}).get('texto') or '').split())[:300]
    if len(texto) < 3:
        return jsonify({'status': 'error', 'message': 'Escribe el aviso.'}), 400
    conn = get_db_connection()
    try:
        n = 0
        for r in conn.execute('SELECT id FROM users WHERE is_approved=1').fetchall():
            if _notificar(conn, r['id'], 'sistema', texto, datos={'origen': 'admin'}):
                n += 1
        conn.commit()
        return jsonify({'status': 'ok', 'enviados': n})
    finally:
        conn.close()


@app.route('/amigo/<codigo>')
@login_required
def amigo_por_codigo(codigo):
    """Destino del QR de la cuenta: muestra a quién se va a agregar."""
    return render_template('amigo.html', codigo=str(codigo).strip().upper())


@app.route('/amigos')
@login_required
def pagina_amigos():
    return render_template('amigos.html')


@app.route('/notificaciones')
@login_required
def pagina_notificaciones():
    return render_template('notificaciones.html')


@app.route('/api/v1/social/mi-qr.png')
@api_login_required
def api_v1_social_mi_qr():
    """QR con el link de mi cuenta (para que un amigo lo escanee)."""
    if not _QRCODE_AVAILABLE:
        return '', 404
    conn = get_db_connection()
    try:
        codigo = _social_codigo(conn, g.api_user['id'])
    finally:
        conn.close()
    img = qrcode.make(_social_link_codigo(codigo), border=1)
    buf = BytesIO()
    img.save(buf, format='PNG')
    resp = Response(buf.getvalue(), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    return resp



# ══ Ticket C-10 etapa 2: playlists compartidas ═══════════════════════════════
# El dueño comparte con amigos («ver» o «editar» por persona); quien edita
# agrega, quita y reordena (no renombra, borra ni comparte); cualquiera con
# acceso guarda una copia propia; el invitado puede «dejar» la playlist.
# Quitar la amistad corta lo compartido (ver api_v1_social_quitar).

_PLAYLIST_CAMBIOS_VENTANA_H = 12   # avisos de cambios agrupados por amigo/playlist


def _playlist_miembros(conn, playlist_id):
    out = []
    for r in conn.execute('SELECT usuario_id, permiso FROM playlist_compartidas WHERE playlist_id=? '
                          'ORDER BY compartida_at', (playlist_id,)).fetchall():
        u = _social_usuario(conn, r['usuario_id'])
        if u:
            u['permiso'] = r['permiso']
            out.append(u)
    return out


def _playlist_compartir_con(conn, playlist_id, nombre, dueno, uid, permiso):
    """Comparte (o cambia el permiso). Solo con amigos. Avisa si es nueva o si
    pasa de ver a editar. Devuelve True si quedó compartida."""
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return False
    if uid == dueno or permiso not in ('ver', 'editar') or _social_relacion(conn, dueno, uid) != 'amigo':
        return False
    antes = conn.execute('SELECT permiso FROM playlist_compartidas WHERE playlist_id=? AND usuario_id=?',
                         (playlist_id, uid)).fetchone()
    if antes and antes['permiso'] == permiso:
        return True
    if antes:
        conn.execute('UPDATE playlist_compartidas SET permiso=? WHERE playlist_id=? AND usuario_id=?',
                     (permiso, playlist_id, uid))
    else:
        conn.execute('INSERT INTO playlist_compartidas (playlist_id, usuario_id, permiso, compartida_at) '
                     'VALUES (?, ?, ?, ?)', (playlist_id, uid, permiso, _utcnow_iso()))
    if not antes or permiso == 'editar':
        quien = _social_usuario(conn, dueno)['nombre']
        if permiso == 'editar':
            texto = (f'{quien} te invitó a editar la playlist «{nombre}».' if antes else
                     f'{quien} compartió contigo la playlist «{nombre}» (puedes editarla).')
        else:
            texto = f'{quien} compartió contigo la playlist «{nombre}».'
        _notificar(conn, uid, 'playlist_compartida', texto, actor_id=dueno,
                   datos={'playlist_id': playlist_id, 'permiso': permiso})
    return True


def _playlist_avisar_cambio(conn, pl, actor):
    """Aviso agrupado a los demás con acceso: «Juan agregó 3 pistas a «X»»
    (una notificación no leída por amigo y playlist en la ventana)."""
    destinos = {pl['user_id']} | {r['usuario_id'] for r in conn.execute(
        'SELECT usuario_id FROM playlist_compartidas WHERE playlist_id=?', (pl['id'],))}
    destinos.discard(actor)
    if not destinos:
        return
    quien = _social_usuario(conn, actor)['nombre']
    desde = (datetime.now(timezone.utc) - timedelta(hours=_PLAYLIST_CAMBIOS_VENTANA_H)).strftime('%Y-%m-%dT%H:%M:%S')
    for uid in destinos:
        previa = None
        for r in conn.execute("SELECT id, datos_json FROM notificaciones WHERE usuario_id=? AND tipo='playlist_cambios' "
                              "AND actor_id=? AND leida_at IS NULL AND creada_at>? ORDER BY id DESC",
                              (uid, actor, desde)).fetchall():
            try:
                d = json.loads(r['datos_json'] or '{}')
            except ValueError:
                d = {}
            if d.get('playlist_id') == pl['id']:
                previa = (r['id'], d)
                break
        if previa:
            nid, d = previa
            d['n'] = int(d.get('n') or 1) + 1
            conn.execute('UPDATE notificaciones SET texto=?, datos_json=?, creada_at=? WHERE id=?',
                         (f'{quien} agregó {d["n"]} pistas a «{pl["name"]}».'[:300],
                          json.dumps(d, ensure_ascii=False), _utcnow_iso(), nid))
        else:
            _notificar(conn, uid, 'playlist_cambios', f'{quien} agregó una pista a «{pl["name"]}».',
                       actor_id=actor, datos={'playlist_id': pl['id'], 'n': 1})


@app.route('/api/v1/playlists/<int:playlist_id>/compartir', methods=['GET', 'POST'])
@api_login_required
def api_v1_playlist_compartir(playlist_id):
    """Solo el dueño. GET: mis amigos con el permiso actual de cada uno
    (null = no la ve). POST: {user_id, permiso: 'ver'|'editar'|null} o
    {cambios: [{user_id, permiso}, ...]}."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        pl = _playlist_owned_or_404(conn, playlist_id, yo)
        if not pl:
            return jsonify({'status': 'error', 'message': 'Solo el dueño puede compartirla.'}), 404
        if request.method == 'POST':
            datos = request.get_json(silent=True) or {}
            cambios = datos.get('cambios') if isinstance(datos.get('cambios'), list) else [datos]
            for c in cambios[:50]:
                if not isinstance(c, dict):
                    continue
                permiso = c.get('permiso')
                if permiso in ('ver', 'editar'):
                    _playlist_compartir_con(conn, playlist_id, pl['name'], yo, c.get('user_id'), permiso)
                else:
                    try:
                        conn.execute('DELETE FROM playlist_compartidas WHERE playlist_id=? AND usuario_id=?',
                                     (playlist_id, int(c.get('user_id'))))
                    except (TypeError, ValueError):
                        pass
            conn.commit()
        actuales = {r['usuario_id']: r['permiso'] for r in conn.execute(
            'SELECT usuario_id, permiso FROM playlist_compartidas WHERE playlist_id=?', (playlist_id,))}
        amigos, _, _ = _social_lista_amigos(conn, yo)
        out = [{'id': a['id'], 'nombre': a['nombre'], 'avatar': a['avatar'], 'permiso': actuales.get(a['id'])}
               for a in amigos]
        return jsonify({'status': 'ok', 'amigos': out, 'compartida_n': len(actuales)})
    finally:
        conn.close()


@app.route('/api/v1/playlists/<int:playlist_id>/dejar', methods=['POST'])
@api_login_required
def api_v1_playlist_dejar(playlist_id):
    """El invitado se quita una playlist que le compartieron."""
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM playlist_compartidas WHERE playlist_id=? AND usuario_id=?',
                     (playlist_id, g.api_user['id']))
        conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


@app.route('/api/v1/playlists/<int:playlist_id>/copiar', methods=['POST'])
@api_login_required
def api_v1_playlist_copiar(playlist_id):
    """Guardar una copia propia y editable. {name?}."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        pl, rol = _playlist_acceso(conn, playlist_id, yo)
        if not pl:
            return jsonify({'error': 'not_found'}), 404
        name = ' '.join(str((request.get_json(silent=True) or {}).get('name') or '').split())[:120] \
            or f'{pl["name"]} (copia)'
        cur = conn.execute('INSERT INTO playlists (user_id, name, copia_de) VALUES (?, ?, ?)', (yo, name, playlist_id))
        nuevo = cur.lastrowid
        conn.execute('INSERT INTO playlist_tracks (playlist_id, track_id, position) '
                     'SELECT ?, track_id, position FROM playlist_tracks WHERE playlist_id=? ORDER BY position',
                     (nuevo, playlist_id))
        n = conn.execute('SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id=?', (nuevo,)).fetchone()[0]
        conn.commit()
        return jsonify({'id': nuevo, 'name': name, 'track_count': n}), 201
    finally:
        conn.close()


@app.route('/api/v1/admin/collab/guardar-playlist', methods=['POST'])
@api_admin_required
def api_v1_admin_collab_guardar_playlist():
    """Guardar la sesión colaborativa (la activa, o la última cerrada hace
    menos de 24 h) como playlist del anfitrión. {nombre?, solo_sonadas?,
    compartir?}: compartir = con los participantes con cuenta que son amigos
    del anfitrión (permiso «ver»). Cada pista queda «agregada por» quien la
    pidió si tenía cuenta."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            desde = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S')
            row = conn.execute('SELECT * FROM collab_sessions WHERE closed_at>? ORDER BY id DESC LIMIT 1',
                               (desde,)).fetchone()
            sess = dict(row) if row else None
        if not sess:
            return jsonify({'status': 'error', 'message': 'No hay una sesión colaborativa para guardar.'}), 404
        filas = conn.execute(
            'SELECT cqi.track_id, cqi.played_at, cp.user_id FROM collab_queue_items cqi '
            'JOIN collab_participants cp ON cp.id = cqi.participant_id '
            'WHERE cqi.session_id=? ORDER BY (cqi.played_at IS NULL), cqi.played_at, cqi.added_at, cqi.id',
            (sess['id'],)).fetchall()
        if datos.get('solo_sonadas'):
            filas = [f for f in filas if f['played_at']]
        if not filas:
            return jsonify({'status': 'error', 'message': 'La sesión no tiene pistas para guardar.'}), 409
        fecha = (sess.get('created_at') or '')[:19].replace('T', ' ')
        try:   # created_at es UTC: la fecha en la hora local del servidor
            fecha = datetime.strptime(fecha, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).astimezone().strftime('%d-%m-%Y')
        except ValueError:
            fecha = fecha[:10]
        nombre = ' '.join(str(datos.get('nombre') or '').split())[:120] or f'Colaborativa {fecha}'.strip()
        cur = conn.execute('INSERT INTO playlists (user_id, name) VALUES (?, ?)', (yo, nombre))
        pid = cur.lastrowid
        pos, usuarios = 0, set()
        for f in filas:
            agrego = f['user_id'] if f['user_id'] else None
            c = conn.execute('INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position, added_by) '
                             'VALUES (?, ?, ?, ?)', (pid, f['track_id'], pos, agrego))
            if c.rowcount:
                pos += 1
            if agrego and agrego != yo:
                usuarios.add(agrego)
        compartida = 0
        if datos.get('compartir'):
            for uid in usuarios:
                if _playlist_compartir_con(conn, pid, nombre, yo, uid, 'ver'):
                    compartida += 1
        conn.commit()
        return jsonify({'status': 'ok', 'id': pid, 'name': nombre, 'track_count': pos, 'compartida_n': compartida})
    finally:
        conn.close()

# ══ Ticket C-10 etapa 3: actividad entre amigos ══════════════════════════════
# Conectado / escuchando ahora (según la privacidad de cada uno), ❤️ like a
# playlists compartidas y a lo que suena un amigo, 🔥 toque, respect (dar 1 al
# día en total), recomendar a todos los amigos (sin repetir pista en el día,
# máx. 5 al día), dashboard del perfil y comentarios en playlists.

_RECOMENDACIONES_POR_DIA = 5
_ESCUCHANDO_VIGENCIA_S = 15 * 60       # sin noticias del cliente, deja de mostrarse
_social_escuchando = {}                # user_id -> {track_id, playing, at, duracion}


def _social_hoy():
    return datetime.now().strftime('%Y-%m-%d')   # fecha local del servidor


def _social_pista(conn, track_id):
    """Pista con el mismo formato que el detalle de playlist (las apps la
    reproducen o la agregan a la cola sin pedir nada más)."""
    d = conn.execute(
        'SELECT t.*, al.name as album_name, al.year as album_year, al.cover_path, al.artist_id, '
        'ar.name as artist_name FROM tracks t LEFT JOIN albums al ON al.id = t.album_id '
        'LEFT JOIN artists ar ON al.artist_id = ar.id WHERE t.id=?', (track_id,)).fetchone()
    if not d:
        return None
    d = dict(d)
    fmt, led = _fmt_format(d)
    return {
        'id': d['id'], 'title': d.get('title'), 'track_number': d.get('track_number'),
        'disc_number': d.get('disc_number'), 'duration': d.get('duration'),
        'duration_fmt': _fmt_seconds(d.get('duration')), 'format_display': fmt, 'format_color': led,
        'artist_id': d.get('artist_id'), 'artist_name': d.get('artist_name'), 'album_id': d.get('album_id'),
        'album_name': d.get('album_name'), 'album_year': d.get('album_year'),
        'cover_url': cover_url_filter(clean_db_path(d.get('cover_path'))),
        'stream_url': f'/api/v1/stream/{d["id"]}', 'is_dsd': d.get('is_dsd'), 'dsd_rate': d.get('dsd_rate'),
        'sample_rate_real': d.get('sample_rate_real'), 'bit_depth': d.get('bit_depth'),
        'container_ext': _container_ext(d),
    }


def _social_estado(conn, user_id):
    """Lo que un amigo puede ver de user_id según su privacidad:
    {conectado: bool|None, escuchando: pista|None} (None = no lo comparte)."""
    priv = _social_ajustes(conn, user_id)['privacidad']
    out = {'conectado': None, 'escuchando': None}
    if priv.get('mostrar_conectado'):
        row = conn.execute('SELECT last_seen FROM users WHERE id=?', (user_id,)).fetchone()
        cutoff = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=ONLINE_WINDOW_MINUTES)).isoformat()
        out['conectado'] = bool(row and row['last_seen'] and row['last_seen'] >= cutoff)
    if priv.get('mostrar_escuchando'):
        e = _social_escuchando.get(user_id)
        if e and e.get('playing') and e.get('track_id'):
            vigencia = max(_ESCUCHANDO_VIGENCIA_S, (e.get('duracion') or 0) + 120)
            if time.time() - e['at'] < vigencia:
                out['escuchando'] = _social_pista(conn, e['track_id'])
    return out


def _social_es_amigo(conn, yo, otro):
    return otro is not None and _social_relacion(conn, yo, otro) == 'amigo'


def _social_amigos_ids(conn, yo):
    return [r[0] for r in conn.execute(
        "SELECT CASE WHEN usuario_a=? THEN usuario_b ELSE usuario_a END FROM amistades "
        "WHERE (usuario_a=? OR usuario_b=?) AND estado='aceptada'", (yo, yo, yo)).fetchall()]


def _social_titulo(pista):
    if not pista:
        return 'una pista'
    return f'«{pista["title"]}»' + (f' de {pista["artist_name"]}' if pista.get('artist_name') else '')


def _social_contadores(conn, uid):
    return {
        'respect': conn.execute('SELECT COUNT(*) FROM social_respects WHERE para_id=?', (uid,)).fetchone()[0],
        'likes': conn.execute('SELECT COUNT(*) FROM social_likes WHERE para_id=?', (uid,)).fetchone()[0],
        'toques': conn.execute('SELECT COUNT(*) FROM social_toques WHERE para_id=?', (uid,)).fetchone()[0],
        'recomendaciones': conn.execute('SELECT COUNT(*) FROM social_recomendaciones WHERE de_id=?', (uid,)).fetchone()[0],
    }


@app.route('/api/v1/social/escuchando', methods=['POST'])
@api_login_required
def api_v1_social_escuchando():
    """Las apps avisan qué suena (al cambiar de pista o pausar). {track_id|null,
    playing}. Solo se guarda en memoria; solo lo ven los amigos y solo si el
    usuario activó «Mostrar qué estoy escuchando»."""
    datos = request.get_json(silent=True) or {}
    yo = g.api_user['id']
    try:
        tid = int(datos['track_id']) if datos.get('track_id') is not None else None
    except (TypeError, ValueError):
        tid = None
    duracion = 0
    if tid:
        conn = get_db_connection()
        try:
            row = conn.execute('SELECT duration FROM tracks WHERE id=?', (tid,)).fetchone()
            duracion = float(row['duration'] or 0) if row else 0
        finally:
            conn.close()
    _social_escuchando[yo] = {'track_id': tid, 'playing': bool(datos.get('playing')) and bool(tid),
                              'at': time.time(), 'duracion': duracion}
    return jsonify({'status': 'ok'})


@app.route('/api/v1/social/amigo/<int:uid>')
@api_login_required
def api_v1_social_amigo(uid):
    """Perfil de un amigo: estado, contadores (si comparte su actividad),
    playlists que me compartió y qué puedo hacer (respect de hoy, ❤️/🔥 a lo
    que suena)."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        if not _social_es_amigo(conn, yo, uid):
            return jsonify({'status': 'error', 'message': 'Solo puedes ver el perfil de un amigo.'}), 404
        estado = _social_estado(conn, uid)
        hoy = _social_hoy()
        out = {'status': 'ok', 'usuario': _social_usuario(conn, uid), 'estado': estado,
               'silenciado': bool(conn.execute('SELECT 1 FROM amigos_silenciados WHERE usuario_id=? AND amigo_id=?',
                                               (yo, uid)).fetchone())}
        if _social_ajustes(conn, uid)['privacidad'].get('actividad') == 'amigos':
            out['contadores'] = _social_contadores(conn, uid)
        dado = conn.execute('SELECT para_id FROM social_respects WHERE de_id=? AND dia=?', (yo, hoy)).fetchone()
        out['respect'] = {'dado_hoy': bool(dado), 'dado_hoy_a_el': bool(dado and dado['para_id'] == uid)}
        pista = estado.get('escuchando')
        if pista:
            out['reacciones'] = {
                'like': bool(conn.execute("SELECT 1 FROM social_likes WHERE de_id=? AND para_id=? AND tipo='pista' "
                                          "AND ref_id=? AND dia=?", (yo, uid, pista['id'], hoy)).fetchone()),
                'toque': bool(conn.execute('SELECT 1 FROM social_toques WHERE de_id=? AND para_id=? AND track_id=? AND dia=?',
                                           (yo, uid, pista['id'], hoy)).fetchone()),
            }
        out['playlists'] = [dict(r) for r in conn.execute(
            'SELECT p.id, p.name, pc.permiso AS rol, (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id=p.id) '
            'AS track_count FROM playlists p JOIN playlist_compartidas pc ON pc.playlist_id=p.id AND pc.usuario_id=? '
            'WHERE p.user_id=? ORDER BY p.name COLLATE NOCASE', (yo, uid)).fetchall()]
        return jsonify(out)
    finally:
        conn.close()


@app.route('/api/v1/social/reaccion', methods=['POST'])
@api_login_required
def api_v1_social_reaccion():
    """❤️ o 🔥 a lo que está escuchando un amigo. {user_id, track_id, tipo:
    'like'|'toque'}. Una de cada tipo por amigo, pista y día."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    tipo = datos.get('tipo')
    try:
        uid, tid = int(datos.get('user_id')), int(datos.get('track_id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Faltan datos.'}), 400
    if tipo not in ('like', 'toque'):
        return jsonify({'status': 'error', 'message': 'Reacción desconocida.'}), 400
    conn = get_db_connection()
    try:
        if not _social_es_amigo(conn, yo, uid):
            return jsonify({'status': 'error', 'message': 'Solo entre amigos.'}), 404
        pista = _social_pista(conn, tid)
        if not pista:
            return jsonify({'status': 'error', 'message': 'Esa pista no existe.'}), 404
        hoy, ahora = _social_hoy(), _utcnow_iso()
        quien = _social_usuario(conn, yo)['nombre']
        if tipo == 'like':
            if not conn.execute("SELECT 1 FROM social_likes WHERE de_id=? AND para_id=? AND tipo='pista' AND ref_id=? AND dia=?",
                                (yo, uid, tid, hoy)).fetchone():
                conn.execute("INSERT INTO social_likes (de_id, para_id, tipo, ref_id, dia, creada_at) VALUES (?, ?, 'pista', ?, ?, ?)",
                             (yo, uid, tid, hoy, ahora))
                _notificar(conn, uid, 'like', f'A {quien} le gustó {_social_titulo(pista)}, que estás escuchando.',
                           actor_id=yo, datos={'track_id': tid, 'pista': pista})
        else:
            if not conn.execute('SELECT 1 FROM social_toques WHERE de_id=? AND para_id=? AND track_id=? AND dia=?',
                                (yo, uid, tid, hoy)).fetchone():
                conn.execute('INSERT INTO social_toques (de_id, para_id, track_id, dia, creada_at) VALUES (?, ?, ?, ?, ?)',
                             (yo, uid, tid, hoy, ahora))
                _notificar(conn, uid, 'toque', f'🔥 {quien}: ¡buen tema! {_social_titulo(pista)}.',
                           actor_id=yo, datos={'track_id': tid, 'pista': pista})
        conn.commit()
        return jsonify({'status': 'ok', 'tipo': tipo})
    finally:
        conn.close()


@app.route('/api/v1/social/respect', methods=['POST'])
@api_login_required
def api_v1_social_respect():
    """Respect al gusto musical de un amigo. {user_id}. Cada uno da 1 al día."""
    yo = g.api_user['id']
    try:
        uid = int((request.get_json(silent=True) or {}).get('user_id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Falta el amigo.'}), 400
    conn = get_db_connection()
    try:
        if not _social_es_amigo(conn, yo, uid):
            return jsonify({'status': 'error', 'message': 'Solo puedes darle respect a un amigo.'}), 404
        hoy = _social_hoy()
        dado = conn.execute('SELECT para_id FROM social_respects WHERE de_id=? AND dia=?', (yo, hoy)).fetchone()
        if dado:
            a = _social_usuario(conn, dado['para_id'])
            return jsonify({'status': 'error', 'message': f'Hoy ya le diste tu respect a {a["nombre"] if a else "un amigo"}. Mañana tienes otro.'}), 409
        conn.execute('INSERT INTO social_respects (de_id, para_id, dia, creada_at) VALUES (?, ?, ?, ?)',
                     (yo, uid, hoy, _utcnow_iso()))
        quien = _social_usuario(conn, yo)['nombre']
        _notificar(conn, uid, 'respect', f'{quien} te dio su respect por tu gusto musical. 🤘', actor_id=yo, datos={'user_id': yo})
        conn.commit()
        total = conn.execute('SELECT COUNT(*) FROM social_respects WHERE para_id=?', (uid,)).fetchone()[0]
        return jsonify({'status': 'ok', 'total': total})
    finally:
        conn.close()


@app.route('/api/v1/social/recomendar', methods=['POST'])
@api_login_required
def api_v1_social_recomendar():
    """Recomendar una pista a TODOS mis amigos. {track_id}. La misma pista una
    vez al día y máximo 5 recomendaciones al día."""
    yo = g.api_user['id']
    try:
        tid = int((request.get_json(silent=True) or {}).get('track_id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Falta la pista.'}), 400
    conn = get_db_connection()
    try:
        pista = _social_pista(conn, tid)
        if not pista:
            return jsonify({'status': 'error', 'message': 'Esa pista no existe.'}), 404
        amigos = _social_amigos_ids(conn, yo)
        if not amigos:
            return jsonify({'status': 'error', 'message': 'Todavía no tienes amigos a quienes recomendarles.'}), 409
        hoy = _social_hoy()
        if conn.execute('SELECT 1 FROM social_recomendaciones WHERE de_id=? AND track_id=? AND dia=?', (yo, tid, hoy)).fetchone():
            return jsonify({'status': 'error', 'message': 'Ya recomendaste esta pista hoy.'}), 409
        n_hoy = conn.execute('SELECT COUNT(*) FROM social_recomendaciones WHERE de_id=? AND dia=?', (yo, hoy)).fetchone()[0]
        if n_hoy >= _RECOMENDACIONES_POR_DIA:
            return jsonify({'status': 'error', 'message': f'Llegaste al máximo de {_RECOMENDACIONES_POR_DIA} recomendaciones por hoy.'}), 409
        rid = conn.execute('INSERT INTO social_recomendaciones (de_id, track_id, dia, creada_at) VALUES (?, ?, ?, ?)',
                           (yo, tid, hoy, _utcnow_iso())).lastrowid
        quien = _social_usuario(conn, yo)['nombre']
        enviados = 0
        for uid in amigos:
            if _notificar(conn, uid, 'recomendacion', f'{quien} te recomienda {_social_titulo(pista)}.',
                          actor_id=yo, datos={'recomendacion_id': rid, 'track_id': tid, 'pista': pista}):
                enviados += 1
        conn.commit()
        return jsonify({'status': 'ok', 'enviados': enviados, 'amigos': len(amigos),
                        'restantes': _RECOMENDACIONES_POR_DIA - n_hoy - 1, 'titulo': pista['title']})
    finally:
        conn.close()


@app.route('/api/v1/social/recomendacion/<int:rid>/escuchada', methods=['POST'])
@api_login_required
def api_v1_social_recomendacion_escuchada(rid):
    """La app avisa que se tocó «▶ Escuchar» o «Agregar a la cola» (dashboard
    de quien recomendó: recomendaciones más escuchadas)."""
    conn = get_db_connection()
    try:
        if conn.execute('SELECT 1 FROM social_recomendaciones WHERE id=?', (rid,)).fetchone():
            conn.execute('INSERT OR IGNORE INTO social_recomendacion_escuchas (recomendacion_id, usuario_id, creada_at) '
                         'VALUES (?, ?, ?)', (rid, g.api_user['id'], _utcnow_iso()))
            conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


@app.route('/api/v1/social/dashboard')
@api_login_required
def api_v1_social_dashboard():
    """Dashboard de mi perfil: lo que generé entre mis amigos."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        c = _social_contadores(conn, yo)
        c['likes_playlists'] = conn.execute("SELECT COUNT(*) FROM social_likes WHERE para_id=? AND tipo='playlist'", (yo,)).fetchone()[0]
        c['copias'] = conn.execute('SELECT COUNT(*) FROM playlists p JOIN playlists o ON o.id=p.copia_de '
                                   'WHERE o.user_id=? AND p.user_id<>?', (yo, yo)).fetchone()[0]
        c['escuchas_playlists'] = conn.execute('SELECT COUNT(*) FROM playlist_escuchas e JOIN playlists p ON p.id=e.playlist_id '
                                               'WHERE p.user_id=?', (yo,)).fetchone()[0]
        c['recomendaciones_escuchadas'] = conn.execute(
            'SELECT COUNT(*) FROM social_recomendacion_escuchas e JOIN social_recomendaciones r ON r.id=e.recomendacion_id '
            'WHERE r.de_id=?', (yo,)).fetchone()[0]
        top = []
        for r in conn.execute('SELECT r.track_id, COUNT(e.usuario_id) AS n FROM social_recomendaciones r '
                              'JOIN social_recomendacion_escuchas e ON e.recomendacion_id=r.id WHERE r.de_id=? '
                              'GROUP BY r.track_id ORDER BY n DESC, MAX(e.creada_at) DESC LIMIT 5', (yo,)).fetchall():
            p = _social_pista(conn, r['track_id'])
            if p:
                p['escuchas'] = r['n']
                top.append(p)
        playlists = [dict(r) for r in conn.execute(
            "SELECT p.id, p.name, (SELECT COUNT(*) FROM playlist_escuchas e WHERE e.playlist_id=p.id) AS escuchas, "
            "(SELECT COUNT(*) FROM social_likes l WHERE l.tipo='playlist' AND l.ref_id=p.id) AS likes, "
            "(SELECT COUNT(*) FROM playlists x WHERE x.copia_de=p.id AND x.user_id<>p.user_id) AS copias "
            "FROM playlists p WHERE p.user_id=? AND EXISTS (SELECT 1 FROM playlist_compartidas pc WHERE pc.playlist_id=p.id) "
            "ORDER BY escuchas DESC, likes DESC LIMIT 5", (yo,)).fetchall()]
        hoy = _social_hoy()
        dado = conn.execute('SELECT para_id FROM social_respects WHERE de_id=? AND dia=?', (yo, hoy)).fetchone()
        n_hoy = conn.execute('SELECT COUNT(*) FROM social_recomendaciones WHERE de_id=? AND dia=?', (yo, hoy)).fetchone()[0]
        return jsonify({'status': 'ok', 'contadores': c, 'top_recomendaciones': top, 'playlists': playlists,
                        'hoy': {'recomendaciones_restantes': max(0, _RECOMENDACIONES_POR_DIA - n_hoy),
                                'respect_disponible': not dado}})
    finally:
        conn.close()


@app.route('/api/v1/playlists/<int:playlist_id>/like', methods=['POST'])
@api_login_required
def api_v1_playlist_like(playlist_id):
    """❤️ a una playlist compartida (alterna). El dueño no se da like a sí mismo."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        pl, rol = _playlist_acceso(conn, playlist_id, yo)
        if not pl:
            return jsonify({'status': 'error', 'message': 'No encontré esa playlist.'}), 404
        if rol == 'dueno':
            return jsonify({'status': 'error', 'message': 'Es tu playlist.'}), 409
        previo = conn.execute("SELECT id FROM social_likes WHERE tipo='playlist' AND ref_id=? AND de_id=?",
                              (playlist_id, yo)).fetchone()
        if previo:
            conn.execute('DELETE FROM social_likes WHERE id=?', (previo['id'],))
        else:
            conn.execute("INSERT INTO social_likes (de_id, para_id, tipo, ref_id, dia, creada_at) VALUES (?, ?, 'playlist', ?, ?, ?)",
                         (yo, pl['user_id'], playlist_id, _social_hoy(), _utcnow_iso()))
            quien = _social_usuario(conn, yo)['nombre']
            _notificar(conn, pl['user_id'], 'like', f'A {quien} le gustó tu playlist «{pl["name"]}».',
                       actor_id=yo, datos={'playlist_id': playlist_id})
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM social_likes WHERE tipo='playlist' AND ref_id=?", (playlist_id,)).fetchone()[0]
        return jsonify({'status': 'ok', 'me_gusta': not previo, 'likes': total})
    finally:
        conn.close()


@app.route('/api/v1/playlists/<int:playlist_id>/comentarios', methods=['GET', 'POST'])
@api_login_required
def api_v1_playlist_comentarios(playlist_id):
    """Comentarios de una playlist compartida. POST {texto} agrega; POST
    {borrar: id} borra (el autor o el dueño)."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        pl, rol = _playlist_acceso(conn, playlist_id, yo)
        if not pl:
            return jsonify({'status': 'error', 'message': 'No encontré esa playlist.'}), 404
        if request.method == 'POST':
            datos = request.get_json(silent=True) or {}
            if datos.get('borrar'):
                try:
                    cid = int(datos['borrar'])
                except (TypeError, ValueError):
                    cid = 0
                conn.execute('DELETE FROM playlist_comentarios WHERE id=? AND playlist_id=? AND (usuario_id=? OR ?)',
                             (cid, playlist_id, yo, 1 if rol == 'dueno' else 0))
            else:
                texto = ' '.join(str(datos.get('texto') or '').split())[:500]
                if not texto:
                    return jsonify({'status': 'error', 'message': 'Escribe un comentario.'}), 400
                conn.execute('INSERT INTO playlist_comentarios (playlist_id, usuario_id, texto, creada_at) VALUES (?, ?, ?, ?)',
                             (playlist_id, yo, texto, _utcnow_iso()))
                quien = _social_usuario(conn, yo)['nombre']
                destinos = {pl['user_id']} | {r[0] for r in conn.execute(
                    'SELECT usuario_id FROM playlist_compartidas WHERE playlist_id=?', (playlist_id,))}
                destinos.discard(yo)
                corto = texto if len(texto) <= 80 else texto[:77] + '…'
                for uid in destinos:
                    _notificar(conn, uid, 'playlist_comentario', f'{quien} comentó en «{pl["name"]}»: {corto}',
                               actor_id=yo, datos={'playlist_id': playlist_id})
            conn.commit()
        cache = {}
        items = []
        for r in conn.execute('SELECT * FROM playlist_comentarios WHERE playlist_id=? ORDER BY id', (playlist_id,)).fetchall():
            if r['usuario_id'] not in cache:
                cache[r['usuario_id']] = _social_usuario(conn, r['usuario_id'])
            items.append({'id': r['id'], 'texto': r['texto'], 'creada': r['creada_at'], 'autor': cache[r['usuario_id']],
                          'puedo_borrar': r['usuario_id'] == yo or rol == 'dueno'})
        return jsonify({'status': 'ok', 'items': items})
    finally:
        conn.close()

# ══ Ticket C-11 (pedido por Niko): Descubrir ═══════════════════════════════════
# El servidor recolecta a diario de fuentes gratuitas (descubrir_fuentes.py) y
# guarda BORRADORES de publicación. Ciclo semanal: el lote cierra el sábado en
# la noche, el admin lo revisa el domingo en su Editor de Descubrir y lo
# aprobado se publica el lunes 00:01 (avisos a los usuarios a las 09:00). Modo
# por publicación: general (todos), favoritos (solo a quienes tienen ese
# artista en favoritos: aviso + «Para ti») o ambos. Todo vence y se borra solo
# (almacenamiento liviano: sin imágenes guardadas, solo enlaces). Especificación:
# claude/especificacion-c11-descubrir.md (proyecto).

import descubrir_fuentes as _dsc_fuentes
import integraciones as _integ   # Ticket 42b

DESCUBRIR_CIUDADES = ['Santiago', 'Valparaíso', 'Viña del Mar', 'Concepción', 'La Serena/Coquimbo']
_DSC_TM_CIUDADES = {'Santiago': ['Santiago'], 'Valparaíso': ['Valparaíso', 'Valparaiso'],
                    'Viña del Mar': ['Viña del Mar', 'Vina del Mar'], 'Concepción': ['Concepción', 'Concepcion'],
                    'La Serena/Coquimbo': ['La Serena', 'Coquimbo']}
_DSC_MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre',
              'octubre', 'noviembre', 'diciembre']
_DSC_ARTISTAS_POR_DIA = int(os.environ.get('DESCUBRIR_ARTISTAS_POR_DIA', '400') or 400)
_DSC_DISCOS_MB_POR_DIA = int(os.environ.get('DESCUBRIR_DISCOS_MB_POR_DIA', '300') or 300)
_DSC_MAX_LANZAMIENTOS_LOTE = 60
_DSC_VISTO_DIAS = 90

_dsc_estado = {'corriendo': False, 'paso': '', 'inicio': None, 'fin': None, 'creados': 0}
_dsc_lock = threading.Lock()
_dsc_hilo = None


def _dsc_migrar(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS descubrir_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, clave TEXT UNIQUE NOT NULL, tipo TEXT NOT NULL,
            semana TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'borrador', modo TEXT,
            titulo TEXT NOT NULL, texto TEXT, imagen TEXT, enlace TEXT, fecha TEXT,
            artista_id INTEGER, album_id INTEGER, en_orbyte INTEGER NOT NULL DEFAULT 0,
            afectados INTEGER NOT NULL DEFAULT 0, relevancia REAL NOT NULL DEFAULT 0, datos_json TEXT,
            publicar_desde TEXT, avisar_desde TEXT, vence TEXT, avisado INTEGER NOT NULL DEFAULT 0,
            creada_at TEXT NOT NULL, publicada_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_descubrir_items_estado ON descubrir_items (estado, semana);
        CREATE TABLE IF NOT EXISTS descubrir_vistos (clave TEXT PRIMARY KEY, hasta TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS descubrir_artistas_ext (
            artista_id INTEGER PRIMARY KEY, deezer_id INTEGER, revisado_at TEXT);
        CREATE TABLE IF NOT EXISTS descubrir_albums_ext (album_id INTEGER PRIMARY KEY, fecha TEXT, revisado_at TEXT);
        CREATE TABLE IF NOT EXISTS descubrir_meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS material_pedidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT, clave TEXT UNIQUE NOT NULL, tipo TEXT, artista TEXT,
            titulo TEXT, imagen TEXT, enlace TEXT, estado TEXT NOT NULL DEFAULT 'pendiente',
            creada_at TEXT NOT NULL, resuelto_at TEXT);
        CREATE TABLE IF NOT EXISTS material_pedido_usuarios (
            pedido_id INTEGER NOT NULL, usuario_id INTEGER NOT NULL, creada_at TEXT NOT NULL,
            PRIMARY KEY (pedido_id, usuario_id));
    ''')
    # C-11c: huella de contenido para no duplicar (la misma novedad con otro id de la fuente).
    cols = {r[1] for r in conn.execute('PRAGMA table_info(descubrir_items)').fetchall()}
    if 'huella' not in cols:
        conn.execute('ALTER TABLE descubrir_items ADD COLUMN huella TEXT')
        for r in conn.execute('SELECT * FROM descubrir_items').fetchall():
            conn.execute('UPDATE descubrir_items SET huella=? WHERE id=?', (_dsc_huella(dict(r)), r['id']))
    conn.execute('CREATE INDEX IF NOT EXISTS idx_descubrir_items_huella ON descubrir_items (huella)')


# ---------------------------------------------------------------- tiempo y lotes

def _dsc_ahora():
    return datetime.now()   # hora local del servidor (Chile)


def _dsc_iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


def _dsc_lote_que_junta(d):
    """Lunes de publicación del lote que está juntando el día d: lo del lunes
    al sábado va al lote del lunes siguiente; lo del domingo, al del otro."""
    return d + timedelta(days=(5 - d.weekday()) % 7 + 2)


def _dsc_lote_en_revision(d):
    """Próximo lunes (el lote que el admin revisa/aprueba)."""
    return d + timedelta(days=(7 - d.weekday()) or 7)


def _dsc_fecha_txt(iso, con_anio=True):
    try:
        f = datetime.strptime(iso[:10], '%Y-%m-%d')
    except (TypeError, ValueError):
        return iso or ''
    return f'{f.day} de {_DSC_MESES[f.month - 1]}' + (f' de {f.year}' if con_anio else '')


def _dsc_meta(conn, k, v=None):
    if v is None:
        row = conn.execute('SELECT v FROM descubrir_meta WHERE k=?', (k,)).fetchone()
        return row['v'] if row else None
    conn.execute('INSERT INTO descubrir_meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, str(v)))


# ---------------------------------------------------------------- biblioteca y favoritos

def _dsc_fav_artistas(conn):
    """artista_id -> set(user_id) (favoritos de artista, de sus discos o pistas)."""
    out = {}
    for r in conn.execute('''
        SELECT user_id, item_id AS aid FROM user_item_favorites WHERE item_type='artist'
        UNION SELECT f.user_id, al.artist_id FROM user_item_favorites f JOIN albums al ON al.id=f.item_id
              WHERE f.item_type='album'
        UNION SELECT f.user_id, al.artist_id FROM user_item_favorites f JOIN tracks t ON t.id=f.item_id
              JOIN albums al ON al.id=t.album_id WHERE f.item_type='track' ''').fetchall():
        if r['aid'] is not None:
            out.setdefault(r['aid'], set()).add(r['user_id'])
    return out


def _dsc_popularidad(conn, artista_id):
    try:
        row = conn.execute('SELECT lastfm_listeners FROM artists WHERE id=?', (artista_id,)).fetchone()
        return float(row['lastfm_listeners'] or 0) if row else 0.0
    except Exception:
        return 0.0


def _dsc_discos_biblioteca(conn, artista_id):
    """{titulo normalizado: album_id} de un artista de la biblioteca."""
    return {_dsc_fuentes.normalizar(r['name']): r['id']
            for r in conn.execute('SELECT id, name FROM albums WHERE artist_id=?', (artista_id,)).fetchall()}


def _dsc_huella(item):
    """Identidad del CONTENIDO (no del id de la fuente): la misma novedad que
    llega dos veces (Deezer publica el mismo disco con varios ids, Ticketmaster
    repite un recital en dos ciudades o con dos fichas) tiene la misma huella."""
    d = item.get('datos') if isinstance(item.get('datos'), dict) else json.loads(item.get('datos_json') or '{}')
    n = _dsc_fuentes.normalizar
    tipo = item.get('tipo')
    if tipo == 'lanzamiento':
        return f'lanz|{item.get("artista_id")}|{n(d.get("disco") or item.get("titulo"))}'
    if tipo == 'evento':
        return f'evt|{item.get("artista_id")}|{item.get("fecha")}|{n(d.get("recinto") or d.get("ciudad"))}'
    if tipo == 'efemeride':
        quien = item.get('album_id') or item.get('artista_id')
        return f'efe|{"disco" if item.get("album_id") else d.get("motivo") or ""}|{quien}|{(item.get("fecha") or "")[:10]}'
    if tipo == 'similar':
        return f'sim|{n(d.get("artista"))}'
    return item.get('clave')


def _dsc_crear(conn, item):
    """Inserta un borrador si no existe ya (por clave ni por contenido) ni se
    vio hace poco. True si se creó."""
    item['huella'] = _dsc_huella(item)
    if conn.execute('SELECT 1 FROM descubrir_vistos WHERE clave IN (?, ?)', (item['clave'], item['huella'])).fetchone():
        return False
    if conn.execute('SELECT 1 FROM descubrir_items WHERE huella=?', (item['huella'],)).fetchone():
        return False
    datos = item.pop('datos', None)
    cols = ['clave', 'huella', 'tipo', 'semana', 'titulo', 'texto', 'imagen', 'enlace', 'fecha', 'artista_id', 'album_id',
            'en_orbyte', 'afectados', 'relevancia']
    vals = [item.get(c) for c in cols]
    try:
        conn.execute(f'INSERT INTO descubrir_items ({",".join(cols)}, datos_json, creada_at) '
                     f'VALUES ({",".join("?" * len(cols))}, ?, ?)',
                     vals + [json.dumps(datos or {}, ensure_ascii=False), _dsc_iso(_dsc_ahora())])
        return True
    except sqlite3.IntegrityError:
        return False


def _dsc_paso(texto):
    _dsc_estado['paso'] = texto
    app.logger.info(f'[descubrir] {texto}')


# ---------------------------------------------------------------- recolección

def _dsc_recolectar_lanzamientos(conn, hoy, lote, favs):
    pendientes = conn.execute("SELECT COUNT(*) FROM descubrir_items WHERE tipo='lanzamiento' AND semana=?",
                              (lote,)).fetchone()[0]
    if pendientes >= _DSC_MAX_LANZAMIENTOS_LOTE:
        return 0
    limite_rev = _dsc_iso(_dsc_ahora() - timedelta(hours=20))
    fav_ids = list(favs.keys())
    ids = []
    if fav_ids:
        marcas = ','.join('?' * len(fav_ids))
        ids += [r['id'] for r in conn.execute(
            f'SELECT a.id FROM artists a LEFT JOIN descubrir_artistas_ext e ON e.artista_id=a.id '
            f'WHERE a.id IN ({marcas}) AND (e.revisado_at IS NULL OR e.revisado_at < ?)', fav_ids + [limite_rev])]
    ids += [r['id'] for r in conn.execute(
        'SELECT a.id FROM artists a LEFT JOIN descubrir_artistas_ext e ON e.artista_id=a.id '
        'WHERE (e.revisado_at IS NULL OR e.revisado_at < ?) ORDER BY e.revisado_at IS NOT NULL, e.revisado_at '
        'LIMIT ?', (limite_rev, _DSC_ARTISTAS_POR_DIA))]
    vistos, orden = set(), []
    for i in ids:
        if i not in vistos:
            vistos.add(i)
            orden.append(i)
    desde, hasta = (hoy - timedelta(days=30)).isoformat(), (hoy + timedelta(days=14)).isoformat()
    creados = 0
    for n, aid in enumerate(orden[:_DSC_ARTISTAS_POR_DIA + len(fav_ids)]):
        ar = conn.execute('SELECT id, name FROM artists WHERE id=?', (aid,)).fetchone()
        if not ar or not ar['name']:
            continue
        if n % 25 == 0:
            _dsc_paso(f'Lanzamientos: {n} de {len(orden)} artistas revisados')
        ext = conn.execute('SELECT deezer_id, revisado_at FROM descubrir_artistas_ext WHERE artista_id=?', (aid,)).fetchone()
        dz = ext['deezer_id'] if ext else None
        if dz is None or (dz == 0 and (ext['revisado_at'] or '') < _dsc_iso(_dsc_ahora() - timedelta(days=30))):
            dz = _dsc_fuentes.deezer_buscar_artista(ar['name'])
            if dz is None:
                continue   # Deezer no respondió: se reintenta otro día
        conn.execute('INSERT INTO descubrir_artistas_ext (artista_id, deezer_id, revisado_at) VALUES (?, ?, ?) '
                     'ON CONFLICT(artista_id) DO UPDATE SET deezer_id=excluded.deezer_id, revisado_at=excluded.revisado_at',
                     (aid, dz, _dsc_iso(_dsc_ahora())))
        conn.commit()
        if not dz:
            continue
        propios = None
        for d in _dsc_fuentes.deezer_discos(dz):
            if not (desde <= d['fecha'] <= hasta) or d['tipo'] not in ('album', 'ep'):
                continue
            if propios is None:
                propios = _dsc_discos_biblioteca(conn, aid)
            album_id = propios.get(_dsc_fuentes.normalizar(d['titulo']))
            if album_id and re.search(r'remaster|deluxe|edition|edición|anniversary|aniversario|expanded|live',
                                      d['titulo'], re.I):
                continue   # reedición de algo que ya está en la biblioteca: no es novedad
            tipo_txt = 'EP' if d['tipo'] == 'ep' else 'disco'
            futuro = d['fecha'] > hoy.isoformat()
            afect = len(favs.get(aid, ()))
            rel = 50 + afect * 20 + math.log10(_dsc_popularidad(conn, aid) + 1) * 4 + (5 if album_id else 0)
            if _dsc_crear(conn, {
                'clave': f'lanz:dz:{d["id"]}', 'tipo': 'lanzamiento', 'semana': lote,
                'titulo': f'Nuevo {tipo_txt} de {ar["name"]}',
                'texto': f'{ar["name"]} {"lanzará" if futuro else "lanzó"} «{d["titulo"]}» el {_dsc_fecha_txt(d["fecha"])}.'
                         + (' Ya está en Orbyte.' if album_id else ''),
                'imagen': d['portada'], 'enlace': d['enlace'], 'fecha': d['fecha'], 'artista_id': aid,
                'album_id': album_id, 'en_orbyte': 1 if album_id else 0, 'afectados': afect, 'relevancia': rel,
                'datos': {'artista': ar['name'], 'disco': d['titulo'], 'formato': d['tipo'], 'pedido_clave': f'lanz:dz:{d["id"]}'},
            }):
                creados += 1
        conn.commit()
        if creados + pendientes >= _DSC_MAX_LANZAMIENTOS_LOTE:
            break
    return creados


def _dsc_recolectar_efemerides(conn, lote, favs):
    lunes = datetime.strptime(lote, '%Y-%m-%d').date()
    dias = [lunes + timedelta(days=i) for i in range(7)]
    creados = 0
    # 1) Fecha exacta de los discos de la biblioteca (MusicBrainz, de a poco y una sola vez por disco).
    fav_ids = list(favs.keys()) or [-1]
    marcas = ','.join('?' * len(fav_ids))
    try:
        orden_pop = 'COALESCE(CAST(ar.lastfm_listeners AS REAL), 0) DESC'
        conn.execute(f'SELECT {orden_pop} FROM artists ar LIMIT 1')
    except Exception:
        orden_pop = 'ar.id'
    filas = conn.execute(
        f'SELECT al.id, al.name, ar.name AS artista FROM albums al JOIN artists ar ON ar.id=al.artist_id '
        f'LEFT JOIN descubrir_albums_ext e ON e.album_id=al.id WHERE e.album_id IS NULL '
        f'ORDER BY (al.artist_id IN ({marcas})) DESC, {orden_pop} LIMIT ?', fav_ids + [_DSC_DISCOS_MB_POR_DIA]).fetchall()
    for n, r in enumerate(filas):
        if n % 25 == 0:
            _dsc_paso(f'Un día como hoy: fechas de discos {n} de {len(filas)}')
        fecha = _dsc_fuentes.musicbrainz_fecha_disco(r['artista'], r['name'])
        if fecha is None:
            continue
        conn.execute('INSERT OR REPLACE INTO descubrir_albums_ext (album_id, fecha, revisado_at) VALUES (?, ?, ?)',
                     (r['id'], fecha or '-', _dsc_iso(_dsc_ahora())))
        if n % 20 == 0:
            conn.commit()
    conn.commit()
    # 2) Aniversarios redondos (cada 5 años) de discos de la biblioteca en la semana del lote.
    for dia in dias:
        sufijo = dia.strftime('-%m-%d')
        for r in conn.execute(
                'SELECT al.id, al.name, al.cover_path, al.artist_id, ar.name AS artista, e.fecha FROM descubrir_albums_ext e '
                'JOIN albums al ON al.id=e.album_id JOIN artists ar ON ar.id=al.artist_id WHERE e.fecha LIKE ?',
                ('%' + sufijo,)).fetchall():
            anios = dia.year - int(r['fecha'][:4])
            if anios < 5 or anios % 5:
                continue
            afect = len(favs.get(r['artist_id'], ()))
            rel = 40 + afect * 20 + math.log10(_dsc_popularidad(conn, r['artist_id']) + 1) * 4 + \
                (15 if anios % 25 == 0 else 8 if anios % 10 == 0 else 0)
            if _dsc_crear(conn, {
                'clave': f'efe:disco:{r["id"]}:{dia.year}', 'tipo': 'efemeride', 'semana': lote,
                'titulo': f'Hace {anios} años: «{r["name"]}»',
                'texto': f'Un {_dsc_fecha_txt(dia.isoformat(), False)} de {r["fecha"][:4]}, hace {anios} años, '
                         f'{r["artista"]} publicaba «{r["name"]}».',
                'imagen': cover_url_filter(clean_db_path(r['cover_path'])), 'enlace': '', 'fecha': dia.isoformat(),
                'artista_id': r['artist_id'], 'album_id': r['id'], 'en_orbyte': 1, 'afectados': afect, 'relevancia': rel,
                'datos': {'artista': r['artista'], 'disco': r['name'], 'anios': anios},
            }):
                creados += 1
    conn.commit()
    # 3) Nacimientos y muertes de músicos de la biblioteca (Wikimedia).
    por_nombre = {_dsc_fuentes.normalizar(r['name']): r['id'] for r in conn.execute('SELECT id, name FROM artists')}
    for dia in dias:
        _dsc_paso(f'Un día como hoy: {_dsc_fecha_txt(dia.isoformat(), False)}')
        for tipo in ('births', 'deaths'):
            if _dsc_meta(conn, f'wiki:{tipo}:{dia.isoformat()}'):
                continue
            lista = _dsc_fuentes.wikimedia_efemerides(dia.month, dia.day, tipo)
            if lista is None:
                continue
            for p in lista:
                aid = por_nombre.get(_dsc_fuentes.normalizar(p['nombre']))
                if not aid or not p.get('anio'):
                    continue
                anios = dia.year - int(p['anio'])
                verbo = 'nacía' if tipo == 'births' else 'fallecía'
                afect = len(favs.get(aid, ()))
                rel = 35 + afect * 20 + math.log10(_dsc_popularidad(conn, aid) + 1) * 4 + (10 if anios % 10 == 0 else 0)
                if _dsc_crear(conn, {
                    'clave': f'efe:wiki:{tipo}:{aid}:{dia.year}', 'tipo': 'efemeride', 'semana': lote,
                    'titulo': f'Hace {anios} años {verbo} {p["nombre"]}',
                    'texto': f'Un {_dsc_fecha_txt(dia.isoformat(), False)} de {p["anio"]} {verbo} {p["nombre"]}'
                             + (f', {p["descripcion"]}.' if p.get('descripcion') else '.'),
                    'imagen': p.get('imagen') or '', 'enlace': p.get('enlace') or '', 'fecha': dia.isoformat(),
                    'artista_id': aid, 'album_id': None, 'en_orbyte': 1, 'afectados': afect, 'relevancia': rel,
                    'datos': {'artista': p['nombre'], 'anios': anios, 'motivo': tipo},
                }):
                    creados += 1
            _dsc_meta(conn, f'wiki:{tipo}:{dia.isoformat()}', '1')
            conn.commit()
    return creados


def _dsc_recolectar_eventos(conn, hoy, lote, favs):
    clave = _dsc_fuentes.claves()['ticketmaster']
    if not clave:
        return 0
    por_nombre = {_dsc_fuentes.normalizar(r['name']): r['id'] for r in conn.execute('SELECT id, name FROM artists')}
    limite = (hoy + timedelta(days=120)).isoformat()
    creados = 0
    for ciudad, nombres in _DSC_TM_CIUDADES.items():
        _dsc_paso(f'Eventos: {ciudad}')
        vistos = set()
        for nombre_tm in nombres:
            for ev in _dsc_fuentes.ticketmaster_eventos(nombre_tm, clave):
                if ev['id'] in vistos or not ev['fecha'] or not (hoy.isoformat() <= ev['fecha'] <= limite):
                    continue
                vistos.add(ev['id'])
                aid = next((por_nombre[n] for n in (_dsc_fuentes.normalizar(a) for a in ev['artistas'] + [ev['nombre']])
                            if n in por_nombre), None)
                if not aid:
                    continue
                artista = conn.execute('SELECT name FROM artists WHERE id=?', (aid,)).fetchone()['name']
                afect = len(favs.get(aid, ()))
                dias_falta = (datetime.strptime(ev['fecha'], '%Y-%m-%d').date() - hoy).days
                rel = 45 + afect * 20 + math.log10(_dsc_popularidad(conn, aid) + 1) * 4 + max(0, 20 - dias_falta / 3)
                if _dsc_crear(conn, {
                    'clave': f'evt:tm:{ev["id"]}', 'tipo': 'evento', 'semana': lote,
                    'titulo': f'{artista} en {ciudad}',
                    'texto': f'{ev["nombre"]} · {_dsc_fecha_txt(ev["fecha"])}' + (f' · {ev["recinto"]}' if ev['recinto'] else ''),
                    'imagen': ev['imagen'], 'enlace': ev['enlace'], 'fecha': ev['fecha'], 'artista_id': aid,
                    'album_id': None, 'en_orbyte': 1, 'afectados': afect, 'relevancia': rel,
                    'datos': {'artista': artista, 'ciudad': ciudad, 'recinto': ev['recinto'], 'evento': ev['nombre']},
                }):
                    creados += 1
        conn.commit()
    return creados


def _dsc_recolectar_semanales(conn, lote, favs):
    """Tops y «te podría gustar»: una vez por lote."""
    creados = 0
    if _dsc_meta(conn, f'semanales:{lote}'):
        return 0
    _dsc_paso('Tops de la semana')
    mapa = {}
    for r in conn.execute('SELECT al.id, al.name, ar.name AS artista FROM albums al JOIN artists ar ON ar.id=al.artist_id'):
        mapa[_dsc_fuentes.normalizar(r['artista']) + '|' + _dsc_fuentes.normalizar(r['name'])] = r['id']
    artistas = {_dsc_fuentes.normalizar(r['name']) for r in conn.execute('SELECT name FROM artists')}
    top = _dsc_fuentes.deezer_top_discos(10)
    if top:
        lista = []
        for t in top:
            aid = mapa.get(_dsc_fuentes.normalizar(t['artista']) + '|' + _dsc_fuentes.normalizar(t['titulo']))
            lista.append({'titulo': t['titulo'], 'artista': t['artista'], 'portada': t['portada'], 'album_id': aid,
                          'en_orbyte': bool(aid)})
        n = sum(1 for x in lista if x['en_orbyte'])
        if _dsc_crear(conn, {
            'clave': f'top:dz:{lote}', 'tipo': 'top', 'semana': lote, 'titulo': 'Top 10 de discos en el mundo',
            'texto': f'Los discos más escuchados esta semana. En Orbyte tienes {n} de los {len(lista)}.',
            'imagen': lista[0]['portada'], 'enlace': 'https://www.deezer.com/charts', 'fecha': lote,
            'artista_id': None, 'album_id': None, 'en_orbyte': 1 if n else 0, 'afectados': 0, 'relevancia': 30,
            'datos': {'lista': lista, 'fuente': 'Deezer'},
        }):
            creados += 1
    top_cl = _dsc_fuentes.lastfm_top_chile(_dsc_fuentes.claves()['lastfm'], 10)
    if top_cl:
        lista = [{'titulo': t['titulo'], 'artista': t['artista'], 'portada': '', 'album_id': None,
                  'en_orbyte': _dsc_fuentes.normalizar(t['artista']) in artistas} for t in top_cl]
        n = sum(1 for x in lista if x['en_orbyte'])
        if _dsc_crear(conn, {
            'clave': f'top:lfm:cl:{lote}', 'tipo': 'top', 'semana': lote, 'titulo': 'Top 10 de canciones en Chile',
            'texto': f'Lo más escuchado en Chile esta semana (Last.fm). De {n} de estos artistas hay música en Orbyte.',
            'imagen': '', 'enlace': 'https://www.last.fm/places/chile', 'fecha': lote, 'artista_id': None,
            'album_id': None, 'en_orbyte': 1 if n else 0, 'afectados': 0, 'relevancia': 30,
            'datos': {'lista': lista, 'fuente': 'Last.fm'},
        }):
            creados += 1
    # Te podría gustar: artistas parecidos a los favoritos de los usuarios que no están en la biblioteca.
    _dsc_paso('Te podría gustar')
    for aid, usuarios in sorted(favs.items(), key=lambda kv: -len(kv[1]))[:8]:
        ext = conn.execute('SELECT deezer_id FROM descubrir_artistas_ext WHERE artista_id=?', (aid,)).fetchone()
        ar = conn.execute('SELECT name FROM artists WHERE id=?', (aid,)).fetchone()
        if not ar:
            continue
        dz = ext['deezer_id'] if ext else _dsc_fuentes.deezer_buscar_artista(ar['name'])
        if not dz:
            continue
        for rel_ar in _dsc_fuentes.deezer_relacionados(dz, 10):
            if _dsc_fuentes.normalizar(rel_ar['nombre']) in artistas:
                continue
            if conn.execute('SELECT 1 FROM descubrir_vistos WHERE clave=?', (f'art:dz:{rel_ar["id"]}',)).fetchone():
                continue
            fans = int(rel_ar.get('fans') or 0)
            if _dsc_crear(conn, {
                'clave': f'sim:dz:{rel_ar["id"]}:{lote}', 'tipo': 'similar', 'semana': lote,
                'titulo': f'Si te gusta {ar["name"]}, prueba {rel_ar["nombre"]}',
                'texto': f'{rel_ar["nombre"]} no está en Orbyte' + (f' y tiene {fans:,} seguidores en Deezer.'.replace(',', '.') if fans else '.'),
                'imagen': rel_ar['imagen'], 'enlace': rel_ar['enlace'], 'fecha': lote, 'artista_id': aid,
                'album_id': None, 'en_orbyte': 0, 'afectados': len(usuarios),
                'relevancia': 25 + len(usuarios) * 15 + math.log10(fans + 1) * 2,
                'datos': {'artista': rel_ar['nombre'], 'parecido_a': ar['name'], 'pedido_clave': f'art:dz:{rel_ar["id"]}'},
            }):
                creados += 1
                conn.execute('INSERT OR REPLACE INTO descubrir_vistos (clave, hasta) VALUES (?, ?)',
                             (f'art:dz:{rel_ar["id"]}', (_dsc_ahora() + timedelta(days=60)).date().isoformat()))
            break
    _dsc_meta(conn, f'semanales:{lote}', '1')
    conn.commit()
    return creados


def _dsc_recolectar():
    """Corrida completa (en un hilo aparte). Cada fuente por separado: si una
    falla, las demás siguen."""
    with _dsc_lock:
        if _dsc_estado['corriendo']:
            return
        _dsc_estado.update({'corriendo': True, 'inicio': _dsc_iso(_dsc_ahora()), 'fin': None, 'creados': 0})
    conn = get_db_connection()
    try:
        hoy = _dsc_ahora().date()
        lote = _dsc_lote_que_junta(hoy).isoformat()
        favs = _dsc_fav_artistas(conn)
        for nombre, fn in (('lanzamientos', lambda: _dsc_recolectar_lanzamientos(conn, hoy, lote, favs)),
                           ('efemérides', lambda: _dsc_recolectar_efemerides(conn, lote, favs)),
                           ('eventos', lambda: _dsc_recolectar_eventos(conn, hoy, lote, favs)),
                           ('semanales', lambda: _dsc_recolectar_semanales(conn, lote, favs))):
            try:
                _dsc_estado['creados'] += fn() or 0
            except Exception as e:  # noqa: BLE001
                app.logger.exception(f'[descubrir] falló {nombre}: {e}')
                _dsc_fuentes.ultimo_error[nombre] = f'{type(e).__name__}: {e}'[:200]
        _dsc_meta(conn, 'ultima_recoleccion', hoy.isoformat())
        conn.commit()
    finally:
        conn.close()
        _dsc_estado.update({'corriendo': False, 'fin': _dsc_iso(_dsc_ahora()), 'paso': ''})


def _dsc_recolectar_en_hilo():
    if _dsc_estado['corriendo']:
        return False
    threading.Thread(target=_dsc_recolectar, name='descubrir-recolector', daemon=True).start()
    return True


# ---------------------------------------------------------------- publicación, avisos y limpieza

def _dsc_usuario_ciudad(conn, uid):
    row = conn.execute('SELECT settings_json FROM users WHERE id=?', (uid,)).fetchone()
    try:
        c = ((json.loads(row['settings_json']) if row and row['settings_json'] else {}).get('descubrir') or {}).get('ciudad')
    except ValueError:
        c = None
    return c if c in DESCUBRIR_CIUDADES else 'Santiago'


def _dsc_texto_aviso(it):
    d = json.loads(it['datos_json'] or '{}')
    if it['tipo'] == 'lanzamiento':
        return f'🎵 {it["texto"]}' + ('' if it['en_orbyte'] else ' ¿Te gustaría tenerlo en Orbyte?')
    if it['tipo'] == 'efemeride':
        return f'📅 Un día como hoy: {it["texto"]}'
    if it['tipo'] == 'evento':
        return f'🎤 {it["titulo"]}: {it["texto"]}'
    if it['tipo'] == 'similar':
        return f'✨ {it["titulo"]}. ¿Te gustaría tenerlo en Orbyte?'
    return it['titulo']


def _dsc_tick():
    """Cada minuto: publica lo aprobado que ya toca, manda los avisos de
    «Solo favoritos», borra lo vencido y lanza la recolección diaria."""
    ahora = _dsc_ahora()
    ah = _dsc_iso(ahora)
    conn = get_db_connection()
    try:
        conn.execute("UPDATE descubrir_items SET estado='publicado', publicada_at=? WHERE estado='aprobado' "
                     "AND publicar_desde <= ?", (ah, ah))
        pend = conn.execute("SELECT * FROM descubrir_items WHERE estado='publicado' AND avisado=0 AND avisar_desde <= ? "
                            "AND modo IN ('favoritos', 'ambos')", (ah,)).fetchall()
        if pend:
            favs = _dsc_fav_artistas(conn)
            for it in pend:
                d = json.loads(it['datos_json'] or '{}')
                for uid in favs.get(it['artista_id'], ()):
                    if it['tipo'] == 'evento' and _dsc_usuario_ciudad(conn, uid) != d.get('ciudad'):
                        continue
                    _notificar(conn, uid, 'descubrir', _dsc_texto_aviso(it),
                               datos={'descubrir_id': it['id'], 'puede_pedir': _dsc_puede_pedir(it)})
                conn.execute('UPDATE descubrir_items SET avisado=1 WHERE id=?', (it['id'],))
        conn.commit()
        if _dsc_meta(conn, 'ultima_limpieza') != ahora.strftime('%Y-%m-%dT%H'):
            _dsc_limpiar(conn, ahora)
            _dsc_meta(conn, 'ultima_limpieza', ahora.strftime('%Y-%m-%dT%H'))
            conn.commit()
        # Domingo 09:00: aviso al admin con lo que hay para revisar.
        lote = _dsc_lote_en_revision(ahora.date()).isoformat()
        if ahora.weekday() == 6 and ahora.hour >= 9 and _dsc_meta(conn, 'aviso_editor') != lote:
            n = conn.execute("SELECT COUNT(*) FROM descubrir_items WHERE estado='borrador' AND semana<=?", (lote,)).fetchone()[0]
            if n:
                for a in conn.execute('SELECT id FROM users WHERE is_admin=1').fetchall():
                    _notificar(conn, a['id'], 'sistema',
                               f'Editor de Descubrir: tienes {n} publicaciones por revisar. Lo aprobado sale el lunes a las 00:01 '
                               f'(las efemérides 12 h antes de su día y los recitales cercanos, al aprobarlos).',
                               datos={'origen': 'descubrir'})
            _dsc_meta(conn, 'aviso_editor', lote)
            conn.commit()
        recolectar = ahora.hour * 60 + ahora.minute >= 3 * 60 + 30 and \
            _dsc_meta(conn, 'ultima_recoleccion') != ahora.date().isoformat()
    finally:
        conn.close()
    if recolectar:
        _dsc_recolectar_en_hilo()


def _dsc_marcar_visto(conn, clave, dias=_DSC_VISTO_DIAS, huella=None):
    hasta = (_dsc_ahora() + timedelta(days=dias)).date().isoformat()
    for k in {clave, huella} - {None, ''}:
        conn.execute('INSERT OR REPLACE INTO descubrir_vistos (clave, hasta) VALUES (?, ?)', (k, hasta))


def _dsc_limpiar(conn, ahora):
    """Mantiene todo liviano: lo vencido, los borradores de lotes ya cerrados
    sin aprobar y los pedidos resueltos hace tiempo se borran (las claves
    quedan un tiempo en descubrir_vistos para no volver a proponerlos)."""
    ah = _dsc_iso(ahora)
    hoy = ahora.date().isoformat()
    # Vencido (publicado o aprobado que ya no alcanza a salir); borradores sin
    # aprobar: efemérides y recitales cuando pasa su día (se pueden aprobar
    # durante toda la semana), lo demás cuando sale su lote.
    viejos = conn.execute(
        "SELECT id, clave, huella FROM descubrir_items WHERE (estado IN ('publicado', 'aprobado') AND vence < ?) "
        "OR (estado='borrador' AND tipo IN ('efemeride', 'evento') AND fecha < ?) "
        "OR (estado='borrador' AND tipo NOT IN ('efemeride', 'evento') AND semana <= ?)", (ah, hoy, hoy)).fetchall()
    for r in viejos:
        _dsc_marcar_visto(conn, r['clave'], huella=r['huella'])
    conn.executemany('DELETE FROM descubrir_items WHERE id=?', [(r['id'],) for r in viejos])
    conn.execute('DELETE FROM descubrir_vistos WHERE hasta < ?', (hoy,))
    limite = (ahora - timedelta(days=60)).isoformat()
    viejos_p = [r['id'] for r in conn.execute("SELECT id FROM material_pedidos WHERE estado<>'pendiente' AND resuelto_at < ?",
                                              (limite,))]
    for pid in viejos_p:
        conn.execute('DELETE FROM material_pedido_usuarios WHERE pedido_id=?', (pid,))
        conn.execute('DELETE FROM material_pedidos WHERE id=?', (pid,))
    conn.execute('DELETE FROM descubrir_meta WHERE k LIKE ? AND k < ?', ('wiki:%', f'wiki:births:{hoy}'))
    conn.execute("DELETE FROM descubrir_meta WHERE k LIKE 'wiki:deaths:%' AND k < ?", (f'wiki:deaths:{hoy}',))
    conn.execute("DELETE FROM descubrir_meta WHERE k LIKE 'semanales:%' AND k < ?", (f'semanales:{hoy}',))


def _dsc_vigilar():
    while True:
        try:
            _dsc_tick()
        except Exception as e:  # noqa: BLE001 - el vigilante nunca se cae
            app.logger.exception(f'[descubrir] tick: {e}')
        time.sleep(60)


@app.before_request
def _dsc_asegurar_hilo():
    """El vigilante arranca con el primer pedido (solo en el proceso que
    atiende, no en el del recargador de Flask)."""
    global _dsc_hilo
    if _dsc_hilo is not None or app.config.get('TESTING'):
        return
    with _dsc_lock:
        if _dsc_hilo is None:
            _dsc_hilo = threading.Thread(target=_dsc_vigilar, name='descubrir', daemon=True)
            _dsc_hilo.start()


# ---------------------------------------------------------------- vistas

def _dsc_puede_pedir(it):
    return it['tipo'] in ('lanzamiento', 'similar') and not it['en_orbyte']


def _dsc_neg(iso):
    """Clave para ordenar fechas ISO de la más nueva a la más vieja."""
    return ''.join(chr(255 - ord(c)) for c in (iso or ''))


def _dsc_cuando(fecha):
    """«Hoy», «Mañana», «Ayer», «Hace 2 días»… para las efemérides."""
    try:
        dias = (datetime.strptime((fecha or '')[:10], '%Y-%m-%d').date() - _dsc_ahora().date()).days
    except ValueError:
        return None
    return {0: 'Hoy', 1: 'Mañana', -1: 'Ayer'}.get(dias) or (f'En {dias} días' if dias > 0 else f'Hace {-dias} días')


def _dsc_item_json(conn, it, yo=None, admin=False):
    d = json.loads(it['datos_json'] or '{}')
    out = {'id': it['id'], 'tipo': it['tipo'], 'titulo': it['titulo'], 'texto': it['texto'] or '',
           'imagen': it['imagen'] or '', 'enlace': it['enlace'] or '', 'fecha': it['fecha'],
           'en_orbyte': bool(it['en_orbyte']), 'album_id': it['album_id'], 'artista_id': it['artista_id'],
           'artista': d.get('artista'), 'ciudad': d.get('ciudad'), 'recinto': d.get('recinto'),
           'lista': d.get('lista') or [], 'fuente': d.get('fuente'), 'puede_pedir': _dsc_puede_pedir(it),
           'cuando': _dsc_cuando(it['fecha']) if it['tipo'] == 'efemeride' else None}
    if yo is not None and out['puede_pedir'] and d.get('pedido_clave'):
        out['pedido'] = bool(conn.execute(
            'SELECT 1 FROM material_pedidos p JOIN material_pedido_usuarios u ON u.pedido_id=p.id '
            'WHERE p.clave=? AND u.usuario_id=?', (d['pedido_clave'], yo)).fetchone())
    if admin:
        out.update({'estado': it['estado'], 'modo': it['modo'], 'afectados': it['afectados'],
                    'relevancia': round(it['relevancia'] or 0, 1), 'semana': it['semana'],
                    'publicar_desde': it['publicar_desde'], 'avisar_desde': it['avisar_desde'], 'vence': it['vence']})
    return out


@app.route('/api/v1/descubrir')
@api_login_required
def api_v1_descubrir():
    """Pantalla Descubrir: Un día como hoy, Para ti, General y Eventos."""
    yo = g.api_user['id']
    conn = get_db_connection()
    try:
        favs = _dsc_fav_artistas(conn)
        mios = {aid for aid, us in favs.items() if yo in us}
        hoy = _dsc_ahora().date().isoformat()
        efe, para_ti, general, eventos = [], [], [], []
        for it in conn.execute("SELECT * FROM descubrir_items WHERE estado='publicado' "
                               "ORDER BY relevancia DESC, id DESC").fetchall():
            es_mio = it['artista_id'] in mios and it['modo'] in ('favoritos', 'ambos')
            es_general = it['modo'] in ('general', 'ambos')
            if not (es_mio or es_general):
                continue
            j = _dsc_item_json(conn, it, yo)
            j['para_ti'] = es_mio
            if it['tipo'] == 'efemeride':
                efe.append(j)
            elif it['tipo'] == 'evento':
                eventos.append(j)
            elif es_mio:
                para_ti.append(j)
            elif es_general:
                general.append(j)
        eventos.sort(key=lambda e: e['fecha'] or '')
        # Un día como hoy: primero la de hoy, después la de mañana y al final
        # las de días pasados (se quedan 72 h), de la más reciente a la más vieja.
        efe.sort(key=lambda e: (0 if e['fecha'] == hoy else 1 if (e['fecha'] or '') > hoy else 2,
                                '' if (e['fecha'] or '') > hoy else _dsc_neg(e['fecha'])))
        return jsonify({'status': 'ok', 'hoy': hoy, 'hoy_txt': _dsc_fecha_txt(hoy, False),
                        'efemerides': efe, 'para_ti': para_ti, 'general': general, 'eventos': eventos,
                        'ciudad': _dsc_usuario_ciudad(conn, yo), 'ciudades': DESCUBRIR_CIUDADES})
    finally:
        conn.close()


@app.route('/api/v1/descubrir/ajustes', methods=['POST'])
@api_login_required
def api_v1_descubrir_ajustes():
    """{ciudad}."""
    yo = g.api_user['id']
    ciudad = (request.get_json(silent=True) or {}).get('ciudad')
    if ciudad not in DESCUBRIR_CIUDADES:
        return jsonify({'status': 'error', 'message': 'Ciudad desconocida.'}), 400
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT settings_json FROM users WHERE id=?', (yo,)).fetchone()
        try:
            todo = json.loads(row['settings_json']) if row and row['settings_json'] else {}
        except ValueError:
            todo = {}
        todo.setdefault('descubrir', {})['ciudad'] = ciudad
        conn.execute('UPDATE users SET settings_json=? WHERE id=?', (json.dumps(todo), yo))
        conn.commit()
        return jsonify({'status': 'ok', 'ciudad': ciudad})
    finally:
        conn.close()


@app.route('/api/v1/descubrir/pedir', methods=['POST'])
@api_login_required
def api_v1_descubrir_pedir():
    """«¿Te gustaría que estuviera en Orbyte?» {id, quiero: bool}."""
    yo = g.api_user['id']
    datos = request.get_json(silent=True) or {}
    conn = get_db_connection()
    try:
        it = conn.execute('SELECT * FROM descubrir_items WHERE id=?', (datos.get('id'),)).fetchone()
        if not it or not _dsc_puede_pedir(it):
            return jsonify({'status': 'error', 'message': 'Esa publicación ya no está disponible.'}), 404
        d = json.loads(it['datos_json'] or '{}')
        clave = d.get('pedido_clave') or it['clave']
        p = conn.execute('SELECT id FROM material_pedidos WHERE clave=?', (clave,)).fetchone()
        if datos.get('quiero', True):
            if not p:
                pid = conn.execute('INSERT INTO material_pedidos (clave, tipo, artista, titulo, imagen, enlace, creada_at) '
                                   'VALUES (?, ?, ?, ?, ?, ?, ?)',
                                   (clave, 'disco' if it['tipo'] == 'lanzamiento' else 'artista', d.get('artista'),
                                    d.get('disco') or d.get('artista'), it['imagen'], it['enlace'], _utcnow_iso())).lastrowid
            else:
                pid = p['id']
                conn.execute("UPDATE material_pedidos SET estado='pendiente', resuelto_at=NULL WHERE id=? AND estado='descartado'",
                             (pid,))
            conn.execute('INSERT OR IGNORE INTO material_pedido_usuarios (pedido_id, usuario_id, creada_at) VALUES (?, ?, ?)',
                         (pid, yo, _utcnow_iso()))
            pedido = True
        else:
            if p:
                conn.execute('DELETE FROM material_pedido_usuarios WHERE pedido_id=? AND usuario_id=?', (p['id'], yo))
            pedido = False
        conn.commit()
        return jsonify({'status': 'ok', 'pedido': pedido})
    finally:
        conn.close()


# ---------------------------------------------------------------- editor (admin)

@app.route('/api/v1/admin/descubrir/borradores')
@api_admin_required
def api_v1_admin_descubrir_borradores():
    """Lote en revisión (próximo lunes) + aprobados pendientes de salir +
    estado del recolector."""
    conn = get_db_connection()
    try:
        ahora = _dsc_ahora()
        lote = _dsc_lote_en_revision(ahora.date())
        cierre = lote - timedelta(days=2)
        items = [_dsc_item_json(conn, it, admin=True) for it in conn.execute(
            "SELECT * FROM descubrir_items WHERE estado IN ('borrador', 'aprobado') AND semana<=? "
            "ORDER BY estado='aprobado', relevancia DESC, id DESC", (lote.isoformat(),)).fetchall()]
        juntando = conn.execute("SELECT COUNT(*) FROM descubrir_items WHERE estado='borrador' AND semana>?",
                                (lote.isoformat(),)).fetchone()[0]
        en_espera = conn.execute("SELECT COUNT(*) FROM descubrir_items WHERE estado='aprobado' AND tipo<>'efemeride'"
                                 ).fetchone()[0]
        claves = _dsc_fuentes.claves()
        return jsonify({
            'status': 'ok', 'lote': lote.isoformat(), 'lote_txt': _dsc_fecha_txt(lote.isoformat(), False),
            'cierre_txt': f'sábado {_dsc_fecha_txt(cierre.isoformat(), False)} a las 23:59',
            'cerrado': ahora.weekday() == 6, 'items': items, 'siguiente_lote': juntando, 'en_espera': en_espera,
            'recolector': {'corriendo': _dsc_estado['corriendo'], 'paso': _dsc_estado['paso'],
                           'ultima': _dsc_meta(conn, 'ultima_recoleccion'), 'creados': _dsc_estado['creados'],
                           'errores': dict(_dsc_fuentes.ultimo_error),
                           'fuentes': {'Deezer': True, 'MusicBrainz': True, 'Wikimedia': True,
                                       'Ticketmaster': bool(claves['ticketmaster']), 'Last.fm': bool(claves['lastfm'])}},
        })
    finally:
        conn.close()


@app.route('/api/v1/admin/descubrir/decidir', methods=['POST'])
@api_admin_required
def api_v1_admin_descubrir_decidir():
    """{id, modo: 'general'|'favoritos'|'ambos'|null (descartar)|'borrador' (deshacer),
    titulo?, texto?, ya?: bool (publicar ahora)}."""
    datos = request.get_json(silent=True) or {}
    modo = datos.get('modo')
    conn = get_db_connection()
    try:
        it = conn.execute('SELECT * FROM descubrir_items WHERE id=?', (datos.get('id'),)).fetchone()
        if not it:
            return jsonify({'status': 'error', 'message': 'Esa publicación ya no existe.'}), 404
        titulo = ' '.join(str(datos.get('titulo') or it['titulo']).split())[:160]
        texto = str(datos.get('texto') if datos.get('texto') is not None else (it['texto'] or '')).strip()[:600]
        if modo is None:
            _dsc_marcar_visto(conn, it['clave'], huella=it['huella'])
            conn.execute('DELETE FROM descubrir_items WHERE id=?', (it['id'],))
            conn.commit()
            return jsonify({'status': 'ok', 'descartado': True})
        if modo == 'borrador':
            conn.execute("UPDATE descubrir_items SET estado='borrador', modo=NULL, publicar_desde=NULL, avisar_desde=NULL, "
                         "vence=NULL, titulo=?, texto=? WHERE id=? AND estado IN ('aprobado', 'publicado')",
                         (titulo, texto, it['id']))
            conn.commit()
            return jsonify({'status': 'ok', 'item': _dsc_item_json(conn, conn.execute(
                'SELECT * FROM descubrir_items WHERE id=?', (it['id'],)).fetchone(), admin=True)})
        if modo not in ('general', 'favoritos', 'ambos'):
            return jsonify({'status': 'error', 'message': 'Modo desconocido.'}), 400
        if it['estado'] == 'publicado' and not datos.get('ya'):
            # Ya está publicado: solo cambia modo/texto, sin volver a programarlo.
            conn.execute('UPDATE descubrir_items SET modo=?, titulo=?, texto=? WHERE id=?', (modo, titulo, texto, it['id']))
        else:
            desde, avisar, vence, sin_aviso = _dsc_programa(it, _dsc_ahora(), bool(datos.get('ya')))
            # `avisado` NO se reinicia: si ya se avisó (y se despublicó y volvió a
            # aprobar) no se manda el mismo aviso dos veces.
            conn.execute("UPDATE descubrir_items SET estado='aprobado', modo=?, titulo=?, texto=?, publicar_desde=?, "
                         "avisar_desde=?, vence=?, avisado=MAX(avisado, ?) WHERE id=?",
                         (modo, titulo, texto, _dsc_iso(desde), _dsc_iso(avisar), _dsc_iso(vence), 1 if sin_aviso else 0,
                          it['id']))
        conn.commit()
        if datos.get('ya'):
            _dsc_tick_seguro()
        it = conn.execute('SELECT * FROM descubrir_items WHERE id=?', (it['id'],)).fetchone()
        return jsonify({'status': 'ok', 'item': _dsc_item_json(conn, it, admin=True)})
    finally:
        conn.close()


_DSC_EFE_ANTES = timedelta(hours=12)       # la efeméride aparece 12 h antes de su día…
_DSC_EFE_DESPUES = timedelta(hours=72)     # …y se queda hasta 72 h después de terminado
_DSC_EVENTO_YA_DIAS = 30                   # recital a 30 días o menos: sale al aprobarlo


def _dsc_programa(it, ahora, ya=False):
    """(publicar_desde, avisar_desde, vence, sin_aviso) de algo aprobado. Lo
    aprobado se publica solo:
    - efeméride: 12 h antes de su día (o ya, si es más tarde), aviso a las
      09:00 del día mismo, vence 72 h después de terminado el día; si el día
      ya pasó no se avisa («un día como hoy» ya no es hoy);
    - recital: si faltan 30 días o menos sale al aprobarlo; si no, con el
      lote del lunes. Vence al día siguiente del recital;
    - lo demás, con el lote del lunes a las 00:01 (lanzamiento 30 días, resto 7).
    `ya` (Publicar ahora) adelanta la salida a este instante."""
    lunes = datetime.strptime(it['semana'], '%Y-%m-%d').replace(hour=0, minute=1)
    fecha = None
    try:
        fecha = datetime.strptime((it['fecha'] or '')[:10], '%Y-%m-%d')
    except ValueError:
        pass
    sin_aviso = False
    if it['tipo'] == 'efemeride' and fecha:
        desde = ahora if ya else max(fecha - _DSC_EFE_ANTES, ahora)
        vence = fecha + timedelta(days=1) + _DSC_EFE_DESPUES
        avisar = max(fecha.replace(hour=9), ahora)
        sin_aviso = ahora >= fecha + timedelta(days=1)
        return desde, avisar, vence, sin_aviso
    if ya:
        desde = ahora
    elif it['tipo'] == 'evento' and fecha and (fecha.date() - ahora.date()).days <= _DSC_EVENTO_YA_DIAS:
        desde = ahora
    else:
        desde = max(lunes, ahora)
    avisar = desde.replace(hour=9, minute=0, second=0) if desde.hour < 9 else desde
    if it['tipo'] == 'evento' and fecha:
        vence = fecha + timedelta(days=1)
    elif it['tipo'] == 'lanzamiento':
        vence = desde + timedelta(days=30)
    else:
        vence = desde + timedelta(days=7)
    return desde, avisar, vence, sin_aviso


@app.route('/api/v1/admin/descubrir/publicar-ahora', methods=['POST'])
@api_admin_required
def api_v1_admin_descubrir_publicar_ahora():
    """Publica YA todo lo aprobado que espera su lote (sin esperar al lunes).
    Las efemérides siguen su horario (12 h antes de su día): adelantarlas
    días mostraría «un día como hoy» que no es hoy."""
    conn = get_db_connection()
    try:
        ahora = _dsc_ahora()
        filas = conn.execute("SELECT * FROM descubrir_items WHERE estado='aprobado' AND tipo<>'efemeride'").fetchall()
        for it in filas:
            desde, avisar, vence, _ = _dsc_programa(it, ahora, ya=True)
            conn.execute('UPDATE descubrir_items SET publicar_desde=?, avisar_desde=?, vence=? WHERE id=?',
                         (_dsc_iso(desde), _dsc_iso(avisar), _dsc_iso(vence), it['id']))
        conn.commit()
        _dsc_tick_seguro()
        return jsonify({'status': 'ok', 'publicadas': len(filas)})
    finally:
        conn.close()


def _dsc_tick_seguro():
    try:
        _dsc_tick()
    except Exception as e:  # noqa: BLE001
        app.logger.exception(f'[descubrir] tick: {e}')


@app.route('/api/v1/admin/descubrir/publicadas')
@api_admin_required
def api_v1_admin_descubrir_publicadas():
    conn = get_db_connection()
    try:
        items = [_dsc_item_json(conn, it, admin=True) for it in conn.execute(
            "SELECT * FROM descubrir_items WHERE estado='publicado' ORDER BY publicada_at DESC").fetchall()]
        return jsonify({'status': 'ok', 'items': items})
    finally:
        conn.close()


@app.route('/api/v1/admin/descubrir/recolectar', methods=['POST'])
@api_admin_required
def api_v1_admin_descubrir_recolectar():
    """Lanza la recolección ahora (en segundo plano)."""
    iniciado = _dsc_recolectar_en_hilo()
    return jsonify({'status': 'ok', 'iniciado': iniciado, 'corriendo': True})


@app.route('/api/v1/admin/descubrir/pedidos', methods=['GET', 'POST'])
@api_admin_required
def api_v1_admin_descubrir_pedidos():
    """Ranking de lo más solicitado. POST {id, estado: 'subido'|'descartado'|'pendiente'}:
    al marcar «subido» se avisa a quienes lo pidieron."""
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            datos = request.get_json(silent=True) or {}
            estado = datos.get('estado')
            p = conn.execute('SELECT * FROM material_pedidos WHERE id=?', (datos.get('id'),)).fetchone()
            if not p or estado not in ('subido', 'descartado', 'pendiente'):
                return jsonify({'status': 'error', 'message': 'Pedido o estado desconocido.'}), 400
            conn.execute('UPDATE material_pedidos SET estado=?, resuelto_at=? WHERE id=?',
                         (estado, None if estado == 'pendiente' else _utcnow_iso(), p['id']))
            if estado == 'subido' and p['estado'] != 'subido':
                nombre = f'«{p["titulo"]}»' + (f' de {p["artista"]}' if p['tipo'] == 'disco' and p['artista'] else '')
                for u in conn.execute('SELECT usuario_id FROM material_pedido_usuarios WHERE pedido_id=?', (p['id'],)).fetchall():
                    _notificar(conn, u['usuario_id'], 'pedido_subido', f'🎉 Ya está en Orbyte: {nombre}. Gracias por pedirlo.',
                               datos={'pedido_id': p['id']})
            conn.commit()
        items = [dict(r) for r in conn.execute(
            'SELECT p.id, p.tipo, p.artista, p.titulo, p.imagen, p.enlace, p.estado, p.creada_at, '
            '(SELECT COUNT(*) FROM material_pedido_usuarios u WHERE u.pedido_id=p.id) AS pedidos '
            "FROM material_pedidos p ORDER BY p.estado<>'pendiente', pedidos DESC, p.creada_at DESC LIMIT 200").fetchall()]
        return jsonify({'status': 'ok', 'items': items})
    finally:
        conn.close()

if __name__ == '__main__':
    # threaded=True: sin esto, el server de desarrollo de Flask atiende UN
    # solo pedido HTTP a la vez. Con "Reproducir en…" activo hay como mínimo
    # DOS clientes pidiendo streams de audio en simultáneo — el navegador
    # local (silenciado, pero igual está bajando /audio/...) y el
    # dispositivo UPnP bajando /cast-audio/... — y sin threaded, uno de los
    # dos queda haciendo fila detrás del otro. Es la causa más probable del
    # desfasaje de ~30s y el audio "pegado"/entrecortado en el parlante:
    # cada vez que el hilo único se lo lleva otro pedido (un heartbeat, el
    # polling del panel de cast, el propio audio local), la descarga hacia
    # el dispositivo remoto se corta un instante y retoma de a tirones.
    #
    # Lote 42cl (pedido por Niko): SIN modo depuración por defecto. Con
    # debug=True el recargador de Werkzeug reiniciaba el servidor cada vez
    # que cambiaba un .py (un git pull, una herramienta nueva…) y cortaba lo
    # que estuvieran escuchando los usuarios (y mandaba Stop al equipo DLNA).
    # Ahora el servidor solo se reinicia con `systemctl restart`. Los logs no
    # dependen de esto (handler propio en _configure_logging). Para una
    # sesión puntual de desarrollo: ORBYTE_DEBUG=1 en el .env.
    depurar = os.environ.get('ORBYTE_DEBUG', '').strip().lower() in ('1', 'true', 'si', 'sí', 'yes')
    # Las plantillas HTML se recargan solas aunque no haya modo depuración:
    # un cambio en templates/ no necesita reiniciar.
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.jinja_env.auto_reload = True
    app.run(debug=depurar, use_reloader=depurar, host='0.0.0.0', port=5001, threaded=True)
