"""Ticket AI-04 (Etapa 5 del epic "Agente de IA") — fallback inteligente:
favoritos + perfil + comportamiento, en sus dos niveles (individual y
agregado). Ver AI_AGENT_MASTER_PLAN.md §8/§9.

Reemplaza, solo en el ÚLTIMO tramo antes del backstop de popularidad
global, el fallback provisorio que traía el Ticket AI-01 (que iba directo
de "relajación de filtros agotada" a "top popularidad global sin ningún
criterio personal"). Ese backstop de popularidad global SIGUE existiendo
tal cual en ai_playlist.py — este módulo solo se inserta ANTES, no lo
reemplaza, así que la garantía de "nunca playlist vacía" no cambia.

Módulo nuevo y aislado (mismo criterio que ai_playlist.py y
behavior_engine.py, ver AI_AGENT_MASTER_PLAN.md §3): solo lecturas sobre
tablas existentes (user_favorite_artists, user_item_favorites, users,
listening_events) y las ya creadas por Ticket AI-02. No importa app.py ni
toca _build_adv_filters/_paginate/_track_dedupe_condition — arma sus
propias queries, mismo join tracks/albums/artists/track_meta/
track_pop_cache que ya usa el resto del proyecto, para no depender de
funciones internas de otro módulo.
"""
import json
import random

_POOL_LIMIT = 200


def _rows_to_tracks(rows, track_to_json_fn):
    tracks = []
    for r in rows:
        d = track_to_json_fn(dict(r))
        d['album_id'] = r['album_id']
        d['album_name'] = r['album_name']
        d['album_year'] = r['album_year']
        d['artist_id'] = r['artist_id']
        d['artist_name'] = r['artist_name']
        d['mood'] = r['mood']
        d['pop_score'] = r['pop_score']
        d['stream_url'] = f'/api/v1/stream/{d["id"]}'
        tracks.append(d)
    return tracks


def _favorites_pool(conn, user_id, track_to_json_fn):
    """Nivel individual, señal más fuerte: lo que el usuario ya marcó
    explícitamente como favorito (pistas, artistas —de las dos tablas que
    los guardan—, álbumes y géneros de perfil)."""
    fav_track_ids = [r['item_id'] for r in conn.execute(
        "SELECT item_id FROM user_item_favorites WHERE user_id=? AND item_type='track'", (user_id,)
    ).fetchall()]

    fav_artist_ids = set(r['artist_id'] for r in conn.execute(
        "SELECT artist_id FROM user_favorite_artists WHERE user_id=?", (user_id,)
    ).fetchall())
    fav_artist_ids |= set(r['item_id'] for r in conn.execute(
        "SELECT item_id FROM user_item_favorites WHERE user_id=? AND item_type='artist'", (user_id,)
    ).fetchall())

    fav_album_ids = [r['item_id'] for r in conn.execute(
        "SELECT item_id FROM user_item_favorites WHERE user_id=? AND item_type='album'", (user_id,)
    ).fetchall()]

    row = conn.execute('SELECT favorite_genres_json FROM users WHERE id=?', (user_id,)).fetchone()
    fav_genres = []
    if row and row['favorite_genres_json']:
        try:
            fav_genres = json.loads(row['favorite_genres_json']) or []
        except (ValueError, TypeError):
            fav_genres = []

    if not (fav_track_ids or fav_artist_ids or fav_album_ids or fav_genres):
        return []

    clauses, params = [], []
    if fav_track_ids:
        clauses.append(f"t.id IN ({','.join('?' * len(fav_track_ids))})")
        params += fav_track_ids
    if fav_artist_ids:
        ids = list(fav_artist_ids)
        clauses.append(f"al.artist_id IN ({','.join('?' * len(ids))})")
        params += ids
    if fav_album_ids:
        clauses.append(f"t.album_id IN ({','.join('?' * len(fav_album_ids))})")
        params += fav_album_ids
    if fav_genres:
        genre_clauses = []
        for g in fav_genres:
            genre_clauses.append('(t.genre=? OR tm.genre_primary=? OR tm.genre_secondary=?)')
            params += [g, g, g]
        clauses.append('(' + ' OR '.join(genre_clauses) + ')')

    where = ' OR '.join(clauses)
    sql = f'''SELECT t.*, al.id as album_id, al.name as album_name, al.year as album_year,
                     al.cover_path, ar.id as artist_id, ar.name as artist_name,
                     tm.mood, COALESCE(tpc.pop_score,0) as pop_score
              FROM tracks t
              JOIN albums al ON al.id=t.album_id
              LEFT JOIN artists ar ON ar.id=al.artist_id
              LEFT JOIN track_meta tm ON tm.track_id=t.id
              LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
              WHERE {where}
              ORDER BY COALESCE(tpc.pop_score,0) DESC LIMIT {_POOL_LIMIT}'''
    return _rows_to_tracks(conn.execute(sql, params).fetchall(), track_to_json_fn)


def _individual_behavior_pool(conn, user_id, track_to_json_fn):
    """Nivel individual, segunda señal: lo que este usuario más escuchó de
    verdad (listening_events, Ticket AI-02/AI-03), no lo que dijo que le
    gusta. Cubre al usuario que nunca usó favoritos pero sí tiene
    historial real de reproducción."""
    sql = f'''SELECT t.*, al.id as album_id, al.name as album_name, al.year as album_year,
                     al.cover_path, ar.id as artist_id, ar.name as artist_name,
                     tm.mood, COALESCE(tpc.pop_score,0) as pop_score,
                     COUNT(le.id) as play_count
              FROM listening_events le
              JOIN tracks t ON t.id = le.track_id
              JOIN albums al ON al.id=t.album_id
              LEFT JOIN artists ar ON ar.id=al.artist_id
              LEFT JOIN track_meta tm ON tm.track_id=t.id
              LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
              WHERE le.user_id=?
              GROUP BY t.id
              ORDER BY play_count DESC, COALESCE(tpc.pop_score,0) DESC
              LIMIT {_POOL_LIMIT}'''
    return _rows_to_tracks(conn.execute(sql, (user_id,)).fetchall(), track_to_json_fn)


def _aggregate_behavior_pool(conn, track_to_json_fn):
    """Nivel agregado/global (ver AI_AGENT_MASTER_PLAN.md §8): lo más
    escuchado entre TODOS los usuarios, sin filtrar por user_id. Sirve de
    prior para cold-start — usuario nuevo, sin favoritos ni historial
    propio todavía, pero la base ya tiene señal de otros usuarios."""
    sql = f'''SELECT t.*, al.id as album_id, al.name as album_name, al.year as album_year,
                     al.cover_path, ar.id as artist_id, ar.name as artist_name,
                     tm.mood, COALESCE(tpc.pop_score,0) as pop_score,
                     COUNT(le.id) as play_count
              FROM listening_events le
              JOIN tracks t ON t.id = le.track_id
              JOIN albums al ON al.id=t.album_id
              LEFT JOIN artists ar ON ar.id=al.artist_id
              LEFT JOIN track_meta tm ON tm.track_id=t.id
              LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
              GROUP BY t.id
              ORDER BY play_count DESC, COALESCE(tpc.pop_score,0) DESC
              LIMIT {_POOL_LIMIT}'''
    return _rows_to_tracks(conn.execute(sql).fetchall(), track_to_json_fn)


def personalized_fallback(conn, user_id, track_to_json_fn, limit):
    """Punto de entrada único. Prueba, en orden de señal más fuerte a más
    débil: favoritos -> comportamiento individual -> comportamiento
    agregado. Devuelve (tracks, source_label) — source_label es None si
    ninguna de las tres tuvo nada (usuario nuevo Y base sin uso todavía),
    en cuyo caso quien llama cae al backstop de popularidad global de
    ai_playlist.py, sin cambios respecto al Ticket AI-01."""
    for source_name, pool_fn in (
        ('favorites', lambda: _favorites_pool(conn, user_id, track_to_json_fn)),
        ('individual_behavior', lambda: _individual_behavior_pool(conn, user_id, track_to_json_fn)),
        ('aggregate_behavior', lambda: _aggregate_behavior_pool(conn, track_to_json_fn)),
    ):
        pool = pool_fn()
        if pool:
            sample_size = min(limit, len(pool))
            return random.sample(pool, sample_size), source_name
    return [], None
