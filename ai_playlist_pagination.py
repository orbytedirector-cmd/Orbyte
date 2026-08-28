"""Ticket AI-27 (pedido por Niko) — paginación de resultados de Orbitron
("Expandir"), en un módulo propio y exclusivo para esto, sin tocar
generate_playlist() ni las funciones de consulta (_query_tracks,
fallback_engine.py, la cascada de AI-24) que ya usaba el resto de la
app y debían quedar sin modificar — mismo criterio de aislamiento que
ya usan ai_playlist.py y fallback_engine.py (ver AI_AGENT_MASTER_PLAN.md
§3), llevado un paso más allá: ni siquiera comparte parámetros nuevos
con esas funciones.

Contexto del porqué (Tickets AI-25/AI-26, revertidos): la primera
versión de "Expandir" re-consultaba la base en cada tanda, mandando una
lista creciente de qué excluir — eso obligó a tocar 6 funciones
compartidas (_query_tracks, _query_tracks_own_listens,
_cascade_ranked_tracks, _personalized_then_global_fallback, y los 3
niveles de fallback_engine.py) para que supieran excluir. Funcionaba,
pero Niko pidió explícitamente no seguir tocando ese código ya estable,
y además encontró un bug real en el camino (el dedupe compartido con
"más popular por artista" y demás solo compara duplicados DENTRO de
cada consulta — excluir por id exacto entre tandas dejaba pasar
versiones distintas del mismo tema).

Idea de Niko, validada: en vez de re-consultar con exclusiones, se le
pide a generate_playlist() UN pool más amplio de una sola vez al armar
la respuesta original (ya deduplicado/ordenado/filtrado exactamente
como siempre — nada nuevo ahí, ni un parámetro más en esas funciones),
se guarda ese pool completo acá, y "Expandir" simplemente sirve la
próxima tanda de ESE mismo pool ya resuelto. Nunca hay una segunda
consulta con criterios de dedupe/ranking — sirve directamente lo que
generate_playlist ya decidió una vez, en el mismo orden.
"""
import json

# Cuánto se le pide de más a generate_playlist (vía entities['cantidad']
# temporalmente ampliado, ver fetch_size_for) al armar la respuesta
# original, para tener margen de sobra para paginar después. Mismo
# orden de magnitud que _CANDIDATE_POOL_SIZE de ai_playlist.py — no es
# casualidad, es el pool natural del que ya se muestrea cuando no hay
# ranking.
_POOL_FETCH_SIZE = 150


def fetch_size_for(display_size):
    """Cuánto pedirle a generate_playlist para tener de dónde paginar
    después. Nunca menos que lo que el usuario efectivamente va a ver
    en la respuesta original."""
    return max(display_size, _POOL_FETCH_SIZE)


def store_pool(conn, request_id, pool_tracks, already_shown_count):
    """Guarda el pool COMPLETO que devolvió generate_playlist (no solo
    lo mostrado en la respuesta original) y cuántos de esos ya se
    entregaron — "Expandir" arranca a servir desde ahí, sin volver a
    consultar nada más que los datos puntuales de esas pistas."""
    pool_ids = [t['id'] for t in pool_tracks]
    conn.execute(
        'UPDATE ai_playlist_requests SET candidate_pool_json=?, pool_served_count=? WHERE id=?',
        (json.dumps(pool_ids), already_shown_count, request_id)
    )
    conn.commit()


def next_page(conn, user_id, request_id, track_to_json_fn):
    """Sirve la próxima tanda del pool ya guardado por store_pool — sin
    ninguna consulta con criterios de dedupe/ranking/exclusión, solo lee
    qué track_id siguen en la lista guardada y trae sus datos completos,
    respetando el mismo orden en que quedaron guardados (importante
    para los modos con ranking real: ese orden ES el ranking, no algo
    que se pueda recalcular después).

    El tamaño de cada tanda es el mismo `track_count` que ya mostró la
    respuesta original (si el usuario pidió "Top 10", cada "Expandir" da
    10 más; si no especificó cantidad, da 25 más — sin necesitar que
    quien llama a esto tenga que volver a saber ese número).

    `WHERE id=? AND user_id=?`, mismo criterio que record_feedback en
    ai_playlist.py — evita que un usuario expanda una petición ajena.

    Devuelve:
      - None si la petición no existe o no es de este usuario (el
        caller en app.py lo traduce a 404).
      - Lista vacía (no None) si el pool ya se agotó — distinción
        importante para que el botón "Expandir" del lado de iOS sepa
        cuándo ocultarse."""
    row = conn.execute(
        'SELECT candidate_pool_json, pool_served_count, track_count '
        'FROM ai_playlist_requests WHERE id=? AND user_id=?',
        (request_id, user_id)
    ).fetchone()
    if not row:
        return None

    page_size = row['track_count'] or _POOL_FETCH_SIZE
    try:
        pool_ids = json.loads(row['candidate_pool_json'] or '[]')
    except (ValueError, TypeError):
        pool_ids = []
    served = row['pool_served_count'] or 0
    next_ids = pool_ids[served:served + page_size]
    if not next_ids:
        return []

    placeholders = ','.join('?' * len(next_ids))
    rows = conn.execute(
        f'''SELECT t.*, al.id as album_id, al.name as album_name, al.year as album_year,
                   al.cover_path, ar.id as artist_id, ar.name as artist_name, tm.mood,
                   COALESCE(tpc.pop_score,0) as pop_score
            FROM tracks t
            JOIN albums al ON al.id=t.album_id
            LEFT JOIN artists ar ON ar.id=al.artist_id
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
            WHERE t.id IN ({placeholders})''',
        next_ids
    ).fetchall()
    by_id = {r['id']: r for r in rows}

    ordered_tracks = []
    for tid in next_ids:
        r = by_id.get(tid)
        if not r:
            continue  # pista borrada de la biblioteca entre medio -- se salta sin romper nada
        d = track_to_json_fn(dict(r))
        d['album_id'] = r['album_id']
        d['album_name'] = r['album_name']
        d['album_year'] = r['album_year']
        d['artist_id'] = r['artist_id']
        d['artist_name'] = r['artist_name']
        d['mood'] = r['mood']
        d['pop_score'] = r['pop_score']
        d['stream_url'] = f'/api/v1/stream/{d["id"]}'
        ordered_tracks.append(d)

    conn.execute('UPDATE ai_playlist_requests SET pool_served_count=? WHERE id=?',
                 (served + len(next_ids), request_id))
    conn.commit()
    return ordered_tracks
