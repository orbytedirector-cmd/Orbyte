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
import ai_playlist_pagination  # Ticket AI-27 — paginación ("Expandir"), módulo aislado

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
# Ticket AI-25 (pedido por Niko: "Top 10 de los Beatles", "las mejores
# 50 del rock clásico") — tope duro para 'cantidad' explícita, para que
# "las mejores 5000" no intente devolver media biblioteca en una sola
# respuesta. Si alguien quiere más que esto, está la paginación
# ("Expandir", mismo ticket) para pedir más de a tandas.
_MAX_CANTIDAD = 100

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
        # Ticket AI-23 (pedido por Niko) — false (default) = búsqueda
        # CERRADA: "Lo mejor de Stratovarius" trae solo Stratovarius.
        # true = búsqueda ABIERTA: el usuario dio alguna señal explícita
        # de querer expandir ("parecido a", "similar a", "como", "y
        # artistas similares") — ver _SYSTEM_PROMPT_TEMPLATE para las
        # señales exactas que dispara true. Aplica igual a artistas,
        # álbumes y pistas nombradas — ver _resolve_artist_ids/
        # generate_playlist.
        'buscar_similares': False,
        # Ticket AI-25 (pedido por Niko: "Top 10 de los Beatles", "las
        # mejores 50 del rock clásico", "las 5 más populares de Iron
        # Maiden") — cantidad EXPLÍCITA que el usuario pidió, o None si
        # no especificó (en cuyo caso se usa _PLAYLIST_SIZE, 25, como
        # siempre). Ver _normalize_entities para la validación numérica.
        'cantidad': None,
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


def _normalize_entities(raw_entities, vocab, max_cantidad=_MAX_CANTIDAD):
    """Normaliza cada valor devuelto por el LLM contra el vocabulario real de
    la base, descartando lo que no matchea nada (nunca fuerza un match malo).

    `max_cantidad` (Ticket 26, Categoría B): tope real a validar contra —
    _MAX_CANTIDAD sigue siendo el default si no se pasa nada (compatibilidad
    hacia atrás), pero interpret_query/handle_request lo resuelven por
    usuario desde settings_json antes de llegar acá."""
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

    # Ticket AI-23: booleano simple, sin fuzzy-match — bool() de Python ya
    # maneja bien tanto un true/false real del LLM como los casos borde
    # (None, string vacío, etc.) sin necesitar lógica extra.
    out['buscar_similares'] = bool(raw_entities.get('buscar_similares'))

    # Ticket AI-25: validar que sea un entero positivo dentro del tope —
    # si el LLM devuelve algo raro (texto, negativo, cero, o un número
    # absurdo), se descarta a None (cae al default de siempre) en vez de
    # arriesgarse a un LIMIT inválido o desproporcionado.
    cantidad_raw = raw_entities.get('cantidad')
    try:
        cantidad = int(cantidad_raw)
        out['cantidad'] = cantidad if 1 <= cantidad <= max_cantidad else None
    except (TypeError, ValueError):
        out['cantidad'] = None

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
    "anios": [número], "ranking": string o null, "buscar_similares": boolean, "cantidad": número o null,
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
el usuario da un año exacto, va acá; si describe una época en general, va en "eras". EXCEPCIÓN: si el \
usuario menciona una DÉCADA junto con un pedido de ranking/popularidad (ej. "los éxitos más populares de \
los 90"), expandí la década COMPLETA acá como lista de años (1990, 1991, ..., 1999) en vez de (o además \
de) usar "eras" — así se puede rankear por reproducciones/oyentes dentro de ese rango exacto.
- "ranking": tiene que ser EXACTAMENTE uno de estos 4 valores, o null si el usuario no pidió ningún \
orden de popularidad/escuchas en particular:
  * "popularidad_global": el usuario pide lo más POPULAR/FAMOSO/CONOCIDO en general (ej: "lo más \
popular de Metallica", "los hits de Queen", "lo más famoso del género"). Se mide en cantidad de OYENTES \
distintos a nivel mundial (lastfm_listeners) — cuánta gente lo conoce, no cuántas veces se reprodujo.
  * "escuchas_global": el usuario pide lo más ESCUCHADO/REPRODUCIDO/MEJOR, sin calificar que sea "de \
nosotros" o "en casa" (ej: "lo más escuchado de Lord Huron", "las canciones más reproducidas del rock \
alternativo", "LO MEJOR de Stratovarius", "los mejores temas de X"). "Lo mejor de X" cae acá — se mide en \
cantidad total de REPRODUCCIONES a nivel mundial (lastfm_playcount), que puede diferir de \
popularidad_global (algo con pocos oyentes muy fieles que lo repiten mucho puede tener más reproducciones \
que oyentes distintos).
  * "escuchas_propias": el usuario pide lo que ÉL/ELLA o "nosotros"/"en casa" escuchó más, no lo popular \
en el mundo (ej: "lo que más escuchamos de Queen", "mis canciones más escuchadas", "lo que más sonó en \
casa este mes"). Señal clara: primera persona o referencia a "nosotros"/nuestra casa, no al público \
general.
  * "infravalorado": el usuario pide algo POCO CONOCIDO pero BUENO — "infravalorado", "subestimado", \
"que no es tan conocido pero vale la pena", "joyitas ocultas", "hidden gems" (ej: "lo más infravalorado \
de Radiohead", "canciones subestimadas del jazz").
- "buscar_similares": true SOLO si el usuario da alguna señal explícita de querer expandir más allá de \
lo nombrado literalmente — palabras como "parecido a", "similar a", "como", "tipo", "al estilo de", "y \
artistas/bandas similares", "y algo más de ese estilo". false (default, úsalo salvo que veas una de esas \
señales) si el usuario pide algo cerrado y específico — un artista, álbum o canción puntual, sin pedir \
nada "parecido". Ejemplos: "Lo mejor de Stratovarius" -> false (SOLO Stratovarius, nada de bandas \
parecidas). "Quiero oír Stratovarius y artistas similares" -> true (Stratovarius + similares). "El álbum \
X" -> false (solo ese álbum, sin nada añadido). "Algo como el álbum X" -> true. Si el usuario nombra una \
canción puntual en "tracks" con buscar_similares=false, el sistema va a traer TODAS las versiones \
disponibles de esa canción (no una sola) — no hace falta que vos elijas cuál versión, tu trabajo es solo \
decidir si hay que expandir a "parecidos" o no.
- "cantidad": el número EXACTO de canciones si el usuario lo especifica (ej: "Top 10 de los Beatles" -> \
10, "las mejores 50 del rock clásico" -> 50, "las 5 más populares de Iron Maiden" -> 5, "dame 20 \
canciones tranquilas" -> 20). null si no menciona ninguna cantidad — en ese caso el sistema usa una \
cantidad default razonable, no hace falta que inventes un número.
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


def interpret_query(conn, raw_query, max_cantidad=_MAX_CANTIDAD):
    """Devuelve (parsed_dict, provider_used_or_None). parsed_dict sigue el
    schema de AI_AGENT_MASTER_PLAN.md §6. Gemini primero, Groq como
    respaldo — ver ticket §3 para la justificación de por qué ese orden.

    `max_cantidad` (Ticket 26, Categoría B): pasado tal cual a
    _normalize_entities — ver ese docstring."""
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
        entities = _normalize_entities(parsed.get('entities') or {}, vocab, max_cantidad=max_cantidad)
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


def _resolve_artist_ids(conn, artist_names, build_similar_artists_fn, similar_limit=8, expand_similar=True):
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

    `expand_similar` (Ticket AI-23, pedido por Niko): controla si se hace
    la expansión a similares o no — false para búsquedas CERRADAS ("Lo
    mejor de Stratovarius" = solo Stratovarius), true para ABIERTAS
    ("Stratovarius y artistas similares"). Ver entities.buscar_similares
    en generate_playlist.

    Devuelve un set de artist_id: los nombrados que se pudieron resolver
    + (si expand_similar) sus similares que efectivamente existen en esta
    biblioteca (los que no, `build_similar_artists_fn` ya los devuelve
    con id=None y se descartan acá)."""
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

        if not expand_similar:
            continue
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

# Ticket AI-26 (bug encontrado probando la paginación, pero independiente
# de ella): al combinar la cascada de AI-24 con su enriquecimiento
# directo DENTRO de una sola llamada a generate_playlist, comparar por
# id exacto dejaba pasar una versión distinta (otro álbum) de un tema ya
# traído por la cascada. Se usa acá nomás, para ese merge puntual — la
# paginación entre pedidos separados ("Expandir") NO usa esto, ver
# ai_playlist_pagination.py (Ticket AI-27).
def _dedupe_key(title, artist):
    return f"{(title or '').strip().lower()}\x1f{(artist or '').strip().lower()}"


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

# Ticket AI-24 (pedido por Niko, ejemplos 3/4 — "lo mejor de la música
# chilena" / "los éxitos más populares de los 90"): mismo criterio que
# _RANKING_ORDER_SQL pero a nivel de ARTISTA (ar.lastfm_listeners/
# ar.lastfm_playcount — columnas propias de `artists`, independientes de
# las de track_meta), para la primera etapa de la cascada
# artista-primero-pista-después. 'escuchas_propias' no está acá por el
# mismo motivo que en _RANKING_ORDER_SQL — necesita JOIN con
# listening_events, se maneja aparte en _top_artist_ids_by_ranking.
_ARTIST_RANKING_ORDER_SQL = {
    'popularidad_global': 'COALESCE(ar.lastfm_listeners, 0) DESC',
    'escuchas_global': 'COALESCE(ar.lastfm_playcount, 0) DESC',
    'infravalorado': (
        f'CASE WHEN COALESCE(ar.lastfm_listeners, 0) >= {_INFRAVALORADO_MIN_LISTENERS} '
        f'THEN CAST(COALESCE(ar.lastfm_playcount, 0) AS REAL) / ar.lastfm_listeners '
        f'ELSE -1 END DESC'
    ),
}

_CASCADE_TOP_N_ARTISTS = 15  # cuántos artistas trae la etapa 1 — valor
# de partida, da margen suficiente para que la etapa 2 tenga de dónde
# elegir _PLAYLIST_SIZE pistas sin agotar el catálogo de 1-2 artistas.


def _cascade_rankings(ranking):
    """Ticket AI-24 — qué criterio usa cada etapa de la cascada
    artista-primero-pista-después (ver _cascade_ranked_tracks). La
    etapa 1 identifica QUÉ artistas son relevantes para la categoría
    pedida (país, género, era, etc.); la etapa 2 saca sus mejores
    pistas de esos artistas.

    - 'infravalorado': las dos etapas buscan lo mismo — artistas poco
      conocidos con alto enganche, y de esos, sus pistas más queridas
      (no las más obscuras — una vez encontrado el artista infravalorado,
      lo que importa de sus pistas es cuál es la que más pegó).
    - 'escuchas_propias': las dos etapas son sobre el historial real del
      usuario — qué artistas (de los que matchean el filtro) escuchó
      más, y de esos, qué pistas escuchó más.
    - 'popularidad_global'/'escuchas_global': la etapa 1 SIEMPRE
      identifica a los artistas líderes por OYENTES (quiénes son, en
      términos de alcance) — la etapa 2 siempre busca sus pistas más
      REPRODUCIDAS (sus éxitos reales). Ejemplo de Niko: "lo mejor de la
      música chilena" -> primero los artistas chilenos con más oyentes,
      después sus pistas con más reproducciones — no importa si el
      usuario pidió "oyentes" o "reproducciones" para el conjunto
      completo, una vez identificados los artistas líderes lo relevante
      de sus pistas es cuáles pegaron más."""
    if ranking == 'infravalorado':
        return 'infravalorado', 'escuchas_global'
    if ranking == 'escuchas_propias':
        return 'escuchas_propias', 'escuchas_propias'
    return 'popularidad_global', 'escuchas_global'


def _top_artist_ids_by_ranking(conn, args_dict, build_adv_filters_fn, ranking, user_id=None,
                                limit=_CASCADE_TOP_N_ARTISTS):
    """Ticket AI-24 — etapa 1 de la cascada. Reusa la MISMA estructura de
    join y los MISMOS filtros de build_adv_filters_fn que ya usa
    _query_tracks (así "país=Chile" significa exactamente lo mismo acá
    que en cualquier otra parte del sistema, sin reinventar el filtro a
    nivel de artista) pero agrupa por artista y ordena por el criterio
    de _ARTIST_RANKING_ORDER_SQL en vez de por pista. Devuelve una lista
    de artist_id, en orden."""
    from werkzeug.datastructures import MultiDict
    args = MultiDict()
    for field, vals in args_dict.items():
        for v in vals:
            args.add(field, v)
    clauses, params = build_adv_filters_fn(args, pop_alias='tpc', for_albums=False)
    where = (' AND ' + ' AND '.join(clauses)) if clauses else ''

    if ranking == 'escuchas_propias':
        if not user_id:
            return []
        sql = f'''SELECT ar.id as artist_id, COUNT(le.id) as play_count
                  FROM listening_events le
                  JOIN tracks t ON t.id = le.track_id
                  JOIN albums al ON al.id=t.album_id
                  JOIN artists ar ON ar.id=al.artist_id
                  LEFT JOIN track_meta tm ON tm.track_id=t.id
                  LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                  WHERE le.user_id=?{where}
                  GROUP BY ar.id
                  ORDER BY play_count DESC
                  LIMIT ?'''
        rows = conn.execute(sql, [user_id] + params + [limit]).fetchall()
        return [r['artist_id'] for r in rows]

    order_sql = _ARTIST_RANKING_ORDER_SQL.get(ranking)
    if not order_sql:
        return []
    sql = f'''SELECT ar.id as artist_id
              FROM tracks t
              JOIN albums al ON al.id=t.album_id
              JOIN artists ar ON ar.id=al.artist_id
              LEFT JOIN track_meta tm ON tm.track_id=t.id
              LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
              WHERE 1=1{where}
              GROUP BY ar.id
              ORDER BY {order_sql}
              LIMIT ?'''
    rows = conn.execute(sql, params + [limit]).fetchall()
    return [r['artist_id'] for r in rows]


def _cascade_ranked_tracks(conn, args_dict, build_adv_filters_fn, dedupe_condition_fn, track_to_json_fn,
                            ranking, user_id, playlist_size=_PLAYLIST_SIZE):
    """Ticket AI-24 (pedido por Niko, ejemplos 3/4) — cascada completa:
    etapa 1 identifica los artistas más relevantes para la categoría
    pedida (_top_artist_ids_by_ranking), etapa 2 trae sus mejores pistas
    (_query_tracks/_query_tracks_own_listens, con artist_ids= el
    resultado de la etapa 1). Puede devolver una lista corta (categoría
    con pocos artistas/pocas pistas) — quien llama decide si enriquecer
    con el camino de una sola etapa (ver generate_playlist).

    `playlist_size` (Ticket AI-25): se pasa tal cual a la etapa 2 — la
    etapa 1 (qué artistas) no lo necesita, solo afecta cuántas pistas se
    traen de esos artistas."""
    stage1_ranking, stage2_ranking = _cascade_rankings(ranking)
    top_artist_ids = _top_artist_ids_by_ranking(
        conn, args_dict, build_adv_filters_fn, stage1_ranking, user_id=user_id
    )
    if not top_artist_ids:
        return []
    artist_ids = set(top_artist_ids)
    if stage2_ranking == 'escuchas_propias':
        return _query_tracks_own_listens(
            conn, args_dict, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            artist_ids=artist_ids, playlist_size=playlist_size
        )
    return _query_tracks(
        conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
        artist_ids=artist_ids, ranking=stage2_ranking, playlist_size=playlist_size
    )


def _query_tracks(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                   artist_ids=None, album_ids=None, track_ids=None, ranking=None, skip_dedupe=False,
                   limit_override=None, playlist_size=_PLAYLIST_SIZE):
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
    el LIMIT pasa a ser playlist_size en vez de _CANDIDATE_POOL_SIZE:
    con ranking explícito el usuario pidió un orden real, no un pool
    para muestrear al azar (ver generate_playlist).

    `skip_dedupe` (Ticket AI-23, pedido por Niko): cuando el usuario
    nombra una pista puntual SIN pedir "parecido/similar" (búsqueda
    cerrada), la expectativa es "todas las versiones disponibles, de
    mejor calidad a peor" — no una sola versión deduplicada. Con
    skip_dedupe=True se omite `dedupe_condition_fn` por completo; el
    ORDER BY sigue siendo pop_score (que acá SÍ es el criterio correcto:
    calidad de audio/metadata, exactamente lo que "mejor a peor" pide).

    `limit_override`: si viene, pisa el LIMIT calculado automáticamente
    (usado por el camino de "todas las versiones", que quiere hasta
    playlist_size versiones sin pasar por la lógica de pool-para-samplear).

    `playlist_size` (Ticket AI-25, pedido por Niko: "Top 10 de los
    Beatles") — reemplaza el _PLAYLIST_SIZE fijo cuando el usuario pidió
    una cantidad explícita, o cuando generate_playlist pide un pool más
    amplio para poder paginar después (Ticket AI-27, ver
    ai_playlist_pagination.py). Default _PLAYLIST_SIZE, comportamiento
    idéntico al de antes de AI-25 si no se pasa nada distinto.

    Ticket AI-27 (pedido por Niko): esta función vuelve a su forma
    exacta de antes de AI-25/AI-26 — sin ningún parámetro de exclusión.
    La paginación ("Expandir") ya no re-consulta la base con una lista
    creciente de qué descartar; en cambio, generate_playlist pide un
    pool más amplio UNA sola vez (subiendo playlist_size acá arriba,
    nada nuevo en esta función) y ai_playlist_pagination.py sirve las
    tandas siguientes desde ese mismo pool ya resuelto, sin volver a
    tocar esta función ni el dedupe compartido con el resto de la app."""
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
    if skip_dedupe:
        where = (' AND ' + extra_where) if extra_where else ''
    else:
        dedupe_clause = dedupe_condition_fn(extra_where=extra_where, track_alias='t', pop_alias='tpc')
        clauses = clauses + [dedupe_clause]
        params = params + params
        where = (' AND ' + ' AND '.join(clauses)) if clauses else ''

    order_sql = _RANKING_ORDER_SQL.get(ranking, 'COALESCE(tpc.pop_score,0) DESC')
    limit_n = limit_override if limit_override is not None else (playlist_size if ranking else _CANDIDATE_POOL_SIZE)

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
                               dedupe_condition_fn, artist_ids=None, album_ids=None, track_ids=None,
                               playlist_size=_PLAYLIST_SIZE):
    """Ticket AI-22 — variante de _query_tracks para ranking='escuchas_propias':
    en vez de ordenar por pop_score o por señales globales de Last.fm,
    cuenta las reproducciones REALES de este usuario (listening_events,
    Ticket AI-02/AI-03) sobre el mismo conjunto de candidatos filtrado, y
    devuelve el top en orden estricto. JOIN distinto al resto (INNER con
    listening_events, no LEFT) a propósito: si el usuario nunca escuchó
    nada de lo que está pidiendo, no tiene sentido devolver resultados
    con 0 reproducciones como si fueran "lo más escuchado" — mejor caer
    a la relajación de filtros o al fallback, que si tienen sentido para
    ese caso.

    `playlist_size` (Ticket AI-25): cantidad explícita del usuario, o el
    pool ampliado que pide generate_playlist para poder paginar después
    (Ticket AI-27 — ver ai_playlist_pagination.py; esta función no sabe
    nada de paginación, solo de cuánto traer)."""
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
    rows = conn.execute(data_sql, [user_id] + params + [playlist_size]).fetchall()

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


def _finalize_pool(pool, ranking, playlist_size=_PLAYLIST_SIZE):
    """Ticket AI-22 — con ranking explícito, el pool ya viene ordenado y
    acotado a playlist_size desde la query (ver _query_tracks/
    _query_tracks_own_listens): el usuario pidió un orden real ("lo más
    popular", "lo más infravalorado"), no variedad — se devuelve tal
    cual, sin randomizar. Sin ranking, sigue el comportamiento de
    siempre: muestreo al azar del pool de candidatos más amplio.

    `playlist_size` (Ticket AI-25): _PLAYLIST_SIZE por default, o la
    cantidad explícita que pidió el usuario ("Top 10 de los Beatles")."""
    if ranking:
        return pool[:playlist_size]
    sample_size = min(playlist_size, len(pool))
    return random.sample(pool, sample_size) if pool else []


def _personalized_then_global_fallback(conn, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                                        playlist_size=_PLAYLIST_SIZE):
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
    configuradas, o ambos proveedores caídos).

    Ticket AI-27: vuelve a su forma exacta de antes de AI-25/AI-26 — sin
    ningún parámetro de exclusión. `playlist_size` sigue funcionando
    igual que siempre (cuánto traer); si generate_playlist pide acá un
    pool ampliado para poder paginar (ver ai_playlist_pagination.py),
    esta función ni se entera — solo ve un playlist_size más grande,
    nada nuevo que aprender."""
    personalized, source = fallback_engine.personalized_fallback(
        conn, user_id, track_to_json_fn, dedupe_condition_fn, limit=playlist_size
    )
    if personalized:
        return personalized, {'fallback_source': source}

    pool = _query_tracks(conn, {}, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn)
    sample_size = min(playlist_size, len(pool))
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
    JOIN, contra listening_events en vez de track_meta).

    `buscar_similares` (Ticket AI-23, pedido por Niko): controla si
    artist_ids se expande a artistas similares y si album_ids/tracks se
    expanden vía similar_tracks_json. false (default, búsqueda CERRADA)
    = solo lo nombrado literalmente. true (búsqueda ABIERTA, disparada
    por palabras como "parecido/similar/como" — ver el prompt) = con
    expansión, comportamiento idéntico al de antes de este ticket.

    Caso especial (Ticket AI-23): si el usuario nombra una pista puntual
    en 'tracks' SIN pedir similares, la expectativa no es "una pista
    resuelta y deduplicada" sino "TODAS las versiones disponibles,
    ordenadas de mejor a peor calidad" — esto se resuelve ANTES de
    entrar al flujo normal de filtros/relajación, como un camino
    aparte, porque el usuario ya fue 100% específico sobre qué pista
    quiere.

    `playlist_size` (Ticket AI-25, pedido por Niko: "Top 10 de los
    Beatles", "las mejores 50 del rock clásico") — se calcula UNA vez
    acá arriba desde entities['cantidad'] (ya validado y acotado por
    _normalize_entities), o _PLAYLIST_SIZE si no se especificó. Se pasa
    a TODOS los caminos de abajo, reemplazando el _PLAYLIST_SIZE fijo de
    antes de ese ticket.

    Ticket AI-27 (pedido por Niko): esta función vuelve a su forma
    EXACTA de antes de los Tickets AI-25/AI-26 — sin ningún parámetro de
    exclusión ni de paginación. Toda la lógica de "Expandir" vive en
    ai_playlist_pagination.py, que llama a esta función pasándole
    entities['cantidad'] YA AMPLIADO cuando quiere un pool más grande
    para poder paginar (ver handle_request más abajo) — desde acá
    adentro, eso es indistinguible de un usuario que pidió una cantidad
    grande de una ("las mejores 150 de..."), así que no hace falta que
    esta función sepa nada de paginación en absoluto."""
    playlist_size = entities.get('cantidad') or _PLAYLIST_SIZE
    buscar_similares = bool(entities.get('buscar_similares'))
    artist_ids = _resolve_artist_ids(
        conn, entities.get('artists') or [], build_similar_artists_fn, expand_similar=buscar_similares
    )
    album_ids = _resolve_album_ids(conn, entities.get('albums') or [], artist_ids_hint=artist_ids)
    named_track_ids = _resolve_track_ids(conn, entities.get('tracks') or [], artist_ids_hint=artist_ids)

    # Ticket AI-23: pista puntual + búsqueda cerrada -> todas las
    # versiones disponibles, sin deduplicar, ordenadas por pop_score
    # (calidad de audio/metadata — acá SÍ es el criterio correcto). No
    # pasa por relajación ni por fallback: el usuario fue específico.
    if named_track_ids and not buscar_similares:
        versions = _query_tracks(
            conn, {}, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            track_ids=named_track_ids, skip_dedupe=True, limit_override=playlist_size
        )
        if versions:
            filters_applied = {'track_ids': sorted(named_track_ids), 'todas_las_versiones': True}
            return versions, filters_applied, False

    if buscar_similares:
        similar_seed_ids = named_track_ids | _sample_tracks_for_albums(conn, album_ids)
        track_ids = _expand_via_similar_tracks(conn, similar_seed_ids) if similar_seed_ids else set()
    else:
        # Cerrado: ni expansión por similar_tracks_json ni "sabor" de
        # otras pistas del álbum — solo lo nombrado literalmente.
        track_ids = named_track_ids
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
        if entities.get('cantidad'):
            applied['cantidad'] = entities['cantidad']
        return applied

    def _run_query(args_dict_local):
        if ranking == 'escuchas_propias':
            return _query_tracks_own_listens(
                conn, args_dict_local, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids,
                playlist_size=playlist_size
            )
        return _query_tracks(
            conn, args_dict_local, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids, ranking=ranking,
            playlist_size=playlist_size
        )

    # Ticket AI-24 (pedido por Niko, ejemplos 3/4: "lo mejor de la
    # música chilena", "los éxitos más populares de los 90") — cuando no
    # se nombró ningún artista/álbum/pista puntual PERO sí hay un
    # ranking pedido, se intenta primero la cascada
    # artista-primero-pista-después (_cascade_ranked_tracks) en vez de
    # ir directo al ranking plano a nivel de pista: para una categoría
    # amplia (país, género, era), "lo mejor de X" tiene más sentido
    # leído como "los artistas líderes de X, y de esos, sus mejores
    # pistas" que como "las pistas individuales con más reproducciones
    # sin importar de qué artista son" — un artista nicho con una sola
    # pista viral podría ganarle a los artistas realmente
    # representativos de la categoría en un ranking plano.
    #
    # Si la cascada trae MENOS de playlist_size pistas (categoría con
    # pocos artistas, o pocas pistas por artista — ej. "los 90" es
    # amplio y puede no tener "artistas líderes" tan claros como "música
    # chilena"), se enriquece con el camino directo de una sola etapa
    # para completar, sin repetir pistas ya traídas por la cascada. Esto
    # NO cuenta como used_fallback=True — no es un fallback genérico,
    # sigue siendo un resultado dirigido a la categoría pedida.
    if ranking and not (artist_ids or album_ids or track_ids):
        cascade_tracks = _cascade_ranked_tracks(
            conn, args_dict, build_adv_filters_fn, dedupe_condition_fn, track_to_json_fn, ranking, user_id,
            playlist_size=playlist_size
        )
        if len(cascade_tracks) >= playlist_size:
            filters_applied = _filters_applied(args_dict)
            filters_applied['cascada_artista_primero'] = True
            return cascade_tracks[:playlist_size], filters_applied, False
        if cascade_tracks:
            # Bug encontrado en AI-26 (independiente de la paginación,
            # vive acá adentro de una sola llamada): comparar por id
            # exacto acá dejaba pasar una versión distinta (otro álbum)
            # de un tema que la cascada ya había traído. Se compara por
            # título+artista normalizados en vez de por id.
            seen_keys = {_dedupe_key(t.get('title'), t.get('artist')) for t in cascade_tracks}
            direct_pool = _run_query(args_dict)
            extra = [t for t in direct_pool if _dedupe_key(t.get('title'), t.get('artist')) not in seen_keys]
            combined = cascade_tracks + extra[:max(0, playlist_size - len(cascade_tracks))]
            if combined:
                filters_applied = _filters_applied(args_dict)
                filters_applied['cascada_artista_primero'] = True
                filters_applied['enriquecido_directo'] = True
                return combined, filters_applied, False

    pool = _run_query(args_dict)
    if pool:
        return _finalize_pool(pool, ranking, playlist_size=playlist_size), _filters_applied(args_dict), False

    for field in _RELAXATION_ORDER:
        if field not in args_dict:
            continue
        dropped.add(field)
        retry_args = _entities_to_args_dict(entities, drop_fields=dropped)
        pool = _run_query(retry_args)
        if pool:
            return _finalize_pool(pool, ranking, playlist_size=playlist_size), _filters_applied(retry_args), True

    tracks, filters_applied = _personalized_then_global_fallback(
        conn, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
        playlist_size=playlist_size)
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
                    build_similar_artists_fn, prior_entities=None, default_results=None, max_top_n=None):
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
    es un turno aislado, como en AI-01.

    `default_results`/`max_top_n` (Ticket 26, Categoría B): valores por
    usuario (settings_json, resueltos por app.py::api_v1_ai_playlist antes
    de llamar acá) que reemplazan _PLAYLIST_SIZE/_MAX_CANTIDAD como
    default/tope. None (default de este parámetro, no del usuario) cae en
    los mismos módulo-constantes de siempre — mismo comportamiento exacto
    que antes de este ticket para cualquier llamada que no los pase.

    Ticket AI-27 (pedido por Niko, paginación "Expandir" reconstruida
    para no tocar generate_playlist/_query_tracks/fallback_engine): acá
    es donde se pide el pool AMPLIADO (ai_playlist_pagination.fetch_size_for)
    en vez de solo lo que se va a mostrar — generate_playlist ni se
    entera, para ella es indistinguible de un usuario que pidió una
    cantidad grande de una. Se muestra solo `display_size` en la
    respuesta, y se guarda el resto vía
    ai_playlist_pagination.store_pool() para que "Expandir" lo sirva
    después sin volver a consultar nada."""
    t0 = time.time()
    # Ticket 26, Categoría B: None (no vino nada, o el caller no pasó
    # settings) cae en los constantes de siempre — mismo comportamiento
    # exacto que antes de este ticket.
    effective_default = default_results or _PLAYLIST_SIZE
    effective_max = max_top_n or _MAX_CANTIDAD
    result, provider = interpret_query(conn, raw_query, max_cantidad=effective_max)

    if prior_entities:
        result['entities'] = _merge_entities(prior_entities, result['entities'])

    # Ticket AI-27: cuánto mostrar en ESTA respuesta (lo que el usuario
    # pidió, o el default de siempre) vs. cuánto pedirle a
    # generate_playlist para tener de dónde paginar después — dos
    # números distintos a propósito.
    display_size = result['entities'].get('cantidad') or effective_default
    fetch_size = ai_playlist_pagination.fetch_size_for(display_size)

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
        pool, filters_applied = _personalized_then_global_fallback(
            conn, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            playlist_size=fetch_size)
        used_fallback = True
    else:
        entities_for_fetch = dict(result['entities'])
        entities_for_fetch['cantidad'] = fetch_size
        pool, filters_applied, used_fallback = generate_playlist(
            conn, user_id, entities_for_fetch, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            build_similar_artists_fn)
        if result['status'] == 'error':
            used_fallback = True

    tracks = pool[:display_size]

    _request_id = _log_request(conn, user_id, raw_query, result, provider, filters_applied,
                                used_fallback, len(tracks))
    ai_playlist_pagination.store_pool(conn, _request_id, pool, already_shown_count=len(tracks))

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
