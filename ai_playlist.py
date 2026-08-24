"""Ticket AI-01 (Etapas 3+4 del epic "Agente de IA") — intent parser + filter
mapper + generación de playlist.

Módulo nuevo y aislado a propósito (ver AI_AGENT_MASTER_PLAN.md §3): no toca
ninguna función ni ruta existente de app.py, solo las consume de solo lectura
(_build_adv_filters, _track_dedupe_condition, track_to_json). app.py importa
este módulo y expone un único endpoint nuevo (/api/v1/ai/playlist) que llama
a handle_request().

Variables de entorno NUEVAS que introduce este ticket (no existían antes,
ver AGENTE.md regla 2 — se documentan acá y en el ticket, no se inventan
silenciosamente):
  GEMINI_API_KEY   — clave de Google AI Studio. Sin ella, Gemini se salta.
  GEMINI_MODEL     — default 'gemini-2.5-flash'.
  GROQ_API_KEY     — clave de GroqCloud. Sin ella, Groq se salta.
  GROQ_MODEL       — default 'llama-3.3-70b-versatile'.
Si ninguna de las dos claves está seteada, handle_request() nunca intenta
llamar a un proveedor externo y va directo al fallback de popularidad — la
ruta no se cae, solo entrega una playlist más genérica (ver PROVIDER_STATUS).

Dependencia externa: usa el mismo 'requests' que app.py ya importa de forma
opcional (try/except ImportError) — no se agrega ninguna dependencia nueva
al proyecto más que esa (que sí hay que sumar a start.sh, ver el ticket).
"""
import json
import time
import random
import difflib

import fallback_engine  # Ticket AI-04 — fallback inteligente (Etapa 5), módulo aislado

try:
    import requests
except ImportError:
    requests = None

import os

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'llama-3.3-70b-versatile')

_HTTP_TIMEOUT_SECONDS = 12
_PLAYLIST_SIZE = 25
_CANDIDATE_POOL_SIZE = 150  # de dónde se muestrea la playlist final

# Dimensiones de _build_adv_filters que el intent parser puede llenar.
# 'place' y 'motivation' se extraen y se registran igual (valor informativo /
# insumo futuro de personalización) pero hoy no filtran nada — no existe
# columna equivalente en track_meta/album_meta (ver AI_AGENT_MASTER_PLAN.md §7).
_ENTITY_TO_FILTER_FIELD = {
    'genres': 'genero',
    'moods': 'mood',
    'momentos': 'momento',
    'eras': 'era',
    'temas': 'tema',
    'idiomas': 'idioma',
    'paises': 'pais',
}

# Orden de relajación del fallback provisorio (Etapa 4, ver ticket §5): si la
# consulta con todos los filtros no devuelve nada, se van soltando del más
# periférico al más central hasta encontrar resultados. 'genero' y 'artists'
# (manejados aparte, ver _apply_artist_filter) se sueltan al final porque
# suelen ser lo más central de la intención del usuario.
_RELAXATION_ORDER = ['idioma', 'pais', 'era', 'tema', 'momento', 'mood', 'genero']

PROVIDER_STATUS = {
    'gemini_configured': bool(GEMINI_API_KEY),
    'groq_configured': bool(GROQ_API_KEY),
}


def _empty_entities():
    return {
        'artists': [], 'albums': [], 'genres': [], 'moods': [], 'momentos': [],
        'eras': [], 'temas': [], 'idiomas': [], 'paises': [],
        'place': None, 'motivation': None,
    }


def _get_full_vocab(conn):
    """Listas COMPLETAS (sin el LIMIT pensado para selector visual que trae
    _advanced_search_options) para hacer matching contra el texto libre del
    usuario. Consulta nueva y propia — no modifica _advanced_search_options.
    """
    def _col(table, col):
        rows = conn.execute(
            f'SELECT DISTINCT {col} AS v FROM {table} WHERE {col} IS NOT NULL AND {col}!=""'
        ).fetchall()
        return [r['v'] for r in rows]

    return {
        'moods': _col('track_meta', 'mood'),
        'momentos': _col('track_meta', 'momento'),
        'eras': ['early_rock_era', 'british_invasion_era', 'classic_rock_era',
                 'nwobhm_synth_era', 'grunge_alternative_era',
                 'post_millennial_era', 'streaming_era', 'current_era'],
        'temas': _col('track_meta', 'tema_lirico'),
        'idiomas': _col('track_meta', 'idioma'),
        'paises': _col('artists', 'nationality'),
        'genres': sorted(set(_col('tracks', 'genre'))
                          | set(_col('track_meta', 'genre_primary'))
                          | set(_col('track_meta', 'genre_secondary'))),
    }


def _closest_match(value, options, cutoff=0.6):
    """Coincidencia más cercana simple (difflib, stdlib, sin dependencias
    nuevas) contra el vocabulario canónico. Reemplazo liviano para la
    primera ola — extenderlo a una taxonomía jerárquica tipo
    genre_similarity (familia -> subfamilia) queda para una iteración
    posterior (ver AI_AGENT_MASTER_PLAN.md §7)."""
    if not value or not options:
        return None
    value_norm = value.strip().lower()
    for opt in options:
        if opt.strip().lower() == value_norm:
            return opt
    matches = difflib.get_close_matches(value_norm, [o.lower() for o in options], n=1, cutoff=cutoff)
    if not matches:
        return None
    for opt in options:
        if opt.lower() == matches[0]:
            return opt
    return None


def _normalize_entities(raw_entities, vocab):
    """Normaliza cada valor devuelto por el LLM contra el vocabulario real de
    la base, descartando lo que no matchea nada (nunca fuerza un match malo)."""
    out = _empty_entities()
    out['artists'] = [a for a in (raw_entities.get('artists') or []) if a][:5]
    out['albums'] = [a for a in (raw_entities.get('albums') or []) if a][:5]
    out['place'] = (raw_entities.get('place') or None)
    out['motivation'] = (raw_entities.get('motivation') or None)

    for field, vocab_key in (('genres', 'genres'), ('moods', 'moods'),
                              ('momentos', 'momentos'), ('eras', 'eras'),
                              ('temas', 'temas'), ('idiomas', 'idiomas'),
                              ('paises', 'paises')):
        vals = raw_entities.get(field) or []
        if not isinstance(vals, list):
            vals = [vals]
        matched = []
        for v in vals:
            m = _closest_match(str(v), vocab.get(vocab_key, []))
            if m and m not in matched:
                matched.append(m)
        out[field] = matched
    return out


_SYSTEM_PROMPT_TEMPLATE = """Eres el intérprete de intención musical de Orbyte, un sistema de streaming \
personal. El usuario describe en lenguaje natural (español) qué quiere escuchar. Tu trabajo es extraer \
entidades y devolver SOLO un objeto JSON (sin markdown, sin texto extra) con esta forma exacta:

{{
  "status": "resolved" o "needs_clarification",
  "entities": {{
    "artists": [string], "albums": [string],
    "genres": [string], "moods": [string], "momentos": [string],
    "eras": [string], "temas": [string], "idiomas": [string], "paises": [string],
    "place": string o null, "motivation": string o null
  }},
  "confidence": número entre 0 y 1,
  "question": string o null,
  "missing_fields": [string]
}}

Reglas:
- Usa "needs_clarification" solo si la petición es demasiado vaga para extraer NINGUNA entidad útil \
(ej: "ponme algo"). En ese caso "question" debe ser una sola pregunta corta en español y "missing_fields" \
debe listar qué falta (ej: ["mood"]).
- Para genres/moods/momentos/eras/temas/idiomas/paises: propón el valor que mejor describa la intención \
del usuario en tus propias palabras, no hace falta que coincida exacto con ningún catálogo — el sistema \
hace el matching después. Ejemplos de vocabulario ya usado en el catálogo real, como referencia de estilo \
(no son la lista completa): moods={moods_sample}; momentos={momentos_sample}; temas={temas_sample}; \
eras=[early_rock_era, british_invasion_era, classic_rock_era, nwobhm_synth_era, grunge_alternative_era, \
post_millennial_era, streaming_era, current_era].
- "motivation" es el propósito de la escucha si el usuario lo menciona (ej: "para entrenar", "para \
estudiar") — no es un filtro, es contexto.
- "place" es un lugar mencionado explícitamente (ej: "para un roadtrip"), si aplica.
- confidence refleja qué tan segura es tu extracción, no cuántas entidades encontraste — una petición \
simple y clara ("Rock de los 80s") puede tener confidence alta con pocas entidades.
- Responde ÚNICAMENTE el JSON, nada más.

Petición del usuario: {user_query}"""


def _build_system_prompt(user_query, vocab):
    def sample(key, n=8):
        vals = vocab.get(key) or []
        return json.dumps(vals[:n], ensure_ascii=False)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        moods_sample=sample('moods'), momentos_sample=sample('momentos'),
        temas_sample=sample('temas'), user_query=user_query,
    )


def _extract_json_object(text):
    """Los LLMs a veces envuelven el JSON en ```json ... ``` pese a que se les
    pide que no lo hagan — se lo saca antes de json.loads, sin tocar nada más."""
    t = text.strip()
    if t.startswith('```'):
        t = t.strip('`')
        if t.lower().startswith('json'):
            t = t[4:]
    return json.loads(t.strip())


def _call_gemini(prompt):
    if not (requests and GEMINI_API_KEY):
        return None
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
           f'{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}')
    body = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'response_mime_type': 'application/json', 'temperature': 0.2},
    }
    resp = requests.post(url, json=body, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    text = data['candidates'][0]['content']['parts'][0]['text']
    return _extract_json_object(text)


def _call_groq(prompt):
    if not (requests and GROQ_API_KEY):
        return None
    url = 'https://api.groq.com/openai/v1/chat/completions'
    headers = {'Authorization': f'Bearer {GROQ_API_KEY}', 'Content-Type': 'application/json'}
    body = {
        'model': GROQ_MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'response_format': {'type': 'json_object'},
        'temperature': 0.2,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    text = data['choices'][0]['message']['content']
    return _extract_json_object(text)


def interpret_query(conn, raw_query):
    """Devuelve (parsed_dict, provider_used_or_None). parsed_dict sigue el
    schema de AI_AGENT_MASTER_PLAN.md §6. Gemini primero, Groq como
    respaldo — ver ticket §3 para la justificación de por qué ese orden."""
    vocab = _get_full_vocab(conn)
    prompt = _build_system_prompt(raw_query, vocab)

    for provider_name, call_fn in (('gemini', _call_gemini), ('groq', _call_groq)):
        try:
            parsed = call_fn(prompt)
        except Exception:
            parsed = None
        if not parsed:
            continue
        entities = _normalize_entities(parsed.get('entities') or {}, vocab)
        return {
            'status': parsed.get('status') if parsed.get('status') in
                      ('resolved', 'needs_clarification') else 'resolved',
            'entities': entities,
            'confidence': float(parsed.get('confidence') or 0.0),
            'question': parsed.get('question'),
            'missing_fields': parsed.get('missing_fields') or [],
        }, provider_name

    # Ningún proveedor disponible o ambos fallaron.
    return {
        'status': 'error', 'entities': _empty_entities(), 'confidence': 0.0,
        'question': None, 'missing_fields': [],
    }, None


def _entities_to_args_dict(entities, drop_fields=()):
    """Arma el dict de listas que espera MultiDict (y por lo tanto
    _build_adv_filters) a partir de las entidades ya normalizadas, salteando
    los campos en drop_fields (usado por la relajación progresiva)."""
    args = {}
    for entity_key, filter_field in _ENTITY_TO_FILTER_FIELD.items():
        if filter_field in drop_fields:
            continue
        vals = entities.get(entity_key) or []
        if vals:
            args[filter_field] = list(vals)
    return args


def _query_tracks(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn):
    """Ejecuta la búsqueda de pistas reutilizando EXACTAMENTE el mismo
    join/alias que ya usa _api_search_advanced_payload (view=tracks) en
    app.py — no se reinventa el criterio de matching (ver ticket §3)."""
    from werkzeug.datastructures import MultiDict
    args = MultiDict()
    for field, vals in args_dict.items():
        for v in vals:
            args.add(field, v)

    clauses, params = build_adv_filters_fn(args, pop_alias='tpc', for_albums=False)
    extra_where = ' AND '.join(clauses)
    dedupe_clause = dedupe_condition_fn(extra_where=extra_where, track_alias='t', pop_alias='tpc')
    clauses = clauses + [dedupe_clause]
    params = params + params
    where = (' AND ' + ' AND '.join(clauses)) if clauses else ''

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
                   ORDER BY COALESCE(tpc.pop_score,0) DESC LIMIT ?'''
    rows = conn.execute(data_sql, params + [_CANDIDATE_POOL_SIZE]).fetchall()

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


def generate_playlist(conn, user_id, entities, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn):
    """Filter mapper + relajación progresiva (Etapa 4) + fallback
    inteligente (Etapa 5, Ticket AI-04). Devuelve (tracks,
    filters_applied_dict, used_fallback_bool). Nunca devuelve una lista
    vacía si hay AL MENOS una pista en la biblioteca (último recurso: top
    popularidad global, sin ningún filtro — sin cambios respecto al
    Ticket AI-01)."""
    args_dict = _entities_to_args_dict(entities)
    dropped = set()

    pool = _query_tracks(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)
    if pool:
        sample_size = min(_PLAYLIST_SIZE, len(pool))
        return random.sample(pool, sample_size), args_dict, False

    for field in _RELAXATION_ORDER:
        if field not in args_dict:
            continue
        dropped.add(field)
        retry_args = _entities_to_args_dict(entities, drop_fields=dropped)
        pool = _query_tracks(conn, retry_args, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)
        if pool:
            sample_size = min(_PLAYLIST_SIZE, len(pool))
            return random.sample(pool, sample_size), retry_args, True

    # Ticket AI-04 (Etapa 5): antes de caer al backstop de popularidad
    # global sin ningún criterio personal, probar favoritos ->
    # comportamiento individual -> comportamiento agregado. Reemplaza acá
    # el fallback provisorio del Ticket AI-01, que iba directo a la línea
    # de abajo.
    personalized, source = fallback_engine.personalized_fallback(
        conn, user_id, track_to_json_fn, limit=_PLAYLIST_SIZE
    )
    if personalized:
        return personalized, {'fallback_source': source}, True

    # Último recurso: top popularidad global, sin filtros.
    pool = _query_tracks(conn, {}, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)
    sample_size = min(_PLAYLIST_SIZE, len(pool))
    return (random.sample(pool, sample_size) if pool else []), {'fallback_source': 'global_popularity'}, True


def _log_request(conn, user_id, raw_query, result, provider, filters_applied, used_fallback, track_count):
    cur = conn.execute(
        '''INSERT INTO ai_playlist_requests
           (user_id, raw_query, status, parsed_intent_json, filters_applied_json,
            used_fallback, confidence, provider, track_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, raw_query, result['status'], json.dumps(result['entities'], ensure_ascii=False),
         json.dumps(filters_applied, ensure_ascii=False), int(used_fallback),
         result['confidence'], provider, track_count)
    )
    conn.commit()
    return cur.lastrowid


def record_feedback(conn, user_id, request_id, rating, comment, saved_playlist_id):
    """Ticket AI-05 (Etapa 7). UPDATE simple y naturalmente idempotente —
    a diferencia de behavior_engine.py no hace falta un client_event_id
    para dedupe: reenviar el mismo rating/comentario solo vuelve a pisar
    el mismo valor, no duplica ninguna fila. `WHERE user_id=?` a propósito:
    evita que un usuario le deje feedback a una petición ajena."""
    fields, params = [], []
    if rating is not None:
        fields.append('rating=?')
        params.append(rating)
    if comment is not None:
        fields.append('feedback_comment=?')
        params.append(comment)
    if saved_playlist_id is not None:
        fields.append('saved_playlist_id=?')
        params.append(saved_playlist_id)
    fields.append("feedback_at=datetime('now')")
    params += [request_id, user_id]
    cur = conn.execute(
        f"UPDATE ai_playlist_requests SET {', '.join(fields)} WHERE id=? AND user_id=?",
        params
    )
    conn.commit()
    return cur.rowcount > 0


def _merge_entities(prior, new):
    """Ticket AI-07 (Etapa 6) — fusión de entidades entre turnos de una
    misma conversación. Regla simple, a propósito: por cada campo, si el
    turno NUEVO trajo algo, gana; si no, se conserva lo del turno previo.
    No se le pide nada de esto al LLM (el prompt de cada turno sigue
    siendo el mismo de AI-01, sin cambios) — el merge es puramente
    determinístico, del lado de Python, después de parsear el turno nuevo
    de forma aislada. Mantiene el prompt simple y evita depender de que
    el LLM recuerde contexto de turnos anteriores."""
    merged = _empty_entities()
    for key, empty_val in merged.items():
        new_val = new.get(key)
        prior_val = prior.get(key) if prior else None
        if isinstance(empty_val, list):
            merged[key] = new_val if new_val else (prior_val or [])
        else:
            merged[key] = new_val if new_val else prior_val
    return merged


def handle_request(conn, user_id, raw_query, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                    prior_entities=None):
    """Punto de entrada único, llamado desde app.py::api_v1_ai_playlist.
    Nunca lanza (todo error de proveedor externo se degrada a fallback) salvo
    por errores de la propia base de datos, que sí deben propagarse.

    `prior_entities` (Ticket AI-07, Etapa 6): si el cliente manda las
    entidades del turno anterior de la misma conversación (ej. el usuario
    está respondiendo una pregunta de aclaración), se fusionan con lo que
    se extraiga de este turno antes de mapear a filtros — ver
    _merge_entities. Sin este parámetro (valor por defecto None) el
    comportamiento es idéntico al de antes de este ticket: cada llamada
    es un turno aislado, como en AI-01."""
    t0 = time.time()
    result, provider = interpret_query(conn, raw_query)

    if prior_entities:
        result['entities'] = _merge_entities(prior_entities, result['entities'])

    tracks, filters_applied, used_fallback = generate_playlist(
        conn, user_id, result['entities'], track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)

    if result['status'] == 'error':
        used_fallback = True

    _request_id = _log_request(conn, user_id, raw_query, result, provider, filters_applied,
                                used_fallback, len(tracks))

    return {
        'request_id': _request_id,
        'query': raw_query,
        'status': result['status'],
        'entities': result['entities'],
        'confidence': result['confidence'],
        'question': result.get('question'),
        'filters_applied': filters_applied,
        'used_fallback': used_fallback,
        'provider': provider,
        'tracks': tracks,
        'track_count': len(tracks),
        'elapsed_ms': int((time.time() - t0) * 1000),
    }
