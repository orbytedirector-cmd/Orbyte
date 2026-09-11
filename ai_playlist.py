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
import math
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

# Ticket AI-31 (pedido por Niko: "que se genere algún log separado para
# todo lo que es Orbitron, donde quede el prompt del usuario, la
# sugerencia de Gemini, y los resultados filtrados... para ir afinando
# cada vez más el modelo"). Mismo criterio que _logger de arriba —
# nombre propio, propaga solo al logger raíz ya configurado en app.py
# (RotatingFileHandler + formatter JSONL en logs/orbyte.log), sin
# montar ningún archivo/handler nuevo. Nombre distinto a propósito para
# poder filtrar SOLO estas líneas con
# `grep '"logger": "ai_playlist.audit"' logs/orbyte.log | jq .msg -r | jq .`
# (doble jq: la línea entera es JSONL, y el campo "msg" es a su vez un
# JSON con el detalle — ver _log_audit más abajo. No se extendió
# _JSONLFormatter con campos nuevos porque esa clase es compartida por
# TODO el logging de la app — todo el detalle de Orbitron va empaquetado
# dentro de "msg" en vez de arriesgar una regresión en el formatter
# central por algo que solo necesita este módulo).
_audit_logger = logging.getLogger('ai_playlist.audit')

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
        # Ticket AI-30 (idea de Niko: aprovechar el conocimiento musical
        # general de Gemini, no solo su capacidad de extraer filtros) —
        # exactamente uno de los dos se llena por turno, nunca los dos:
        # 'pistas_sugeridas_por_artista' cuando el usuario SÍ nombró
        # artistas (título de canciones que Gemini cree representativas
        # de CADA uno), 'artistas_sugeridos' cuando NO nombró ninguno
        # (nombres de artista que calzan con toda la intención
        # combinada). Ver _SYSTEM_PROMPT_TEMPLATE y generate_playlist.
        'pistas_sugeridas_por_artista': {}, 'artistas_sugeridos': [],
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
    # Ticket AI-29 (Ticket 41, punto 2, Candidato A confirmado en vivo):
    # el cap era [:5] — pensado para "un par de años puntuales que el
    # usuario nombra a mano" (ej. "canciones de 2015 y 2018"), pero
    # desde Ticket AI-22 esta lista TAMBIÉN recibe la expansión de una
    # década completa (10 años, ver la regla de "anios" en
    # _SYSTEM_PROMPT_TEMPLATE) — con [:5] se perdía la mitad de la
    # década en silencio (confirmado con log real: el LLM mandó
    # 1980..1989 completos, se logueaba con éxito, y acá se cortaba a
    # 1980-1984). 12 cubre una década completa (10) + margen para un
    # par de años sueltos adicionales sin abrir la puerta a que el LLM
    # mande una lista arbitrariamente larga.
    out['anios'] = [str(int(a)) for a in anios_raw if str(a).strip().lstrip('-').isdigit()][:12]

    # Ticket AI-30 — a diferencia de genres/moods/etc., esto NO pasa por
    # _closest_match: son nombres propios de artistas/canciones que
    # Gemini propone desde su conocimiento general, no vocabulario
    # cerrado de la biblioteca. Acá solo se sanea la FORMA (tipos,
    # topes); la validación real contra lo que Niko realmente tiene pasa
    # después, en generate_playlist (_resolve_suggested_track_version /
    # _resolve_artist_ids) — mismo principio que "artists"/"tracks" de
    # arriba, que tampoco fuzzy-matchean acá.
    #
    # La clave se normaliza con el MISMO .strip().lower() que usa
    # _resolve_artist_ids_grouped para las claves de artist_id_groups —
    # generate_playlist hace lookup de las sugerencias por ese nombre
    # normalizado (ver _run_query); sin esto, "Helloween" (como lo haya
    # escrito Gemini) nunca matchearía contra la clave "helloween" de
    # artist_id_groups y las sugerencias se perderían en silencio.
    suggested_tracks_raw = raw_entities.get('pistas_sugeridas_por_artista')
    out['pistas_sugeridas_por_artista'] = {
        str(k).strip().lower(): [str(t) for t in v if t][:8]
        for k, v in (suggested_tracks_raw.items() if isinstance(suggested_tracks_raw, dict) else [])
        if k and isinstance(v, list)
    }
    out['artistas_sugeridos'] = [a for a in (raw_entities.get('artistas_sugeridos') or []) if a][:10]

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
    "place": string o null, "motivation": string o null,
    "pistas_sugeridas_por_artista": {{}}, "artistas_sugeridos": [string]
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
año, no un rango como texto. TAMBIÉN va acá — expandida COMPLETA como lista de años (ej. 1990, 1991, ..., \
1999) — cualquier DÉCADA explícita que el usuario mencione (ej: "rock de los 80", "los éxitos más \
populares de los 90", "algo de los 2000"), tenga o no un pedido de ranking/popularidad junto — así se \
puede filtrar/rankear por ese rango exacto de años. "eras" (más abajo) es SOLO para períodos descritos de \
forma temática/abierta, SIN un número de década explícito (ej: "rock clásico", "la época del grunge", \
"los inicios del rock") — si el usuario da un número de década, siempre va en "anios" expandida, nunca en \
"eras".
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
- Para genres/moods/momentos/eras/temas/paises: propón el valor que mejor describa la intención del \
usuario en tus propias palabras, no hace falta que coincida exacto con ningún catálogo — el sistema hace \
el matching después. Ejemplos de vocabulario ya usado en el catálogo real, como referencia de estilo (no \
son la lista completa): moods={moods_sample}; momentos={momentos_sample}; temas={temas_sample}; \
eras=[early_rock_era, british_invasion_era, classic_rock_era, nwobhm_synth_era, grunge_alternative_era, \
post_millennial_era, streaming_era, current_era].
- "idiomas": a DIFERENCIA de genres/moods/momentos/eras/temas, este es un catálogo CERRADO — los ÚNICOS \
valores válidos son estos códigos ISO 639-1 de 2 letras (los idiomas que realmente existen en la \
biblioteca): {idiomas_list}. Identificá el idioma que pide el usuario y devolvé SIEMPRE el código de 2 \
letras correspondiente (ej: "en español" -> "es", "en inglés" -> "en", "en alemán" -> "de", "en japonés" \
-> "ja") — NUNCA el nombre completo del idioma en ningún idioma, ni una variante distinta de 2 letras que \
no esté en esa lista exacta.
- "pistas_sugeridas_por_artista" y "artistas_sugeridos" (Ticket AI-30): a diferencia de todo lo de \
arriba, que son FILTROS, estos dos campos son sugerencias que salen de TU PROPIO conocimiento musical \
general — el sistema las valida después contra la biblioteca real, así que no hace falta que existan ahí, \
pero sí tienen que ser reales y precisas (nada inventado). Se llena EXACTAMENTE UNO de los dos, nunca los \
dos, según si el usuario nombró artistas o no:
  * Si "artists" tiene uno o más nombres: llená "pistas_sugeridas_por_artista" con hasta 8 títulos de \
canciones por cada uno de esos artistas — las que vos consideres más relevantes/icónicas/representativas \
de ESE artista puntual, según tu conocimiento (ej: para "Metallica" -> "Master of Puppets", "Enter \
Sandman", "One", etc.). La clave de cada entrada tiene que ser EXACTAMENTE el mismo string que pusiste en \
"artists" para ese artista. Dejá "artistas_sugeridos" como lista vacía.
  * Si "artists" está VACÍO: llená "artistas_sugeridos" con 5 a 10 nombres de artistas reales y concretos \
que vos consideres que mejor calzan con TODA la intención combinada del pedido a la vez (género + idioma + \
década/era + mood + tema juntos, no un artista que solo cumpla uno de esos aspectos por separado). Dejá \
"pistas_sugeridas_por_artista" como objeto vacío ({{}}).
  * Si no tenés una sugerencia de calidad y precisa para el caso que corresponda, dejá el campo vacío \
({{}} o []) en vez de forzar algo genérico o dudoso — una sugerencia de baja calidad es peor que ninguna.
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
    # Ticket AI-29 (Ticket 41, punto 2) — a diferencia de moods/momentos/
    # temas (sample() de 8, son campos abiertos de verdad), "idiomas" es
    # un catálogo CERRADO y chico (códigos ISO 639-1 reales de la
    # biblioteca, confirmados con Niko: en/es/de/ja/it/fr/sv/pt/so/fi/
    # la/sw/hr/id/pl/ru/tr/ca/cy/et) — se pasa COMPLETO, no un sample,
    # para que el LLM tenga la lista exacta y no tenga que adivinar
    # cuáles de los ~180 códigos ISO existentes son los que realmente
    # hay en esta biblioteca puntual.
    idiomas_list = json.dumps(sorted(vocab.get('idiomas') or []), ensure_ascii=False)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        moods_sample=sample('moods'), momentos_sample=sample('momentos'),
        temas_sample=sample('temas'), idiomas_list=idiomas_list, user_query=user_query,
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


def _resolve_artist_ids_grouped(conn, artist_names, build_similar_artists_fn, similar_limit=8, expand_similar=True):
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

    Ticket AI-28 (Ticket 41, punto 1 — bug reportado por Niko: un mix de
    varios artistas nombrados quedaba desbalanceado hacia el más
    escuchado globalmente). Antes de este ticket, esta función devolvía
    un solo `set` plano con todos los IDs de todos los nombres
    mezclados — no había forma de saber "estos IDs son de Helloween,
    estos otros de Stratovarius" para poder darle una cuota a cada uno
    más adelante (ver _query_tracks_balanced_by_artist). Ahora devuelve
    un dict {nombre_normalizado: set_de_ids}: el nombre normalizado
    (mismo `.strip().lower()` de siempre) es la clave, así que un mismo
    artista nombrado dos veces con grafía distinta no genera dos grupos
    separados. Los similares expandidos de un artista quedan agrupados
    bajo ESE mismo nombre, no como grupos aparte — "Metallica y
    similares" sigue siendo UN solo bucket para efectos de cuota.

    Devuelve dict {nombre: set_de_artist_id}: por cada nombre, los IDs
    nombrados que se pudieron resolver + (si expand_similar) sus
    similares que efectivamente existen en esta biblioteca (los que no,
    `build_similar_artists_fn` ya los devuelve con id=None y se
    descartan acá). Nombres que el LLM mencionó pero no matchearon
    ningún artista real simplemente no aparecen como clave."""
    if not artist_names:
        return {}

    rows = conn.execute('SELECT id, name FROM artists').fetchall()
    name_to_id = {r['name'].strip().lower(): r['id'] for r in rows}

    grouped = {}
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

        bucket = grouped.setdefault(norm, set())
        bucket.add(matched_id)

        if not expand_similar:
            continue
        similar_row = conn.execute(
            'SELECT similar_artists_json FROM artists WHERE id=?', (matched_id,)
        ).fetchone()
        if similar_row and similar_row['similar_artists_json']:
            for similar in build_similar_artists_fn(conn, similar_row['similar_artists_json'], limit=similar_limit):
                if similar.get('id'):
                    bucket.add(similar['id'])
    return grouped


def _resolve_artist_ids(conn, artist_names, build_similar_artists_fn, similar_limit=8, expand_similar=True):
    """Wrapper delgado sobre _resolve_artist_ids_grouped (Ticket AI-28 —
    ver docstring ahí para el detalle completo de la resolución). Se
    mantiene con esta firma/comportamiento exacto (un solo set plano,
    todos los nombres combinados) porque sigue siendo lo único que
    necesitan los demás usos de artist_ids en generate_playlist
    (filters_applied, hint para álbumes/pistas nombradas, la cascada
    artista-primero) — el balanceo por cuota (Ticket 41, punto 1) es la
    ÚNICA parte que necesita el dict agrupado, y lo pide aparte."""
    grouped = _resolve_artist_ids_grouped(
        conn, artist_names, build_similar_artists_fn, similar_limit=similar_limit, expand_similar=expand_similar
    )
    ids = set()
    for bucket in grouped.values():
        ids |= bucket
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


# Ticket AI-28 (Ticket 41, punto 1) — reparto de cuotas para el mix de
# varios artistas nombrados, decidido con Niko: los 2 artistas más
# populares del grupo se llevan el 60% del total entre ellos dos, el
# 40% restante se reparte parejo entre el resto. Con exactamente 2
# artistas nombrados no aplica este split (no hay "resto") — ahí es
# 50/50 directo, ver _compute_artist_mix_quotas.
_ARTIST_MIX_TOP2_SHARE = 0.6


def _artist_popularity_proxy(conn, artist_ids):
    """Ticket AI-28 — señal de "qué tan popular es este artista" para
    decidir cuotas en el mix multi-artista. Usa el mayor
    `lastfm_playcount` entre TODAS las pistas del artista (su tema más
    escuchado) en vez de sumar o promediar el catálogo completo: sumar
    favorecería a un artista con catálogo grande de temas menores por
    sobre uno con menos pistas pero un hit fuerte, que es exactamente el
    tipo de comparación que "más popular" debería capturar acá. No
    reusa track_pop_cache.pop_score a propósito — ese campo mide calidad
    de audio/metadata, no popularidad real (ver nota en
    _RANKING_ORDER_SQL/AI_AGENT_MASTER_PLAN.md)."""
    if not artist_ids:
        return 0
    placeholders = ','.join('?' * len(artist_ids))
    row = conn.execute(
        f'''SELECT MAX(COALESCE(tm.lastfm_playcount, 0)) as maxpc
            FROM tracks t
            JOIN albums al ON al.id = t.album_id
            LEFT JOIN track_meta tm ON tm.track_id = t.id
            WHERE al.artist_id IN ({placeholders})''',
        list(artist_ids)
    ).fetchone()
    return (row['maxpc'] or 0) if row else 0


def _compute_artist_mix_quotas(pop_by_name, playlist_size):
    """Ticket AI-28 (Ticket 41, punto 1) — cuánto de `playlist_size` le
    corresponde a cada artista nombrado en un mix de 2+, decidido con
    Niko:
    - 2 artistas: 50/50, sin importar popularidad relativa.
    - 3+ artistas: los 2 más populares (por _artist_popularity_proxy) se
      reparten _ARTIST_MIX_TOP2_SHARE (60%) del total ENTRE ELLOS DOS,
      ponderado por raíz cuadrada de su popularidad (no proporcional
      directo — si uno le saca mucha ventaja al otro, la raíz cuadrada
      suaviza la brecha en vez de llevársela casi toda); el 40% restante
      se reparte PAREJO entre el resto de los artistas nombrados, sin
      pesar por popularidad.

    `pop_by_name`: dict {nombre: valor_de_popularidad} (ver
    _artist_popularity_proxy), ya con una entrada por cada nombre
    resuelto — esta función no toca la base, solo hace la aritmética del
    reparto.

    Devuelve dict {nombre: cuota_int}. Las cuotas siempre suman
    exactamente `playlist_size` (los restos de redondeo se ajustan sobre
    el artista más popular del top-2, nunca se pierde una pista por
    redondeo)."""
    names = list(pop_by_name.keys())
    k = len(names)
    if k == 0:
        return {}
    if k == 1:
        return {names[0]: playlist_size}

    if k == 2:
        base = playlist_size // 2
        return {names[0]: base, names[1]: playlist_size - base}

    ordered = sorted(names, key=lambda nm: pop_by_name[nm], reverse=True)
    top2, rest = ordered[:2], ordered[2:]

    top2_total = round(playlist_size * _ARTIST_MIX_TOP2_SHARE)
    rest_total = playlist_size - top2_total

    p1, p2 = pop_by_name[top2[0]], pop_by_name[top2[1]]
    w1, w2 = math.sqrt(max(p1, 0)), math.sqrt(max(p2, 0))
    if w1 + w2 <= 0:
        share1 = top2_total // 2  # ninguno de los dos tiene dato de popularidad — parejo entre ellos
    else:
        share1 = round(top2_total * (w1 / (w1 + w2)))
    share2 = top2_total - share1
    quotas = {top2[0]: share1, top2[1]: share2}

    if rest:
        base_rest = rest_total // len(rest)
        remainder = rest_total - base_rest * len(rest)
        for i, nm in enumerate(rest):
            quotas[nm] = base_rest + (1 if i < remainder else 0)

    # Ajuste final: cualquier desfase por los round() de arriba (a lo
    # sumo 1-2 pistas) se corrige sobre el artista más popular — nunca
    # se pierde ni se inventa una pista de más respecto a playlist_size.
    diff = playlist_size - sum(quotas.values())
    if diff:
        quotas[top2[0]] += diff
    return quotas


def _track_playcount(track):
    """Ticket AI-28 — lastfm_playcount puede llegar desde SQLite como
    string o None (mismo problema de tipos ya documentado del lado de
    app.py en Ticket 39, ver _normalize_lastfm_count ahí) — acá solo
    hace falta un número para poder comparar/ordenar pistas, así que
    cualquier valor inválido cae a 0 en vez de None (a diferencia de
    _normalize_lastfm_count, que sí distingue "sin dato" para mostrarlo
    distinto en la UI — acá esa distinción no aplica, solo importa el
    orden relativo)."""
    try:
        return int(track.get('lastfm_playcount') or 0)
    except (TypeError, ValueError):
        return 0


def _track_album_year(track):
    """Ticket AI-30 — bugfix confirmado con traceback real de Niko
    (TypeError: unsupported operand type(s) for -: 'str' and 'str' en
    _score_track_version): album_year también llega desde SQLite como
    STRING, no int — mismo problema de tipos que _track_playcount ya
    blinda para lastfm_playcount, pero acá se me pasó blindarlo en la
    v1 de _score_track_version. A diferencia de _track_playcount, un
    valor inválido cae a None (no a 0): 0 sería un "año" falso que
    rompería silenciosamente cuál versión es más antigua/nueva del
    grupo — mejor no tener el dato que tener uno inventado."""
    try:
        v = track.get('album_year')
        return int(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _dedupe_by_normalized_title(tracks, normalize_title_fn):
    """Ticket AI-30 — bug real confirmado con log de Niko: "For Whom The
    Bell Tolls" y "For Whom The Bell Tolls (Remastered)" aparecían las
    DOS en el mismo resultado, sin que ninguna fuera una sugerencia de
    Gemini — el bugfix anterior (dedupe contra sugerencias) no cubre
    este caso porque acá ninguna de las dos viene de una sugerencia,
    son dos filas distintas que el propio filter_pool trae. La SQL
    (_track_dedupe_condition) ya dedupea títulos EXACTOS entre álbumes/
    calidades distintas, pero no variantes con sufijo de versión
    distinto (mismo caso que Ticket 14 en Infinite Radio) — se agrupa acá
    por título normalizado y se conserva solo la PRIMERA de cada grupo
    (la lista ya viene ordenada por ranking/pop_score desde
    _query_tracks, así que la primera es la mejor rankeada de ese
    grupo)."""
    if not normalize_title_fn:
        return tracks
    seen_titles = set()
    result = []
    for t in tracks:
        key = normalize_title_fn(t.get('title'))
        if key and key in seen_titles:
            continue
        if key:
            seen_titles.add(key)
        result.append(t)
    return result


def _merge_selected_by_track_popularity(selected_by_name, order, priority_ids=None):
    """Ticket AI-28 — v3 (ajuste sobre v2, ver commit 81adad3c). v2
    alternaba estrictamente por RONDA (una pista de cada artista con
    cuota disponible, en orden fijo dentro de la ronda por reproducciones
    reales) — arregló el bug de v1 (ver docstring histórico más abajo),
    pero Niko lo sintió demasiado mecánico/predecible: "1,1,1,1 y luego
    1,1,1,1 pierde algo de naturalidad".

    v3: en vez de una ronda fija, en cada paso se SORTEA al azar qué
    artista pone la próxima pista, con probabilidad proporcional a
    cuántas pistas le quedan pendientes de SU cuota en ese momento
    (`random.choices` con weights = cuota restante) — es el mismo efecto
    que barajar varios mazos juntos, cada uno con tantas cartas como su
    cuota, sin tocar el orden interno de cada mazo. Un artista con más
    cuota pendiente tiene más chances de salir en cualquier paso dado,
    pero no hay ningún patrón fijo — a veces sale 2 o 3 veces seguidas
    por azar (como un shuffle real), sin que eso implique volver al bug
    de v1: acá NUNCA se comparan reproducciones de un artista contra las
    de otro para decidir el turno, solo la cantidad de pistas que le
    quedan — el bug de v1 (un catálogo "parejo" eclipsando a uno "pico y
    caída") no puede reaparecer porque el valor absoluto de reproducciones
    ya no interviene en esa decisión.

    Las reproducciones reales de cada pista (lastfm_playcount, a nivel de
    PISTA) siguen decidiendo el orden interno de cada artista (sus
    propios mazos vienen pre-ordenados de más a menos escuchada antes de
    barajar).

    Ticket AI-30 (feedback de Niko revisando el log real: las
    sugerencias de Gemini casi nunca aparecían en la página 1, porque
    con solo 8 sugeridas compitiendo contra una cuota de ~90 dentro de
    un pool de 150, terminaban perdidas en algún punto intermedio según
    su reproducciones real). `priority_ids` (set de ids, default None =
    sin prioridad, comportamiento IDÉNTICO al de antes): dentro del mazo
    de CADA artista, las pistas cuyo id está en `priority_ids` (las
    sugerencias ya resueltas) van SIEMPRE primero, sin importar sus
    reproducciones — "las sugerencias van siempre primero en el orden
    final, sin importar reproducciones" (pedido explícito de Niko). El
    barajado ENTRE artistas (de quién pone la próxima carta) no cambia —
    sigue siendo aleatorio ponderado por cupo restante, para no perder
    la mezcla orgánica entre artistas que ya se validó. Efecto práctico:
    todas las sugerencias resueltas de un artista salen antes que
    cualquiera de sus propias pistas de filtro (aunque estas tengan más
    reproducciones), pero siguen intercalándose con las de OTROS
    artistas como siempre.

    Efecto secundario esperado y aceptado (hablado con Niko): al usar
    random.choices, dos pedidos idénticos ("lo mejor de Helloween,
    Stratovarius...") pueden devolver un orden distinto cada vez — es
    justamente lo que da la sensación de "natural"/shuffle real, no un
    bug. La CUOTA por artista (_compute_artist_mix_quotas) sigue sin
    cambios, esto es puramente el orden de presentación.

    Historia (v1, commit 3c84d314): la primera versión de este merge
    comparaba lastfm_playcount en valor absoluto CRUZANDO artistas — se
    rompía cuando un artista con catálogo parejo (ej. Sonata Arctica)
    eclipsaba a uno con perfil pico-y-caída (ej. Helloween) durante
    muchas posiciones seguidas. v2 lo arregló con rondas estrictas; v3
    mantiene ese arreglo pero afloja la rigidez de la ronda."""
    priority_ids = priority_ids or set()

    def _sort_key(t):
        # (0, ...) = es una sugerencia -> siempre antes que (1, ...).
        # Dentro de cada uno de esos dos grupos, orden de siempre por
        # reproducciones reales descendente.
        return (0 if t.get('id') in priority_ids else 1, -_track_playcount(t))

    pools = {nm: sorted(selected_by_name.get(nm, []), key=_sort_key) for nm in order}
    pointers = {nm: 0 for nm in order}
    remaining_len = {nm: len(pools[nm]) for nm in order}
    result = []
    total = sum(remaining_len.values())
    for _ in range(total):
        candidates = [nm for nm in order if pointers[nm] < remaining_len[nm]]
        if not candidates:
            break
        weights = [remaining_len[nm] - pointers[nm] for nm in candidates]
        chosen = random.choices(candidates, weights=weights, k=1)[0]
        result.append(pools[chosen][pointers[chosen]])
        pointers[chosen] += 1
    return result


def _query_tracks_balanced_by_artist(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                                      artist_id_groups, album_ids=None, track_ids=None, ranking=None,
                                      playlist_size=_PLAYLIST_SIZE, pistas_sugeridas_por_artista=None,
                                      normalize_title_fn=None, led_quality_rank_fn=None):
    """Ticket AI-28 (Ticket 41, punto 1 — reportado por Niko: "Dame un
    Mix con lo mejor de: Helloween, Stratovarius, Sonata Arctica,
    Hammerfall..." quedaba casi todo concentrado en uno solo). Causa
    raíz (ver ticket): _query_tracks() de siempre arma UNA sola consulta
    con todos los artist_id mezclados en un set plano y un único
    ORDER BY + LIMIT — el artista con más `lastfm_playcount` copa el
    LIMIT antes de que el sistema llegue a los demás, sin que exista
    ninguna cuota ni garantía por artista.

    Se llama a esta función cuando hay 2+ NOMBRES de artista resueltos
    (`artist_id_groups` con 2+ claves), O exactamente 1 nombre CON
    sugerencias de Gemini para él (Ticket AI-30, ver generate_playlist)
    — con 1 solo nombre y sin sugerencias se sigue usando _query_tracks
    tal cual, orden puro por ranking, sin ningún cambio de comportamiento
    (pedido explícito de Niko: el balanceo es solo para "mix" de 2+ o
    para inyectar sugerencias, no para "lo mejor de Stratovarius" a
    secas sin más señales).

    Estrategia (decidida con Niko, Ticket 41):
    1. Popularidad de cada artista nombrado vía _artist_popularity_proxy.
    2. Cuotas vía _compute_artist_mix_quotas (50/50 con 2 nombres; con
       3+, top-2 se llevan 60% ponderado por raíz cuadrada entre ellos,
       el resto se reparte parejo con el 40% restante; con 1 solo
       nombre, toda la cuota es playlist_size).
    3. Por cada artista, se trae un pool CANDIDATO más grande que su
       cuota. Ticket AI-30 (pedido por Niko: aprovechar el conocimiento
       musical de Gemini): si `pistas_sugeridas_por_artista` trae
       títulos para este artista puntual, el 60% de SU cuota
       (_SUGGESTION_SHARE) se intenta cubrir primero con esas
       sugerencias, resueltas y validadas contra la biblioteca real de
       ESE artista (_resolve_suggested_track_version — elige la mejor
       versión entre estudio/vivo/remaster si hay varias); el resto de
       la cuota (o toda, si no hay sugerencias para este artista, o si
       las sugeridas no llegan al 60%) sale de la búsqueda por filtros
       de siempre (mismo ORDER BY/ranking y mismos filtros de
       args_dict/album_ids/track_ids de siempre, vía _query_tracks — no
       se reinventa el criterio de matching ni de orden).
    4. Si un artista nombrado no tiene suficientes pistas en la
       biblioteca para cubrir su cuota, el faltante se reparte entre los
       demás artistas nombrados que sí tengan pistas de sobra —
       priorizando al más popular primero, en ronda (nunca se acorta la
       playlist por esto salvo que TODOS los artistas nombrados juntos
       no alcancen para playlist_size pistas — ahí sí, la lista sale más
       corta, no hay de dónde más sacar).
    5. El resultado final se arma con un shuffle ponderado por cuota
       restante (_merge_selected_by_track_popularity — v3, ver su
       docstring para el historial de v1/v2 y por qué se llegó acá).

    Devuelve una lista de tracks ya en el orden final (longitud
    <= playlist_size). Con `ranking` presente, _finalize_pool (llamado
    desde generate_playlist) hace pool[:playlist_size] sobre este
    resultado sin reordenar — el orden que arma esta función ES el
    orden final que ve el usuario. Sin ranking, _finalize_pool hace
    random.sample, que reordena pero conserva el CONJUNTO ya balanceado
    de pistas (la mezcla de artistas no se pierde, solo el orden)."""
    names = list(artist_id_groups.keys())
    pop_by_name = {nm: _artist_popularity_proxy(conn, artist_id_groups[nm]) for nm in names}
    quotas = _compute_artist_mix_quotas(pop_by_name, playlist_size)
    order = sorted(names, key=lambda nm: pop_by_name[nm], reverse=True)
    suggestions_by_name = pistas_sugeridas_por_artista or {}

    fetched = {}
    all_priority_ids = set()
    for nm in order:
        quota = quotas.get(nm, 0)
        # Buffer de candidatos por sobre la cuota, para tener margen de
        # dónde tapar el hueco de otro artista si le falta (paso 4). Tope
        # en _CANDIDATE_POOL_SIZE para no pedir de más si la cuota ya es
        # grande (ej. "las mejores 100 de estos 3 artistas").
        candidate_limit = min(_CANDIDATE_POOL_SIZE, max(quota * 4, quota + 20))
        suggested_titles = suggestions_by_name.get(nm) or []

        resolved_suggestions = []
        if suggested_titles and normalize_title_fn and led_quality_rank_fn:
            # Ticket AI-30 — fix (hallazgo revisando el log real:
            # "Metallica y artistas similares" hacía fallar la
            # resolución de "One", que con SOLO Metallica sí resolvía
            # bien). Causa: artist_id_groups[nm] incluye los ids de los
            # similares expandidos cuando buscar_similares=True — con 9
            # artistas combinados (Metallica + 8 similares), el límite
            # de _fetch_artist_track_pool (500, pensado para UN
            # catálogo) podía dejar afuera pistas de Metallica antes de
            # llegar a "One", según cómo ordena pop_score al grupo
            # combinado. La sugerencia es de Gemini PARA Metallica
            # puntual, no para "Metallica y 8 más" — tiene que resolver
            # solo contra el catálogo propio del artista nombrado, sin
            # la expansión a similares (que sigue aplicando igual que
            # siempre para el resto de la función — quota, filter_pool,
            # etc. — esto NO cambia). Se re-resuelve sin expand_similar
            # en vez de guardar esto aparte en _resolve_artist_ids_grouped
            # para no tocar su firma/return por un caso acotado.
            own_artist_ids = _resolve_artist_ids(conn, [nm], None, expand_similar=False) or artist_id_groups[nm]
            artist_pool = _fetch_artist_track_pool(
                conn, own_artist_ids, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn
            )
            suggested_target = round(quota * _SUGGESTION_SHARE)
            seen_ids = set()
            resolution_log = []
            for title in suggested_titles:
                match = _resolve_suggested_track_version(artist_pool, title, normalize_title_fn, led_quality_rank_fn)
                if match and match['id'] not in seen_ids:
                    resolved_suggestions.append(match)
                    seen_ids.add(match['id'])
                    resolution_log.append({'sugerido': title, 'resuelto': True, 'match': match.get('title')})
                elif not match:
                    # Ticket AI-31 (afinar el modelo): título que Gemini
                    # propuso pero no matcheó nada en el catálogo real de
                    # este artista — o Gemini "alucinó" la canción, o el
                    # título está guardado muy distinto en la biblioteca.
                    resolution_log.append({'sugerido': title, 'resuelto': False})
                if len(resolved_suggestions) >= suggested_target:
                    break
            if resolution_log:
                _audit_logger.info(json.dumps(
                    {'artista_sugerencias': nm, 'resolucion': resolution_log}, ensure_ascii=False
                ))

        filter_pool = _query_tracks(
            conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            artist_ids=artist_id_groups[nm], album_ids=album_ids, track_ids=track_ids, ranking=ranking,
            limit_override=candidate_limit
        )
        # Ticket AI-30 (bug real: "For Whom The Bell Tolls" duplicada
        # sin ser sugerencia — ver _dedupe_by_normalized_title). Se
        # aplica siempre, haya o no sugerencias resueltas este turno.
        filter_pool = _dedupe_by_normalized_title(filter_pool, normalize_title_fn)
        if resolved_suggestions:
            # Las sugeridas van PRIMERO en fetched[nm] — el slice
            # [:quota] de más abajo (`selected`, sin cambios respecto a
            # AI-28) es lo que efectivamente logra el reparto 60/40 sin
            # tocar esa lógica en absoluto: toma hasta `suggested_target`
            # sugeridas + el resto de filter_pool hasta completar la
            # cuota.
            #
            # Bugfix (Ticket AI-30, confirmado con log real de Niko:
            # "For Whom The Bell Tolls" aparecía DOS veces — la elegida
            # por sugerencia y también su "(Remastered)" vía el filtro).
            # No alcanza con descartar por id exacto: otra VERSIÓN de la
            # misma canción sugerida (remaster/vivo/acústico) tiene un id
            # distinto pero es el mismo tema — se descarta también por
            # título normalizado (mismo criterio de agrupación que ya usa
            # _resolve_suggested_track_version).
            suggested_ids = {t['id'] for t in resolved_suggestions}
            suggested_norm_titles = {normalize_title_fn(t.get('title')) for t in resolved_suggestions}
            filter_pool = [
                t for t in filter_pool
                if t['id'] not in suggested_ids and normalize_title_fn(t.get('title')) not in suggested_norm_titles
            ]
            fetched[nm] = resolved_suggestions + filter_pool
            # Ticket AI-30 (pedido de Niko revisando el log real): se
            # acumulan estos ids para que _merge_selected_by_track_popularity
            # los ponga siempre primero dentro del mazo de este artista,
            # sin importar sus reproducciones — antes quedaban perdidos
            # en algún punto intermedio del pool de 150 según su
            # popularidad real, y casi nunca aparecían en la página 1.
            all_priority_ids |= suggested_ids
        else:
            fetched[nm] = filter_pool

    selected = {nm: fetched[nm][:quotas.get(nm, 0)] for nm in order}
    total_deficit = sum(max(0, quotas.get(nm, 0) - len(fetched[nm])) for nm in order)
    if total_deficit > 0:
        surplus_ptr = {nm: len(selected[nm]) for nm in order}
        progressed = True
        while total_deficit > 0 and progressed:
            progressed = False
            for nm in order:
                if total_deficit <= 0:
                    break
                ptr = surplus_ptr[nm]
                if ptr < len(fetched[nm]):
                    selected[nm].append(fetched[nm][ptr])
                    surplus_ptr[nm] = ptr + 1
                    total_deficit -= 1
                    progressed = True
            # si progressed queda False, ningún artista nombrado tiene ya
            # más pistas disponibles — la playlist sale más corta que
            # playlist_size, no hay de dónde más sacar (caso real de
            # biblioteca chica para TODOS los artistas del mix a la vez).

    merged = _merge_selected_by_track_popularity(selected, order, priority_ids=all_priority_ids)
    return merged, all_priority_ids


# Ticket AI-30 — qué fracción del cupo de un artista (su cuota en un
# mix, o el playlist_size completo si es el único nombrado) se llena
# con sugerencias de Gemini antes de completar con la búsqueda por
# filtros de siempre. Mismo valor y mismo criterio de "si no alcanza el
# 60%, completa el filtro" para el caso "artistas sugeridos" (sin
# ningún artista nombrado) — decidido con Niko en el chat, no hay razón
# para que sean números distintos entre los dos casos.
_SUGGESTION_SHARE = 0.6


def _score_track_version(track, group, led_quality_rank_fn):
    """Ticket AI-30 — score para elegir la versión "canónica" entre
    variantes de una misma canción (estudio/vivo/remaster/acústico) al
    resolver una sugerencia de Gemini contra la biblioteca real. Fórmula
    confirmada con Niko (ejemplo trabajado en el chat: Master of Puppets
    estudio 1986 vs remaster 2017 vs en vivo 1999 — gana la de estudio):

        score = calidad×0.4 + reproducciones×0.4 + bonus_original×0.2

    - calidad: ranking de led_color (led_quality_rank_fn, mismo mapeo
      que ya usa _track_dedupe_condition del lado de app.py — inyectada
      por parámetro, este módulo no reimplementa ese mapeo).
    - reproducciones: lastfm_playcount normalizado 0-100 RELATIVO al
      máximo del grupo (no de toda la biblioteca) — lo que importa acá
      es cuál versión puntual es más escuchada, no compararla contra
      canciones de otros artistas.
    - bonus_original: 100 si es la versión del álbum con el `year` más
      antiguo del grupo (el debut de ese tema), decayendo LINEAL según
      qué tan lejos está ese año del más antiguo del grupo (0 en el año
      más nuevo del grupo) — así "ser el original" pesa fuerte sin
      aplastar del todo a una versión bastante mejor grabada o más
      escuchada. 0 si no hay dato de año en el grupo."""
    quality = led_quality_rank_fn(track.get('led_color'))
    max_pc = max((_track_playcount(t) for t in group), default=0) or 1
    pop_norm = _track_playcount(track) / max_pc * 100.0

    years = [_track_album_year(t) for t in group]
    years = [y for y in years if y is not None]
    my_year = _track_album_year(track)
    if years and my_year is not None:
        oldest, newest = min(years), max(years)
        bonus = 100.0 if newest == oldest else 100.0 * (newest - my_year) / (newest - oldest)
    else:
        bonus = 0.0

    return quality * 0.4 + pop_norm * 0.4 + bonus * 0.2


def _fetch_artist_track_pool(conn, artist_ids, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn):
    """Ticket AI-30 — trae TODAS las pistas de un artista puntual (o de
    su grupo de ids si buscar_similares expandió a similares — mismo
    set que ya usa el resto de generate_playlist, ver artist_id_groups),
    ya convertidas a dict vía track_to_json_fn (mismo formato que el
    resto del pipeline), para poder agruparlas por título normalizado y
    resolver sugerencias de Gemini contra ellas (ver
    _resolve_suggested_track_version). Reusa _query_tracks tal cual en
    vez de escribir SQL nuevo — mismo JOIN/criterio de matching de
    siempre (ver AGENTE.md regla 2). skip_dedupe=True a propósito: acá
    hacen falta TODAS las versiones existentes para poder puntuarlas
    (_score_track_version), no la que ya elige _track_dedupe_condition
    para la vista general. limit_override generoso (500) — ni el
    catálogo de un artista muy prolífico debería superarlo en una
    biblioteca personal."""
    return _query_tracks(
        conn, {}, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
        artist_ids=artist_ids, skip_dedupe=True, limit_override=500
    )


def _resolve_suggested_track_version(artist_pool, suggested_title, normalize_title_fn, led_quality_rank_fn):
    """Ticket AI-30 — dado el pool YA TRAÍDO de todas las pistas de un
    artista (ver _fetch_artist_track_pool) y un título que Gemini
    propuso como "de las más relevantes" de ese artista, encuentra el
    grupo de pistas que representan ese tema puntual (mismo título
    normalizado — agrupa estudio/vivo/remaster/acústico aunque el
    título literal sea distinto, vía normalize_title_fn inyectada,
    _normalize_title_for_radio_dedupe de app.py / Ticket 14) y devuelve
    UNA sola pista para representarlo, elegida por _score_track_version.

    No reusa _resolve_track_ids (Ticket AI-12) a propósito: esa función
    busca el match ÚNICO más cercano (fuzzy con n=1) contra el título
    tal cual está guardado — no agrupa variantes con sufijo de versión
    distinto, que es exactamente lo que hace falta acá. Devuelve None si
    ninguna pista del artista se parece lo suficiente al título
    sugerido (ej. Gemini alucinó una canción que ese artista no tiene)."""
    norm_target = normalize_title_fn(suggested_title)
    if not norm_target:
        return None

    groups = {}
    for t in artist_pool:
        key = normalize_title_fn(t.get('title'))
        if key:
            groups.setdefault(key, []).append(t)

    group = groups.get(norm_target)
    if not group:
        close = difflib.get_close_matches(norm_target, list(groups.keys()), n=1, cutoff=_TRACK_MATCH_CUTOFF)
        if not close:
            return None
        group = groups[close[0]]

    return max(group, key=lambda t: _score_track_version(t, group, led_quality_rank_fn))


def _query_tracks_with_suggested_artists(conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                                          suggested_artist_ids, album_ids, track_ids, ranking, playlist_size,
                                          normalize_title_fn=None):
    """Ticket AI-30 — caso "sin artistas nombrados" (complementario al de
    _query_tracks_balanced_by_artist, que cubre "con artistas
    nombrados"). Cuando el usuario NO nombró ningún artista pero Gemini
    propuso artistas reales que calzan con TODA la intención combinada
    del pedido (género+idioma+década+mood juntos — ver
    "artistas_sugeridos" en el prompt) y esos nombres SÍ existen en la
    biblioteca (resuelto en generate_playlist vía _resolve_artist_ids,
    sin expansión a similares — son ya la sugerencia final de Gemini,
    no hace falta expandir más), se arma el pool así:

    1. _SUGGESTION_SHARE (60%) del playlist_size sale de esos artistas
       sugeridos — CON los mismos filtros del pedido (género/idioma/
       década/etc, vía args_dict) para no traer, por ejemplo, un tema en
       inglés de un artista sugerido si el usuario pidió específicamente
       "en español".
    2. El resto (40%, o más si lo sugerido no alcanza el 60%) sale de la
       búsqueda por filtros de siempre, sin restricción de artista.
    3. Se descarta de la lista de filtros cualquier pista que ya haya
       salido por sugerencia — por id exacto Y por título normalizado
       (Ticket AI-30 bugfix, mismo confirmado con log real en
       _query_tracks_balanced_by_artist: dos versiones de la misma
       canción, ej. "(Remasterizado)", tienen id distinto y se colaban
       igual) —, y el resultado final se arma con el mismo shuffle
       ponderado que el resto de este ticket
       (_merge_selected_by_track_popularity)."""
    suggested_target = round(playlist_size * _SUGGESTION_SHARE)
    suggested_candidate_limit = min(_CANDIDATE_POOL_SIZE, max(suggested_target * 4, suggested_target + 20))
    suggested_pool = _query_tracks(
        conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
        artist_ids=suggested_artist_ids, album_ids=album_ids, track_ids=track_ids, ranking=ranking,
        limit_override=suggested_candidate_limit
    )
    # Ticket AI-30 (mismo bug de "For Whom The Bell Tolls" — ver
    # _dedupe_by_normalized_title): se aplica también acá, antes de
    # cortar a suggested_target, para no gastar cupo en 2 versiones del
    # mismo tema de un artista sugerido.
    suggested_pool = _dedupe_by_normalized_title(suggested_pool, normalize_title_fn)[:suggested_target]
    suggested_ids = {t['id'] for t in suggested_pool}
    suggested_norm_titles = (
        {normalize_title_fn(t.get('title')) for t in suggested_pool} if normalize_title_fn else set()
    )

    filter_target = playlist_size - len(suggested_pool)
    filter_pool = []
    if filter_target > 0:
        filter_candidate_limit = min(_CANDIDATE_POOL_SIZE, max(filter_target * 4, filter_target + 20))
        filter_candidates = _query_tracks(
            conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            album_ids=album_ids, track_ids=track_ids, ranking=ranking,
            limit_override=filter_candidate_limit
        )
        filter_candidates = _dedupe_by_normalized_title(filter_candidates, normalize_title_fn)
        filter_pool = [
            t for t in filter_candidates
            if t['id'] not in suggested_ids
            and (not normalize_title_fn or normalize_title_fn(t.get('title')) not in suggested_norm_titles)
        ][:filter_target]

    merged = _merge_selected_by_track_popularity(
        {'sugeridos_gemini': suggested_pool, 'filtro_general': filter_pool},
        ['sugeridos_gemini', 'filtro_general'],
        priority_ids=suggested_ids
    )
    return merged, suggested_ids


def _ensure_suggested_artists_represented(conn, pool, suggested_artist_ids, args_dict, track_to_json_fn,
                                           build_adv_filters_fn, dedupe_condition_fn, ranking, playlist_size,
                                           normalize_title_fn):
    """Ticket AI-30 (feedback de Niko revisando el log real: "Lo mejor
    del jazz de los 60" -> Nina Simone sola llenó las 150 pistas del
    pool vía la cascada artista-primero (Ticket AI-24) ANTES de que
    _query_tracks_with_suggested_artists tuviera oportunidad de correr
    — ninguna de las 8 sugerencias de Gemini (Miles Davis, Coltrane,
    Bill Evans...) entraba). La cascada puede llenar playlist_size por
    sí sola sin dejar ningún hueco para el camino de "enriquecido" que
    es el único lugar donde hoy se invoca la reserva de sugerencias.

    Esta función se llama DESPUÉS de que la cascada (o su enriquecido)
    ya armó `pool`, como un chequeo final: si `suggested_artist_ids`
    tiene contenido y ese pool no llega al cupo esperado
    (_SUGGESTION_SHARE de playlist_size) de pistas de esos artistas,
    trae más de ellos (con los MISMOS filtros del pedido) y los inyecta,
    descartando del final del pool lo que NO sea de un artista sugerido
    para no pasarse de playlist_size. Si el pool YA tenía suficiente
    representación (por casualidad, ej. "música chilena" donde varios
    sugeridos ya eran genuinamente populares), no toca nada.

    Devuelve (pool_final, priority_ids) — priority_ids para que quien
    llama reordene con _merge_selected_by_track_popularity y las nuevas
    salgan primero (mismo criterio ya validado con Niko para el resto
    de AI-30), no solo que "estén" en el pool sino que se VEAN."""
    if not suggested_artist_ids:
        return pool, set()

    target = round(playlist_size * _SUGGESTION_SHARE)
    already_from_suggested = [t for t in pool if t.get('artist_id') in suggested_artist_ids]
    deficit = target - len(already_from_suggested)
    if deficit <= 0:
        return pool, {t['id'] for t in already_from_suggested}

    candidate_limit = min(_CANDIDATE_POOL_SIZE, max(deficit * 4, deficit + 20))
    extra_candidates = _query_tracks(
        conn, args_dict, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
        artist_ids=suggested_artist_ids, ranking=ranking, limit_override=candidate_limit
    )
    already_ids = {t['id'] for t in pool}
    already_norm_titles = {normalize_title_fn(t.get('title')) for t in pool} if normalize_title_fn else set()
    new_ones = [
        t for t in extra_candidates
        if t['id'] not in already_ids
        and (not normalize_title_fn or normalize_title_fn(t.get('title')) not in already_norm_titles)
    ][:deficit]
    if not new_ones:
        # No hay más pistas disponibles de los artistas sugeridos (con
        # estos filtros) — se deja el pool tal cual, no hay de dónde
        # sacar más (mismo criterio de "no forzar" del resto del ticket).
        return pool, {t['id'] for t in already_from_suggested}

    # Se descartan del FINAL del pool tantas pistas ajenas a los
    # artistas sugeridos como haga falta, para no superar playlist_size.
    non_suggested_idx = [i for i, t in enumerate(pool) if t.get('artist_id') not in suggested_artist_ids]
    to_remove = set(non_suggested_idx[-len(new_ones):]) if non_suggested_idx else set()
    kept = [t for i, t in enumerate(pool) if i not in to_remove]
    final_pool = kept + new_ones
    priority_ids = {t['id'] for t in already_from_suggested} | {t['id'] for t in new_ones}
    return final_pool, priority_ids


def _finalize_pool(pool, ranking, playlist_size=_PLAYLIST_SIZE, priority_ids=None):
    """Ticket AI-22 — con ranking explícito, el pool ya viene ordenado y
    acotado a playlist_size desde la query (ver _query_tracks/
    _query_tracks_own_listens): el usuario pidió un orden real ("lo más
    popular", "lo más infravalorado"), no variedad — se devuelve tal
    cual, sin randomizar. Sin ranking, sigue el comportamiento de
    siempre: muestreo al azar del pool de candidatos más amplio.

    `playlist_size` (Ticket AI-25): _PLAYLIST_SIZE por default, o la
    cantidad explícita que pidió el usuario ("Top 10 de los Beatles").

    Ticket AI-30 (bug real confirmado por Niko: "Metallica y artistas
    similares" — sin "lo mejor", sin ranking — resolvía las 8
    sugerencias de Gemini perfecto, pero NINGUNA aparecía en la página
    1). Causa: el camino sin ranking hacía random.sample() sobre TODO
    el pool sin distinguir sugerencias del resto — con 8 sugerencias
    entre ~150 candidatos, la chance de que alguna caiga en las
    primeras 25 al muestrear al azar es baja. `priority_ids` (default
    None = sin cambio de comportamiento): las pistas con id en ese set
    se incluyen SIEMPRE en el resultado (hasta playlist_size), el resto
    de los cupos se llena con random.sample sobre lo que queda — mismo
    principio de "las sugerencias van siempre primero" ya aplicado en
    _merge_selected_by_track_popularity, ahora también acá."""
    if ranking:
        return pool[:playlist_size]
    priority_ids = priority_ids or set()
    if not priority_ids:
        sample_size = min(playlist_size, len(pool))
        return random.sample(pool, sample_size) if pool else []
    priority_tracks = [t for t in pool if t.get('id') in priority_ids]
    rest = [t for t in pool if t.get('id') not in priority_ids]
    rest_sample_size = max(0, min(playlist_size - len(priority_tracks), len(rest)))
    return priority_tracks[:playlist_size] + (random.sample(rest, rest_sample_size) if rest_sample_size else [])


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
                       build_similar_artists_fn, normalize_title_fn=None, led_quality_rank_fn=None):
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
    # Ticket AI-28 (Ticket 41, punto 1): se resuelve agrupado por nombre
    # (no un set plano) para poder darle una cuota a cada artista
    # nombrado más abajo si son 2+ — ver _run_query. `artist_ids` (el
    # set plano de siempre) se deriva del mismo dict y sigue
    # alimentando exactamente lo mismo que antes (filters_applied, hint
    # para álbumes/pistas, la cascada artista-primero): ningún otro uso
    # de artist_ids en esta función cambia de comportamiento.
    artist_id_groups = _resolve_artist_ids_grouped(
        conn, entities.get('artists') or [], build_similar_artists_fn, expand_similar=buscar_similares
    )
    artist_ids = set()
    for _bucket in artist_id_groups.values():
        artist_ids |= _bucket

    # Ticket AI-30 (idea de Niko: aprovechar el conocimiento musical
    # general de Gemini) — SOLO tiene sentido cuando el usuario no
    # nombró ningún artista (con nombres explícitos, la sugerencia que
    # importa es de PISTAS por artista — ver pistas_sugeridas_por_artista
    # más abajo, en _run_query). expand_similar=False y
    # build_similar_artists_fn=None a propósito: estos ya SON la
    # sugerencia final de Gemini, no hace falta expandirlos a similares
    # de nuevo (ver _resolve_artist_ids_grouped — con expand_similar=False
    # nunca se llega a invocar build_similar_artists_fn, es seguro pasar
    # None acá).
    suggested_artist_ids = set()
    if not artist_id_groups and entities.get('artistas_sugeridos'):
        suggested_artist_ids = _resolve_artist_ids(
            conn, entities['artistas_sugeridos'], None, expand_similar=False
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
            # Ticket AI-28: el balanceo por cuota no cubre este camino
            # (JOIN distinto, contra listening_events — fuera del
            # alcance investigado en el Ticket 41, que se enfocó en
            # _query_tracks). Con 2+ artistas nombrados y "mis escuchas
            # de X, Y y Z", sigue el comportamiento de siempre.
            pool = _query_tracks_own_listens(
                conn, args_dict_local, user_id, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids,
                playlist_size=playlist_size
            )
            return pool, set()
        suggested_tracks_by_artist = entities.get('pistas_sugeridas_por_artista') or {}
        if len(artist_id_groups) >= 2 or (len(artist_id_groups) == 1 and suggested_tracks_by_artist):
            # Ticket AI-28 (Ticket 41, punto 1) — 2+ artistas nombrados:
            # cuota por artista + intercalado, en vez de una sola
            # consulta con todos los IDs mezclados (causa raíz del mix
            # desbalanceado hacia el más escuchado). Ticket AI-30 amplía
            # esto a 1 SOLO artista cuando Gemini sugirió pistas para él
            # (con 1 artista y SIN sugerencias, sigue cayendo al camino
            # de _query_tracks de siempre más abajo, sin cambios — pedido
            # explícito de Niko: el balanceo/sugerencias no aplican a
            # "lo mejor de Stratovarius" a secas, sin más señales).
            return _query_tracks_balanced_by_artist(
                conn, args_dict_local, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                artist_id_groups=artist_id_groups, album_ids=album_ids, track_ids=track_ids, ranking=ranking,
                playlist_size=playlist_size, pistas_sugeridas_por_artista=suggested_tracks_by_artist,
                normalize_title_fn=normalize_title_fn, led_quality_rank_fn=led_quality_rank_fn
            )
        if not artist_id_groups and suggested_artist_ids:
            # Ticket AI-30 — sin ningún artista nombrado, pero Gemini
            # propuso artistas que sí existen en la biblioteca: 60% del
            # pool sale de ellos (con los mismos filtros del pedido),
            # 40% de la búsqueda por filtros de siempre sin restricción
            # de artista. Ver _query_tracks_with_suggested_artists.
            #
            # Interacción con la cascada artista-primero (Ticket AI-24,
            # corregido — confirmado con log real de Niko, request 102):
            # cuando hay ranking Y ni artist/album/track identity, la
            # cascada corre PRIMERO (más abajo en esta función) y, si
            # trae MENOS de playlist_size, llama a _run_query(args_dict)
            # para completar el resto ("enriquecido_directo") — ESA
            # llamada sí pasa por acá. En la práctica, entonces, esta
            # rama SÍ termina contribuyendo cuando la cascada se queda
            # corta — no compite con ella, la complementa en el hueco que
            # deja. Si la cascada por sí sola ya llena playlist_size, esta
            # rama nunca se ejecuta para ese pedido — comportamiento
            # correcto: la cascada (basada en agregados reales de la
            # biblioteca) ya alcanzó, no hace falta la sugerencia.
            return _query_tracks_with_suggested_artists(
                conn, args_dict_local, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                suggested_artist_ids=suggested_artist_ids, album_ids=album_ids, track_ids=track_ids,
                ranking=ranking, playlist_size=playlist_size, normalize_title_fn=normalize_title_fn
            )
        pool = _query_tracks(
            conn, args_dict_local, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
            artist_ids=artist_ids, album_ids=album_ids, track_ids=track_ids, ranking=ranking,
            playlist_size=playlist_size
        )
        return pool, set()

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
            final_pool = cascade_tracks[:playlist_size]
            # Ticket AI-30 (feedback de Niko: la cascada por sí sola
            # puede llenar TODO el cupo con un solo artista dominante —
            # ej. "lo mejor del jazz de los 60" -> Nina Simone — sin
            # dejar hueco para las sugerencias de Gemini. Se chequea acá
            # SIEMPRE, no solo en el camino de "enriquecido" de más
            # abajo, que es el único que corría antes.
            final_pool, priority_ids = _ensure_suggested_artists_represented(
                conn, final_pool, suggested_artist_ids, args_dict, track_to_json_fn, build_adv_filters_fn,
                dedupe_condition_fn, ranking, playlist_size, normalize_title_fn
            )
            if priority_ids:
                final_pool = _merge_selected_by_track_popularity({'pool': final_pool}, ['pool'], priority_ids=priority_ids)
                filters_applied['sugerencias_inyectadas'] = True
            return final_pool, filters_applied, False
        if cascade_tracks:
            # Bug encontrado en AI-26 (independiente de la paginación,
            # vive acá adentro de una sola llamada): comparar por id
            # exacto acá dejaba pasar una versión distinta (otro álbum)
            # de un tema que la cascada ya había traído. Se compara por
            # título+artista normalizados en vez de por id.
            seen_keys = {_dedupe_key(t.get('title'), t.get('artist')) for t in cascade_tracks}
            direct_pool, _ = _run_query(args_dict)
            extra = [t for t in direct_pool if _dedupe_key(t.get('title'), t.get('artist')) not in seen_keys]
            combined = cascade_tracks + extra[:max(0, playlist_size - len(cascade_tracks))]
            if combined:
                filters_applied = _filters_applied(args_dict)
                filters_applied['cascada_artista_primero'] = True
                filters_applied['enriquecido_directo'] = True
                # Ticket AI-30 — mismo chequeo que arriba: _run_query ya
                # intenta esto vía _query_tracks_with_suggested_artists,
                # pero acá "direct_pool" es el resultado COMPLETO de
                # _run_query, no necesariamente con la reserva del 60%
                # respetada tras el recorte de `extra[:...]` — se
                # confirma acá, no se asume.
                combined, priority_ids = _ensure_suggested_artists_represented(
                    conn, combined, suggested_artist_ids, args_dict, track_to_json_fn, build_adv_filters_fn,
                    dedupe_condition_fn, ranking, playlist_size, normalize_title_fn
                )
                if priority_ids:
                    combined = _merge_selected_by_track_popularity({'pool': combined}, ['pool'], priority_ids=priority_ids)
                    filters_applied['sugerencias_inyectadas'] = True
                return combined, filters_applied, False

    pool, priority_ids = _run_query(args_dict)
    if pool:
        return _finalize_pool(pool, ranking, playlist_size=playlist_size, priority_ids=priority_ids), \
            _filters_applied(args_dict), False

    for field in _RELAXATION_ORDER:
        if field not in args_dict:
            continue
        dropped.add(field)
        retry_args = _entities_to_args_dict(entities, drop_fields=dropped)
        pool, priority_ids = _run_query(retry_args)
        if pool:
            return _finalize_pool(pool, ranking, playlist_size=playlist_size, priority_ids=priority_ids), \
                _filters_applied(retry_args), True

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


def _log_audit(request_id, raw_query, provider, entities, filters_applied, used_fallback, tracks):
    """Ticket AI-31 (pedido por Niko) — una línea por pedido de playlist
    con todo lo necesario para ir afinando el prompt/modelo con el
    tiempo: el pedido tal cual lo escribió el usuario, qué sugirió
    Gemini (si sugirió algo — vacío si no aplicaba, ver
    "pistas_sugeridas_por_artista"/"artistas_sugeridos" en
    _empty_entities), qué filtros se terminaron usando, y el resultado
    final YA FILTRADO (artista + título de cada pista — no hace falta
    el dict completo de cada una acá, con eso alcanza para ver a ojo si
    la sugerencia de Gemini terminó representada en el resultado o no).

    Nunca lanza — un log de auditoría roto NUNCA debe tumbar un pedido
    real (aprendizaje directo del bug de _score_track_version: si algo
    inesperado llega en `tracks`, esto degrada a un WARNING en el
    logger de siempre en vez de propagar)."""
    try:
        payload = {
            'request_id': request_id,
            'raw_query': raw_query,
            'provider': provider,
            'artists': entities.get('artists') or [],
            'pistas_sugeridas_por_artista': entities.get('pistas_sugeridas_por_artista') or {},
            'artistas_sugeridos': entities.get('artistas_sugeridos') or [],
            'filters_applied': filters_applied,
            'used_fallback': used_fallback,
            'track_count': len(tracks),
            'tracks': [f"{t.get('artist_name') or '?'} - {t.get('title') or '?'}" for t in tracks],
        }
        _audit_logger.info(json.dumps(payload, ensure_ascii=False))
    except Exception:
        _logger.warning('AI-31: no se pudo armar el log de auditoría (request_id=%r)', request_id, exc_info=True)


def handle_request(conn, user_id, raw_query, track_to_json_fn, build_adv_filters_fn, dedupe_condition_fn,
                    build_similar_artists_fn, normalize_title_fn=None, led_quality_rank_fn=None,
                    prior_entities=None, default_results=None, max_top_n=None):
    """Punto de entrada único, llamado desde app.py::api_v1_ai_playlist.
    Nunca lanza (todo error de proveedor externo se degrada a fallback) salvo
    por errores de la propia base de datos, que sí deben propagarse.

    `build_similar_artists_fn` (Ticket AI-11): `build_similar_artists` de
    app.py, inyectada — resuelve artistas nombrados por el usuario contra
    la biblioteca real y los expande a artistas similares (mismo dato que
    ya alimenta la sección "Similares" de cada artista en la app). Ver
    _resolve_artist_ids.

    `normalize_title_fn`/`led_quality_rank_fn` (Ticket AI-30): también
    inyectadas de app.py — `_normalize_title_for_radio_dedupe` (Ticket
    14, agrupa versiones de una misma pista con sufijo de versión
    distinto) y `led_quality_rank` (equivalente en Python del ranking de
    calidad por led_color que ya usa `_track_dedupe_condition`). Se usan
    para resolver y elegir la mejor versión de las sugerencias de pistas
    que Gemini propone por artista — ver generate_playlist/
    _resolve_suggested_track_version. None (default) si el caller no las
    pasa: las sugerencias de Gemini simplemente no se aplican en ese
    caso, sin romper nada del resto del pipeline.

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
            build_similar_artists_fn, normalize_title_fn=normalize_title_fn,
            led_quality_rank_fn=led_quality_rank_fn)
        if result['status'] == 'error':
            used_fallback = True

    tracks = pool[:display_size]

    _request_id = _log_request(conn, user_id, raw_query, result, provider, filters_applied,
                                used_fallback, len(tracks))
    ai_playlist_pagination.store_pool(conn, _request_id, pool, already_shown_count=len(tracks))
    _log_audit(_request_id, raw_query, provider, result['entities'], filters_applied, used_fallback, tracks)

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
