"""Ticket AI-02 (Etapa 1 del epic "Agente de IA", mitad backend) — motor de
comportamiento: tablas + funciones de logging. Ver AI_AGENT_MASTER_PLAN.md §8.

Módulo nuevo y aislado, mismo criterio que ai_playlist.py (ver §3 del master
plan): solo inserciones/actualizaciones sobre tablas propias, no toca nada
existente. app.py solo registra las rutas y le pasa la conexión + user_id.

Todos los endpoints que consumen este módulo son idempotentes ante
reintentos de red: el cliente manda un client_event_id/client_session_id
(UUID) y cada tabla tiene un UNIQUE sobre (user_id, ese campo) — un
reintento simplemente no inserta de nuevo (o no vuelve a pisar ended_at),
nunca duplica ni corrompe el conteo. Esto sigue el mismo criterio ya
documentado en el proyecto sobre endpoints no idempotentes (ver AGENTE.md /
AI_AGENT_MASTER_PLAN.md).

Esta ola es solo de COLECCIÓN (escritura). Las consultas de agregación
—individual y global, ver AI_AGENT_MASTER_PLAN.md §8— se agregan recién en
la Etapa 5 (fallback engine), que es quien realmente las necesita. No se
adelanta ese código acá para no construir algo sin un consumidor real
todavía (mismo criterio de alcance ya usado en tickets anteriores).
"""
import sqlite3


def log_listen(conn, user_id, client_event_id, track_id, duration_played_seconds, completed, source):
    """Un intento de escucha ya terminado (skip, pausa larga, fin de pista o
    cambio de pista) — el cliente lo dispara una sola vez por track_id, no
    en cada tick de reproducción."""
    try:
        conn.execute(
            '''INSERT INTO listening_events
               (user_id, client_event_id, track_id, duration_played_seconds, completed, source)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (user_id, client_event_id, track_id,
             int(duration_played_seconds or 0), int(bool(completed)), source or 'manual')
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # client_event_id repetido = reintento de red del mismo evento, no
        # es un error real — ya se había registrado antes.
        return False


def log_search(conn, user_id, client_event_id, query_text, result_count):
    try:
        conn.execute(
            '''INSERT INTO search_history (user_id, client_event_id, query_text, result_count)
               VALUES (?, ?, ?, ?)''',
            (user_id, client_event_id, query_text,
             int(result_count) if result_count is not None else None)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def start_session(conn, user_id, client_session_id, device):
    try:
        conn.execute(
            '''INSERT INTO session_log (user_id, client_session_id, device)
               VALUES (?, ?, ?)''',
            (user_id, client_session_id, device)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def end_session(conn, user_id, client_session_id):
    """Idempotente por diseño: solo pisa ended_at si todavía era NULL, así
    que un reintento de red del mismo cierre de sesión no corrompe la
    duración calculada."""
    cur = conn.execute(
        '''UPDATE session_log SET ended_at = datetime('now')
           WHERE user_id=? AND client_session_id=? AND ended_at IS NULL''',
        (user_id, client_session_id)
    )
    conn.commit()
    return cur.rowcount > 0
