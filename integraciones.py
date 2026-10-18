"""Ticket 42b (pedido por Niko, 18-10-2026): clave propia para la skill de
Alexa, que NO vence.

La skill usaba una clave de sesión normal (la de las apps), que vence a los
30 días: al vencer, Alexa dejaba de responder sin aviso. Esta clave:
- no vence; se puede revocar y generar otra (una activa por tipo);
- se guarda solo su hash (sha256), nunca el texto;
- solo sirve para lo que usa Alexa (ver ALEXA_ENDPOINTS en app.py).

Uso desde el servidor (ver clave_alexa.sh):
    ./clave_alexa.sh generar | revocar | estado
"""
import getpass
import hashlib
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

PREFIJO = 'alx_'
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'music.db')

SQL_TABLA = '''
CREATE TABLE IF NOT EXISTS integracion_claves (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo          TEXT NOT NULL,
    user_id       INTEGER NOT NULL,
    hash          TEXT UNIQUE NOT NULL,
    pista         TEXT,
    creada_at     TEXT NOT NULL,
    ultimo_uso_at TEXT,
    revocada_at   TEXT
)'''


def _ahora():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')


def hash_clave(clave):
    return hashlib.sha256(clave.encode('utf-8')).hexdigest()


def es_clave_integracion(token):
    return bool(token) and token.startswith(PREFIJO)


def migrar(conn):
    conn.execute(SQL_TABLA)


def buscar(conn, clave):
    """Fila activa de la clave (o None)."""
    if not es_clave_integracion(clave):
        return None
    return conn.execute('SELECT * FROM integracion_claves WHERE hash=? AND revocada_at IS NULL',
                        (hash_clave(clave),)).fetchone()


def generar(conn, user_id, tipo='alexa'):
    """Revoca la anterior del mismo tipo y devuelve la clave nueva (texto,
    se muestra una sola vez)."""
    clave = PREFIJO + secrets.token_urlsafe(32)
    ahora = _ahora()
    conn.execute('UPDATE integracion_claves SET revocada_at=? WHERE tipo=? AND revocada_at IS NULL', (ahora, tipo))
    conn.execute('INSERT INTO integracion_claves (tipo, user_id, hash, pista, creada_at) VALUES (?, ?, ?, ?, ?)',
                 (tipo, user_id, hash_clave(clave), clave[-4:], ahora))
    conn.commit()
    return clave


def revocar(conn, tipo='alexa'):
    n = conn.execute('UPDATE integracion_claves SET revocada_at=? WHERE tipo=? AND revocada_at IS NULL',
                     (_ahora(), tipo)).rowcount
    conn.commit()
    return n


# ---------------------------------------------------------------- CLI

def _cli():
    accion = (sys.argv[1] if len(sys.argv) > 1 else '').lower()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    migrar(conn)
    if accion == 'generar':
        email = input('Correo de la cuenta de Orbyte que usará Alexa: ').strip().lower()
        u = conn.execute('SELECT id, email, is_approved FROM users WHERE lower(email)=?', (email,)).fetchone()
        if not u or not u['is_approved']:
            print('No encontré una cuenta aprobada con ese correo.')
            return 1
        previa = conn.execute("SELECT 1 FROM integracion_claves WHERE tipo='alexa' AND revocada_at IS NULL").fetchone()
        clave = generar(conn, u['id'])
        print()
        if previa:
            print('(La clave anterior quedó revocada.)')
        print('Clave para Alexa (cópiala a ORBYTE_TOKEN en AWS; no se vuelve a mostrar):')
        print()
        print(clave)
        print()
        return 0
    if accion == 'revocar':
        n = revocar(conn)
        print('Clave de Alexa revocada.' if n else 'No había una clave de Alexa activa.')
        return 0
    if accion == 'estado':
        r = conn.execute("SELECT c.*, u.email FROM integracion_claves c LEFT JOIN users u ON u.id=c.user_id "
                         "WHERE c.tipo='alexa' AND c.revocada_at IS NULL ORDER BY c.id DESC LIMIT 1").fetchone()
        if not r:
            print('No hay una clave de Alexa activa. Genera una con: ./clave_alexa.sh generar')
            return 0
        print(f"Clave de Alexa activa (termina en …{r['pista']}), cuenta {r['email']}.")
        print(f"Creada: {r['creada_at']} UTC · Último uso de Alexa: {(r['ultimo_uso_at'] or 'todavía no') }"
              f"{' UTC' if r['ultimo_uso_at'] else ''}")
        return 0
    print('Uso: ./clave_alexa.sh generar | revocar | estado')
    return 2


if __name__ == '__main__':
    sys.exit(_cli())
