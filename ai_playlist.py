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

# Ticket AI-18 (bug reportado por Niko, diagnosticado cruzando log de
# servidor + log de dispositivo con el logueo agregado en AI-17): el
# cliente iOS timeouteaba tanto por LAN como por Tailscale en este
# endpoint específico, y el servidor JAMÁS lo registraba — ni éxito ni
# excepción — porque el pedido seguía procesando en su propio hilo
# (Flask ya corre con threaded=True) mucho después de que el cliente ya
# había tirado la toalla. Con 12s por proveedor y hasta 2 intentos
# secuenciales (Gemini falla -> Groq), el peor caso llegaba a 24s+, muy
# por encima de lo que el cliente esperaba en ese momento.
#
# 8s por proveedor dejan el peor caso (2 proveedores + overhead de DB)
# en ~17s — ver Ticket AI-18 del lado iOS (OrbyteApiClient.swift) para
# los timeouts nuevos de ese lado (25s LAN / 35s Tailscale para este
# endpoint puntual), pensados con margen real sobre este número, no al
# revés. Si se cambia este valor acá, hay que revisar ese lado también.
_HTTP_TIMEOUT_SECONDS = 8
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
        'artists': [], 'albums': [], 'tracks': [], 'genres': [], 'moods': [], 'momentos': [],
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
    out['tracks'] = [a for a in (raw_entities.get('tracks') or []) if a][:5]
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
    "artists": [string], "albums": [string], "tracks": [string],
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
- "tracks": nombres de canciones específicas que el usuario mencione, EN ESPECIAL cuando pide algo \
"parecido a" o "similar a" una canción puntual (ej: "algo parecido a Enter Sandman", "quiero esa canción \
de Metallica que se llama One") — no confundir con "albums" (nombre de un disco) ni con "temas" (de qué \
habla la letra).
- "albums" es el nombre de un disco/álbum específico si el usuario lo menciona (ej: "quiero escuchar \
Master of Puppets entero", "algo del álbum Appetite for Destruction").
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
    los campos en drop_fields (usado por la relajación progresiva).

    Nota (Ticket AI-11, bugfix): 'artists' y 'albums' NO están en
    _ENTITY_TO_FILTER_FIELD a propósito — _build_adv_filters no tiene
    ningún parámetro de artista ni álbum (solo `pais` para
    artists.nationality). El manejo de 'artists' vive aparte, en
    _resolve_artist_ids() + el parámetro artist_ids de _query_tracks, más
    abajo. 'albums' queda sin resolver todavía — ver limitación conocida
    en el ticket."""
    args = {}
    for entity_key, filter_field in _ENTITY_TO_FILTER_FIELD.items():
        if filter_field in drop_fields:
            continue
        vals = entities.get(entity_key) or []
        if vals:
            args[filter_field] = list(vals)
    return args


_ARTIST_MATCH_CUTOFF = 0.75  # más estricto que _closest_match (0.6):
# nombres de artista son mucho más numerosos que el vocabulario de
# género/mood, más riesgo de un falso positivo con un cutoff laxo.


def _resolve_artist_ids(conn, artist_names, build_similar_artists_fn, similar_limit=8):
    """Ticket AI-11 (bugfix, reportado por Niko: "Metallica y similares"
    no encontraba a Metallica pese a que el artista SÍ está en la
    biblioteca). Resuelve cada nombre de artista que el LLM extrajo
    (texto libre, puede venir con mayúsculas/typos distintos) contra la
    tabla real de artists — exacto case-insensitive primero, fuzzy
    después — y expande cada uno a sus artistas similares ya cacheados en
    `artists.similar_artists_json`. Es el MISMO dato que ya alimenta la
    sección "Similares" de cada artista en la app (`build_similar_artists`,
    reusada acá tal cual, inyectada por parámetro — mismo patrón de
    inyección explícita que el resto de este módulo).

    Devuelve un set de artist_id: los nombrados que se pudieron resolver
    + sus similares que efectivamente existen en esta biblioteca (los que
    no, `build_similar_artists_fn` ya los devuelve con id=None y se
    descartan acá)."""
    if not artist_names:
        return set()

    rows = conn.execute('SELECT id, name FROM artists').fetchall()
    name_to_id = {r['name'].strip().lower(): r['id'] for r in rows}

    ids = set()
    for raw_name in artist_names:
        norm = str(raw_name).strip().lower()
        if not norm:
            continue
        matched_id = name_to_id.get(norm)
        if matched_id is None:
            close = difflib.get_close_matches(norm, list(name_to_id.keys()), n=1, cutoff=_ARTIST_MATCH_CUTOFF)
            if close:
                matched_id = name_to_id[close[0]]
        if matched_id is None:
            continue  # el LLM mencionó un artista que no está en la biblioteca — se ignora, no se fuerza nada
        ids.add(matched_id)

        similar_row = conn.execute(
            'SELECT similar_artists_json FROM artists WHERE id=?', (matched_id,)
        ).fetchone()
        if similar_row and similar_row['similar_artists_json']:
            for similar in build_similar_artists_fn(conn, similar_row['similar_artists_json'], limit=similar_limit):
                if similar.get('id'):
                    ids.add(similar['id'])
    return ids


_ALBUM_MATCH_CUTOFF = 0.70   # títulos de álbum varían más en redacción que
# nombres de artista (subtítulos, "(Remastered)", etc.) — un poco más
# laxo que _ARTIST_MATCH_CUTOFF, pero igual bastante por encima del 0.6
# de género/mood.
_TRACK_MATCH_CUTOFF = 0.70


def _name_index(rows, name_field='name'):
    """Helper compartido por _resolve_album_ids/_resolve_track_ids: arma
    un {nombre_normalizado: [ids]} — una lista de ids porque más de una
    fila puede compartir el mismo nombre (álbumes homónimos de distintos
    artistas, o directamente reediciones)."""
    idx = {}
    for r in rows:
        key = r[name_field].strip().lower()
        idx.setdefault(key, []).append(r['id'])
    return idx


def _resolve_names(name_to_resolve, hinted_index, all_rows_fn, cutoff):
    """Motor común de _resolve_album_ids/_resolve_track_ids (Ticket
    AI-12): matchea contra `hinted_index` primero (ej. álbumes/pistas de
    los artistas que ya se resolvieron en este mismo turno, si los hay —
    evita ambigüedad con nombres homónimos de otro artista); si no
    encuentra nada ahí, recién ahí busca en toda la biblioteca
    (`all_rows_fn`, llamada de forma perezosa — solo si hace falta)."""
    norm = str(name_to_resolve).strip().lower()
    if not norm:
        return []

    matched = hinted_index.get(norm)
    if not matched:
        close = difflib.get_close_matches(norm, list(hinted_index.keys()), n=1, cutoff=cutoff)
        if close:
            matched = hinted_index[close[0]]
    if matched:
        return matched

    all_index = all_rows_fn()
    matched = all_index.get(norm)
    if not matched:
        close = difflib.get_close_matches(norm, list(all_index.keys()), n=1, cutoff=cutoff)
        if close:
            matched = all_index[close[0]]
    return matched or []


def _resolve_album_ids(conn, album_names, artist_ids_hint=None):
    """Ticket AI-12 — análogo a _resolve_artist_ids pero para álbumes,
    pedido explícitamente por Niko ("distinguir si parte del prompt es un
    álbum"). Si ya se resolvieron artist_ids en este mismo turno
    (entities.artists), se prioriza matchear el nombre de álbum ENTRE
    esos artistas primero — evita, por ejemplo, que "Master of Puppets"
    dicho junto con "Metallica" se confunda con un álbum homónimo de otro
    artista, si lo hubiera. Sin esa pista, o si no matchea ahí, busca en
    toda la biblioteca (puede devolver más de un álbum con el mismo
    nombre de distintos artistas — se incluyen todos, no se arriesga a
    elegir mal)."""
    if not album_names:
        return set()

    hinted_rows = []
    if artist_ids_hint:
        placeholders = ','.join('?' * len(artist_ids_hint))
        hinted_rows = conn.execute(
            f'SELECT id, name FROM albums WHERE artist_id IN ({placeholders})', list(artist_ids_hint)
        ).fetchall()
    hinted_index = _name_index(hinted_rows)

    all_index_cache = {}

    def _all_albums_index():
        if 'idx' not in all_index_cache:
            all_index_cache['idx'] = _name_index(conn.execute('SELECT id, name FROM albums').fetchall())
        return all_index_cache['idx']

    ids = set()
    for raw_name in album_names:
        for matched_id in _resolve_names(raw_name, hinted_index, _all_albums_index, _ALBUM_MATCH_CUTOFF):
            ids.add(matched_id)
    return ids


def _resolve_track_ids(conn, track_names, artist_ids_hint=None):
    """Ticket AI-12 — resuelve nombres de canción (texto libre del LLM,
    típicamente de un "algo parecido a <canción>") contra tracks.title.
    Mismo criterio de desambiguación por artist_ids_hint que
    _resolve_album_ids — títulos de canción se repiten mucho más entre
    artistas distintos que los de álbum."""
    if not track_names:
        return set()

    hinted_rows = []
    if artist_ids_hint:
        placeholders = ','.join('?' * len(artist_ids_hint))
        hinted_rows = conn.execute(
            f'''SELECT t.id, t.title as name FROM tracks t
                JOIN albums al ON al.id=t.album_id
                WHERE al.artist_id IN ({placeholders})''',
            list(artist_ids_hint)
        ).fetchall()
    hinted_index = _name_index(hinted_rows)

    all_index_cache = {}

    def _all_tracks_index():
        if 'idx' not in all_index_cache:
            all_index_cache['idx'] = _name_index(conn.execute('SELECT id, title as name FROM tracks').fetchall())
        return all_index_cache['idx']

    ids = set()
    for raw_name in track_names:
        for matched_id in _resolve_names(raw_name, hinted_index, _all_tracks_index, _TRACK_MATCH_CUTOFF):
            ids.add(matched_id)
    return ids


def _sample_tracks_for_albums(conn, album_ids, per_album=5):
    """Ticket AI-12 — una muestra de pistas top de cada álbum resuelto,
    para alimentar _expand_via_similar_tracks. No hace falta el álbum
    entero como semilla, unas pocas pistas representativas alcanzan."""
    if not album_ids:
        return set()
    placeholders = ','.join('?' * len(album_ids))
    rows = conn.execute(
        f'''SELECT t.id, t.album_id FROM tracks t
            LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
            WHERE t.album_id IN ({placeholders})
            ORDER BY t.album_id, COALESCE(tpc.pop_score,0) DESC''',
        list(album_ids)
    ).fetchall()
    seen_per_album = {}
    sample = set()
    for r in rows:
        count = seen_per_album.get(r['album_id'], 0)
        if count < per_album:
            sample.add(r['id'])
            seen_per_album[r['album_id']] = count + 1
    return sample


def _expand_via_similar_tracks(conn, seed_track_ids, limit_per_seed=15):
    """Ticket AI-12 — expande un conjunto semilla de track_ids usando
    `track_meta.similar_tracks_json`, el MISMO dato que ya alimenta
    `/api/track/<id>/similar` (modal "Similares" del Now Playing). A
    diferencia de similar_artists_json, acá similar_tracks_json ya
    guarda track_id directo (no un nombre a resolver) — no hace falta
    inyectar ninguna función de app.py para esto, es autocontenido.
    Devuelve el set expandido, incluyendo las semillas originales."""
    ids = set(seed_track_ids)
    if not seed_track_ids:
        return ids
    placeholders = ','.join('?' * len(seed_track_ids))
    rows = conn.execute(
        f'SELECT track_id, similar_tracks_json FROM track_meta WHERE track_id IN ({placeholders})',
        list(seed_track_ids)
    ).fetchall()
    for row in rows:
        if not row['similar_tracks_json']:
            continue
        try:
            similar_raw = json.loads(row['similar_tracks_json'])
        except (ValueError, TypeError):
            continue
        if not isinstance(similar_raw, list):
            continue
        for s in similar_raw[:limit_per_seed]:
            if isinstance(s, dict) and s.get('track_id'):
                ids.add(s['track_id'])
    return ids


def _query_tracks(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                   artist_ids=None, album_ids=None, track_ids=None):
    """Ejecuta la búsqueda de pistas reutilizando EXACTAMENTE el mismo
    join/alias que ya usa _api_search_advanced_payload (view=tracks) en
    app.py — no se reinventa el criterio de matching (ver ticket §3).

    `artist_ids`/`album_ids`/`track_ids` (Tickets AI-11/AI-12): sets
    opcionales de identidad — se combinan entre sí con OR (son formas
    alternativas de decir "qué música", no restricciones simultáneas) y
    ese OR se agrega como AND adicional junto a lo que devuelva
    build_adv_filters_fn (que sí son restricciones que califican sobre
    la selección: género, mood, etc.). Ninguno de los tres pasa por
    build_adv_filters_fn porque esa función no tiene parámetros de
    artista/álbum/pista.
    artista/álbum/pista."""
    from werkzeug.datastructures import MultiDict
    args = MultiDict()
    for field, vals in args_dict.items():
        for v in vals:
            args.add(field, v)

    clauses, params = build_adv_filters_fn(args, pop_alias='tpc', for_albums=False)

    identity_clauses, identity_params = [], []
    if artist_ids:
        identity_clauses.append(f"al.artist_id IN ({','.join('?' * len(artist_ids))})")
        identity_params += list(artist_ids)
    if album_ids:
        identity_clauses.append(f"al.id IN ({','.join('?' * len(album_ids))})")
        identity_params += list(album_ids)
    if track_ids:
        identity_clauses.append(f"t.id IN ({','.join('?' * len(track_ids))})")
        identity_params += list(track_ids)
    if identity_clauses:
        clauses = clauses + ['(' + ' OR '.join(identity_clauses) + ')']
        params = params + identity_params

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


def _personalized_then_global_fallback(conn, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn):
    """Hotfix (Ticket AI-09, reportado por Niko) — escalón final común:
    favoritos/comportamiento (Etapa 5, Ticket AI-04) y, si tampoco hay
    nada ahí, popularidad global sin filtros (backstop original de
    AI-01). Extraído de generate_playlist() a una función propia porque
    ahora lo usan DOS casos, no uno: (a) cuando la relajación de filtros
    se agota sin resultados (mismo lugar de siempre), y (b) cuando el
    intent parser falló por completo — ver el `if result['status'] ==
    'error'` en handle_request más abajo. Antes de este fix, el caso (b)
    no pasaba por acá: al tener entities vacías, `_query_tracks` con
    args_dict={} "matcheaba todo" (sin ningún filtro) y devolvía top
    popularidad global como si fuera un match legítimo con cero
    criterios, sin distinguir "el usuario no pidió nada específico" de
    "el parser nunca corrió" (ej. sin GEMINI_API_KEY/GROQ_API_KEY
    configuradas, o ambos proveedores caídos)."""
    personalized, source = fallback_engine.personalized_fallback(
        conn, user_id, track_to_json_fn, dedupe_condition_fn, limit=_PLAYLIST_SIZE
    )
    if personalized:
        return personalized, {'fallback_source': source}

    pool = _query_tracks(conn, {}, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)
    sample_size = min(_PLAYLIST_SIZE, len(pool))
    return (random.sample(pool, sample_size) if pool else []), {'fallback_source': 'global_popularity'}


def generate_playlist(conn, user_id, entities, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                       build_similar_artists_fn):
    """Filter mapper + relajación progresiva (Etapa 4) + fallback
    inteligente (Etapa 5, Ticket AI-04). Devuelve (tracks,
    filters_applied_dict, used_fallback_bool). Nunca devuelve una lista
    vacía si hay AL MENOS una pista en la biblioteca (último recurso: top
    popularidad global, sin ningún filtro — sin cambios respecto al
    Ticket AI-01).

    Precondición (ver handle_request): `entities` viene de un turno que
    el parser SÍ logró interpretar (status != 'error'). Si el parser
    falló del todo, handle_request no llama a esta función — va directo
    a _personalized_then_global_fallback (ver Ticket AI-09).

    Ticket AI-11/AI-12 (bugfix): artist_ids/album_ids/track_ids se
    calculan UNA sola vez acá arriba y se mantienen constantes durante
    toda la relajación de género/mood/etc. — un artista, álbum o pista
    nombrados explícitamente son señal más central que cualquiera de esos
    campos, no tiene sentido soltarlos antes. Si ni siquiera "identidad +
    similares, sin ningún otro filtro" encuentra nada (último tramo del
    loop, cuando `dropped` ya sacó todo lo demás), recién ahí se cae a
    _personalized_then_global_fallback más abajo.

    track_ids (AI-12) combina dos fuentes, ambas expandidas vía
    _expand_via_similar_tracks (track_meta.similar_tracks_json, el mismo
    dato del modal "Similares" del Now Playing): pistas nombradas
    directamente por el usuario (entities.tracks) y una muestra de pistas
    de los álbumes ya resueltos (entities.albums) — así "el álbum X"
    también se beneficia de una expansión "suena parecido", no solo trae
    las pistas literales del álbum."""
    artist_ids = _resolve_artist_ids(conn, entities.get('artists') or [], build_similar_artists_fn)
    album_ids = _resolve_album_ids(conn, entities.get('albums') or [], artist_ids_hint=artist_ids)
    named_track_ids = _resolve_track_ids(conn, entities.get('tracks') or [], artist_ids_hint=artist_ids)
    similar_seed_ids = named_track_ids | _sample_tracks_for_albums(conn, album_ids)
    track_ids = _expand_via_similar_tracks(conn, similar_seed_ids) if similar_seed_ids else set()

    args_dict = _entities_to_args_dict(entities)
    dropped = set()

    def _filters_applied(args):
        applied = dict(args)
        if artist_ids:
            applied['artist_ids'] = sorted(artist_ids)
        if album_ids:
            applied['album_ids'] = sorted(album_ids)
        if track_ids:
            applied['track_ids'] = sorted(track_ids)
        return applied

    pool = _query_tracks(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                          artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids)
    if pool:
        sample_size = min(_PLAYLIST_SIZE, len(pool))
        return random.sample(pool, sample_size), _filters_applied(args_dict), False

    for field in _RELAXATION_ORDER:
        if field not in args_dict:
            continue
        dropped.add(field)
        retry_args = _entities_to_args_dict(entities, drop_fields=dropped)
        pool = _query_tracks(conn, retry_args, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                              artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids)
        if pool:
            sample_size = min(_PLAYLIST_SIZE, len(pool))
            return random.sample(pool, sample_size), _filters_applied(retry_args), True

    tracks, filters_applied = _personalized_then_global_fallback(
        conn, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)
    return tracks, filters_applied, True


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


def _entities_are_empty(entities):
    return not any(entities.get(key) for key in entities)


def handle_request(conn, user_id, raw_query, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                    build_similar_artists_fn, prior_entities=None):
    """Punto de entrada único, llamado desde app.py::api_v1_ai_playlist.
    Nunca lanza (todo error de proveedor externo se degrada a fallback) salvo
    por errores de la propia base de datos, que sí deben propagarse.

    `build_similar_artists_fn` (Ticket AI-11): `build_similar_artists` de
    app.py, inyectada — resuelve artistas nombrados por el usuario contra
    la biblioteca real y los expande a artistas similares (mismo dato que
    ya alimenta la sección "Similares" de cada artista en la app). Ver
    _resolve_artist_ids.

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

    # Hotfix (Ticket AI-09): si tras el merge las entidades siguen
    # completamente vacías (parser sin proveedores configurados, ambos
    # fallaron, o un turno de conversación sin nada nuevo ni previo que
    # aportar), ir directo al fallback inteligente. Antes de este fix se
    # llamaba igual a generate_playlist(), que con entities vacías
    # "matcheaba todo" (sin ningún filtro) y devolvía popularidad global
    # cruda como si fuera un resultado válido con cero criterios, en vez
    # de favoritos/comportamiento. Si HAY algo de señal (aunque
    # result['status'] sea 'error' pero prior_entities haya aportado algo
    # vía merge), se sigue intentando filtrar por eso primero — no se
    # descarta señal real solo porque este turno puntual falló.
    if _entities_are_empty(result['entities']):
        tracks, filters_applied = _personalized_then_global_fallback(
            conn, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)
        used_fallback = True
    else:
        tracks, filters_applied, used_fallback = generate_playlist(
            conn, user_id, result['entities'], track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            build_similar_artists_fn)
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
