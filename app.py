import os
import re
import json
import tempfile
import hashlib
import socket
import signal
import atexit
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
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import sqlite3
import mimetypes
import subprocess
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

# ── Favorites (persisted in DB favorites table) ────────────────────────────────
_favorites_set: set = set()   # in-memory cache of track_id ints

def _load_favorites():
    global _favorites_set
    try:
        conn = get_db_connection()
        rows = conn.execute('SELECT track_id FROM favorites').fetchall()
        _favorites_set = {r[0] for r in rows}
        conn.close()
    except Exception as e:
        app.logger.warning(f'Could not load favorites: {e}')

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

def _send_email(to_addr, subject, html_body):
    """Envía un correo HTML. Nunca lanza excepción — si falla, solo lo deja
    registrado en el log para no interrumpir signup/aprobación/rechazo."""
    if not SMTP_HOST:
        app.logger.warning(
            f'SMTP no configurado — se omite el envío de "{subject}" a {to_addr}. '
            f'Definí SMTP_HOST / SMTP_USER / SMTP_PASSWORD (y opcionalmente '
            f'SMTP_PORT, SMTP_FROM_EMAIL, SMTP_FROM_NAME, SMTP_USE_TLS) como '
            f'variables de entorno para activar las notificaciones por correo.'
        )
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>'
        msg['To'] = to_addr
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
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
app.jinja_env.globals['favorites_set']    = lambda: _favorites_set
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
}
LED_ORDER = ['yellow', 'white', 'cyan', 'red', 'green', 'blue', 'magenta']

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
_COLLAB_GUEST_BLOCKED_ENDPOINTS = {
    'admin_dashboard', 'admin_users', 'admin_users_estado', 'admin_approve_user',
    'admin_reject_user', 'admin_revoke_user',
    'admin_collab', 'admin_collab_crear', 'admin_collab_finalizar', 'admin_collab_qr',
    'admin_collab_permiso',
    'api_admin_collab_estado', 'api_admin_collab_pull',
    'api_favorites_list', 'api_favorites_toggle', 'api_favorites_rebuild_cache', 'favorites_page',
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
    if ep in ('login', 'signup', 'static', 'service_worker', 'collab_join', 'cast_audio', 'cast_cover', 'cover_file') or request.path.startswith('/static/'):
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
        if auth.startswith('Bearer '):
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
        elif session.get('user_id'):
            conn = get_db_connection()
            try:
                row = conn.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
                user = dict(row) if row else None
            finally:
                conn.close()
        if not user or not user['is_approved']:
            return jsonify({'error': 'not_authenticated'}), 401
        g.api_user = user
        return view(*args, **kwargs)
    return wrapped

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
            'SELECT avatar, bio, favorite_genres_json FROM users WHERE id=?', (user_id,)
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
    })

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
        conn.execute('UPDATE users SET is_approved=1, approved_at=? WHERE id=?',
                     (_utcnow_iso(), user_id))
        conn.commit()
        row = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
    finally:
        conn.close()
    if row:
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

def _api_v1_filtered_albums(conn, filter_type, filter_value, page, sort, dir_):
    """Despacha al mismo helper que ya usan las rutas web (/mood/, /genre/,
    /led/, etc.) — así el resultado que ve el cliente nativo es idéntico al
    de la web, sin reimplementar cada tipo de filtro."""
    order = _album_order(sort, dir_)
    if filter_type in ('mood', 'idioma', 'momento', 'era', 'tier', 'tema_lirico'):
        return _meta_browse(conn, filter_type, filter_value, page, '', filter_type, sort, dir_)
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
        return _paginate(conn, count_sql, [filter_value, filter_value], data_sql, [filter_value, filter_value], page, order)
    if filter_type == 'led':
        count_sql = '''SELECT COUNT(DISTINCT al.id)
                       FROM albums al JOIN tracks t ON t.album_id=al.id
                       WHERE t.led_color=?'''
        data_sql = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, ? as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN artists ar ON al.artist_id=ar.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE t.led_color=?'''
        return _paginate(conn, count_sql, [filter_value], data_sql, [filter_value, filter_value], page, order)
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
            'SELECT led_color, COUNT(*) as c FROM tracks WHERE led_color IS NOT NULL GROUP BY led_color ORDER BY c DESC'
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
            })
        return jsonify({'tracks': result})
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
        f"(range={request.headers.get('Range', '-')})"
    )
    return _serve_audio(absolute_path)

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
            albums_raw, total, total_pages = _api_v1_filtered_albums(conn, filter_type, filter_value, page, sort, dir_)
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
                      (SELECT led_color FROM tracks WHERE album_id=al.id
                       ORDER BY CASE led_color
                         WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                         WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                         ELSE 6 END LIMIT 1) as album_led
               FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
               WHERE al.name LIKE ? OR ar.name LIKE ? ORDER BY al.name LIMIT 20''',
            (like, like)
        ).fetchall()
        tracks = conn.execute(
            '''SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.is_mqa,
                      t.codec, t.duration, t.sample_rate_real,
                      a.id as album_id, a.name as album_name, a.cover_path,
                      ar.name as artist_name,
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
        conn.execute('UPDATE users SET is_approved=1, approved_at=? WHERE id=?',
                     (_utcnow_iso(), user_id))
        conn.commit()
        row = conn.execute('SELECT email FROM users WHERE id=?', (user_id,)).fetchone()
    finally:
        conn.close()
    if row:
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

def _collab_avatar_display(category, fname, name):
    """Arma el dict {'type': 'image'|'initials', ...} listo para renderizar,
    a partir de la referencia guardada en collab_participants (avatar_category
    + avatar_file). No depende de ninguna cookie de sesión — por eso sirve
    tanto para el header del propio invitado como para el badge "agregado
    por" en el player del admin (otro navegador — ver ticket)."""
    if category and fname:
        return {'type': 'image',
                'url': url_for('static', filename=f'avatares/{category}/{fname}')}
    return {'type': 'initials', 'text': _collab_initials(name)}


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

def _collab_fair_order(conn, session_id):
    """Intercala round-robin las pistas TODAVÍA NO entregadas al reproductor
    del host, agrupadas por participante (cada quien conserva el orden en que
    fue agregando las suyas). Con 10/3/5 pistas por dispositivo el resultado
    es D1,D2,D3, D1,D2,D3, D1,D2,D3, D1,D3, D1,D3, D1,D1,D1,D1,D1,D1 — todos
    escuchan algo propio en las primeras 3 pistas en vez de que el que agregó
    menos espere a que se acaben las 10 del primero."""
    rows = conn.execute(
        'SELECT id, participant_id, track_id FROM collab_queue_items '
        'WHERE session_id=? AND dispatched=0 ORDER BY added_at ASC', (session_id,)
    ).fetchall()
    by_participant, order = {}, []
    for r in rows:
        pid = r['participant_id']
        if pid not in by_participant:
            by_participant[pid] = []
            order.append(pid)
        by_participant[pid].append(dict(r))
    result, pending = [], True
    while pending:
        pending = False
        for pid in order:
            if by_participant[pid]:
                result.append(by_participant[pid].pop(0))
                pending = True
    return result

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

    count = _collab_window_count(conn, sess, participant_id)
    if count >= sess['max_tracks']:
        return {'status': 'limit', 'message':
                f"Llegaste al máximo de {sess['max_tracks']} pistas cada {sess['window_hours']:g}h. "
                f"Esperá un poco — el cupo se libera solo con el tiempo."}

    dup = conn.execute(
        '''SELECT cp.name FROM collab_queue_items cqi
           JOIN collab_participants cp ON cp.id = cqi.participant_id
           WHERE cqi.session_id=? AND cqi.track_id=? LIMIT 1''',
        (session_id, track_id)
    ).fetchone()
    if dup:
        return {'status': 'duplicate', 'added_by': dup['name'],
                'message': f"«{track['title']}» ya está en la playlist colaborativa (la agregó {dup['name']})."}

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

    conn.execute(
        'INSERT INTO collab_queue_items (session_id, participant_id, track_id, added_at) VALUES (?, ?, ?, ?)',
        (session_id, participant_id, track_id, _utcnow_iso())
    )
    conn.commit()
    return {'status': 'ok', 'remaining': sess['max_tracks'] - (count + 1)}


@app.route('/admin/colaborativa')
@admin_required
def admin_collab():
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        participants, pending_count, dispatched_count, join_url = [], 0, 0, None
        if sess:
            participants = [dict(r) for r in conn.execute(
                'SELECT * FROM collab_participants WHERE session_id=? ORDER BY joined_at', (sess['id'],)
            ).fetchall()]
            pending_count = conn.execute(
                'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND dispatched=0', (sess['id'],)
            ).fetchone()[0]
            dispatched_count = conn.execute(
                'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND dispatched=1', (sess['id'],)
            ).fetchone()[0]
            join_url = url_for('collab_join', token=sess['token'], _external=True)
        return render_template('collab_host.html', collab_session=sess, participants=participants,
                               pending_count=pending_count, dispatched_count=dispatched_count,
                               join_url=join_url, qrcode_available=_QRCODE_AVAILABLE,
                               default_max=COLLAB_DEFAULT_MAX_TRACKS,
                               default_window=COLLAB_DEFAULT_WINDOW_HOURS)
    finally:
        conn.close()


@app.route('/admin/colaborativa/crear', methods=['POST'])
@admin_required
def admin_collab_crear():
    max_tracks   = request.form.get('max_tracks', COLLAB_DEFAULT_MAX_TRACKS, type=int) or COLLAB_DEFAULT_MAX_TRACKS
    window_hours = request.form.get('window_hours', COLLAB_DEFAULT_WINDOW_HOURS, type=float) or COLLAB_DEFAULT_WINDOW_HOURS
    max_tracks   = max(1, max_tracks)
    window_hours = max(0.5, window_hours)
    conn = get_db_connection()
    try:
        # Solo puede haber una sesión activa: cerrar cualquier otra antes de
        # abrir la nueva (mismo QR viejo deja de servir automáticamente).
        conn.execute('UPDATE collab_sessions SET is_active=0, closed_at=? WHERE is_active=1', (_utcnow_iso(),))
        token = secrets.token_urlsafe(16)
        conn.execute(
            'INSERT INTO collab_sessions (token, created_by, max_tracks, window_hours, is_active, created_at) '
            'VALUES (?, ?, ?, ?, 1, ?)',
            (token, session['user_id'], max_tracks, window_hours, _utcnow_iso())
        )
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('admin_collab'))


@app.route('/admin/colaborativa/finalizar', methods=['POST'])
@admin_required
def admin_collab_finalizar():
    conn = get_db_connection()
    try:
        conn.execute('UPDATE collab_sessions SET is_active=0, closed_at=? WHERE is_active=1', (_utcnow_iso(),))
        conn.commit()
    finally:
        conn.close()
    return redirect(url_for('admin_collab'))


@app.route('/admin/colaborativa/participante/<int:participant_id>/permiso', methods=['POST'])
@admin_required
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
            'SELECT * FROM collab_participants WHERE id=? AND session_id=?',
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
    join_url = url_for('collab_join', token=sess['token'], _external=True)
    img = qrcode.make(join_url)
    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    resp = Response(buf.getvalue(), mimetype='image/png')
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/api/admin/colaborativa/estado')
@admin_required
def api_admin_collab_estado():
    """JSON liviano para refrescar en vivo el panel /admin/colaborativa
    (participantes + cola pendiente) — mismo patrón que /admin/usuarios/estado."""
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            return jsonify({'active': False})
        participants = [dict(r) for r in conn.execute(
            'SELECT id, name, joined_at, can_pull FROM collab_participants WHERE session_id=? ORDER BY joined_at',
            (sess['id'],)
        ).fetchall()]
        pending = conn.execute(
            'SELECT COUNT(*) FROM collab_queue_items WHERE session_id=? AND dispatched=0', (sess['id'],)
        ).fetchone()[0]
        # Pedido remoto de actualización (ver /api/collab/solicitar-pull): el
        # delegado lo dispara desde su celular, este poll (cada 6s) es lo que
        # se lo hace llegar al dispositivo del anfitrión sin que tenga que
        # tocar nada — ver collab_host.js.
        pull_requested = bool(sess.get('pull_requested_at'))
        pull_requested_by_name = None
        if pull_requested and sess.get('pull_requested_by'):
            req = conn.execute(
                'SELECT name FROM collab_participants WHERE id=?', (sess['pull_requested_by'],)
            ).fetchone()
            pull_requested_by_name = req['name'] if req else None
        return jsonify({'active': True, 'participants': participants, 'pending_count': pending,
                        'max_tracks': sess['max_tracks'], 'window_hours': sess['window_hours'],
                        'pull_requested': pull_requested, 'pull_requested_by_name': pull_requested_by_name})
    finally:
        conn.close()


@app.route('/api/admin/colaborativa/cola-pendiente')
@admin_required
def api_admin_collab_pull():
    """Devuelve — y marca como entregadas — las pistas todavía no
    despachadas, ya intercaladas en orden justo (_collab_fair_order). El
    admin las agrega a SU cola local con appendToQueue() (ver collab_host.js)
    exactamente como agregaría cualquier otra pista, y desde ahí las maneja
    con total libertad — reordenar/quitar ya lo permite el playlist-panel
    existente, no hace falta tocarlo."""
    conn = get_db_connection()
    try:
        sess = _collab_active_session(conn)
        if not sess:
            return jsonify([])
        ordered_items = _collab_fair_order(conn, sess['id'])
        # Se limpia el pedido remoto (si había uno) apenas se atiende el
        # pull, haya o no pistas nuevas — así el delegado no queda con el
        # pedido "trabado" si ya no había nada pendiente para cargar.
        conn.execute(
            'UPDATE collab_sessions SET pull_requested_at=NULL, pull_requested_by=NULL WHERE id=?',
            (sess['id'],)
        )
        if not ordered_items:
            conn.commit()
            return jsonify([])
        ids = [it['track_id'] for it in ordered_items]
        placeholders = ','.join('?' * len(ids))
        rows = conn.execute(
            f'''SELECT t.*, al.name as album_name, al.cover_path, al.year as album_year,
                       al.artist_id, ar.name as artist_name
                FROM tracks t
                LEFT JOIN albums al ON t.album_id=al.id
                LEFT JOIN artists ar ON al.artist_id=ar.id
                WHERE t.id IN ({placeholders})''', ids
        ).fetchall()
        by_id = {r['id']: r for r in rows}

        # Quién agregó cada pista (ver ticket "avatar en el player del
        # admin") — se resuelve UNA query por participante involucrado, no
        # por pista, y se arma acá porque track_to_json() no sabe nada de
        # playlist colaborativa (sigue usándose para browse/álbum/etc. tal
        # cual, sin este campo extra).
        participant_ids = list({it['participant_id'] for it in ordered_items})
        p_placeholders = ','.join('?' * len(participant_ids))
        p_rows = conn.execute(
            f'SELECT id, name, avatar_category, avatar_file FROM collab_participants '
            f'WHERE id IN ({p_placeholders})', participant_ids
        ).fetchall()
        added_by_map = {
            p['id']: {'name': p['name'],
                      'avatar': _collab_avatar_display(p['avatar_category'], p['avatar_file'], p['name'])}
            for p in p_rows
        }

        result = []
        for it in ordered_items:
            if it['track_id'] not in by_id:
                continue
            d = track_to_json(by_id[it['track_id']])
            d['collab_added_by'] = added_by_map.get(it['participant_id'])
            result.append(d)

        item_ids = [it['id'] for it in ordered_items]
        placeholders2 = ','.join('?' * len(item_ids))
        conn.execute(f'UPDATE collab_queue_items SET dispatched=1 WHERE id IN ({placeholders2})', item_ids)
        conn.commit()
        return jsonify(result)
    finally:
        conn.close()


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
        return redirect(url_for('home'))
    finally:
        conn.close()


@app.route('/colab/salir')
def collab_leave():
    session.clear()
    return redirect(url_for('login'))


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
        conn.execute(
            'UPDATE collab_sessions SET pull_requested_at=?, pull_requested_by=? WHERE id=?',
            (_utcnow_iso(), session['collab_participant_id'], sess['id'])
        )
        conn.commit()
        return jsonify({'status': 'ok'})
    finally:
        conn.close()


def _fmt_seconds(seconds):
    if not seconds or seconds < 0:
        return "0:00"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

def _fmt_bitrate(bps):
    if not bps:
        return "N/A"
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
    d['bitrate_fmt']     = _fmt_bitrate(d.get('bitrate'))
    sr = d.get('sample_rate_real')
    d['sample_rate_fmt'] = f"{sr/1000:.1f} kHz" if sr else "N/A"
    d['bit_depth_fmt']   = f"{d.get('bit_depth') or 24} bit"
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
            'SELECT led_color, COUNT(*) as c FROM tracks WHERE led_color IS NOT NULL GROUP BY led_color ORDER BY c DESC'
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
                """SELECT led_color FROM tracks WHERE album_id=?
                   ORDER BY CASE led_color
                     WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                     WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                     ELSE 6 END LIMIT 1""",
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
                '''SELECT led_color FROM tracks
                   WHERE album_id=? AND led_color IS NOT NULL
                   GROUP BY led_color
                   ORDER BY CASE led_color
                     WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                     WHEN 'red'     THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                     ELSE 6 END
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
                               total_tracks=total_tracks, fav_ids=_favorites_set,
                               genres=genres, similar_artists=similar_artists,
                               top_tracks=top_tracks,
                               artist_nationality=ar_data.get('nationality', ''))
    finally:
        conn.close()


@app.route('/api/artist/<int:artist_id>/website')
def api_artist_website(artist_id):
    """Return (and cache) the official website URL for an artist via MusicBrainz."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT name, website_url FROM artists WHERE id=?', (artist_id,)
        ).fetchone()
        if not row:
            return jsonify({'url': None})

        # Return cached value if present
        if row['website_url']:
            # '-' means "looked up, nothing found" — don't retry
            return jsonify({'url': None if row['website_url'] == '-' else row['website_url']})

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

        return jsonify({'url': official_url})
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

        # Favorites set for this page
        fav_ids = _favorites_set

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
                      (SELECT led_color FROM tracks WHERE album_id=al.id
                       ORDER BY CASE led_color
                         WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                         WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                         ELSE 6 END LIMIT 1) as album_led
               FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
               WHERE al.name LIKE ? OR ar.name LIKE ? ORDER BY al.name LIMIT 20''',
            (like, like)
        ).fetchall()
        tracks = conn.execute(
            '''SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.is_mqa,
                      t.codec, t.duration, t.sample_rate_real,
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

PAGE_SIZE = 30

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
_TRACK_QUALITY_RANK_SQL = """(CASE {alias}.led_color
        WHEN 'magenta' THEN 100.0
        WHEN 'blue'    THEN 83.3333333333
        WHEN 'green'   THEN 66.6666666667
        WHEN 'red'     THEN 50.0
        WHEN 'cyan'    THEN 33.3333333333
        WHEN 'white'   THEN 16.6666666667
        ELSE 0.0
    END)"""

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

def _paginate(conn, count_sql, count_params, data_sql, data_params, page,
              order_by='al.name ASC'):
    """Run paginated query with dynamic ORDER BY."""
    total  = conn.execute(count_sql, count_params).fetchone()[0]
    offset = (page - 1) * PAGE_SIZE
    final_sql = data_sql + f' ORDER BY {order_by} LIMIT ? OFFSET ?'
    rows  = conn.execute(final_sql, list(data_params) + [PAGE_SIZE, offset]).fetchall()
    albums = []
    for a in rows:
        d = dict(a)
        d['cover_path'] = clean_db_path(d.get('cover_path'))
        d['album_led']  = d.get('album_led') or 'yellow'
        albums.append(d)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
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
QUALITY_OPTIONS = ['CD', 'HI-RES', 'DSD64', 'DSD128', 'DSD256', 'MQA', 'MQA Studio', 'OSR']

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
    if v == 'CD':         return "t.led_color = 'yellow'"
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

def _api_search_advanced_payload(args):
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
    """
    page = max(1, args.get('page', 1, type=int))
    sort = args.get('sort', 'popularidad')
    dir_ = args.get('dir', 'desc')
    view = args.get('view', 'albums')

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
            offset = (page - 1) * PAGE_SIZE
            data_sql = f'''SELECT t.*, al.id as album_id, al.name as album_name,
                                  al.year as album_year, al.cover_path,
                                  ar.name as artist_name,
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
            rows = conn.execute(data_sql, params + [PAGE_SIZE, offset]).fetchall()

            tracks = []
            for r in rows:
                d = track_to_json(dict(r))
                d['album_id']      = r['album_id']
                d['album_name']    = r['album_name']
                d['album_year']    = r['album_year']
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

            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
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
                              (SELECT led_color FROM tracks WHERE album_id=al.id
                               ORDER BY CASE led_color
                                   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                                   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                                   ELSE 6 END LIMIT 1) as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN artists ar ON ar.id=al.artist_id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       LEFT JOIN album_meta am ON am.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE 1=1{where}'''
        order = _album_order(sort, dir_)
        albums, total, total_pages = _paginate(conn, count_sql, params, data_sql, params, page, order)
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
    protegido por token en vez de cookie de sesión."""
    return jsonify(_api_search_advanced_payload(request.args))

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
                         (SELECT led_color FROM tracks WHERE album_id=al.id
                          ORDER BY CASE led_color
                            WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                            WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                            ELSE 6 END LIMIT 1) as album_led
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
                       WHERE t.led_color=?'''
        data_sql  = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, ? as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN artists ar ON al.artist_id=ar.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE t.led_color=?'''
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
                               title=f"LED {color.capitalize()} — {LED_LABELS[color]}",
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

def _meta_browse(conn, field, value, page, title, filter_type, sort='popularidad', dir_='desc'):
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
            "(SELECT led_color FROM tracks WHERE album_id=al.id "
            " ORDER BY CASE led_color "
            "   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2 "
            "   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5 "
            "   ELSE 6 END LIMIT 1) as album_led, "
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
            "(SELECT led_color FROM tracks WHERE album_id=al.id "
            " ORDER BY CASE led_color "
            "   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2 "
            "   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5 "
            "   ELSE 6 END LIMIT 1) as album_led, "
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
    return _paginate(conn, count_sql, [value], data_sql, [value], page, order)


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


@app.route('/api/lyrics')
def api_lyrics():
    artist   = request.args.get('artist',   '').strip()
    title    = request.args.get('title',    '').strip()
    track_id = request.args.get('track_id', None, type=int)
    version  = request.args.get('version',  None)

    import sys

    def _log(msg):
        print(f"[lyrics] {msg}", file=sys.stderr, flush=True)

    def _empty(msg=''):
        return jsonify({'has_lyrics': False, 'has_synced': False,
                        'lyrics': '', 'synced': '', 'alternatives': [], 'error': msg})

    def _ok_file(synced, plain, source):
        return jsonify({
            'has_lyrics': bool(synced or plain),
            'has_synced': bool(synced),
            'lyrics':     plain  or '',
            'synced':     synced or '',
            'alternatives': [],
            'source': source,
        })

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
                        return jsonify({**_fmt_lrc(data), 'alternatives': [], 'source': 'lrclib_id'})
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
                return jsonify({**_fmt_lrc(r.json()), 'alternatives': []})

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
            return jsonify({**_fmt_lrc(best), 'alternatives': alts, 'source': 'lrclib_search'})

        _log(f"No lyrics found for artist='{artist}' title='{title}'")
        return _empty()

    except Exception as e:
        _log(f"Unexpected error: {e}")
        return _empty(str(e))


def _api_meta_tracks_payload(args):
    """Cuerpo compartido de /api/meta/tracks (web) y /api/v1/meta/tracks
    (nativo, con @api_login_required) — Ticket 08, Lote A §4.3. Extraído de
    la ruta web al agregar el mirror nativo, mismo criterio que
    _api_search_advanced_payload (no mantener dos copias de la misma
    lógica de dedupe/paginación). Devuelve (payload_dict, status_code) para
    que ambas rutas puedan propagar el 400 de 'field/value inválido' sin
    duplicar esa validación. Agrega 'stream_url' por pista
    incondicionalmente, igual que en _api_search_advanced_payload."""
    field      = args.get('field',      '').strip()
    value      = args.get('value',      '').strip()
    page       = max(1, args.get('page', 1, type=int))
    sort       = args.get('sort',       'popularidad')
    dir_       = args.get('dir',        'desc')
    intercalar = args.get('intercalar', '0') == '1'

    ALLOWED_FIELDS = {'mood', 'momento', 'era', 'tema_lirico', 'tier', 'idioma', 'genre'}
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
        else:
            base_where  = f'tm.{field}=?'
            base_params = (value,)

        dedupe = _track_dedupe_condition(extra_where=base_where, track_alias='t', pop_alias='tpc')
        # dedupe reincorpora una copia (re-prefijada dup_*) de base_where —
        # sus '?' necesitan su valor otra vez, en el mismo orden.
        full_params = base_params + base_params

        if field == 'genre':
            # Same match as browse_genre: classic tracks.genre OR track_meta.genre_primary
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
            limit  = PAGE_SIZE
            offset = (page - 1) * PAGE_SIZE
            order  = _track_order(sort, dir_)

        # Build the WHERE clause — per-track always (see comment above)
        where_clause = f'{base_where} AND {dedupe}'
        extra_join   = ''
        where_params = full_params

        rows = conn.execute(f'''
            SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.is_mqa, t.codec,
                   t.duration, t.track_number, t.file_path,
                   a.id as album_id, a.name as album_name, a.cover_path, a.year as album_year,
                   ar.name as artist_name,
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
            result.append(d)

        total_pages = 1 if intercalar else max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
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
    protegido por token en vez de cookie de sesión."""
    payload, status = _api_meta_tracks_payload(request.args)
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
def cast_audio(track_id):
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
    return _serve_audio(path)


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
    for mime in mime_candidates:
        protocol_info = f'http-get:*:{mime}:*'
        didl = _cast_build_didl(track['id'], track['title'], track['artist'], protocol_info,
                                 media_url, file_size, album=track['album_name'], genre=track['genre'],
                                 track_number=track['track_number'], cover_url=cover_url)
        status, body = _cast_send_track(control_url, media_url, didl)
        app.logger.info(f"[cast] SetAVTransportURI+Play mime={mime} -> HTTP {status}")
        if status != 200:
            last_reason = f'El dispositivo devolvió HTTP {status} para {mime}'
            continue
        time.sleep(1.2)  # darle tiempo al renderer a intentar arrancar antes de preguntar
        state = _cast_get_transport_state(control_url)
        app.logger.info(f"[cast] estado tras mime={mime}: {state}")
        if state in ('STOPPED', 'NO_MEDIA_PRESENT', None):
            last_reason = f'El dispositivo no aceptó el formato {mime} (estado: {state or "no se pudo consultar"})'
            continue
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

@app.route('/cover/<path:filepath>')
def cover_file(filepath):
    # Try exact path first
    absolute_path = os.path.join(MUSIC_ROOT, filepath.lstrip('/'))
    if os.path.isfile(absolute_path):
        mime, _ = mimetypes.guess_type(absolute_path)
        return send_file(absolute_path, mimetype=mime or 'image/jpeg', max_age=86400)

    # Fallback: try common cover filenames in same directory
    directory = os.path.dirname(absolute_path)
    if os.path.isdir(directory):
        for name in COVER_NAMES:
            alt = os.path.join(directory, name)
            if os.path.isfile(alt):
                mime, _ = mimetypes.guess_type(alt)
                return send_file(alt, mimetype=mime or 'image/jpeg', max_age=86400)
        # Last resort: first image file found in directory
        try:
            for fname in sorted(os.listdir(directory)):
                if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    alt = os.path.join(directory, fname)
                    if os.path.isfile(alt):
                        mime, _ = mimetypes.guess_type(alt)
                        return send_file(alt, mimetype=mime or 'image/jpeg', max_age=86400)
        except PermissionError:
            pass

    # Try parent directory (multi-disc albums store cover one level up)
    parent = os.path.dirname(directory)
    if parent != directory and os.path.isdir(parent):
        for name in COVER_NAMES:
            alt = os.path.join(parent, name)
            if os.path.isfile(alt):
                mime, _ = mimetypes.guess_type(alt)
                return send_file(alt, mimetype=mime or 'image/jpeg', max_age=86400)

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


@app.route('/api/favorites', methods=['GET'])
def api_favorites_list():
    """Return all favorited tracks with full metadata."""
    try:
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT t.id, t.title, t.artist, t.duration, t.led_color, t.file_path,
                   t.codec, t.is_dsd, t.dsd_rate, t.is_mqa, t.sample_rate_real,
                   al.name as album_name, al.cover_path,
                   tm.tier, f.added_at
            FROM favorites f
            JOIN tracks t ON t.id=f.track_id
            JOIN albums al ON al.id=t.album_id
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            ORDER BY f.added_at DESC
        ''').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/favorites/toggle', methods=['POST'])
def api_favorites_toggle():
    """Add or remove a track from favorites."""
    global _favorites_set
    data = request.get_json() or {}
    tid  = data.get('track_id')
    if not tid:
        return jsonify({'error': 'track_id required'}), 400
    try:
        conn = get_db_connection()
        if tid in _favorites_set:
            conn.execute('DELETE FROM favorites WHERE track_id=?', (tid,))
            _favorites_set.discard(tid)
            action = 'removed'
        else:
            conn.execute('INSERT OR IGNORE INTO favorites (track_id) VALUES (?)', (tid,))
            _favorites_set.add(tid)
            action = 'added'
        conn.commit()
        conn.close()
        return jsonify({'action': action, 'track_id': tid, 'total': len(_favorites_set)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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


@app.route('/favorites')
def favorites_page():
    """Favorites page — shows all bookmarked tracks."""
    try:
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT t.id, t.title, t.artist, t.duration, t.led_color, t.file_path,
                   t.codec, t.is_dsd, t.dsd_rate, t.is_mqa, t.sample_rate_real,
                   al.id as album_id, al.name as album_name, al.cover_path,
                   al.artist_id,
                   CASE WHEN t.is_dsd=1 THEN COALESCE(t.dsd_rate,'DSD')
                        WHEN t.is_mqa=1 THEN 'MQA'
                        ELSE UPPER(COALESCE(t.codec,'FLAC')) END as format_display,
                   f.added_at
            FROM favorites f
            JOIN tracks t ON t.id=f.track_id
            JOIN albums al ON al.id=t.album_id
            ORDER BY f.added_at DESC
        ''').fetchall()
        conn.close()
        tracks = [dict(r) for r in rows]
        # Build audio_url for each track
        for t in tracks:
            t['audio_url'] = audio_url_filter(t['file_path'])
            t['cover_url'] = cover_url_filter(t['cover_path'] or '')
        return render_template('favorites.html',
                               tracks=tracks,
                               tracks_json=json.dumps(tracks),
                               fav_ids=_favorites_set)
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


if __name__ == '__main__':
    _load_favorites()
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
    app.run(debug=True, host='0.0.0.0', port=5001, threaded=True)
