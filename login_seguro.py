"""Inicio de sesión más seguro (pedido de Niko, 31-10-2026).

  - Límite de intentos: 5 contraseñas incorrectas para la misma cuenta desde la misma
    IP -> espera de 1 min; si sigue fallando, 5 y luego 15 min. Por cuenta+IP (no se
    bloquea la cuenta entera: nadie puede dejar afuera a otro a propósito). En RAM.
  - Recuperar contraseña: el usuario la pide (web o app) -> queda una solicitud y se
    avisa al admin; el admin genera una contraseña temporal que se manda al correo del
    usuario. Con esa contraseña, al entrar se le pide cambiarla.
  - Versión de sesión por usuario (users.token_version): cambiar o restablecer la
    contraseña, o «Cerrar sesión en todos sus dispositivos», la sube y deja inválidas
    las sesiones abiertas (tokens de las apps y sesión web).

Sin Flask aquí: app.py llama a estas funciones y arma las respuestas.
"""
import secrets
import threading
import time

ESPERAS_S = (60, 300, 900)
FALLOS_PERMITIDOS = 5
OLVIDO_TRAMO_S = 3600          # cuánto se recuerda el historial de fallos sin actividad
SOLICITUDES_POR_IP_HORA = 5

_lock = threading.Lock()
_fallos = {}        # (email, ip) -> {'n', 'nivel', 'hasta', 'ultimo'}
_solicitudes_ip = {}  # ip -> [timestamps]


def migrar(conn):
    for sql in ("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN debe_cambiar_clave INTEGER NOT NULL DEFAULT 0"):
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass
    conn.execute('''CREATE TABLE IF NOT EXISTS recuperar_clave (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        email TEXT NOT NULL,
        ip TEXT,
        estado TEXT NOT NULL DEFAULT 'pendiente',   -- pendiente | resuelta | descartada
        creada_at TEXT NOT NULL,
        resuelta_at TEXT,
        resuelta_por INTEGER)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS login_fallos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT, ip TEXT, bloqueado INTEGER NOT NULL DEFAULT 0, creada_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_login_fallos_fecha ON login_fallos (creada_at)')
    conn.commit()


# ── límite de intentos ──────────────────────────────────────────────────────

def bloqueado(email, ip):
    """Segundos que faltan para poder volver a intentar (0 = puede)."""
    k = ((email or '').strip().lower(), ip or '')
    with _lock:
        e = _fallos.get(k)
        if not e:
            return 0
        resto = e['hasta'] - time.time()
        return int(resto + 0.999) if resto > 0 else 0


def registrar_fallo(conn, email, ip, ahora_iso):
    """Anota un intento fallido. Devuelve los segundos de espera si con este quedó bloqueado (si no, 0)."""
    k = ((email or '').strip().lower(), ip or '')
    ahora = time.time()
    espera = 0
    with _lock:
        e = _fallos.get(k)
        if not e or ahora - e['ultimo'] > OLVIDO_TRAMO_S:
            e = {'n': 0, 'nivel': 0, 'hasta': 0.0, 'ultimo': ahora}
            _fallos[k] = e
        e['n'] += 1
        e['ultimo'] = ahora
        if e['n'] >= FALLOS_PERMITIDOS:
            espera = ESPERAS_S[min(e['nivel'], len(ESPERAS_S) - 1)]
            e['nivel'] += 1
            e['n'] = 0
            e['hasta'] = ahora + espera
    try:
        conn.execute('INSERT INTO login_fallos (email, ip, bloqueado, creada_at) VALUES (?, ?, ?, ?)',
                     (k[0][:200], k[1][:64], 1 if espera else 0, ahora_iso))
        conn.commit()
    except Exception:
        pass
    return espera


def registrar_exito(email, ip):
    with _lock:
        _fallos.pop(((email or '').strip().lower(), ip or ''), None)


def texto_espera(segundos):
    m = max(1, (int(segundos) + 59) // 60)
    return f'Demasiados intentos. Vuelve a probar en {m} minuto{"s" if m != 1 else ""}.'


# ── recuperar contraseña ────────────────────────────────────────────────────

def solicitud_permitida(ip):
    ahora = time.time()
    with _lock:
        lista = [t for t in _solicitudes_ip.get(ip or '', []) if ahora - t < 3600]
        if len(lista) >= SOLICITUDES_POR_IP_HORA:
            _solicitudes_ip[ip or ''] = lista
            return False
        lista.append(ahora)
        _solicitudes_ip[ip or ''] = lista
        return True


def crear_solicitud(conn, email, ip, ahora_iso):
    """Devuelve (usuario, nueva) — usuario None si el correo no corresponde a una cuenta aprobada;
    nueva False si ya había una pendiente (no se vuelve a avisar al admin)."""
    u = conn.execute('SELECT id, email, nickname, is_approved FROM users WHERE email=?',
                     ((email or '').strip().lower(),)).fetchone()
    if not u or not u['is_approved']:
        return None, False
    if conn.execute("SELECT 1 FROM recuperar_clave WHERE user_id=? AND estado='pendiente'", (u['id'],)).fetchone():
        return dict(u), False
    conn.execute('INSERT INTO recuperar_clave (user_id, email, ip, creada_at) VALUES (?, ?, ?, ?)',
                 (u['id'], u['email'], (ip or '')[:64], ahora_iso))
    conn.commit()
    return dict(u), True


def solicitudes_pendientes(conn):
    return [dict(r) for r in conn.execute(
        '''SELECT r.id, r.user_id, r.email, r.creada_at, u.nickname
             FROM recuperar_clave r LEFT JOIN users u ON u.id = r.user_id
            WHERE r.estado = 'pendiente' ORDER BY r.creada_at''')]


def clave_temporal():
    """12 caracteres sin los que se confunden (0/O, 1/l/I), en 3 grupos: «k7mq-4rtx-9pwe»."""
    letras = 'abcdefghjkmnpqrstuvwxyz23456789'
    s = ''.join(secrets.choice(letras) for _ in range(12))
    return f'{s[:4]}-{s[4:8]}-{s[8:]}'


def cerrar_sesiones(conn, user_id):
    """Sube la versión de sesión: todos los tokens y sesiones web del usuario dejan de valer."""
    conn.execute('UPDATE users SET token_version = COALESCE(token_version, 0) + 1 WHERE id=?', (user_id,))
    conn.commit()
    return conn.execute('SELECT token_version FROM users WHERE id=?', (user_id,)).fetchone()[0]


def resolver_solicitudes(conn, user_id, admin_id, estado, ahora_iso):
    conn.execute("UPDATE recuperar_clave SET estado=?, resuelta_at=?, resuelta_por=? "
                 "WHERE user_id=? AND estado='pendiente'", (estado, ahora_iso, admin_id, user_id))
    conn.commit()


def fallos_periodo(conn, desde):
    """Para las estadísticas: (total, bloqueos, [(email, ip, n)] más repetidos)."""
    try:
        total = conn.execute('SELECT COUNT(*), COALESCE(SUM(bloqueado), 0) FROM login_fallos WHERE creada_at >= ?',
                             (desde,)).fetchone()
        top = conn.execute('''SELECT email, ip, COUNT(*) AS n FROM login_fallos WHERE creada_at >= ?
                              GROUP BY email, ip ORDER BY n DESC LIMIT 3''', (desde,)).fetchall()
        return total[0] or 0, total[1] or 0, [(r['email'], r['ip'], r['n']) for r in top]
    except Exception:
        return 0, 0, []
