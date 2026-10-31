# -*- coding: utf-8 -*-
"""
Ticket 42 — Skill de Alexa para Orbitron (control por voz).

Backend de la skill: recibe el texto capturado por el slot AMAZON.SearchQuery,
se lo reenvía TAL CUAL a POST /api/v1/ai/playlist de Orbyte (endpoint
existente, sin ningún cambio), y arma la cola de reproducción para
AudioPlayer con las URLs transcodeadas de /api/v1/alexa/stream/<id>.

Variables de entorno requeridas (se configuran en la consola de Lambda):
  ORBYTE_BASE_URL  -> URL pública de Orbyte via Tailscale Funnel,
                      SIN barra final. Ej: https://serverkillerlinux.tuTailnet.ts.net
  ORBYTE_TOKEN     -> Clave de Alexa (`alx_…`) generada en el servidor con
                      ./clave_alexa.sh generar (Ticket 42b): no vence y se
                      revoca con ./clave_alexa.sh revocar.

31-10-2026 — tiempos de espera (bug real visto en CloudWatch): la conexión
tenía 1 s para establecerse. Con la Lambda recién arrancada no hay conexión
abierta: hay que abrir una nueva (AWS Ohio -> entrada del Funnel -> servidor
en Chile) y a veces eso tarda más de 1 s -> «está tardando más de lo normal»
sin que el pedido llegara a Orbyte. Ahora: plazo total calculado contra los
8 s de Alexa, hasta 3 s para conectar, un reintento si la conexión falla
rápido, conexión reutilizada entre pedidos, «Buscando…» inmediato
(respuesta progresiva) y mensajes distintos según qué falló.

No modifica absolutamente nada del backend de Orbyte mas alla de lo ya
pusheado en el Lote 1 (endpoint /api/v1/alexa/stream/<id>). No depende de
ai_playlist.py mas que a traves del endpoint HTTP publico ya existente.
"""

import json
import logging
import os
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

# Las librerías de Alexa traen docstrings con "\&" que Python 3.14 marca como
# SyntaxWarning al importarlas: solo ensucian CloudWatch, no afectan nada.
warnings.filterwarnings("ignore", category=SyntaxWarning)

import boto3
import requests
from requests.adapters import HTTPAdapter
from botocore.config import Config as BotoConfig
from ask_sdk_core.skill_builder import CustomSkillBuilder
from ask_sdk_core.dispatch_components import AbstractRequestHandler, AbstractExceptionHandler
from ask_sdk_core.utils import is_request_type, is_intent_name
from ask_sdk_core.handler_input import HandlerInput
from ask_sdk_dynamodb.adapter import DynamoDbAdapter
from ask_sdk_model import Response
from ask_sdk_model.interfaces.audioplayer import (
    PlayDirective, StopDirective, PlayBehavior, AudioItem, Stream, AudioItemMetadata,
)
from ask_sdk_model.interfaces.display import Image, ImageInstance
from ask_sdk_model.interfaces.alexa.presentation.apl import RenderDocumentDirective

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ORBYTE_BASE_URL = os.environ.get("ORBYTE_BASE_URL", "").rstrip("/")
ORBYTE_TOKEN = os.environ.get("ORBYTE_TOKEN", "")

# Ticket 42, Lote 6 — logo real de Orbyte (fondo negro incluido), ya
# publico sin auth en Flask (/static/* esta exento del gate de sesion).
# Solo hace falta que montes /static tambien en Funnel (mismo comando de
# siempre, ver instrucciones).
LOGO_ART_URL = f"{ORBYTE_BASE_URL}/static/icon-512.png"
LOGO_BACKGROUND_URL = f"{ORBYTE_BASE_URL}/static/og-image.png"

# Ticket 42, Lote 6 — documento APL minimo: pantalla negra + logo de
# Orbyte centrado, para reemplazar la tarjeta generica ("Orbyte" en texto
# plano sobre gris) que Alexa muestra por defecto al abrir la skill. Solo
# se manda si el dispositivo declara soporte para APL (ver
# device_supports_apl) — un Echo Dot sin pantalla nunca recibe esto.
LOGO_APL_DOCUMENT = {
    "type": "APL",
    "version": "2024.1",
    "mainTemplate": {
        "item": {
            "type": "Container",
            "width": "100vw",
            "height": "100vh",
            "backgroundColor": "black",
            "items": [
                {
                    "type": "Image",
                    "source": LOGO_BACKGROUND_URL,
                    "width": "100vw",
                    "height": "100vh",
                    "scale": "best-fit",
                    "align": "center",
                }
            ],
        }
    },
}


def device_supports_apl(handler_input):
    try:
        interfaces = handler_input.request_envelope.context.system.device.supported_interfaces
        return bool(interfaces and interfaces.alexa_presentation_apl)
    except AttributeError:
        return False

# Ticket 42, Lote 6 — bug real encontrado en CloudWatch: una invocacion
# colgo los 10 segundos completos del timeout de Lambda SIN loguear nada
# de lo nuestro, lo que apunta a una llamada a boto3/DynamoDB que se quedo
# esperando (probable: permisos IAM recien agregados, todavia
# propagandose) sin un timeout corto propio — boto3 por defecto puede
# esperar mucho mas que el presupuesto de 8s que nos da Alexa. Con esto,
# si DynamoDB tiene un problema, falla rapido (2s) y el CatchAllException
# ya existente contesta con un mensaje hablado en vez de silencio total.
_boto_config = BotoConfig(connect_timeout=2, read_timeout=2, retries={"max_attempts": 1})

# Ticket 42, Lote 5 — bug real encontrado probando contra un Echo físico:
# los requests de AudioPlayer.* (PlaybackStopped, PlaybackNearlyFinished,
# etc.) NO tienen sesión — Alexa los manda fuera del contexto de cualquier
# sesión de skill (documentado así por Amazon). session_attributes no
# existe en ese momento, así que guardar ahí el token/offset para que
# Next/Previous/Resume lo lean después NUNCA funcionaba en un dispositivo
# real (el mock de pruebas no lo detectó porque simulaba sesión en todos
# los eventos por igual, sin darse cuenta). La solución que recomienda la
# propia documentación de Alexa es persistencia real (DynamoDB), con la
# partition key resuelta desde context.System.user.userId — ese campo SÍ
# está presente incluso sin sesión.
DYNAMODB_TABLE_NAME = os.environ.get("DYNAMODB_TABLE_NAME", "OrbyteAlexaSkillState")
dynamodb_adapter = DynamoDbAdapter(
    table_name=DYNAMODB_TABLE_NAME,
    create_table=True,
    dynamodb_resource=boto3.resource("dynamodb", config=_boto_config),
)

# Ticket 42, Lote 6 — bug real encontrado en CloudWatch: un timeout como
# número único (`timeout=6.5`) en requests aplica SEPARADO a la fase de
# conectar y a la de leer — en el peor caso, ambas fases lentas pueden
# sumar hasta ~13s, superando el límite duro de Lambda (10s) ANTES de que
# nuestro propio except tenga la oportunidad de correr y loguear algo
# (por eso el timeout "invisible" de 10s sin ningún log nuestro). Con una
# tupla (conectar, leer) cada fase queda acotada por separado.
#
# Ticket 42, Lote 7 — corrección sobre el Lote 6: el primer valor que puse
# (2.0 / 4.5, total 6.5s) le sacó de más presupuesto a la fase de LECTURA
# — que es justo donde vive la variabilidad real de Gemini — y esto hizo
# fallar pedidos que antes entraban bien dentro de los 6.5s totales.
# Conectar a un túnel Tailscale ya establecido es rápido (no necesita
# más de 1s); el presupuesto generoso tiene que ir para leer.
#
# 31-10-2026 — corrección sobre el Lote 7: 1 s para conectar NO alcanza
# cuando la Lambda arranca de cero (no hay túnel abierto: conexión nueva
# AWS Ohio -> entrada del Funnel -> servidor en Chile). Ahora el tiempo se
# calcula contra un plazo total (ver _plazo_pedido) y la conexión se
# reutiliza entre pedidos (_http).
ORBYTE_CONNECT_TIMEOUT = 3.0        # máximo para abrir la conexión
ALEXA_PRESUPUESTO_S = 8.0           # Alexa corta a los 8 s desde que manda el pedido
MARGEN_RESPUESTA_S = 0.9            # para guardar el estado (DynamoDB) y responder
ARRANQUE_EN_FRIO_S = 1.5            # si no se puede medir, lo que suele costar arrancar en frío

# Conexión HTTP reutilizable: mientras la Lambda siga encendida, los pedidos
# siguientes (y Siguiente/Anterior/portadas) no vuelven a abrir conexión.
_http = requests.Session()
_http.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0))
_http.mount("http://", HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0))
_hilos = ThreadPoolExecutor(max_workers=4)
_arranque_en_frio = [True]

# Tope defensivo de cuantas pistas se encolan de una: el token de cada
# AudioItem tiene un limite de tamaño en Alexa. 40 ids alcanza de sobra para
# el tamaño tipico de playlist de Orbitron y deja margen amplio.
MAX_QUEUE_ITEMS = 40

SPEAK_SLOW_ERROR = (
    "Orbitron está tardando más de lo normal en armar la lista. "
    "Probá de nuevo en un momento."
)
SPEAK_CONNECT_ERROR = (
    "No pude conectarme con Orbyte. Fijate que el servidor esté prendido "
    "y probá de nuevo."
)
SPEAK_AUTH_ERROR = (
    "Orbyte no aceptó la clave de Alexa. En el servidor revisá con clave alexa estado."
)
SPEAK_NO_RESULTS = (
    "No encontré nada para esa búsqueda en tu biblioteca. Probá pedirlo de otra forma."
)
SPEAK_GENERIC_ERROR = (
    "Tuve un problema para conectarme con Orbyte. Fijate que el servidor esté prendido "
    "y probá de nuevo."
)
SPEAK_WELCOME = "¿Qué querés escuchar?"
SPEAK_NO_MORE_TRACKS = "No hay más pistas en la cola. Pedime otra playlist cuando quieras."
SPEAK_NO_PREVIOUS = "No tengo una pista anterior a la que volver."


# ── Helpers ──────────────────────────────────────────────────────────────

def stream_url_for(track_id):
    return f"{ORBYTE_BASE_URL}/api/v1/alexa/stream/{track_id}?token={ORBYTE_TOKEN}"


def metadata_for(track):
    """Ticket 42, Lote 7 — título/artista + PORTADA REAL de la pista (Orbyte
    ya la sirve en /cover/<ruta>, pública, sin token — igual que /static).
    El fondo panorámico sigue siendo el logo de Orbyte (mantiene la marca
    consistente detrás de cualquier portada). Si la pista no tiene
    cover_url, cae al logo también para el ícono. Solo se arma para la
    PRIMERA pista de cada pedido, que es la única para la que tenemos el
    objeto completo a mano sin llamadas extra a Orbyte — Next/Previous/
    encolado automático solo tienen el id numérico en el token, así que
    por ahora siguen sin metadata (ver nota en build_play_response).
    Nunca falla: campos ausentes simplemente no se muestran."""
    title = track.get("title") or "Orbitron"
    artist = track.get("artist_name") or ""
    cover_path = track.get("cover_url")
    art_url = f"{ORBYTE_BASE_URL}{cover_path}" if cover_path else LOGO_ART_URL
    return AudioItemMetadata(
        title=title,
        subtitle=artist,
        art=Image(sources=[ImageInstance(url=art_url)]),
        background_image=Image(sources=[ImageInstance(url=LOGO_BACKGROUND_URL)]),
    )


def new_session_nonce():
    """Ticket 42, Lote 13 — identificador corto y aleatorio por CADA pedido
    nuevo (PlayMusicIntent). Ver make_token/parse_token para el motivo."""
    return uuid.uuid4().hex[:8]


def make_token(track_id, remaining_ids, history_ids, session_nonce):
    """El 'token' de cada AudioItem viaja y vuelve tal cual en los eventos de
    AudioPlayer (PlaybackNearlyFinished, etc.) - lo usamos para llevar el
    estado de la cola sin necesidad de una base de datos (DynamoDB) para un
    solo usuario. remaining_ids: lo que falta reproducir DESPUES de esta
    pista. history_ids: lo ya reproducido ANTES de esta, para soportar
    AMAZON.PreviousIntent.

    `session_nonce` (Ticket 42, Lote 13) — bug real confirmado con logs:
    Alexa a veces manda un PlaybackStopped/Finished TARDÍO de una
    transmisión vieja ya reemplazada (de un pedido anterior), y llega
    DESPUÉS de que el pedido nuevo ya viene sonando — sin nada que lo
    distinga, ese aviso viejo pisaba el estado bueno con datos de otra
    canción/artista por completo. Cada pedido nuevo (PlayMusicIntentHandler)
    genera un nonce propio; PlaybackStopped/PlaybackStartedOrFinishedHandler
    solo aceptan actualizar el estado si el nonce del evento coincide con
    el de la sesión activa — un aviso de una sesión vieja se ignora en vez
    de corromper el estado."""
    payload = {
        "t": track_id,
        "r": remaining_ids[:MAX_QUEUE_ITEMS],
        "h": history_ids[-MAX_QUEUE_ITEMS:],
        "s": session_nonce,
    }
    return json.dumps(payload, separators=(",", ":"))


def parse_token(token):
    try:
        data = json.loads(token)
        return data.get("t"), data.get("r", []), data.get("h", []), data.get("s")
    except (TypeError, ValueError, AttributeError):
        return None, [], [], None


def fetch_track_metadata(track_id):
    """Ticket 42, Lote 10: consulta liviana y sin Gemini de por medio —
    permite mostrar título/artista/álbum/portada REALES en Next/Previous/
    Resume/el encolado automático, que hasta ahora solo tenían el id
    numérico. Timeout corto y tolerante a fallos: si no responde a
    tiempo, esa pista puntual simplemente se queda sin metadata (usa el
    logo como respaldo, ver metadata_for) — nunca bloquea ni rompe la
    respuesta al usuario."""
    if not ORBYTE_BASE_URL or not ORBYTE_TOKEN:
        return {}
    try:
        resp = _http.get(
            f"{ORBYTE_BASE_URL}/api/v1/alexa/track/{track_id}",
            headers={"Authorization": f"Bearer {ORBYTE_TOKEN}"},
            timeout=(2.0, 2.0),
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.RequestException as e:
        logger.warning("No se pudo obtener metadata de track=%s: %s", track_id, e)
        return {}


class OrbyteError(Exception):
    """motivo: 'conexion' | 'lento' | 'clave' | 'servidor'."""

    def __init__(self, motivo, detalle=""):
        super().__init__(f"{motivo}: {detalle}")
        self.motivo = motivo


def _plazo_pedido(handler_input):
    """Momento (time.monotonic) en que hay que dejar de esperar a Orbyte para
    alcanzar a responder antes de que Alexa corte. Se mide desde la hora en
    que Alexa creó el pedido (incluye el arranque en frío de la Lambda); si
    esa hora no sirve, se estima."""
    ahora = time.monotonic()
    transcurrido = None
    try:
        ts = handler_input.request_envelope.request.timestamp
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            t = (datetime.now(timezone.utc) - ts).total_seconds()
            if 0 <= t < 5:
                transcurrido = t
    except Exception:  # noqa: BLE001
        pass
    if transcurrido is None:
        transcurrido = ARRANQUE_EN_FRIO_S if _arranque_en_frio[0] else 0.3
    restante = ALEXA_PRESUPUESTO_S - MARGEN_RESPUESTA_S - transcurrido
    try:  # nunca más allá de lo que le queda a la propia Lambda
        restante = min(restante, handler_input.context.get_remaining_time_in_millis() / 1000.0 - 0.5)
    except Exception:  # noqa: BLE001
        pass
    return ahora + max(restante, 1.5)


def _post_ai_playlist(raw_query, connect_s, read_s):
    resp = _http.post(
        f"{ORBYTE_BASE_URL}/api/v1/ai/playlist",
        headers={"Authorization": f"Bearer {ORBYTE_TOKEN}"},
        json={"query": raw_query, "provider_order": ["groq", "gemini"]},
        timeout=(connect_s, read_s),
    )
    if resp.status_code in (401, 403):
        raise OrbyteError("clave", f"HTTP {resp.status_code}")
    if resp.status_code >= 500:
        raise OrbyteError("servidor", f"HTTP {resp.status_code}")
    resp.raise_for_status()
    return resp.json().get("tracks", [])


def call_orbyte_playlist(raw_query, plazo):
    """POST a /api/v1/ai/playlist. Devuelve la lista de tracks o lanza
    OrbyteError con el motivo. `plazo`: time.monotonic() límite (ver
    _plazo_pedido); pase lo que pase, a esa hora se deja de esperar."""
    if not ORBYTE_BASE_URL or not ORBYTE_TOKEN:
        logger.error("ORBYTE_BASE_URL / ORBYTE_TOKEN no configurados")
        raise OrbyteError("clave", "sin configurar")
    inicio = time.monotonic()
    intento = 0
    while True:
        intento += 1
        restante = plazo - time.monotonic()
        if restante <= 0.3:
            raise OrbyteError("lento", "sin tiempo")
        connect_s = min(ORBYTE_CONNECT_TIMEOUT, restante)
        # La llamada corre en un hilo para cortar EXACTO en el plazo (el timeout
        # de requests es por fase, no total).
        fut = _hilos.submit(_post_ai_playlist, raw_query, connect_s, restante)
        try:
            tracks = fut.result(timeout=restante)
            logger.info("ai/playlist OK en %d ms (intento %d, %d pistas, query=%r)",
                        (time.monotonic() - inicio) * 1000, intento, len(tracks), raw_query)
            return tracks
        except FutureTimeout:
            logger.warning("ai/playlist: plazo agotado tras %d ms (intento %d, query=%r)",
                           (time.monotonic() - inicio) * 1000, intento, raw_query)
            raise OrbyteError("lento", "plazo agotado")
        except OrbyteError:
            raise
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
            # No se pudo conectar (o la conexión reutilizada estaba cerrada):
            # un reintento si queda tiempo razonable.
            quedan = plazo - time.monotonic()
            logger.warning("ai/playlist: falla de conexión en el intento %d tras %d ms (%s); quedan %.1f s",
                           intento, (time.monotonic() - inicio) * 1000, type(e).__name__, quedan)
            if intento >= 2 or quedan < 2.5:
                raise OrbyteError("conexion", str(e))
        except requests.exceptions.ReadTimeout:
            logger.warning("ai/playlist: Orbitron no respondió a tiempo (%d ms, query=%r)",
                           (time.monotonic() - inicio) * 1000, raw_query)
            raise OrbyteError("lento", "read timeout")
        except requests.exceptions.RequestException as e:
            logger.error("Error llamando a /api/v1/ai/playlist: %s", e)
            raise OrbyteError("servidor", str(e))


def _calentar_conexion():
    """Abre la conexión con Orbyte por adelantado (al abrir la skill), así el
    pedido que viene después ya la encuentra lista."""
    if not ORBYTE_BASE_URL or not ORBYTE_TOKEN:
        return
    try:
        _http.get(f"{ORBYTE_BASE_URL}/api/v1/alexa/track/0",
                  headers={"Authorization": f"Bearer {ORBYTE_TOKEN}"}, timeout=(3.0, 2.0))
    except requests.exceptions.RequestException:
        pass


def _enviar_buscando(handler_input, texto):
    """Respuesta progresiva: Alexa dice `texto` de inmediato mientras Orbitron
    arma la lista. Devuelve True si Alexa la aceptó."""
    try:
        sistema = handler_input.request_envelope.context.system
        url = f"{sistema.api_endpoint.rstrip('/')}/v1/directives"
        cuerpo = {
            "header": {"requestId": handler_input.request_envelope.request.request_id},
            "directive": {"type": "VoicePlayer.Speak", "speech": f"<speak>{xml_escape(texto)}</speak>"},
        }
        r = requests.post(url, json=cuerpo, headers={"Authorization": f"Bearer {sistema.api_access_token}"},
                          timeout=(1.0, 1.5))
        if r.status_code in (200, 204):
            return True
        logger.warning("Respuesta progresiva rechazada: HTTP %s %s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001 - nunca debe romper el pedido
        logger.warning("No se pudo enviar la respuesta progresiva: %s", e)
    return False


def build_play_response(handler_input, tracks, speak_text):
    """Alexa rechaza más de una directiva AudioPlayer.Play por respuesta
    ("Too many AudioPlayer.Play directives" — error real, detectado en
    pruebas contra un Echo Show físico; el simulador web no lo marca).
    Por eso acá se manda SOLO la primera pista (REPLACE_ALL) — el resto de
    la cola ya viaja codificada en su token, y PlaybackNearlyFinishedHandler
    la va encolando de a una a medida que hace falta.

    Ticket 42, Lote 13 — arranca una sesión nueva (nonce nuevo) por cada
    pedido: reemplaza por completo el estado persistido en vez de dejar
    que las notificaciones tardías de la sesión anterior sigan flotando.
    Ver make_token para el porqué del nonce."""
    response_builder = handler_input.response_builder
    ids = [t["id"] for t in tracks]

    session_nonce = new_session_nonce()
    attrs = handler_input.attributes_manager.persistent_attributes
    attrs.clear()
    attrs["session_nonce"] = session_nonce
    handler_input.attributes_manager.persistent_attributes = attrs
    handler_input.attributes_manager.save_persistent_attributes()

    first_id = ids[0]
    remaining = ids[1:]
    first_token = make_token(first_id, remaining, [], session_nonce)
    logger.info("build_play_response: query=%r primer_id=%s nonce=%s token=%s",
                speak_text, first_id, session_nonce, first_token)

    if speak_text:
        response_builder.speak(speak_text)
    response_builder.add_directive(
        PlayDirective(
            play_behavior=PlayBehavior.REPLACE_ALL,
            audio_item=AudioItem(
                stream=Stream(
                    token=first_token,
                    url=stream_url_for(first_id),
                    offset_in_milliseconds=0,
                ),
                metadata=metadata_for(tracks[0]),
            ),
        )
    )
    response_builder.set_should_end_session(True)
    return response_builder.response


# ── Lógica compartida de control de reproducción ────────────────────────
# Ticket 42, Lote 5: extraída a funciones de módulo para que tanto los
# intents de voz (AMAZON.PauseIntent, etc. — "Alexa, pausa") como los
# eventos de PlaybackController (botón físico o de la pantalla "Now
# Playing" de la app — "PlaybackController.PauseCommandIssued") disparen
# exactamente el mismo comportamiento. Antes solo existían los de voz; los
# de botón no tenían ningún handler, por eso los controles táctiles no
# hacían nada.

def _do_pause(handler_input):
    return handler_input.response_builder.add_directive(StopDirective()).response


def _do_resume(handler_input):
    attrs = handler_input.attributes_manager.persistent_attributes
    token = attrs.get("stopped_token")
    offset = attrs.get("stopped_offset_ms", 0)
    if not token:
        return handler_input.response_builder.speak(SPEAK_NO_MORE_TRACKS).response
    track_id, remaining, history, _nonce = parse_token(token)
    response_builder = handler_input.response_builder
    response_builder.add_directive(
        PlayDirective(
            play_behavior=PlayBehavior.REPLACE_ALL,
            audio_item=AudioItem(
                stream=Stream(
                    token=token,
                    url=stream_url_for(track_id),
                    offset_in_milliseconds=offset,
                ),
                metadata=metadata_for(fetch_track_metadata(track_id)),
            ),
        )
    )
    return response_builder.response


def _do_next(handler_input):
    attrs = handler_input.attributes_manager.persistent_attributes
    token = attrs.get("stopped_token") or attrs.get("last_token")
    logger.info("_do_next: leyendo stopped_token=%s last_token=%s -> usa %s",
                attrs.get("stopped_token"), attrs.get("last_token"), token)
    if not token:
        return handler_input.response_builder.speak(SPEAK_NO_MORE_TRACKS).response
    current_id, remaining, history, nonce = parse_token(token)
    if not remaining:
        return handler_input.response_builder.speak(SPEAK_NO_MORE_TRACKS).response
    next_id = remaining[0]
    rest = remaining[1:]
    new_history = history + [current_id]
    new_token = make_token(next_id, rest, new_history, nonce)
    response_builder = handler_input.response_builder
    response_builder.add_directive(
        PlayDirective(
            play_behavior=PlayBehavior.REPLACE_ALL,
            audio_item=AudioItem(
                stream=Stream(
                    token=new_token,
                    url=stream_url_for(next_id),
                    offset_in_milliseconds=0,
                ),
                metadata=metadata_for(fetch_track_metadata(next_id)),
            ),
        )
    )
    return response_builder.response


def _do_previous(handler_input):
    attrs = handler_input.attributes_manager.persistent_attributes
    token = attrs.get("stopped_token") or attrs.get("last_token")
    if not token:
        return handler_input.response_builder.speak(SPEAK_NO_PREVIOUS).response
    current_id, remaining, history, nonce = parse_token(token)
    if not history:
        return handler_input.response_builder.speak(SPEAK_NO_PREVIOUS).response
    prev_id = history[-1]
    new_history = history[:-1]
    new_remaining = [current_id] + remaining
    new_token = make_token(prev_id, new_remaining, new_history, nonce)
    response_builder = handler_input.response_builder
    response_builder.add_directive(
        PlayDirective(
            play_behavior=PlayBehavior.REPLACE_ALL,
            audio_item=AudioItem(
                stream=Stream(
                    token=new_token,
                    url=stream_url_for(prev_id),
                    offset_in_milliseconds=0,
                ),
                metadata=metadata_for(fetch_track_metadata(prev_id)),
            ),
        )
    )
    return response_builder.response


# ── Handlers ─────────────────────────────────────────────────────────────

class LaunchRequestHandler(AbstractRequestHandler):
    """'Alexa, abre Orbitron' sin pedido — invita a decir qué escuchar.
    Ticket 42, Lote 6: agrega el logo real (fondo negro incluido) en
    pantallas que soportan APL, en vez de la tarjeta genérica de Alexa."""

    def can_handle(self, handler_input):
        return is_request_type("LaunchRequest")(handler_input)

    def handle(self, handler_input):
        # Mientras el usuario dice qué quiere, se abre la conexión con Orbyte.
        _hilos.submit(_calentar_conexion)
        response_builder = handler_input.response_builder
        response_builder.speak(SPEAK_WELCOME).ask(SPEAK_WELCOME)
        if device_supports_apl(handler_input):
            response_builder.add_directive(RenderDocumentDirective(document=LOGO_APL_DOCUMENT))
        return response_builder.response


class PlayMusicIntentHandler(AbstractRequestHandler):
    """Intent principal - captura el pedido en lenguaje natural (uno-shot o
    tras el saludo de LaunchRequest) y lo reenvía a Orbitron tal cual."""

    def can_handle(self, handler_input):
        return is_intent_name("PlayMusicIntent")(handler_input)

    def handle(self, handler_input):
        slots = handler_input.request_envelope.request.intent.slots
        raw_query = ""
        if slots and "SearchQuery" in slots and slots["SearchQuery"].value:
            raw_query = slots["SearchQuery"].value.strip()

        if not raw_query:
            return (
                handler_input.response_builder
                .speak("No te escuché bien. ¿Qué querés escuchar?")
                .ask("¿Qué querés escuchar?")
                .response
            )

        plazo = _plazo_pedido(handler_input)
        # «Buscando…» en paralelo con el pedido a Orbitron (no le resta tiempo).
        buscando = _hilos.submit(_enviar_buscando, handler_input, f"Buscando {raw_query}")
        try:
            tracks = call_orbyte_playlist(raw_query, plazo)
        except OrbyteError as e:
            hablar = {"conexion": SPEAK_CONNECT_ERROR, "clave": SPEAK_AUTH_ERROR,
                      "lento": SPEAK_SLOW_ERROR}.get(e.motivo, SPEAK_GENERIC_ERROR)
            return handler_input.response_builder.speak(hablar).response

        if not tracks:
            return handler_input.response_builder.speak(SPEAK_NO_RESULTS).response

        try:
            dijo_buscando = buscando.result(timeout=0.3)
        except FutureTimeout:
            dijo_buscando = False
        # Si ya dijo «Buscando …», arranca la música sin repetir el pedido.
        return build_play_response(handler_input, tracks, None if dijo_buscando else f"Reproduciendo {raw_query}")


class PauseIntentHandler(AbstractRequestHandler):
    """'Alexa, pausa' — intent de VOZ."""

    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.PauseIntent")(handler_input)

    def handle(self, handler_input):
        return _do_pause(handler_input)


class PlaybackControllerPauseHandler(AbstractRequestHandler):
    """Mismo efecto que arriba, pero disparado por un botón físico o el
    control táctil de la app/pantalla — request distinto, sin voz de por
    medio."""

    def can_handle(self, handler_input):
        return is_request_type("PlaybackController.PauseCommandIssued")(handler_input)

    def handle(self, handler_input):
        return _do_pause(handler_input)


class ResumeIntentHandler(AbstractRequestHandler):
    """Retoma desde el offset guardado en la última pausa — ahora en
    persistencia real (DynamoDB), no en session_attributes (ver nota
    arriba de por qué session_attributes no servía para esto)."""

    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.ResumeIntent")(handler_input)

    def handle(self, handler_input):
        return _do_resume(handler_input)


class PlaybackControllerPlayHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("PlaybackController.PlayCommandIssued")(handler_input)

    def handle(self, handler_input):
        return _do_resume(handler_input)


class NextIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NextIntent")(handler_input)

    def handle(self, handler_input):
        return _do_next(handler_input)


class PlaybackControllerNextHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("PlaybackController.NextCommandIssued")(handler_input)

    def handle(self, handler_input):
        return _do_next(handler_input)


class PreviousIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.PreviousIntent")(handler_input)

    def handle(self, handler_input):
        return _do_previous(handler_input)


class PlaybackControllerPreviousHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("PlaybackController.PreviousCommandIssued")(handler_input)

    def handle(self, handler_input):
        return _do_previous(handler_input)


class PlaybackNearlyFinishedHandler(AbstractRequestHandler):
    """Evento de AudioPlayer (no es un IntentRequest, y no tiene sesión —
    ver nota arriba) - acá es donde se encola la siguiente pista si la
    cola inicial no alcanzó a cubrir todo (más de MAX_QUEUE_ITEMS
    pistas). No usa session ni persistent attributes: el token que viaja
    en el propio request ya trae todo lo necesario."""

    def can_handle(self, handler_input):
        return is_request_type("AudioPlayer.PlaybackNearlyFinished")(handler_input)

    def handle(self, handler_input):
        current_token = handler_input.request_envelope.request.token
        current_id, remaining, history, nonce = parse_token(current_token)
        response_builder = handler_input.response_builder
        if not remaining:
            return response_builder.response  # nada más para encolar, respuesta vacía
        next_id = remaining[0]
        rest = remaining[1:]
        new_history = history + [current_id]
        next_token = make_token(next_id, rest, new_history, nonce)
        response_builder.add_directive(
            PlayDirective(
                play_behavior=PlayBehavior.ENQUEUE,
                audio_item=AudioItem(
                    stream=Stream(
                        token=next_token,
                        url=stream_url_for(next_id),
                        offset_in_milliseconds=0,
                        expected_previous_token=current_token,
                    ),
                    metadata=metadata_for(fetch_track_metadata(next_id)),
                ),
            )
        )
        return response_builder.response


class PlaybackStoppedHandler(AbstractRequestHandler):
    """Guarda token + offset para que ResumeIntent pueda retomar. Ticket
    42, Lote 5: persistent_attributes (DynamoDB) en vez de
    session_attributes — este request NO tiene sesión, escribir en
    session_attributes fallaba en silencio (o directamente tiraba error)
    en un dispositivo real.

    Ticket 42, Lote 13 — bug real confirmado con logs: Alexa a veces manda
    este evento TARDE, para una transmisión vieja ya reemplazada por un
    pedido nuevo. Sin verificar nada, esto pisaba el estado bueno con el
    de una sesión completamente distinta (otro artista, de un pedido
    anterior). Ahora se ignora cualquier evento cuyo nonce de sesión no
    coincida con el de la sesión activa."""

    def can_handle(self, handler_input):
        return is_request_type("AudioPlayer.PlaybackStopped")(handler_input)

    def handle(self, handler_input):
        req = handler_input.request_envelope.request
        attrs = handler_input.attributes_manager.persistent_attributes
        _, _, _, event_nonce = parse_token(req.token)
        active_nonce = attrs.get("session_nonce")
        if event_nonce != active_nonce:
            logger.warning(
                "PlaybackStopped IGNORADO: nonce=%s no coincide con sesión activa=%s (token=%s)",
                event_nonce, active_nonce, req.token,
            )
            return handler_input.response_builder.response
        logger.info("PlaybackStopped: token_nuevo=%s offset=%s (stopped_token_previo=%s)",
                    req.token, req.offset_in_milliseconds, attrs.get("stopped_token"))
        attrs["stopped_token"] = req.token
        attrs["stopped_offset_ms"] = req.offset_in_milliseconds
        handler_input.attributes_manager.persistent_attributes = attrs
        handler_input.attributes_manager.save_persistent_attributes()
        return handler_input.response_builder.response


class PlaybackStartedOrFinishedHandler(AbstractRequestHandler):
    """PlaybackStarted/PlaybackFinished no requieren acción - solo hay que
    responder (vacío) para que Alexa no marque error. Guardamos el último
    token conocido (persistente, mismo motivo que arriba) por si
    Next/Previous se piden sin haber pasado por PlaybackStopped.

    Ticket 42, Lote 13 — mismo guard de nonce que PlaybackStoppedHandler:
    un evento de una sesión vieja no debe pisar el estado de la sesión
    activa."""

    def can_handle(self, handler_input):
        return is_request_type("AudioPlayer.PlaybackStarted")(handler_input) or \
            is_request_type("AudioPlayer.PlaybackFinished")(handler_input)

    def handle(self, handler_input):
        req = handler_input.request_envelope.request
        attrs = handler_input.attributes_manager.persistent_attributes
        _, _, _, event_nonce = parse_token(req.token)
        active_nonce = attrs.get("session_nonce")
        if event_nonce != active_nonce:
            logger.warning(
                "%s IGNORADO: nonce=%s no coincide con sesión activa=%s (token=%s)",
                type(req).__name__, event_nonce, active_nonce, req.token,
            )
            return handler_input.response_builder.response
        logger.info(
            "PlaybackStarted/Finished: type=%s token_nuevo=%s (token_previo=%s)",
            type(req).__name__, req.token, attrs.get("last_token"),
        )
        attrs["last_token"] = req.token
        handler_input.attributes_manager.persistent_attributes = attrs
        handler_input.attributes_manager.save_persistent_attributes()
        return handler_input.response_builder.response


class PlaybackFailedHandler(AbstractRequestHandler):
    """Ticket 42, Lote 11 — antes esto solo logueaba el error y no hacía
    nada más: si una pista fallaba (por ejemplo, un problema puntual de
    transcodeo), la skill se quedaba en silencio ahí — dando pie a que el
    dispositivo retome otra cosa (ej. Amazon Music) por su cuenta. Ahora
    salta directamente a la siguiente pista de la cola en vez de dejar el
    audio cortado."""

    def can_handle(self, handler_input):
        return is_request_type("AudioPlayer.PlaybackFailed")(handler_input)

    def handle(self, handler_input):
        req = handler_input.request_envelope.request
        logger.error("AudioPlayer.PlaybackFailed: %s", getattr(req, "error", None))
        response_builder = handler_input.response_builder
        failed_token = getattr(req, "token", None)
        if not failed_token:
            return response_builder.response
        current_id, remaining, history, nonce = parse_token(failed_token)
        if not remaining:
            return response_builder.response  # nada más para saltar
        next_id = remaining[0]
        rest = remaining[1:]
        new_history = history + [current_id]
        new_token = make_token(next_id, rest, new_history, nonce)
        response_builder.add_directive(
            PlayDirective(
                play_behavior=PlayBehavior.REPLACE_ALL,
                audio_item=AudioItem(
                    stream=Stream(
                        token=new_token,
                        url=stream_url_for(next_id),
                        offset_in_milliseconds=0,
                    ),
                    metadata=metadata_for(fetch_track_metadata(next_id)),
                ),
            )
        )
        return response_builder.response


class HelpIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.HelpIntent")(handler_input)

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .speak(SPEAK_WELCOME)
            .ask(SPEAK_WELCOME)
            .response
        )


class FallbackIntentHandler(AbstractRequestHandler):
    """Ticket 42, Lote 8 — sin esto, cualquier frase que Alexa no logre
    matchear contra ningún intent caía en el CatchAllExceptionHandler
    ("Tuve un problema para conectarme con Orbyte..."), un mensaje
    confuso para un simple "no te entendí"."""

    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.FallbackIntent")(handler_input)

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .speak(f"No entendí eso. {SPEAK_WELCOME}")
            .ask(SPEAK_WELCOME)
            .response
        )


class NavigateHomeIntentHandler(AbstractRequestHandler):
    """Ticket 42, Lote 8 — requerido por la interfaz AudioPlayer; sin
    handler propio caía en el mismo mensaje de error genérico."""

    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.NavigateHomeIntent")(handler_input)

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .speak(SPEAK_WELCOME)
            .ask(SPEAK_WELCOME)
            .response
        )


class CancelOrStopIntentHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_intent_name("AMAZON.CancelIntent")(handler_input) or \
            is_intent_name("AMAZON.StopIntent")(handler_input)

    def handle(self, handler_input):
        return (
            handler_input.response_builder
            .add_directive(StopDirective())
            .set_should_end_session(True)
            .response
        )


class SessionEndedRequestHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        return is_request_type("SessionEndedRequest")(handler_input)

    def handle(self, handler_input):
        return handler_input.response_builder.response


class CatchAllExceptionHandler(AbstractExceptionHandler):
    def can_handle(self, handler_input, exception):
        return True

    def handle(self, handler_input, exception):
        logger.error("Excepción no manejada: %s", exception, exc_info=True)
        return (
            handler_input.response_builder
            .speak(SPEAK_GENERIC_ERROR)
            .response
        )


sb = CustomSkillBuilder(persistence_adapter=dynamodb_adapter)
sb.add_request_handler(LaunchRequestHandler())
sb.add_request_handler(PlayMusicIntentHandler())
sb.add_request_handler(PauseIntentHandler())
sb.add_request_handler(PlaybackControllerPauseHandler())
sb.add_request_handler(ResumeIntentHandler())
sb.add_request_handler(PlaybackControllerPlayHandler())
sb.add_request_handler(NextIntentHandler())
sb.add_request_handler(PlaybackControllerNextHandler())
sb.add_request_handler(PreviousIntentHandler())
sb.add_request_handler(PlaybackControllerPreviousHandler())
sb.add_request_handler(PlaybackNearlyFinishedHandler())
sb.add_request_handler(PlaybackStoppedHandler())
sb.add_request_handler(PlaybackStartedOrFinishedHandler())
sb.add_request_handler(PlaybackFailedHandler())
sb.add_request_handler(HelpIntentHandler())
sb.add_request_handler(FallbackIntentHandler())
sb.add_request_handler(NavigateHomeIntentHandler())
sb.add_request_handler(CancelOrStopIntentHandler())
sb.add_request_handler(SessionEndedRequestHandler())
sb.add_exception_handler(CatchAllExceptionHandler())

_lambda_handler = sb.lambda_handler()


def handler(event, context):
    try:
        return _lambda_handler(event, context)
    finally:
        _arranque_en_frio[0] = False
