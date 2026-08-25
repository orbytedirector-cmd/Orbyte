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
  GEMINI_MODEL     — default 'gemini-3.6-flash' (Ticket AI-20 — el
                     anterior, 'gemini-2.5-flash', dejó de existir).
  GROQ_API_KEY     — clave de GroqCloud. Sin ella, Groq se salta.
  GROQ_MODEL       — default 'openai/gpt-oss-120b' (Ticket AI-20 — el
                     anterior, 'llama-3.3-70b-versatile', fue dado de
                     baja por Groq el 16/08/2026).
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
import logging

# Ticket AI-19 (bug reportado por Niko: "si fuera tema de api key no
# deberíamos tener la confirmación en los logs??" — tenía toda la
# razón, no la había). No hace falta inyectar nada de app.py para esto:
# el logger raíz ya está configurado ahí (RotatingFileHandler +
# formatter JSONL, ver app.py) y cualquier logger con nombre propagra
# hacia arriba por default — con pedir logging.getLogger('ai_playlist')
# alcanza para que esto aparezca en orbyte.log con el mismo formato de
# siempre, sin ningún parámetro nuevo que threadear por todos lados.
_logger = logging.getLogger('ai_playlist')

import fallback_engine  # Ticket AI-04 — fallback inteligente (Etapa 5), módulo aislado

try:
    import requests
except ImportError:
    requests = None

import os

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
# Ticket AI-20 (bug reportado por Niko, confirmado con el logueo del
# Ticket AI-19): 'gemini-2.5-flash' ya no existe — 404 real de Google:
# "This model models/gemini-2.5-flash is no longer available to new
# users. Please update your code to use models/gemini-3.6-flash".
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.6-flash')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
# Ticket AI-20: 'llama-3.3-70b-versatile' se dio de baja el 16/08/2026
# (confirmado en console.groq.com/docs/deprecations) — reemplazo
# recomendado por Groq: openai/gpt-oss-120b.
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b')

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
    # Ticket AI-22 (pedido por Niko): 'anios' — año(s) específicos
    # (ej. "de 2015", "de 1986"), distinto de 'eras' (períodos amplios
    # tipo classic_rock_era). _build_adv_filters ya soportaba el filtro
    # 'anio' desde antes de este epic (albums.year) — nunca estuvo
    # conectado al parser de IA hasta ahora.
    'anios': 'anio',
}

# Orden de relajación del fallback provisorio (Etapa 4, ver ticket §5): si la
# consulta con todos los filtros no devuelve nada, se van soltando del más
# periférico al más central hasta encontrar resultados. 'genero' y 'artists'
# (manejados aparte, ver _apply_artist_filter) se sueltan al final porque
# suelen ser lo más central de la intención del usuario.
_RELAXATION_ORDER = ['idioma', 'pais', 'era', 'tema', 'momento', 'mood', 'genero']

# Ticket AI-22 (pedido por Niko) — valores válidos del campo 'ranking':
# a diferencia de género/mood/etc (texto libre que se resuelve por
# fuzzy-match contra un vocabulario), esto es un enum cerrado — el LLM
# tiene que devolver EXACTAMENTE uno de estos 4 valores o None. Ver
# _normalize_entities: si devuelve cualquier otra cosa, se descarta (no
# se intenta adivinar la más parecida, a diferencia del resto de los
# campos).
#
# 'popularidad_global'  -> lastfm_listeners (cuántos OYENTES distintos)
# 'escuchas_global'     -> lastfm_playcount (cuántas REPRODUCCIONES en total)
# 'escuchas_propias'    -> listening_events de este usuario en Orbyte
# 'infravalorado'       -> pocos oyentes globales, pero ratio
#                          reproducciones/oyente alto (definición de
#                          Niko: "poca gente lo escucha, pero a esa poca
#                          gente le encanta")
_RANKING_VALUES = {'popularidad_global', 'escuchas_global', 'escuchas_propias', 'infravalorado'}

PROVIDER_STATUS = {
    'gemini_configured': bool(GEMINI_API_KEY),
    'groq_configured': bool(GROQ_API_KEY),
}


def _empty_entities():
    return {
        'artists': [], 'albums': [], 'tracks': [], 'genres': [], 'moods': [], 'momentos': [],
        'eras': [], 'temas': [], 'idiomas': [], 'paises': [], 'anios': [],
        'ranking': None,
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

    # Ticket AI-22: 'anios' no pasa por _closest_match — son números, no
    # vocabulario a matchear por texto. _build_adv_filters ya filtra
    # cualquier valor no-numérico del lado del servidor (ver
    # `anio_vals = [v for v in args.getlist('anio') if
    # v.lstrip('-').isdigit()]` en app.py), pero se valida acá también
    # para no arrastrar basura a filters_applied/al log si el LLM
    # devuelve algo raro (ej. "los 2000s" en vez de un año puntual).
    anios_raw = raw_entities.get('anios') or []
    if not isinstance(anios_raw, list):
        anios_raw = [anios_raw]
    out['anios'] = [str(int(a)) for a in anios_raw if str(a).strip().lstrip('-').isdigit()][:5]

    # Ticket AI-22: 'ranking' es un enum cerrado — a diferencia del resto
    # de los campos, NO se intenta fuzzy-match si el LLM devuelve algo
    # fuera de _RANKING_VALUES. Mejor ranking=None (se ignora, cae al
    # orden default por pop_score) que forzar un criterio de orden que
    # el usuario no pidió.
    ranking_raw = raw_entities.get('ranking')
    out['ranking'] = ranking_raw if ranking_raw in _RANKING_VALUES else None

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
    "anios": [número], "ranking": string o null,
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
- "anios": años puntuales que el usuario mencione (ej: "de 2015", "canciones de 1986") — un número por \
año, no un rango como texto. Distinto de "eras" (períodos amplios tipo "los 80s" o "rock clásico") — si \
el usuario da un año exacto, va acá; si describe una época en general, va en "eras".
- "ranking": tiene que ser EXACTAMENTE uno de estos 4 valores, o null si el usuario no pidió ningún \
orden de popularidad/escuchas en particular:
  * "popularidad_global": el usuario pide lo más POPULAR/FAMOSO/CONOCIDO en general (ej: "lo más \
popular de Metallica", "los hits de Queen", "lo más famoso del género"). Se mide en cantidad de OYENTES \
distintos a nivel mundial (lastfm_listeners) — cuánta gente lo conoce, no cuántas veces se reprodujo.
  * "escuchas_global": el usuario pide lo más ESCUCHADO/REPRODUCIDO, sin calificar que sea "de nosotros" \
o "en casa" (ej: "lo más escuchado de Lord Huron", "las canciones más reproducidas del rock alternativo"). \
Se mide en cantidad total de REPRODUCCIONES a nivel mundial (lastfm_playcount) — puede diferir de \
popularidad_global (algo con pocos oyentes muy fieles que lo repiten mucho puede tener más reproducciones \
que oyentes distintos).
  * "escuchas_propias": el usuario pide lo que ÉL/ELLA o "nosotros"/"en casa" escuchó más, no lo popular \
en el mundo (ej: "lo que más escuchamos de Queen", "mis canciones más escuchadas", "lo que más sonó en \
casa este mes"). Señal clara: primera persona o referencia a "nosotros"/nuestra casa, no al público \
general.
  * "infravalorado": el usuario pide algo POCO CONOCIDO pero BUENO — "infravalorado", "subestimado", \
"que no es tan conocido pero vale la pena", "joyitas ocultas", "hidden gems" (ej: "lo más infravalorado \
de Radiohead", "canciones subestimadas del jazz").
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
    # Ticket AI-21 (bug de seguridad, encontrado porque la protección de
    # push de GitHub bloqueó un commit con la key expuesta): antes la key
    # iba metida en la URL como query param (?key=...). Cualquier cosa
    # que loguee o reporte esa URL —una excepción, un proxy, esto mismo—
    # termina exponiendo la key en texto plano. Google soporta mandarla
    # como header en su lugar (`x-goog-api-key`), que no queda pegado a
    # la URL en ningún lado. La URL en sí ya no tiene ningún secreto.
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
    headers = {'x-goog-api-key': GEMINI_API_KEY, 'Content-Type': 'application/json'}
    body = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {'response_mime_type': 'application/json', 'temperature': 0.2},
    }
    resp = requests.post(url, headers=headers, json=body, timeout=_HTTP_TIMEOUT_SECONDS)
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
        except requests.exceptions.HTTPError as e:
            # Ticket AI-19: acá es donde iba a aparecer, por ejemplo, un
            # 401 de Gemini por el problema activo de Google con las keys
            # con prefijo "AQ." (ver ticket) — antes esto se perdía sin
            # dejar rastro. e.response.text trae el cuerpo del error tal
            # cual lo manda el proveedor (útil para diagnosticar sin
            # tener que reproducir la llamada a mano).
            #
            # Ticket AI-21 (bug de seguridad): antes esta línea logueaba
            # `e` directo con %s — la representación en texto de
            # requests.exceptions.HTTPError incluye la URL completa del
            # request que falló, y esa URL traía la API key de Gemini
            # como query param (ver _call_gemini). GitHub bloqueó un push
            # de Niko por esto mismo. Ahora se arman a mano solo los
            # campos puntuales que hacen falta — nunca se referencia `e`
            # directo, así que no importa qué termine incluyendo su
            # representación en texto por dentro.
            status = e.response.status_code if e.response is not None else '?'
            reason = e.response.reason if e.response is not None else ''
            body_preview = (e.response.text or '')[:300] if e.response is not None else ''
            _logger.warning(
                'proveedor %s falló con HTTP %s (%s) — body: %s',
                provider_name, status, reason, body_preview
            )
            parsed = None
        except Exception as e:
            # Mismo criterio que arriba: nombre de la excepción + mensaje
            # acotado a 200 caracteres, nunca el objeto `e` completo.
            _logger.warning('proveedor %s falló: %s: %s', provider_name, type(e).__name__, str(e)[:200])
            parsed = None
        if not parsed:
            continue
        _logger.info('proveedor %s respondió OK', provider_name)
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


# Ticket AI-22 (pedido por Niko) — bajo cuántos oyentes globales (Last.fm)
# se considera que un track puede evaluarse para "infravalorado". Sin
# este piso, un track con 1 oyente y 3 reproducciones (ratio=3) le
# ganaría a uno con 200 oyentes y 3000 reproducciones (ratio=15) por
# pura casualidad estadística de muestra chica — el piso exige que haya
# una base mínima de oyentes reales antes de confiar en el ratio.
# Valor de partida, no medido contra la distribución real de esta
# biblioteca — ajustar si en la práctica queda demasiado laxo/estricto.
_INFRAVALORADO_MIN_LISTENERS = 20

# Ticket AI-22 — criterio de ORDER BY para cada valor de 'ranking'
# (§ver _RANKING_VALUES). Ninguno de los tres pasa por pop_score (que
# mide calidad de audio/metadata, no popularidad — ver
# AI_AGENT_MASTER_PLAN.md). 'escuchas_propias' no está acá porque
# necesita un JOIN distinto (listening_events, no track_meta) — se
# maneja aparte en _query_tracks_own_listens.
_RANKING_ORDER_SQL = {
    'popularidad_global': 'COALESCE(tm.lastfm_listeners, 0) DESC',
    'escuchas_global': 'COALESCE(tm.lastfm_playcount, 0) DESC',
    'infravalorado': (
        f'CASE WHEN COALESCE(tm.lastfm_listeners, 0) >= {_INFRAVALORADO_MIN_LISTENERS} '
        f'THEN CAST(COALESCE(tm.lastfm_playcount, 0) AS REAL) / tm.lastfm_listeners '
        f'ELSE -1 END DESC'
    ),
}


def _query_tracks(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                   artist_ids=None, album_ids=None, track_ids=None, ranking=None):
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

    `ranking` (Ticket AI-22): si viene un valor de _RANKING_VALUES
    (salvo 'escuchas_propias', que usa _query_tracks_own_listens en su
    lugar — necesita otro JOIN), reemplaza el ORDER BY default
    (pop_score, que mide calidad de audio/metadata, no popularidad —
    ver AI_AGENT_MASTER_PLAN.md) por el criterio real correspondiente, y
    el LIMIT pasa a ser _PLAYLIST_SIZE en vez de _CANDIDATE_POOL_SIZE:
    con ranking explícito el usuario pidió un orden real, no un pool
    para muestrear al azar (ver generate_playlist)."""
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

    order_sql = _RANKING_ORDER_SQL.get(ranking, 'COALESCE(tpc.pop_score,0) DESC')
    limit_n = _PLAYLIST_SIZE if ranking else _CANDIDATE_POOL_SIZE

    data_sql = f'''SELECT t.*, al.id as album_id, al.name as album_name,
                          al.year as album_year, al.cover_path,
                          ar.id as artist_id, ar.name as artist_name,
                          tm.mood, tm.momento, tm.era, tm.tema_lirico, tm.idioma,
                          tm.genre_primary, tm.genre_secondary, tm.bpm, tm.energy,
                          tm.bailabilidad, tm.tier, tm.lastfm_listeners, tm.lastfm_playcount,
                          COALESCE(tpc.pop_score,0) as pop_score
                   FROM tracks t
                   JOIN albums al ON al.id=t.album_id
                   LEFT JOIN artists ar ON ar.id=al.artist_id
                   LEFT JOIN track_meta tm ON tm.track_id=t.id
                   LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                   WHERE 1=1{where}
                   ORDER BY {order_sql} LIMIT ?'''
    rows = conn.execute(data_sql, params + [limit_n]).fetchall()

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


def _query_tracks_own_listens(conn, args_dict, user_id, track_to_json_fn, build_adv_filters_fn,
                               dedupe_condition_fn, artist_ids=None, album_ids=None, track_ids=None):
    """Ticket AI-22 — variante de _query_tracks para ranking='escuchas_propias':
    en vez de ordenar por pop_score o por señales globales de Last.fm,
    cuenta las reproducciones REALES de este usuario (listening_events,
    Ticket AI-02/AI-03) sobre el mismo conjunto de candidatos filtrado, y
    devuelve el top en orden estricto. JOIN distinto al resto (INNER con
    listening_events, no LEFT) a propósito: si el usuario nunca escuchó
    nada de lo que está pidiendo, no tiene sentido devolver resultados
    con 0 reproducciones como si fueran "lo más escuchado" — mejor caer
    a la relajación de filtros o al fallback, que si tienen sentido para
    ese caso."""
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

    where = (' AND ' + ' AND '.join(clauses)) if clauses else ''
    data_sql = f'''SELECT t.*, al.id as album_id, al.name as album_name,
                          al.year as album_year, al.cover_path,
                          ar.id as artist_id, ar.name as artist_name,
                          tm.mood, COALESCE(tpc.pop_score,0) as pop_score,
                          COUNT(le.id) as play_count
                   FROM listening_events le
                   JOIN tracks t ON t.id = le.track_id
                   JOIN albums al ON al.id=t.album_id
                   LEFT JOIN artists ar ON ar.id=al.artist_id
                   LEFT JOIN track_meta tm ON tm.track_id=t.id
                   LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                   WHERE le.user_id=?{where}
                   GROUP BY t.id
                   ORDER BY play_count DESC
                   LIMIT ?'''
    rows = conn.execute(data_sql, [user_id] + params + [_PLAYLIST_SIZE]).fetchall()

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


def _finalize_pool(pool, ranking):
    """Ticket AI-22 — con ranking explícito, el pool ya viene ordenado y
    acotado a _PLAYLIST_SIZE desde la query (ver _query_tracks/
    _query_tracks_own_listens): el usuario pidió un orden real ("lo más
    popular", "lo más infravalorado"), no variedad — se devuelve tal
    cual, sin randomizar. Sin ranking, sigue el comportamiento de
    siempre: muestreo al azar del pool de candidatos más amplio."""
    if ranking:
        return pool[:_PLAYLIST_SIZE]
    sample_size = min(_PLAYLIST_SIZE, len(pool))
    return random.sample(pool, sample_size) if pool else []


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
    las pistas literales del álbum.

    `ranking` (Ticket AI-22): si viene, se calcula UNA vez acá arriba
    (igual que artist_ids/album_ids/track_ids) y se mantiene constante
    durante toda la relajación — ver _run_query/_finalize_pool. Con
    ranking='escuchas_propias' se despacha a
    _query_tracks_own_listens en vez de _query_tracks (necesita otro
    JOIN, contra listening_events en vez de track_meta)."""
    artist_ids = _resolve_artist_ids(conn, entities.get('artists') or [], build_similar_artists_fn)
    album_ids = _resolve_album_ids(conn, entities.get('albums') or [], artist_ids_hint=artist_ids)
    named_track_ids = _resolve_track_ids(conn, entities.get('tracks') or [], artist_ids_hint=artist_ids)
    similar_seed_ids = named_track_ids | _sample_tracks_for_albums(conn, album_ids)
    track_ids = _expand_via_similar_tracks(conn, similar_seed_ids) if similar_seed_ids else set()
    ranking = entities.get('ranking')

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
        if ranking:
            applied['ranking'] = ranking
        return applied

    def _run_query(args_dict_local):
        if ranking == 'escuchas_propias':
            return _query_tracks_own_listens(
                conn, args_dict_local, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids
            )
        return _query_tracks(
            conn, args_dict_local, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids, ranking=ranking
        )

    pool = _run_query(args_dict)
    if pool:
        return _finalize_pool(pool, ranking), _filters_applied(args_dict), False

    for field in _RELAXATION_ORDER:
        if field not in args_dict:
            continue
        dropped.add(field)
        retry_args = _entities_to_args_dict(entities, drop_fields=dropped)
        pool = _run_query(retry_args)
        if pool:
            return _finalize_pool(pool, ranking), _filters_applied(retry_args), True

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
