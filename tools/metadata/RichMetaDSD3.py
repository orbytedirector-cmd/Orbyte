#!/usr/bin/env python3
# ============================================================
# RICHMETAPRO v7.2 — Unificación Completa + Batch + Debug
# ============================================================

import os, re, sys, time, json, math, threading
import subprocess, unicodedata, logging, argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm
from rapidfuzz import fuzz
from dotenv import load_dotenv
from mutagen import File
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import (
    ID3, COMM, TXXX, USLT, SYLT, Encoding,
    TALB, TDRC, TRCK, TPOS, TPUB, TBPM, TCON, TSRC)
try:
    from mutagen.dsf import DSF as DSFFile
    HAS_DSF = True
except ImportError:
    DSFFile = None
    HAS_DSF = False

try:
    from langdetect import detect as _langdetect
    from langdetect import detect_langs as _langdetect_langs
    from langdetect.detector_factory import DetectorFactory as _LangDetectorFactory
    # langdetect's classifier draws from Python's global random state internally.
    # Without a fixed seed, the SAME lyrics text can be classified differently
    # on different runs/machines — this was silently reclassifying tracks on
    # re-ingests. Seeding makes results reproducible (does not by itself fix
    # low-confidence misclassifications — see analizar_letras() below).
    _LangDetectorFactory.seed = 0
    LANGDETECT_OK = True
except ImportError as _langdetect_import_error:
    LANGDETECT_OK = False
    # This used to fail completely silently, falling back to a 5-keyword
    # dictionary ("amor","corazón","alma","fuego","muerte" for Spanish, etc.)
    # that only matches Spanish text if it happens to contain one of those
    # exact words — everything else defaults to "en" regardless of its real
    # language. That silent fallback is almost certainly why so many
    # genuinely Spanish/French/German tracks were coming out tagged "en":
    # not a confidence problem, but langdetect never running at all in this
    # environment. Printed immediately (before logging is configured) so it
    # can't be missed in the console output.
    print(f"\n{'!'*70}\n"
          f"  ADVERTENCIA: no se pudo importar 'langdetect' ({_langdetect_import_error}).\n"
          f"  El idioma de TODAS las pistas de esta corrida se va a determinar\n"
          f"  con un diccionario de 5 palabras clave (amor/corazón/alma/fuego/\n"
          f"  muerte para español, etc.) en vez de análisis real de la letra.\n"
          f"  Esto va a mistaggear la gran mayoría de las pistas no-inglesas.\n"
          f"  Solución: pip install langdetect (en el mismo entorno/venv que\n"
          f"  corre este script — verificá con 'which python3' / 'pip show langdetect').\n"
          f"{'!'*70}\n", file=sys.stderr)

try:
    import acoustid
    import chromaprint
    ACOUSTID_LIB_OK = True
except ImportError:
    ACOUSTID_LIB_OK = False


# Claves (Last.fm, AcoustID) desde el entorno o el .env de la raíz del repo de Orbyte — nunca en el código.
def _cargar_env():
    import os as _os
    ruta = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), ".env")
    try:
        with open(ruta, encoding="utf-8") as _f:
            for _l in _f:
                _l = _l.strip()
                if _l and not _l.startswith("#") and "=" in _l:
                    _k, _v = _l.split("=", 1)
                    _os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except OSError:
        pass
_cargar_env()

# ============================================================
# CONFIGURACIÓN GLOBAL
# ============================================================

MUSIC_ROOT        = Path("/mnt/musica")
SUPPORTED         = (".mp3", ".flac", ".dsf", ".dff")
LRCLIB_BASE       = "https://lrclib.net/api"
MB_BASE           = "https://musicbrainz.org/ws/2"
AB_BASE           = "https://acousticbrainz.org"
ACOUSTID_BASE     = "https://api.acoustid.org/v2/lookup"
LASTFM_BASE       = "http://ws.audioscrobbler.com/2.0/"

HEADERS_MB        = {"User-Agent": "RichMetaPro/7.2 (opensource@local)"}
HEADERS_LRC       = {"Lrclib-Client": "RichMetaPro/7.2"}

MB_RATE_LIMIT     = 1.1
LRC_RATE_LIMIT    = 1.5     # Evita el HTTP 429 en LRCLib
CONFIDENCE_MIN    = 0.60
LRC_DURATION_TOL  = 5
BALLAD_BPM        = 80
MAX_WORKERS       = 1       # álbumes en paralelo
TRACK_WORKERS     = 3       # pistas en paralelo dentro de un álbum (los límites por API son globales)
LASTFM_RATE_LIMIT = 0.25    # Last.fm permite 5 req/s → máx. 4
ACOUSTID_RATE_LIMIT = 0.5   # AcoustID permite 3 req/s → máx. 2

VERSION_MARKERS   = {
    "remaster", "remastered", "live", "acoustic", "demo",
    "instrumental", "remix", "cover", "edition", "version",
    "mono", "stereo", "radio edit", "extended", "bonus"
}

# Modos de re-procesamiento selectivo
MODE_FULL    = "full"       # todas las pistas (comportamiento original)
MODE_PENDING = "pending"    # solo pistas sin registro en audit
MODE_LYRICS  = "no_lyrics"  # solo pistas con audit pero sin synced lyrics
MODE_RESUME  = "resume"     # union: pending + sin synced lyrics (recomendado)

LASTFM_API_KEY   = os.environ.get("LASTFM_API_KEY", "")
ACOUSTID_API_KEY = os.environ.get("ACOUSTID_API_KEY", "")

# ============================================================
# ESTADO COMPARTIDO THREAD-SAFE Y RATE LIMITS
# ============================================================

_cache_genres  = {}
_cache_ab      = {}
_cache_lock    = threading.RLock()

_mb_last_call  = 0.0
_mb_lock       = threading.Lock()

_lrc_last_call = 0.0
_lrc_lock      = threading.Lock()

_lfm_last_call = 0.0
_lfm_lock      = threading.Lock()   # además serializa la consulta → un artista se pide 1 sola vez
_ac_last_call  = 0.0
_ac_lock       = threading.Lock()

# Sello/catálogo por release de MusicBrainz: es el mismo para todas las pistas
# del álbum → 1 consulta por release en vez de 1 por pista.
_cache_release_labels = {}

def _lfm_wait():
    global _lfm_last_call
    elapsed = time.time() - _lfm_last_call
    if elapsed < LASTFM_RATE_LIMIT:
        time.sleep(LASTFM_RATE_LIMIT - elapsed)
    _lfm_last_call = time.time()

def _ac_wait():
    global _ac_last_call
    with _ac_lock:
        elapsed = time.time() - _ac_last_call
        if elapsed < ACOUSTID_RATE_LIMIT:
            time.sleep(ACOUSTID_RATE_LIMIT - elapsed)
        _ac_last_call = time.time()

def _mb_wait():
    global _mb_last_call
    with _mb_lock:
        elapsed = time.time() - _mb_last_call
        if elapsed < MB_RATE_LIMIT:
            time.sleep(MB_RATE_LIMIT - elapsed)
        _mb_last_call = time.time()

def _lrc_wait():
    global _lrc_last_call
    with _lrc_lock:
        elapsed = time.time() - _lrc_last_call
        if elapsed < LRC_RATE_LIMIT:
            time.sleep(LRC_RATE_LIMIT - elapsed)
        _lrc_last_call = time.time()

# ============================================================
# LOGGING DUAL
# ============================================================

log = logging.getLogger("richmetapro")
log.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"))
log.addHandler(console_handler)

file_handler = logging.FileHandler("richmetapro_debug.log", mode="a", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] %(levelname)-7s %(message)s"))
file_handler.setLevel(logging.DEBUG)
log.addHandler(file_handler)

# ============================================================
# UTILIDADES Y LECTURA
# ============================================================

def _norm(text: str) -> str:
    if not text: return ""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def _clean_title(t: str) -> str:
    t = re.sub(r"\(.*?\)", "", t or "")
    t = re.sub(r"\[.*?\]", "", t)
    return t.strip()

def _clean_artist(a: str) -> str:
    a = (a or "").lower()
    a = re.split(r"feat\.?|ft\.?|featuring", a)[0]
    a = re.split(r"[,&/;]", a)[0]
    return a.strip()

def _tag_empty(value) -> bool:
    if value is None: return True
    if isinstance(value, (list, tuple)):
        return not any((v or "").strip() for v in value)
    return not str(value).strip()

def _has_version_marker(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in VERSION_MARKERS)

def _lrc_to_sylt(lrc_text: str) -> list[tuple[str, int]]:
    pattern = re.compile(r"\[(\d{1,3}):(\d{2})\.(\d{2,3})\](.*)")
    result  = []
    for line in lrc_text.splitlines():
        m = pattern.match(line.strip())
        if m:
            mins, secs, cs, text = m.groups()
            cs_int = int(cs) * (10 if len(cs) == 2 else 1)
            ms     = int(mins)*60000 + int(secs)*1000 + cs_int
            if text.strip():
                result.append((text.strip(), ms))
    return result

def leer_tags_completo(path: str) -> Optional[dict]:
    result = {
        "standard": {}, "has_synced_lrc": False, "has_plain_lrc": False,
        "synced_lrc_text": None, "plain_lrc_text": None, "all_tags": {},
        "duration": None, "format": None,
    }
    try:
        audio_easy = File(path, easy=True)
        if not audio_easy: return None
        result["duration"] = audio_easy.info.length if audio_easy.info else None
        def _g(k): return audio_easy.get(k, [""])[0] or ""
        result["standard"] = {
            "artist": _g("artist"), "title": _g("title"), "album": _g("album"),
            "albumartist": _g("albumartist"), "date": _g("date"),
            "tracknumber": _g("tracknumber"), "discnumber": _g("discnumber"),
            "genre": _g("genre"), "composer": _g("composer"),
            "comment": _g("comment"), "bpm": _g("bpm"),
        }
        ext = path.lower()
        if ext.endswith(".mp3"):
            result["format"] = "mp3"
            try:
                id3 = ID3(path)
                for key, frame in id3.items():
                    try: result["all_tags"][key] = str(frame)[:300]
                    except: result["all_tags"][key] = repr(frame)[:300]
                sylt_frames = id3.getall("SYLT")
                if sylt_frames:
                    result["has_synced_lrc"] = True
                    sylt_lines = []
                    for frame in sylt_frames:
                        for line_text, ts_ms in frame.text:
                            mins, secs, cs = ts_ms // 60000, (ts_ms % 60000) // 1000, (ts_ms % 1000) // 10
                            sylt_lines.append(f"[{mins:02}:{secs:02}.{cs:02}]{line_text}")
                    result["synced_lrc_text"] = "\n".join(sylt_lines)
                uslt_frames = id3.getall("USLT")
                if uslt_frames:
                    result["has_plain_lrc"] = True
                    result["plain_lrc_text"] = uslt_frames[0].text
            except Exception as e:
                log.debug("ID3 read error [%s]: %s", path, e)
        elif ext.endswith(".flac"):
            result["format"] = "flac"
            try:
                flac = FLAC(path)
                for k, v in flac.tags or []:
                    result["all_tags"][k.lower()] = (v[:300] if isinstance(v, str) else str(v)[:300])
                sync_val = flac.get("syncedlyrics", [])
                if sync_val and sync_val[0].strip():
                    result["has_synced_lrc"] = True
                    result["synced_lrc_text"] = sync_val[0]
                plain_val = flac.get("lyrics", [])
                if plain_val and plain_val[0].strip():
                    result["has_plain_lrc"] = True
                    result["plain_lrc_text"] = plain_val[0]
            except Exception as e:
                log.debug("FLAC read error [%s]: %s", path, e)
        elif ext.endswith(".dsf"):
            result["format"] = "dsf"
            try:
                if HAS_DSF:
                    _dsf = DSFFile(path)
                    if _dsf.tags:
                        _id3 = _dsf.tags
                        for _k, _f in _id3.items():
                            try:    result["all_tags"][_k] = str(_f)[:300]
                            except: result["all_tags"][_k] = repr(_f)[:300]

                        # FIX: mutagen NO soporta easy=True para DSF — File(path, easy=True)
                        # devuelve el objeto crudo, por lo que result["standard"] (construido
                        # arriba via audio_easy) queda con todos los campos vacíos.
                        # Reconstruimos "standard" manualmente desde los frames ID3 ya leídos.
                        def _frame_text(frame_id):
                            fr = _id3.get(frame_id)
                            if fr is None or not getattr(fr, "text", None):
                                return ""
                            return str(fr.text[0])

                        result["standard"]["title"]       = _frame_text("TIT2")
                        result["standard"]["artist"]      = _frame_text("TPE1")
                        result["standard"]["album"]       = _frame_text("TALB")
                        result["standard"]["albumartist"] = _frame_text("TPE2")
                        result["standard"]["date"]        = _frame_text("TDRC")
                        result["standard"]["tracknumber"] = _frame_text("TRCK")
                        result["standard"]["discnumber"]  = _frame_text("TPOS")
                        result["standard"]["genre"]       = _frame_text("TCON")
                        result["standard"]["composer"]    = _frame_text("TCOM")
                        result["standard"]["bpm"]         = _frame_text("TBPM")
                        _comm_keys = [k for k in _id3.keys() if k.startswith("COMM")
                                     and "Mood" not in k]
                        if _comm_keys:
                            _comm_frame = _id3.get(_comm_keys[0])
                            result["standard"]["comment"] = (
                                str(_comm_frame.text[0]) if _comm_frame.text else ""
                            )
                        else:
                            result["standard"]["comment"] = ""
                        _sylt = _id3.getall("SYLT")
                        if _sylt:
                            result["has_synced_lrc"] = True
                            _sl = []
                            for _fr in _sylt:
                                for _tx, _ms in _fr.text:
                                    _m = _ms // 60000
                                    _s = (_ms % 60000) // 1000
                                    _c = (_ms % 1000) // 10
                                    _sl.append(f"[{_m:02}:{_s:02}.{_c:02}]{_tx}")
                            result["synced_lrc_text"] = "\n".join(_sl)
                        _uslt = _id3.getall("USLT")
                        if _uslt:
                            result["has_plain_lrc"] = True
                            result["plain_lrc_text"] = _uslt[0].text
            except Exception as e:
                log.debug("DSF read error [%s]: %s", path, e)
    except Exception as e:
        log.debug("leer_tags_completo [%s]: %s", path, e)
        return None
    return result

# ============================================================
# API FETCHERS
# ============================================================

_BASE64URL_RE = re.compile(r'^[A-Za-z0-9_-]+$')

def get_fingerprint(path: str) -> tuple[Optional[str], Optional[float]]:
    """
    Genera huella acústica Chromaprint para el archivo de audio.

    Estrategia en dos pasos:
    1. fpcalc subprocess: formato garantizado para la web API de AcoustID.
       Instalación: sudo apt install libchromaprint-tools
    2. Fallback a librería Python acoustid+chromaprint (pyacoustid).
       Su formato puede diferir de la web API en algunas versiones.
    """
    # ── Método 1: fpcalc subprocess (formato garantizado para AcoustID API) ──
    try:
        r = subprocess.run(
            ["fpcalc", "-json", path],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            fp   = data.get("fingerprint", "")
            dur  = data.get("duration", 0)
            if fp and dur and _BASE64URL_RE.match(fp):
                log.debug("Fingerprint via fpcalc OK (len=%d, dur=%.1f)", len(fp), dur)
                return fp, float(dur)
        else:
            log.debug("fpcalc returncode %s para %s: %s", r.returncode, path, r.stderr[:100])
    except FileNotFoundError:
        log.debug("fpcalc no encontrado — instalar: sudo apt install libchromaprint-tools")
    except Exception as e:
        log.debug("fpcalc error [%s]: %s", path, e)

    # ── Método 2: librería Python acoustid (fallback) ─────────────────────
    if ACOUSTID_LIB_OK:
        try:
            duration, fp_encoded = acoustid.fingerprint_file(path)
            if fp_encoded and duration:
                fp_str = fp_encoded.decode() if isinstance(fp_encoded, bytes) else str(fp_encoded)
                if _BASE64URL_RE.match(fp_str):
                    log.debug("Fingerprint via acoustid lib OK (len=%d)", len(fp_str))
                    return fp_str, float(duration)
                else:
                    log.debug("acoustid lib devolvió fingerprint con chars inválidos — descartado")
        except Exception as e:
            log.debug("acoustid lib fingerprint error [%s]: %s", path, e)

    return None, None

# Bandera global: se pone en False tras el primer fallo de fpcalc para
# evitar el warning repetido en cada pista de la sesión.
_fpcalc_disponible: Optional[bool] = None

def buscar_acoustid(fp: str, duration: float) -> Optional[dict]:
    """
    Lookup de huella acústica en AcoustID via POST.

    Requisito: fpcalc (chromaprint). Si no está instalado, el fingerprint
    llega vacío y la API retorna 400. Se valida antes de llamar a la API.

    Instalación en Ubuntu/Debian:
        sudo apt install libchromaprint-tools
    """
    global _fpcalc_disponible

    if not ACOUSTID_API_KEY: return None

    # Validar fingerprint: debe ser un string base64 de al menos 100 caracteres
    if not duration or duration <= 0:
        log.debug("AcoustID omitido: duración inválida (%.1f)", duration)
        return None

    if not fp or not isinstance(fp, str) or len(fp) < 100:
        if _fpcalc_disponible is not False:
            _fpcalc_disponible = False
            log.warning(
                "AcoustID desactivado: fpcalc no instalado o fingerprint inválido. "
                "Instalar con: sudo apt install libchromaprint-tools"
            )
        return None

    _fpcalc_disponible = True
    try:
        payload = {
            "client":      ACOUSTID_API_KEY,
            "fingerprint": fp,
            "duration":    int(duration),
            "meta":        "recordings+releasegroups+compress",
        }
        _ac_wait()
        r = requests.post(ACOUSTID_BASE, data=payload, timeout=12)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results: return None
        best = max(results, key=lambda x: x.get("score", 0))
        recs = best.get("recordings", [])
        if not recs: return None
        rec = recs[0]
        return {
            "mbid":   rec.get("id"),
            "title":  rec.get("title"),
            "artist": rec["artists"][0]["name"] if rec.get("artists") else None,
            "score":  best.get("score", 0),
        }
    except requests.exceptions.HTTPError as e:
        body = ""
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text[:200] if e.response else ""
        log.debug("AcoustID HTTP error %s — body: %s", e.response.status_code if e.response else "?", body)
        return None
    except Exception as e:
        log.debug("AcoustID error: %s", e)
        return None

def buscar_musicbrainz(artist: str, title: str) -> Optional[dict]:
    _mb_wait()
    try:
        r = requests.get(f"{MB_BASE}/recording", params={"query": f'recording:"{title}" AND artist:"{artist}"', "fmt": "json", "limit": 5}, headers=HEADERS_MB, timeout=12)
        r.raise_for_status()
        recs = r.json().get("recordings", [])
        if not recs: return None
        best, best_score = None, 0
        for rec in recs:
            score = fuzz.ratio(_norm(rec.get("title", "")), _norm(title))
            if score > best_score: best_score, best = score, rec
        return best if best_score > 60 else None
    except Exception as e:
        log.debug("MusicBrainz error: %s", e)
        return None

def _mb_duration_s(mb_recording: Optional[dict]) -> Optional[float]:
    """
    Extrae la duración en segundos del recording de MusicBrainz.
    MB almacena 'length' en milisegundos. Retorna None si no está disponible
    o si el valor no es razonable para una pista (< 30s o > 1800s / 30 min).
    """
    if not mb_recording: return None
    ms = mb_recording.get("length")
    if not ms: return None
    secs = float(ms) / 1000.0
    if secs < 30 or secs > 1800:
        log.debug("MB duration fuera de rango: %.1fs — descartada", secs)
        return None
    return round(secs, 1)

def obtener_mb_release_details(mbid: str) -> Optional[dict]:
    """
    Obtiene detalles de release desde un recording MBID.

    Inc válidos para /recording (MB API v2):
      releases, isrcs, artist-credits, tags, ratings, aliases,
      annotation, url-rels, work-rels, release-groups
    'labels' NO es válido aquí (causa HTTP 400). La info de label
    se obtiene navegando al release incluido en la respuesta.
    """
    _mb_wait()
    try:
        r = requests.get(
            f"{MB_BASE}/recording/{mbid}",
            params={"fmt": "json", "inc": "releases+isrcs+artist-credits+release-groups"},
            headers=HEADERS_MB, timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        releases = data.get("releases", [])
        release = next((rel for rel in releases if rel.get("release-group", {}).get("primary-type") == "Album"), releases[0] if releases else None)
        isrcs = data.get("isrcs", [])
        result = {"isrc": isrcs[0] if isrcs else None}
        if release:
            result.update({"album": release.get("title"), "date": release.get("date", "")[:4], "country": release.get("country"), "release_mbid": release.get("id")})
            for medium in release.get("media", []):
                for track in medium.get("tracks", []):
                    if track.get("recording", {}).get("id") == mbid:
                        result["tracknumber"], result["discnumber"] = str(track.get("position", "")), str(medium.get("position", ""))
                        break
            # label-info no está disponible en el endpoint /recording.
            # Para obtenerla hay que fetchear el /release por su ID.
            release_id = release.get("id")
            with _cache_lock:
                cached = _cache_release_labels.get(release_id) if release_id else None
            if cached is not None:
                result.update(cached)
            elif release_id:
                try:
                    _mb_wait()
                    r2 = requests.get(
                        f"{MB_BASE}/release/{release_id}",
                        params={"fmt": "json", "inc": "labels"},
                        headers=HEADERS_MB, timeout=10,
                    )
                    if r2.status_code == 200:
                        label_info = r2.json().get("label-info", [])
                        info = {}
                        if label_info:
                            info = {"label":   label_info[0].get("label", {}).get("name"),
                                    "catalog": label_info[0].get("catalog-number")}
                        result.update(info)
                        with _cache_lock:
                            _cache_release_labels[release_id] = info
                except Exception:
                    pass  # label es opcional; no interrumpir el flujo
        return result
    except Exception as e:
        log.debug("MB release details error: %s", e)
        return None

def obtener_generos_lastfm(artist: str) -> list[tuple[str, int]]:
    key = _norm(artist)
    if not LASTFM_API_KEY: return []
    with _lfm_lock:
        with _cache_lock:
            if key in _cache_genres: return _cache_genres[key]
        return _obtener_generos_lastfm_red(artist, key)

def _obtener_generos_lastfm_red(artist: str, key: str) -> list[tuple[str, int]]:
    _lfm_wait()
    try:
        r = requests.get(LASTFM_BASE, params={"method": "artist.gettoptags", "artist": artist, "api_key": LASTFM_API_KEY, "format": "json"}, timeout=10)
        r.raise_for_status()
        tags = r.json().get("toptags", {}).get("tag", [])
        result = sorted([(t["name"], int(t.get("count", 0))) for t in tags if int(t.get("count", 0)) > 5], key=lambda x: x[1], reverse=True)[:10]
        with _cache_lock:
            _cache_genres[key] = result
        return result
    except Exception as e:
        log.debug("Last.fm error: %s", e)
        return []

def obtener_generos_mb(mb_recording: Optional[dict]) -> list[str]:
    if not mb_recording: return []
    return [t["name"] for t in mb_recording.get("tags", []) if int(t.get("count", 0)) > 0][:8]

# Tags de Last.fm que no aportan información de género real
_LASTFM_NOISE = {
    "seen live","love","favorite","favourites","all","albums i own",
    "check out","amazing","awesome","cool","good","great","beautiful",
    "music","singer","vocalist","male vocalists","female vocalists",
    "british","american","german","swedish","norwegian","finnish",
    "00s","10s","20s","80s","90s","70s","60s",
}

def _is_genre_tag(tag: str) -> bool:
    """Descarta tags que son ruido (idioma, década, valoración personal)."""
    return _norm(tag) not in _LASTFM_NOISE and len(tag) > 2

# Géneros canónicos ordenados de más específico a más general.
# El primero que matchee con la lista de géneros obtenidos será el género primario.
_CANONICAL_GENRES = [
    # Metal
    "Black Metal","Death Metal","Thrash Metal","Power Metal","Doom Metal",
    "Progressive Metal","Symphonic Metal","Folk Metal","Groove Metal",
    "Speed Metal","German Metal","Heavy Metal","Hair Metal","Glam Metal",
    "Nu Metal","Alternative Metal","Metalcore","Melodic Death Metal",
    # Rock
    "Punk Rock","Hard Rock","Classic Rock","Progressive Rock","Psychedelic Rock",
    "Alternative Rock","Indie Rock","Garage Rock","Blues Rock","Southern Rock",
    "Gothic Rock","Post Rock","Grunge","New Wave","Post-Punk",
    # Otros géneros base
    "Rock","Blues","Jazz","Soul","Funk","Reggae","Country","Folk",
    "Classical","Electronic","Ambient","Techno","House","Trance","Drum and Bass",
    "Hip Hop","Rap","R&B","Pop","Disco",
]
_CANONICAL_NORM = {_norm(g): g for g in _CANONICAL_GENRES}

def _pick_primary(genres: list[str], lastfm_counts: dict | None = None) -> str:
    """
    Elige el género primario: el de mayor count en Last.fm que además
    sea canónico. Si ninguno es canónico, devuelve el de mayor count.
    lastfm_counts: {norm(tag): count}
    """
    if not genres:
        return ""
    counts = lastfm_counts or {}
    genre_norms = {_norm(g): g for g in genres}

    # Candidatos canónicos presentes en el pool
    canonical_candidates = [
        (genre_norms[cn], counts.get(cn, 0))
        for cn in _CANONICAL_NORM
        if cn in genre_norms
    ]
    if canonical_candidates:
        # El canónico con mayor count en la comunidad
        return max(canonical_candidates, key=lambda x: x[1])[0]
    # Sin canónicos: el primero del pool (ya ordenado por count)
    return genres[0]


def consenso_generos(lastfm_tags, mb_genres) -> tuple[str, list[str]]:
    """
    Retorna (genre_primary, genre_secondary):
      - genre_primary  : un género canónico representativo (el más específico)
      - genre_secondary: lista de 2-3 géneros adicionales representativos

    Fallbacks progresivos para armar el pool total:
    1. Intersección Last.fm ∩ MusicBrainz  (máxima confianza)
    2. Last.fm con count > 30
    3. Last.fm con count > 10
    4. Top 3 Last.fm filtrado de ruido
    5. Top 3 MB
    """
    lf_clean = [(t, c) for t, c in lastfm_tags if _is_genre_tag(t)]
    mb_clean  = [g for g in mb_genres if _is_genre_tag(g)]

    lf_norm = {_norm(t): t for t, _ in lf_clean}
    mb_norm = {_norm(g): g for g in mb_clean}

    pool: list[str] = []

    # 1) Intersección LF ∩ MB
    pool = [orig for nk, orig in lf_norm.items() if nk in mb_norm]

    # 2) Last.fm count > 30
    seen = {_norm(r) for r in pool}
    pool += [t for t, c in lf_clean if c > 30 and _norm(t) not in seen]

    # 3) Last.fm count > 10
    if not pool:
        pool = [t for t, c in lf_clean if c > 10][:6]

    # 4) Top 3 Last.fm limpio
    if not pool and lf_clean:
        pool = [t for t, _ in lf_clean[:4]]

    # 5) Fallback a MusicBrainz
    if not pool and mb_clean:
        pool = mb_clean[:4]

    if not pool:
        return "", []

    lf_counts = {_norm(t): c for t, c in lf_clean}
    primary   = _pick_primary(pool, lastfm_counts=lf_counts)
    secondary = [g for g in pool if _norm(g) != _norm(primary)][:3]
    return primary, secondary

def obtener_ab_features(mbid: str) -> Optional[dict]:
    """
    AcousticBrainz fue dado de baja definitivamente el 17 de noviembre de 2022.
    Todos los requests generan timeout (~10s × 2 endpoints = 20s por pista).
    Función desactivada para evitar ~216h de espera en bibliotecas grandes.
    Se conserva la firma para compatibilidad con el resto del código.
    """
    return None

def _parse_ab(ll, hl) -> dict:
    rhythm, tonal, hl_prob = ll.get("rhythm", {}), ll.get("tonal", {}), hl.get("highlevel", {})
    def _p(k, sk): return float(hl_prob.get(k, {}).get("all", {}).get(sk) or 0)
    return {
        "bpm": round(float(rhythm.get("bpm") or 0)), "key": tonal.get("key_key", ""), "mode": tonal.get("key_scale", ""),
        "danceability": hl_prob.get("danceability", {}).get("value", ""),
        "mood_aggressive": _p("mood_aggressive", "aggressive"), "mood_happy": _p("mood_happy", "happy"),
        "mood_sad": _p("mood_sad", "sad"), "mood_relaxed": _p("mood_relaxed", "relaxed"),
        "mood_party": _p("mood_party", "party"), "mood_acoustic": _p("mood_acoustic", "acoustic"),
        "mood_electronic": _p("mood_electronic", "electronic"),
        "instrumental": hl_prob.get("voice_instrumental", {}).get("value", "") == "instrumental",
    }

# Tolerancias de duración (segundos)
LRC_DUR_EXACT   = 2    # diferencia ≤2s  → match perfecto
LRC_DUR_GOOD    = 5    # diferencia ≤5s  → match bueno
LRC_DUR_MARGIN  = 15   # diferencia >15s → descarte duro

def _score_candidate(cand: dict, title: str, artist: str, album: str,
                     duration_s: Optional[float]) -> float:
    """
    Scorer duración-céntrico para candidatos de LRCLib.

    FILOSOFÍA:
      La duración del archivo de audio es el identificador más preciso disponible
      (los metadatos de audio no mienten). Se usa como ancla principal:

        ≤ 2s de diferencia  → dur_score 1.00  (match exacto)
        ≤ 5s               → dur_score 0.60–1.00 (bueno)
        ≤ 15s              → dur_score 0.00–0.60 (dudoso, no se descarta)
        > 15s              → return 0.0 inmediato (pista diferente)

      Cuando la duración confirma la pista (≤5s), el umbral de título baja de 0.90
      a 0.75, porque ya tenemos certeza suficiente por duración.

    TÍTULO:
      Usamos partial_ratio para tolerar sufijos de versión:
        "Crazy Train (Remastered 2002)" → partial_ratio("crazy train") = 1.00
        fuzz.ratio daría 0.58 → falso negativo.

    VERSIONES:
      Remaster/live/edition: penalización leve (0.08) — mismas letras.
      Cover/remix/acoustic:  penalización fuerte (0.20) — letras posiblemente distintas.
      Instrumental:          penalización máxima (0.40) — sin letras cantadas.
      En todos los casos el candidato limpio siempre gana por score.

    PESOS FINALES (con duración disponible):
      duración 40% · título 35% · artista 20% · álbum 5%
    PESOS SIN DURACIÓN:
      título 50% · artista 35% · álbum 15%
    """
    cand_title  = cand.get("trackName",  "")
    cand_artist = cand.get("artistName", "")
    cand_album  = cand.get("albumName",  "")
    cand_dur    = cand.get("duration",   0)

    # ── Duración (ancla primaria) ─────────────────────────────────────────────
    dur_score    = None   # None = sin dato de duración
    dur_exact    = False
    if duration_s and cand_dur:
        diff = abs(float(cand_dur) - duration_s)
        if diff > LRC_DUR_MARGIN:
            return 0.0                                   # pista completamente distinta
        if diff <= LRC_DUR_EXACT:
            dur_score, dur_exact = 1.00, True
        elif diff <= LRC_DUR_GOOD:
            dur_score = 1.00 - (diff - LRC_DUR_EXACT) / (LRC_DUR_GOOD - LRC_DUR_EXACT) * 0.40
        else:
            dur_score = max(0.0, 0.60 - (diff - LRC_DUR_GOOD) / (LRC_DUR_MARGIN - LRC_DUR_GOOD) * 0.60)

    # ── Título (partial_ratio tolera sufijos de versión) ─────────────────────
    title_p = fuzz.partial_ratio(_norm(cand_title), _norm(title)) / 100
    # Si la duración confirma la pista, bajamos el umbral de título
    title_threshold = 0.75 if dur_exact else 0.90
    if title_p < title_threshold:
        return 0.0

    # ── Penalización por marcadores de versión ────────────────────────────────
    version_penalty = 0.0
    if _has_version_marker(cand_title) and not _has_version_marker(title):
        tl = cand_title.lower()
        if "instrumental" in tl:
            version_penalty = 0.40      # sin letra cantada
        elif any(m in tl for m in ("cover", "remix")):
            version_penalty = 0.20      # letras posiblemente distintas
        elif "acoustic" in tl:
            version_penalty = 0.15
        else:                           # remaster, live, edition, mono…
            version_penalty = 0.08

    # ── Artista ───────────────────────────────────────────────────────────────
    artist_sim = fuzz.ratio(_norm(cand_artist), _norm(artist)) / 100

    # ── Álbum (factor de desempate, muy bajo peso) ────────────────────────────
    album_sim = 1.0
    if album and cand_album:
        album_sim = fuzz.ratio(_norm(cand_album), _norm(album)) / 100
        if album_sim < 0.50:
            album_sim = 0.20   # compilación distinta al original → penalizar, no descartar

    # ── Score ponderado ───────────────────────────────────────────────────────
    if dur_score is not None:
        score = (dur_score  * 0.40 + title_p    * 0.35 +
                 artist_sim * 0.20 + album_sim  * 0.05)
    else:
        score = (title_p    * 0.50 + artist_sim * 0.35 +
                 album_sim  * 0.15)

    score -= version_penalty
    if cand.get("syncedLyrics"):
        score = min(score + 0.05, 1.0)
    return round(max(score, 0.0), 3)


def buscar_lrclib(title: str, artist: str, album: str = "",
                  duration_s: Optional[float] = None) -> dict:
    """
    Búsqueda en cascada de letras en LRCLib.  Se detiene en el primer paso que
    produce al menos UN candidato con syncedLyrics; si ningún paso logra synced,
    acumula todos los candidatos y elige el mejor por score (incluyendo plain).

    CASCADA (orden de ejecución):
    ┌─────┬──────────────────────────────────────────────────────────────────┐
    │ P0  │ /get  exacto: track_name + artist_name [+ duration]             │
    │     │  → endpoint de lookup directo de LRCLib, sin lista de resultados│
    ├─────┼──────────────────────────────────────────────────────────────────┤
    │ P1  │ /search q="<album>"                                              │
    │     │  → busca todo el álbum; la duración filtra la pista exacta      │
    ├─────┼──────────────────────────────────────────────────────────────────┤
    │ P2  │ /search track_name + artist_name  (sin album_name)              │
    │     │  → búsqueda estructurada; omite álbum para no filtrar server-side│
    ├─────┼──────────────────────────────────────────────────────────────────┤
    │ P3  │ /search q="<artist> <title>"                                     │
    │     │  → full-text libre, cubre títulos con caracteres especiales      │
    ├─────┼──────────────────────────────────────────────────────────────────┤
    │ P4  │ /search q="<title>"  (solo título)                               │
    │     │  → máxima cobertura; duración es el filtro crítico aquí          │
    ├─────┼──────────────────────────────────────────────────────────────────┤
    │ P5  │ /search q="<artist>"  (solo artista)                             │
    │     │  → último recurso; duración debe ser exacta (≤2s) para aceptar  │
    └─────┴──────────────────────────────────────────────────────────────────┘

    VALIDACIÓN FINAL:
      _score_candidate() puntúa cada candidato con pesos:
        duración(40%) · título(35%) · artista(20%) · álbum(5%)
      Se prefiere siempre synced > plain, luego mayor score.
      Score mínimo de aceptación: CONFIDENCE_MIN (0.60) cuando hay duración,
      0.50 cuando no hay duración disponible.
    """
    empty = {"synced": None, "plain": None, "lrclib_id": None,
             "score": 0.0, "candidates_found": 0}

    # ── Helpers internos ──────────────────────────────────────────────────────

    def _get_exact() -> Optional[dict]:
        """GET /api/get — lookup directo sin lista. Retorna dict o None."""
        _lrc_wait()
        params: dict = {"track_name": title, "artist_name": artist}
        if duration_s:
            params["duration"] = int(duration_s)
        try:
            log.debug("LRCLib GET /get (exact) -> %s", params)
            r = requests.get(f"{LRCLIB_BASE}/get", params=params,
                             headers=HEADERS_LRC, timeout=10)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None    # no encontrado, no es error
            if r.status_code == 429:
                log.error("LRCLib 429 en /get — esperando 10s")
                time.sleep(10)
            else:
                log.debug("LRCLib /get HTTP %s", r.status_code)
        except requests.RequestException as e:
            log.debug("LRCLib /get error: %s", e)
        return None

    def _search(params: dict) -> list:
        """GET /api/search — retorna lista (puede ser vacía)."""
        _lrc_wait()
        try:
            log.debug("LRCLib GET /search -> %s", params)
            r = requests.get(f"{LRCLIB_BASE}/search", params=params,
                             headers=HEADERS_LRC, timeout=10)
            if r.status_code == 429:
                log.error("LRCLib 429 en /search — esperando 10s")
                time.sleep(10)
                return []
            if r.status_code != 200:
                log.debug("LRCLib /search HTTP %s params=%s", r.status_code, params)
                return []
            return r.json() or []
        except requests.RequestException as e:
            log.debug("LRCLib /search error: %s", e)
            return []

    def _fetch_full(lrclib_id: int) -> Optional[dict]:
        """GET /api/get/<id> — obtiene letra completa por ID."""
        _lrc_wait()
        try:
            r = requests.get(f"{LRCLIB_BASE}/get/{lrclib_id}",
                             headers=HEADERS_LRC, timeout=8)
            if r.status_code == 200:
                return r.json()
            log.debug("LRCLib /get/%s HTTP %s", lrclib_id, r.status_code)
        except Exception as e:
            log.debug("LRCLib /get/%s error: %s", lrclib_id, e)
        return None

    def _merge(base: list, extra: list) -> list:
        seen = {c.get("id") for c in base}
        return base + [c for c in extra if c.get("id") not in seen]

    def _has_synced_scored(pool: list) -> bool:
        """True si algún candidato tiene syncedLyrics Y pasa el scorer."""
        return any(
            c.get("syncedLyrics") and _score_candidate(c, title, artist, album, duration_s) > 0
            for c in pool
        )

    def _best_result(pool: list, total_candidates: int) -> dict:
        """Elige el mejor candidato del pool, obtiene la letra completa y retorna."""
        scored = [(c, _score_candidate(c, title, artist, album, duration_s))
                  for c in pool]
        scored = [(c, s) for c, s in scored if s > 0]
        if not scored:
            log.debug("LRCLib: %d candidatos acumulados, ninguno pasó el scorer "
                      "(title=%r artist=%r dur=%s)", total_candidates, title, artist,
                      f"{duration_s:.1f}s" if duration_s else "N/A")
            return {**empty, "candidates_found": total_candidates}

        # Ordenar: synced primero, luego score descendente
        scored.sort(key=lambda x: (bool(x[0].get("syncedLyrics")), x[1]), reverse=True)
        best_cand, best_score = scored[0]
        best_id = best_cand.get("id")

        # Score mínimo de aceptación
        min_score = 0.50 if not duration_s else CONFIDENCE_MIN
        if best_score < min_score:
            log.debug("LRCLib: mejor score %.3f < %.2f mínimo — descartando",
                      best_score, min_score)
            return {**empty, "candidates_found": total_candidates}

        # Obtener letra completa
        full = _fetch_full(best_id) if best_id else None
        synced = (full or best_cand).get("syncedLyrics") or None
        plain  = (full or best_cand).get("plainLyrics")  or None
        log.debug("LRCLib ✓ id=%s score=%.3f synced=%s plain=%s match=%r album=%r",
                  best_id, best_score, bool(synced), bool(plain),
                  best_cand.get("trackName"), best_cand.get("albumName"))
        return {
            "synced":           synced,
            "plain":            plain,
            "lrclib_id":        best_id,
            "score":            best_score,
            "candidates_found": total_candidates,
            "track_name_match": best_cand.get("trackName"),
            "album_match":      best_cand.get("albumName"),
        }

    # ══════════════════════════════════════════════════════════════════════════
    # P0 — /get exacto (lookup directo, más rápido, sin lista)
    # ══════════════════════════════════════════════════════════════════════════
    exact = _get_exact()
    if exact and exact.get("id"):
        # Validar con scorer antes de aceptar
        s = _score_candidate(exact, title, artist, album, duration_s)
        if s >= (CONFIDENCE_MIN if duration_s else 0.50):
            synced = exact.get("syncedLyrics") or None
            plain  = exact.get("plainLyrics")  or None
            log.debug("LRCLib P0 ✓ /get exacto — score=%.3f synced=%s", s, bool(synced))
            return {
                "synced": synced, "plain": plain,
                "lrclib_id": exact["id"], "score": s,
                "candidates_found": 1,
                "track_name_match": exact.get("trackName"),
                "album_match":      exact.get("albumName"),
            }
        log.debug("LRCLib P0 /get exacto score %.3f insuficiente — continuando", s)

    # ══════════════════════════════════════════════════════════════════════════
    # P1 — /search q="<album>"  (el más productivo para compilaciones)
    # ══════════════════════════════════════════════════════════════════════════
    all_candidates: list = []
    if album:
        p1 = _search({"q": album})
        log.debug("LRCLib P1 q=%r → %d resultados", album, len(p1))
        all_candidates = _merge(all_candidates, p1)
        if _has_synced_scored(all_candidates):
            return _best_result(all_candidates, len(all_candidates))

    # ══════════════════════════════════════════════════════════════════════════
    # P2 — /search track_name + artist_name  (estructurado, sin album_name)
    # ══════════════════════════════════════════════════════════════════════════
    p2 = _search({"track_name": title, "artist_name": artist})
    log.debug("LRCLib P2 track=%r artist=%r → %d resultados", title, artist, len(p2))
    all_candidates = _merge(all_candidates, p2)
    if _has_synced_scored(all_candidates):
        return _best_result(all_candidates, len(all_candidates))

    # ══════════════════════════════════════════════════════════════════════════
    # P3 — /search q="<artist> <title>"
    # ══════════════════════════════════════════════════════════════════════════
    p3 = _search({"q": f"{artist} {title}"})
    log.debug("LRCLib P3 q=%r → %d resultados", f"{artist} {title}", len(p3))
    all_candidates = _merge(all_candidates, p3)
    if _has_synced_scored(all_candidates):
        return _best_result(all_candidates, len(all_candidates))

    # ══════════════════════════════════════════════════════════════════════════
    # P4 — /search q="<title>"  (solo título, duración como filtro duro)
    # ══════════════════════════════════════════════════════════════════════════
    p4 = _search({"q": title})
    log.debug("LRCLib P4 q=%r → %d resultados", title, len(p4))
    all_candidates = _merge(all_candidates, p4)
    if _has_synced_scored(all_candidates):
        return _best_result(all_candidates, len(all_candidates))

    # ══════════════════════════════════════════════════════════════════════════
    # P5 — /search q="<artist>"  (último recurso; duración debe ser exacta)
    # ══════════════════════════════════════════════════════════════════════════
    if artist:
        p5 = _search({"q": artist})
        log.debug("LRCLib P5 q=%r → %d resultados", artist, len(p5))
        # En este paso solo aceptamos si la duración es exacta (≤2s)
        # para no contaminar el pool con cualquier canción del artista
        if duration_s:
            p5_filtered = [c for c in p5
                           # LRCLib a veces trae "duration": null -> float(None) tumbaba el álbum entero
                           if abs(float(c.get("duration") or 0) - duration_s) <= LRC_DUR_EXACT]
            log.debug("LRCLib P5 filtrado por duración exacta: %d/%d", len(p5_filtered), len(p5))
            all_candidates = _merge(all_candidates, p5_filtered)
        else:
            all_candidates = _merge(all_candidates, p5)

    # ── Selección final con todos los candidatos acumulados ───────────────────
    return _best_result(all_candidates, len(all_candidates))

# ============================================================
# === PEGAR DICCIONARIOS AQUÍ ===
# Pega exactamente tus variables:
# LYRIC_LEXICON, LYRIC_THEMES, ERA_MAP, ERA_STYLE_MAP,
# GENRE_VECTORS, MOOD_CENTROIDS, CAMELOT_MAP y CSS
# ============================================================

# ============================================================
# NLP MULTILINGÜE (lexicón completo)
# ============================================================

LYRIC_LEXICON = [
    # ENERGY alta
    ("fight",("en",),"energy",+0.60),("run",("en",),"energy",+0.40),
    ("fire",("en",),"energy",+0.55),("burn",("en",),"energy",+0.50),
    ("power",("en",),"energy",+0.45),("rage",("en",),"energy",+0.65),
    ("rush",("en",),"energy",+0.50),("blast",("en",),"energy",+0.55),
    ("force",("en",),"energy",+0.45),("strike",("en",),"energy",+0.55),
    ("crush",("en",),"energy",+0.55),("explode",("en",),"energy",+0.60),
    ("thunder",("en",),"energy",+0.55),("storm",("en",),"energy",+0.50),
    ("wild",("en",),"energy",+0.45),("scream",("en",),"energy",+0.50),
    ("electric",("en",),"energy",+0.45),("shout",("en",),"energy",+0.45),
    ("lucha",("es",),"energy",+0.60),("fuego",("es",),"energy",+0.55),
    ("fuerza",("es",),"energy",+0.45),("rabia",("es",),"energy",+0.60),
    ("explotar",("es",),"energy",+0.60),("tormenta",("es",),"energy",+0.50),
    ("combat",("fr",),"energy",+0.55),("feu",("fr",),"energy",+0.55),
    ("kampf",("de",),"energy",+0.55),("feuer",("de",),"energy",+0.55),
    ("wut",("de",),"energy",+0.60),("sturm",("de",),"energy",+0.50),
    ("schrei",("de",),"energy",+0.50),("kraft",("de",),"energy",+0.45),
    ("luta",("pt",),"energy",+0.55),("combattere",("it",),"energy",+0.55),
    # ENERGY baja
    ("sleep",("en",),"energy",-0.50),("still",("en",),"energy",-0.35),
    ("slow",("en",),"energy",-0.40),("whisper",("en",),"energy",-0.50),
    ("quiet",("en",),"energy",-0.45),("calm",("en",),"energy",-0.55),
    ("gentle",("en",),"energy",-0.45),("soft",("en",),"energy",-0.45),
    ("dormir",("es","fr"),"energy",-0.50),("susurro",("es",),"energy",-0.50),
    ("silencio",("es",),"energy",-0.40),("suave",("es",),"energy",-0.45),
    ("schlafen",("de",),"energy",-0.50),("stille",("de",),"energy",-0.50),
    # VALENCE positivo
    ("love",("en",),"valence",+0.65),("happy",("en",),"valence",+0.75),
    ("joy",("en",),"valence",+0.65),("smile",("en",),"valence",+0.55),
    ("dance",("en",),"valence",+0.55),("celebrate",("en",),"valence",+0.65),
    ("free",("en",),"valence",+0.50),("freedom",("en",),"valence",+0.55),
    ("hope",("en",),"valence",+0.55),("light",("en",),"valence",+0.45),
    ("shine",("en",),"valence",+0.50),("sun",("en",),"valence",+0.45),
    ("warm",("en",),"valence",+0.40),("paradise",("en",),"valence",+0.65),
    ("beautiful",("en",),"valence",+0.50),("glory",("en",),"valence",+0.55),
    ("victory",("en",),"valence",+0.55),("heaven",("en",),"valence",+0.55),
    ("alive",("en",),"valence",+0.50),("baby",("en",),"valence",+0.35),
    ("wonderful",("en",),"valence",+0.60),("friend",("en",),"valence",+0.45),
    ("together",("en",),"valence",+0.45),("bells",("en",),"valence",+0.30),
    ("peace",("en",),"valence",+0.50),("children",("en",),"valence",+0.35),
    ("amor",("es","pt","it"),"valence",+0.65),("feliz",("es",),"valence",+0.75),
    ("alegría",("es",),"valence",+0.65),("baila",("es",),"valence",+0.55),
    ("libre",("es","fr"),"valence",+0.50),("libertad",("es",),"valence",+0.55),
    ("esperanza",("es",),"valence",+0.55),("sol",("es","pt"),"valence",+0.45),
    ("cielo",("es",),"valence",+0.45),("paraíso",("es",),"valence",+0.65),
    ("gloria",("es","pt"),"valence",+0.55),("victoria",("es",),"valence",+0.55),
    ("amour",("fr",),"valence",+0.65),("bonheur",("fr",),"valence",+0.65),
    ("joie",("fr",),"valence",+0.65),("liberté",("fr",),"valence",+0.55),
    ("espoir",("fr",),"valence",+0.55),
    ("liebe",("de",),"valence",+0.65),("glück",("de",),"valence",+0.65),
    ("freude",("de",),"valence",+0.65),("freiheit",("de",),"valence",+0.55),
    ("sonne",("de",),"valence",+0.45),("hoffnung",("de",),"valence",+0.55),
    ("amore",("it",),"valence",+0.65),("alegria",("pt",),"valence",+0.65),
    # VALENCE negativo
    ("die",("en",),"valence",-0.55),("death",("en",),"valence",-0.65),
    ("hate",("en",),"valence",-0.55),("pain",("en",),"valence",-0.55),
    ("broken",("en",),"valence",-0.45),("empty",("en",),"valence",-0.45),
    ("alone",("en",),"valence",-0.40),("lost",("en",),"valence",-0.40),
    ("cry",("en",),"valence",-0.45),("tears",("en",),"valence",-0.45),
    ("sorrow",("en",),"valence",-0.55),("grief",("en",),"valence",-0.60),
    ("suffer",("en",),"valence",-0.55),("despair",("en",),"valence",-0.60),
    ("hopeless",("en",),"valence",-0.60),("darkness",("en",),"valence",-0.50),
    ("hell",("en",),"valence",-0.50),("chains",("en",),"valence",-0.45),
    ("slave",("en",),"valence",-0.50),("bleed",("en",),"valence",-0.50),
    ("morir",("es",),"valence",-0.55),("muerte",("es",),"valence",-0.65),
    ("odio",("es",),"valence",-0.55),("dolor",("es",),"valence",-0.55),
    ("llorar",("es",),"valence",-0.45),("solo",("es",),"valence",-0.40),
    ("oscuridad",("es",),"valence",-0.50),("cadenas",("es",),"valence",-0.45),
    ("mourir",("fr",),"valence",-0.55),("haine",("fr",),"valence",-0.55),
    ("sterben",("de",),"valence",-0.55),("hass",("de",),"valence",-0.55),
    ("morrer",("pt",),"valence",-0.55),
    # TENSION
    ("war",("en",),"tension",+0.75),("blood",("en",),"tension",+0.65),
    ("fear",("en",),"tension",+0.60),("danger",("en",),"tension",+0.65),
    ("terror",("en",),"tension",+0.70),("panic",("en",),"tension",+0.65),
    ("knife",("en",),"tension",+0.60),("gun",("en",),"tension",+0.65),
    ("battle",("en",),"tension",+0.70),("revenge",("en",),"tension",+0.65),
    ("betrayal",("en",),"tension",+0.65),("lies",("en",),"tension",+0.50),
    ("shadow",("en",),"tension",+0.40),("threat",("en",),"tension",+0.60),
    ("guerra",("es","pt"),"tension",+0.75),("sangre",("es",),"tension",+0.65),
    ("miedo",("es",),"tension",+0.60),("terror",("es",),"tension",+0.70),
    ("traición",("es",),"tension",+0.65),("sangue",("pt",),"tension",+0.65),
    ("sang",("fr",),"tension",+0.65),("peur",("fr",),"tension",+0.60),
    ("blut",("de",),"tension",+0.65),("angst",("de",),"tension",+0.60),
    ("krieg",("de",),"tension",+0.75),("verrat",("de",),"tension",+0.65),
    # DEPTH
    ("dream",("en",),"depth",+0.55),("night",("en",),"depth",+0.45),
    ("soul",("en",),"depth",+0.60),("memory",("en",),"depth",+0.50),
    ("truth",("en",),"depth",+0.50),("silence",("en",),"depth",+0.50),
    ("eternity",("en",),"depth",+0.60),("spirit",("en",),"depth",+0.55),
    ("god",("en",),"depth",+0.50),("faith",("en",),"depth",+0.50),
    ("universe",("en",),"depth",+0.55),("meaning",("en",),"depth",+0.55),
    ("journey",("en",),"depth",+0.45),("cold",("en",),"depth",+0.40),
    ("winter",("en",),"depth",+0.55),("snow",("en",),"depth",+0.45),
    ("moon",("en",),"depth",+0.45),("stars",("en",),"depth",+0.45),
    ("rain",("en",),"depth",+0.40),("fade",("en",),"depth",+0.45),
    ("echo",("en",),"depth",+0.45),("candle",("en",),"depth",+0.40),
    ("wonder",("en",),"depth",+0.45),("infinite",("en",),"depth",+0.55),
    ("time",("en",),"depth",+0.35),("bells",("en",),"depth",+0.40),
    ("peace",("en",),"depth",+0.35),("children",("en",),"depth",+0.30),
    ("sueño",("es",),"depth",+0.55),("noche",("es",),"depth",+0.45),
    ("alma",("es","pt","it"),"depth",+0.60),("verdad",("es",),"depth",+0.50),
    ("eternidad",("es",),"depth",+0.60),("espíritu",("es",),"depth",+0.55),
    ("universo",("es",),"depth",+0.55),("invierno",("es",),"depth",+0.55),
    ("nieve",("es",),"depth",+0.45),("luna",("es","pt","it"),"depth",+0.45),
    ("rêve",("fr",),"depth",+0.55),("âme",("fr",),"depth",+0.60),
    ("éternité",("fr",),"depth",+0.60),("hiver",("fr",),"depth",+0.55),
    ("traum",("de",),"depth",+0.55),("seele",("de",),"depth",+0.60),
    ("ewigkeit",("de",),"depth",+0.60),("winter",("de",),"depth",+0.55),
    ("mond",("de",),"depth",+0.45),("geist",("de",),"depth",+0.55),
    ("sonho",("pt",),"depth",+0.55),("sogno",("it",),"depth",+0.55),
    ("anima",("it",),"depth",+0.60),]

LYRIC_THEMES = {
    "love":         ["love","heart","kiss","darling","baby","lover","amor","amore","liebe","amour"],
    "rebellion":    ["rebel","fight","resist","chains","slave","freedom","rise","revolution","lucha","libertad","freiheit"],
    "introspection":["soul","memory","dream","wonder","truth","reflect","searching","meaning","alma","seele","âme"],
    "spirituality": ["god","faith","pray","sacred","spirit","heaven","divine","dios","geist","espíritu"],
    "war":          ["war","battle","blood","enemy","weapon","attack","army","soldier","guerra","krieg"],
    "nature":       ["rain","snow","wind","moon","stars","forest","ocean","river","winter","spring","bells","children"],
    "party":        ["dance","party","night out","celebrate","drink","move","floor","disco","baila","fiesta","tanzen"],
    "loss":         ["die","death","grief","sorrow","tears","gone","lost","alone","cry","morir","muerte","sterben"],
    "social":       ["society","system","control","power","money","corrupt","lie","truth","justice","people"],
    "nostalgia":    ["memory","remember","yesterday","past","time","years","childhood","old","recall","recuerdo"],
}

# ============================================================
# CONTEXTO Y ERA
# ============================================================

ERA_MAP = {
    range(1950,1960):{"valence":+0.20,"energy":-0.10,"depth":+0.10},
    range(1960,1970):{"valence":+0.30,"energy":+0.10},
    range(1970,1980):{"depth":+0.25,"energy":+0.10},
    range(1980,1990):{"energy":+0.20,"valence":+0.10},
    range(1990,2000):{"energy":+0.25,"tension":+0.15},
    range(2000,2010):{"energy":+0.15},
    range(2010,2020):{"valence":+0.10,"energy":+0.10},
    range(2020,2030):{"valence":+0.10},
}
ERA_STYLE_MAP = {
    range(1950,1960):"early_rock_era",
    range(1960,1970):"british_invasion_era",
    range(1970,1980):"classic_rock_era",
    range(1980,1990):"nwobhm_synth_era",
    range(1990,2000):"grunge_alternative_era",
    range(2000,2010):"post_millennial_era",
    range(2010,2020):"streaming_era",
    range(2020,2030):"current_era",
}

# ============================================================
# FEATURES MUSICEXPLORER
# ============================================================

CAMELOT_MAP = {
    ("C","major"):"8B", ("A","minor"):"8A",
    ("G","major"):"9B", ("E","minor"):"9A",
    ("D","major"):"10B",("B","minor"):"10A",
    ("A","major"):"11B",("F#","minor"):"11A",
    ("E","major"):"12B",("C#","minor"):"12A",
    ("B","major"):"1B", ("G#","minor"):"1A",
    ("F#","major"):"2B",("D#","minor"):"2A",
    ("C#","major"):"3B",("A#","minor"):"3A",
    ("G#","major"):"4B",("F","minor"):"4A",
    ("D#","major"):"5B",("C","minor"):"5A",
    ("A#","major"):"6B",("G","minor"):"6A",
    ("F","major"):"7B", ("D","minor"):"7A",
}

# ============================================================
# MOOD ENGINE (idéntico a v6.2)
# ============================================================

GENRE_VECTORS = {
    "metal":(0.90,-0.30,0.80,0.40),"heavy metal":(0.90,-0.30,0.90,0.35),
    "death metal":(1.00,-0.65,1.00,0.20),"black metal":(0.90,-0.75,1.00,0.55),
    "doom metal":(0.35,-0.65,0.70,0.85),"stoner metal":(0.50,-0.20,0.50,0.70),
    "power metal":(0.90,0.40,0.60,0.55),"progressive metal":(0.70,0.00,0.50,0.90),
    "thrash metal":(1.00,-0.25,0.90,0.20),"speed metal":(1.00,-0.10,0.80,0.15),
    "folk metal":(0.70,0.25,0.40,0.75),"symphonic metal":(0.70,0.10,0.50,0.85),
    "gothic metal":(0.50,-0.60,0.65,0.80),"metalcore":(0.90,-0.20,0.85,0.30),
    "post-metal":(0.55,-0.15,0.45,0.90),"nu metal":(0.80,-0.30,0.70,0.40),
    "rock":(0.70,0.10,0.40,0.40),"classic rock":(0.60,0.20,0.30,0.55),
    "hard rock":(0.80,0.00,0.60,0.30),"indie rock":(0.50,0.20,0.20,0.65),
    "alternative rock":(0.60,0.00,0.35,0.60),"punk rock":(0.90,0.10,0.60,0.20),
    "punk":(0.90,0.00,0.60,0.15),"grunge":(0.70,-0.35,0.55,0.65),
    "post-rock":(0.50,0.00,0.30,0.95),"shoegaze":(0.40,0.00,0.25,0.85),
    "psychedelic rock":(0.50,0.10,0.25,0.85),"progressive rock":(0.50,0.10,0.20,0.95),
    "garage rock":(0.80,0.10,0.40,0.20),"math rock":(0.60,0.05,0.30,0.80),
    "emo":(0.50,-0.35,0.35,0.75),"post-punk":(0.55,-0.20,0.45,0.75),
    "new wave":(0.65,0.20,0.25,0.50),"glam rock":(0.75,0.35,0.20,0.25),
    "electronic":(0.60,0.20,0.20,0.40),"techno":(0.90,0.00,0.55,0.10),
    "house":(0.80,0.50,0.20,0.10),"deep house":(0.60,0.30,0.10,0.45),
    "trance":(0.80,0.40,0.30,0.35),"psytrance":(0.90,0.20,0.40,0.50),
    "drum and bass":(0.90,0.10,0.55,0.20),"dubstep":(0.80,-0.10,0.65,0.25),
    "ambient":(0.10,0.20,0.00,0.85),"dark ambient":(0.10,-0.40,0.40,0.90),
    "idm":(0.55,0.00,0.30,0.90),"synthwave":(0.65,0.30,0.25,0.65),
    "darkwave":(0.40,-0.45,0.55,0.75),"industrial":(0.80,-0.45,0.85,0.45),
    "noise":(0.90,-0.55,0.90,0.35),"edm":(0.85,0.50,0.20,0.05),
    "vaporwave":(0.20,0.10,0.10,0.70),"lo-fi":(0.20,0.15,0.05,0.70),
    "jazz":(0.40,0.30,0.10,0.85),"bebop":(0.70,0.20,0.20,0.85),
    "cool jazz":(0.30,0.30,0.00,0.75),"free jazz":(0.65,-0.10,0.35,0.90),
    "jazz fusion":(0.65,0.20,0.20,0.85),"smooth jazz":(0.30,0.35,0.00,0.60),
    "blues":(0.40,-0.25,0.30,0.85),"delta blues":(0.35,-0.35,0.35,0.90),
    "soul blues":(0.50,0.10,0.20,0.75),"swing":(0.60,0.40,0.10,0.60),
    "classical":(0.30,0.10,0.15,1.00),"baroque":(0.50,0.20,0.10,0.90),
    "romantic":(0.40,0.25,0.20,0.95),"opera":(0.60,0.00,0.45,0.95),
    "modern classical":(0.30,0.00,0.35,1.00),"film score":(0.60,0.10,0.55,0.85),
    "neoclassical":(0.35,0.15,0.20,0.95),
    "pop":(0.60,0.60,0.10,0.10),"indie pop":(0.45,0.40,0.10,0.55),
    "synth-pop":(0.65,0.40,0.15,0.35),"k-pop":(0.75,0.60,0.10,0.10),
    "dream pop":(0.30,0.40,0.05,0.65),"art pop":(0.40,0.20,0.10,0.80),
    "r&b":(0.55,0.45,0.10,0.45),"neo soul":(0.45,0.40,0.10,0.65),
    "soul":(0.50,0.45,0.10,0.75),"funk":(0.85,0.60,0.10,0.20),
    "disco":(0.85,0.70,0.05,0.05),"gospel":(0.60,0.70,0.10,0.70),
    "latin":(0.70,0.50,0.20,0.35),"salsa":(0.90,0.60,0.20,0.20),
    "reggaeton":(0.80,0.50,0.20,0.10),"bossa nova":(0.30,0.55,0.00,0.65),
    "mpb":(0.40,0.45,0.10,0.70),"samba":(0.85,0.65,0.10,0.30),
    "flamenco":(0.70,0.00,0.55,0.85),"tango":(0.60,0.00,0.55,0.85),
    "folklore":(0.40,0.25,0.20,0.85),"cumbia":(0.75,0.55,0.20,0.20),
    "bolero":(0.20,0.35,0.10,0.85),"nueva canción":(0.35,0.20,0.25,0.85),
    "bachata":(0.55,0.35,0.20,0.60),"mariachi":(0.65,0.35,0.25,0.60),
    "hip-hop":(0.70,0.20,0.45,0.55),"rap":(0.70,0.15,0.45,0.55),
    "trap":(0.70,-0.10,0.55,0.35),"conscious rap":(0.60,0.10,0.35,0.85),
    "reggae":(0.50,0.55,0.15,0.55),"ska":(0.85,0.55,0.20,0.10),
    "folk":(0.30,0.30,0.15,0.85),"country":(0.50,0.40,0.15,0.55),
    "americana":(0.40,0.35,0.15,0.65),"bluegrass":(0.65,0.40,0.20,0.65),
    "celtic":(0.55,0.30,0.25,0.80),"afrobeat":(0.80,0.55,0.15,0.35),
    "new age":(0.10,0.40,0.00,0.75),"world":(0.50,0.30,0.25,0.75),
}

MOOD_CENTROIDS = {
    "Eufórico":      dict(energy=0.95,valence=0.85,tension=0.15,depth=0.05),
    "Festivo":       dict(energy=0.85,valence=0.75,tension=0.10,depth=0.05),
    "Energético":    dict(energy=0.80,valence=0.40,tension=0.35,depth=0.15),
    "Épico":         dict(energy=0.75,valence=0.10,tension=0.60,depth=0.75),
    "Agresivo":      dict(energy=0.95,valence=-0.45,tension=0.90,depth=0.20),
    "Tenso":         dict(energy=0.60,valence=-0.20,tension=0.85,depth=0.40),
    "Oscuro":        dict(energy=0.45,valence=-0.55,tension=0.70,depth=0.65),
    "Melancólico":   dict(energy=0.20,valence=-0.35,tension=0.20,depth=0.80),
    "Nostálgico":    dict(energy=0.25,valence=0.10,tension=0.10,depth=0.85),
    "Introspectivo": dict(energy=0.15,valence=0.00,tension=0.10,depth=0.95),
    "Contemplativo": dict(energy=0.20,valence=0.15,tension=0.05,depth=1.00),
    "Espiritual":    dict(energy=0.25,valence=0.45,tension=0.05,depth=0.95),
    "Romántico":     dict(energy=0.30,valence=0.75,tension=0.10,depth=0.65),
    "Sensual":       dict(energy=0.45,valence=0.55,tension=0.20,depth=0.55),
    "Relajado":      dict(energy=0.10,valence=0.35,tension=0.00,depth=0.55),
    "Sereno":        dict(energy=0.15,valence=0.30,tension=0.00,depth=0.70),
    "Esperanzador":  dict(energy=0.40,valence=0.65,tension=0.10,depth=0.55),
    "Apasionado":    dict(energy=0.65,valence=0.45,tension=0.35,depth=0.55),
    "Rebelde":       dict(energy=0.85,valence=0.05,tension=0.55,depth=0.30),
    "Dramático":     dict(energy=0.60,valence=-0.10,tension=0.65,depth=0.75),
    "Humorístico":   dict(energy=0.65,valence=0.70,tension=0.05,depth=0.05),
    "Neutral":       dict(energy=0.50,valence=0.00,tension=0.00,depth=0.00),
}

# ============================================================
# LOG HTML
# ============================================================

CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;background:#0f1117;color:#c9d1d9;margin:0;padding:20px}
h1{color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:10px;margin-bottom:20px}
.summary{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:24px}
.summary h2{color:#58a6ff;margin-top:0;font-size:16px}
.stat{display:inline-block;background:#21262d;border-radius:6px;padding:8px 16px;margin:4px;font-size:13px;vertical-align:top}
.stat .val{font-size:22px;font-weight:bold;display:block;color:#f0f6fc}
.stat .lbl{color:#8b949e;font-size:11px}
.badge{border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;display:inline-block}
.b-gold{background:#3d2b00;color:#d4a017}.b-silver{background:#1e2a1e;color:#a8a9ad}
.b-bronze{background:#2a1a00;color:#cd7f32}.b-review{background:#2d0f0f;color:#f85149}
.mood-label{background:#1f3a5f;color:#79c0ff;border-radius:4px;padding:3px 10px;font-size:13px;font-weight:600}
.track{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px;margin-bottom:14px}
.track-header{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}
.track-title{font-size:15px;font-weight:600;color:#f0f6fc}
.track-file{font-size:11px;color:#8b949e;word-break:break-all;margin-bottom:8px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.section{background:#0d1117;border-radius:6px;padding:10px}
.section h4{margin:0 0 6px;color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.kv{display:flex;justify-content:space-between;font-size:12px;padding:2px 0;border-bottom:1px solid #1c2128;gap:8px}
.kv .k{color:#8b949e;white-space:nowrap;flex-shrink:0}.kv .v{color:#c9d1d9;text-align:right;word-break:break-word}
.wrote{color:#3fb950!important}.skipped{color:#8b949e!important;font-style:italic}
.updated{color:#58a6ff!important}.errv{color:#f85149!important}
.vector-bar{height:8px;border-radius:4px;background:#21262d;margin:2px 0;overflow:hidden;position:relative}
.vector-fill{height:100%;border-radius:4px;position:absolute}
.bar-e{background:#f85149}.bar-vp{background:#3fb950}.bar-vn{background:#d4a017}
.bar-t{background:#ff9a00}.bar-d{background:#79c0ff}
table.sims{width:100%;border-collapse:collapse;font-size:12px}
table.sims td{padding:2px 4px;border-bottom:1px solid #1c2128}
.sim-fill{height:9px;border-radius:3px;background:#1f3a5f;display:inline-block}
.lrc-ok{color:#3fb950}.lrc-plain{color:#e3b341}.lrc-none{color:#6e7681}
details{margin-top:8px}
details summary{cursor:pointer;color:#58a6ff;font-size:12px;padding:4px 0;user-select:none}
details summary:hover{color:#79c0ff}
.lrc-viewer{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px;
  font-family:'Courier New',monospace;font-size:11px;max-height:300px;overflow-y:auto;
  color:#8b949e;white-space:pre-wrap;line-height:1.6;margin-top:6px}
.lrc-viewer .ts{color:#3fb950}.lrc-viewer .txt{color:#c9d1d9}
table.all-tags{width:100%;border-collapse:collapse;font-size:11px}
table.all-tags td{padding:2px 6px;border-bottom:1px solid #1c2128;vertical-align:top}
table.all-tags .tk{color:#8b949e;white-space:nowrap;width:35%}
table.all-tags .tv{color:#c9d1d9;word-break:break-word;max-width:0}
@media(max-width:800px){.grid-3,.grid-2{grid-template-columns:1fr}}
"""

# ============================================================
# FUNCIONES QUE UTILIZAN LOS DICCIONARIOS (RESTAURO TOTAL)
# ============================================================

def analizar_letras(lyrics: str) -> dict:
    if not lyrics:
        return {"energy":0.0,"valence":0.0,"tension":0.0,"depth":0.0,"lang":None,"lyric_theme":None}
    text = lyrics.lower()
    lang = None
    if LANGDETECT_OK:
        # detect() alone returns only the single top guess with no confidence
        # info — a low-confidence guess (e.g. a short chorus, a few repeated
        # non-lexical words like "la la la" / "oh oh") was being accepted as
        # fact just as readily as a clear-cut match. detect_langs() exposes
        # per-language probabilities so a shaky guess can be rejected instead
        # of silently mistagging the track.
        sample = lyrics[:2000].strip()  # more context than the old 600 chars
        if len(sample) < 40:
            lang = "en"  # too little text for any detector to be reliable
        else:
            try:
                candidates = _langdetect_langs(sample)
                top = candidates[0]
                runner_up_prob = candidates[1].prob if len(candidates) > 1 else 0.0
                # Require both a high absolute probability AND a clear margin
                # over the second-place guess — this is what actually filters
                # out the "Paris in the title" / short-snippet false positives
                # (those tend to land at ~0.3-0.6 with a close runner-up).
                if top.prob >= 0.90 and (top.prob - runner_up_prob) >= 0.15:
                    lang = top.lang
                else:
                    lang = "en"
            except Exception:
                lang = "en"
    else:
        if any(w in text for w in {"amor","corazón","alma","fuego","muerte"}): lang = "es"
        elif any(w in text for w in {"liebe","nacht","traum","seele"}): lang = "de"
        elif any(w in text for w in {"amour","nuit","rêve","âme"}): lang = "fr"
        else: lang = "en"

    scores = {"energy":0.0,"valence":0.0,"tension":0.0,"depth":0.0}
    # Utiliza LYRIC_LEXICON (Asegúrate de haberlo pegado arriba)
    for word, langs, dim, delta in LYRIC_LEXICON:
        if lang in langs or "en" in langs:
            count = min(text.count(word), 3)
            if count: scores[dim] += delta * count
    for k in scores:
        scores[k] = max(-1.0, min(1.0, scores[k]))

    # Utiliza LYRIC_THEMES (Asegúrate de haberlo pegado arriba)
    theme_scores = {t: sum(1 for w in words if w in text) for t, words in LYRIC_THEMES.items()}
    best_theme   = max(theme_scores, key=theme_scores.get)
    scores["lyric_theme"] = best_theme if theme_scores[best_theme] > 0 else None
    scores["lang"] = lang
    return scores

def ajuste_contexto(year):
    delta = {"energy":0.0,"valence":0.0,"tension":0.0,"depth":0.0}
    if not year: return delta
    for r, adj in ERA_MAP.items(): # Utiliza ERA_MAP
        if year in r:
            for k,v in adj.items(): delta[k] = v
            break
    return delta

def get_era_style(year):
    if not year: return None
    for r, s in ERA_STYLE_MAP.items(): # Utiliza ERA_STYLE_MAP
        if year in r: return s
    return None

def get_harmonic_key(ab):
    if not ab or not ab.get("key"): return None
    return CAMELOT_MAP.get((ab["key"], ab.get("mode","major"))) # Utiliza CAMELOT_MAP

def get_time_of_day(vector, ab):
    e = vector.get("energy",0.5); v = vector.get("valence",0.0)
    d = vector.get("depth",0.0);  t = vector.get("tension",0.0)
    bpm = (ab or {}).get("bpm",0)
    if e < 0.20 and v >= -0.10 and d > 0.40: return "sleep"
    if e < 0.45 and v > 0.25 and d > 0.30 and t < 0.30: return "morning"
    if e < 0.40 and d > 0.65 and abs(v) < 0.30: return "focus"
    if e > 0.75 and (bpm > 130 or bpm == 0): return "workout"
    if e > 0.65 and v > 0.40: return "party"
    if 0.35 < e < 0.70 and d > 0.35: return "evening"
    if t > 0.50 or (d > 0.60 and v < 0.10): return "night"
    return "anytime"

def get_energy_arc(lyrics, vector):
    if not lyrics or len(lyrics.strip()) < 50:
        e = vector.get("energy",0.5); t = vector.get("tension",0.0)
        if e > 0.75 and t > 0.60: return "steady_high"
        if e < 0.30: return "steady_low"
        return "steady"
    lines  = [l.strip() for l in lyrics.splitlines() if l.strip()]
    thirds = max(1, len(lines)//3)
    parts  = [" ".join(lines[:thirds]).lower(),
               " ".join(lines[thirds:2*thirds]).lower(),
               " ".join(lines[2*thirds:]).lower()]
    high_e = {"fight","fire","burn","rage","scream","storm","explode","power","rush",
              "lucha","fuego","rabia","feuer","wut","combat"}
    low_e  = {"sleep","calm","quiet","whisper","still","slow","gentle","fade",
              "dormir","susurro","silencio","schlafen","stille"}
    def _s(t): return sum(1 for w in high_e if w in t) - sum(1 for w in low_e if w in t)
    s = [_s(p) for p in parts]
    if s[0] < s[1] > s[2]: return "peak"
    if s[0] > s[1] < s[2]: return "valley"
    if s[2] > s[0]: return "rising"
    if s[2] < s[0]: return "falling"
    return "steady"

def get_danceability_score(vector, ab):
    e   = vector.get("energy",0.5)
    v   = max(0.0, vector.get("valence",0.0))
    bpm = (ab or {}).get("bpm",0)
    bpm_score = max(0.0, min(1.0, 1.0 - abs(bpm-125)/125.0)) if bpm > 0 else 0.5
    score = e*0.40 + v*0.25 + bpm_score*0.35
    if ab and ab.get("danceability") == "danceable":
        score = min(score + 0.10, 1.0)
    return round(score*100)

def get_mood_confidence_tier(conf):
    if conf >= 0.85: return "gold"
    if conf >= 0.70: return "silver"
    if conf >= 0.60: return "bronze"
    return "review"

def _vector_from_genres(genre_tags):
    vec = {"energy":0.0,"valence":0.0,"tension":0.0,"depth":0.0}
    total = 0.0
    for tag, count in genre_tags:
        tl = tag.lower()
        matched, best_len = None, 0
        for gk, gv in GENRE_VECTORS.items(): # Utiliza GENRE_VECTORS
            if gk in tl or tl in gk:
                if len(gk) > best_len: matched, best_len = gv, len(gk)
        if matched:
            w = math.log1p(count)
            for i, k in enumerate(["energy","valence","tension","depth"]):
                vec[k] += matched[i] * w
            total += w
    if total > 0:
        for k in vec: vec[k] /= total
    return vec

def _vector_from_ab(ab):
    vec = {"energy":0.0,"valence":0.0,"tension":0.0,"depth":0.0}
    if not ab: return vec
    bpm = ab.get("bpm", 120)
    bpm_factor = max(0.30, bpm/BALLAD_BPM) if 0 < bpm < BALLAD_BPM else 1.0
    vec["energy"] = (min(bpm/200.0,1.0)*0.35*bpm_factor +
                     float(ab.get("mood_aggressive",0))*0.35 +
                     float(ab.get("mood_party",0))*0.30)
    vec["valence"]= (float(ab.get("mood_happy",0))*0.50 -
                     float(ab.get("mood_sad",0))*0.50 +
                     float(ab.get("mood_relaxed",0))*0.15)
    vec["tension"]= float(ab.get("mood_aggressive",0))*0.75
    vec["depth"]  = (float(ab.get("mood_acoustic",0))*0.40 +
                     (0.25 if ab.get("instrumental") else 0.0) +
                     (0.30 if ab.get("mode","") == "minor" else 0.0))
    return vec

def _cosine(a, b):
    dims = ["energy","valence","tension","depth"]
    dot  = sum(a.get(d,0)*b.get(d,0) for d in dims)
    na   = math.sqrt(sum(a.get(d,0)**2 for d in dims))
    nb   = math.sqrt(sum(b.get(d,0)**2 for d in dims))
    return dot/(na*nb) if na and nb else 0.0

def mood_engine(genre_tags, ab_features, lyric_delta, context_delta, mb_recording=None):
    BW = {"genres":0.30,"audio":0.30,"lyrics":0.30,"context":0.10}
    has = {"genres":bool(genre_tags),"audio":bool(ab_features),
           "lyrics":bool(lyric_delta.get("lang")),"context":True,
           "mb":bool(mb_recording)}
    miss  = sum(BW[k] for k,v in has.items() if not v and k in BW)
    act   = [k for k,v in has.items() if v and k in BW]
    bonus = miss/len(act) if act else 0
    W     = {k:(BW[k]+bonus if has[k] else 0.0) for k in BW}
    vg, va = _vector_from_genres(genre_tags), _vector_from_ab(ab_features)
    final  = {d: vg.get(d,0)*W["genres"] + va.get(d,0)*W["audio"] +
                 lyric_delta.get(d,0)*W["lyrics"] + context_delta.get(d,0)*W["context"]
              for d in ["energy","valence","tension","depth"]}
    sims  = {name: round(_cosine(final, c), 3) for name, c in MOOD_CENTROIDS.items()} # Utiliza MOOD_CENTROIDS
    best  = max(sims, key=sims.get)
    sf    = (1.0*int(has["genres"]) + 1.2*int(has["audio"]) +
             1.1*int(has["lyrics"]) + 0.3*int(has["context"])) / 3.6
    conf  = round(min(max(sims[best]*(0.40+0.60*sf), 0.0), 1.0), 3)
    return best, conf, {
        "vector":      {k:round(v,3) for k,v in final.items()},
        "similarities":dict(sorted(sims.items(),key=lambda x:-x[1])[:5]),
        "weights_used":{k:round(v,3) for k,v in W.items()},
        "sources":     has,
    }

# ============================================================
# ESCRITURA DE TAGS (RESTAURO TOTAL)
# ============================================================

def escribir_tags(path, existing_tags, mb_details, genres,
                  genre_primary, genre_secondary,
                  mood, confidence, vector, ab_features, lrc_result,
                  mx_features) -> dict:
    touched = {}
    try:
        if path.endswith(".mp3"):
            touched = _write_mp3(path, existing_tags, mb_details, genres,
                                 genre_primary, genre_secondary,
                                 mood, confidence, vector, ab_features,
                                 lrc_result, mx_features)
        elif path.endswith(".flac"):
            touched = _write_flac(path, existing_tags, mb_details, genres,
                                  genre_primary, genre_secondary,
                                  mood, confidence, vector, ab_features,
                                  lrc_result, mx_features)
        elif path.endswith(".dsf"):
            if HAS_DSF:
                touched = _write_dsf(path, existing_tags, mb_details, genres,
                                     genre_primary, genre_secondary,
                                     mood, confidence, vector, ab_features,
                                     lrc_result, mx_features)
            else:
                touched["format"] = {"action":"skipped","value":"mutagen.dsf no disponible"}
        elif path.endswith(".dff"):
            touched["format"] = {"action":"skipped","value":"DFF no soporta tags editables"}
    except Exception as e:
        log.error("escribir_tags [%s]: %s", path, e)
        touched["_error"] = {"action":"error","value":str(e)}
    return touched

def _note(touched, field, action, value=""):
    touched[field] = {"action": action, "value": str(value)[:200]}

def _write_mp3(path, existing, mb_details, genres, genre_primary, genre_secondary, mood, conf, vector, ab, lrc, mx):
    touched = {}
    std = existing.get("standard", {})
    easy = EasyID3(path)

    def _set_if_empty(key, value):
        if value and _tag_empty(std.get(key,"")):
            easy[key] = [str(value)]; _note(touched, key, "wrote", value)
        elif value:
            _note(touched, key, "skipped_exists", std.get(key,""))

    if mb_details:
        _set_if_empty("album",       mb_details.get("album"))
        _set_if_empty("date",        mb_details.get("date"))
        _set_if_empty("tracknumber", mb_details.get("tracknumber"))
        _set_if_empty("discnumber",  mb_details.get("discnumber"))
        _set_if_empty("organization",mb_details.get("label"))
    if ab and ab.get("bpm"):
        _set_if_empty("bpm", str(ab["bpm"]))
    if genre_primary:
        easy["genre"] = [genre_primary]
        _note(touched, "genre", "updated", genre_primary)
    if genre_secondary:
        easy["genre_secondary"] = genre_secondary[:3]
        _note(touched, "genre_secondary", "updated", ", ".join(genre_secondary[:3]))
    elif genres and not genre_primary:
        easy["genre"] = genres[:1]
        _note(touched, "genre", "updated", genres[0])
    easy.save()

    id3 = ID3(path)
    id3.delall("COMM:Mood:eng"); id3.delall("COMM:MoodConf:eng")
    id3.add(COMM(encoding=3, lang="eng", desc="Mood",     text=mood))
    id3.add(COMM(encoding=3, lang="eng", desc="MoodConf", text=f"{conf:.2f}"))
    id3.add(TXXX(encoding=3, desc="MoodVector",           text=json.dumps(vector)))
    _note(touched, "mood", "updated", mood)
    _note(touched, "mood_confidence", "updated", f"{conf:.2f}")

    if ab and ab.get("key"):
        ks = f"{ab['key']} {ab.get('mode','')}".strip()
        id3.add(TXXX(encoding=3, desc="InitialKey", text=ks))
        _note(touched, "initialkey", "wrote", ks)

    if mb_details and mb_details.get("isrc") and not id3.getall("TSRC"):
        id3.add(TSRC(encoding=3, text=mb_details["isrc"]))
        _note(touched, "isrc", "wrote", mb_details["isrc"])

    for fx_k, fx_v in (mx or {}).items():
        if fx_v is not None:
            id3.add(TXXX(encoding=3, desc=f"MX_{fx_k.upper()}", text=str(fx_v)))

    has_sylt = existing.get("has_synced_lrc", False)
    has_uslt = existing.get("has_plain_lrc",  False)

    if lrc.get("synced") and not has_sylt:
        sylt_data = _lrc_to_sylt(lrc["synced"])
        if sylt_data:
            id3.add(SYLT(encoding=Encoding.UTF8, lang="eng", format=2, type=1, desc="", text=sylt_data))
            _note(touched, "lyrics_synced", "wrote", f"LRCLib id={lrc.get('lrclib_id')} score={lrc.get('score',0):.2f}")
    elif lrc.get("synced"):
        _note(touched, "lyrics_synced", "skipped_exists", "SYLT ya presente")

    if lrc.get("plain") and not has_uslt:
        id3.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=lrc["plain"]))
        _note(touched, "lyrics_plain", "wrote", f"LRCLib id={lrc.get('lrclib_id')} score={lrc.get('score',0):.2f}")
    elif lrc.get("plain"):
        _note(touched, "lyrics_plain", "skipped_exists", "USLT ya presente")

    id3.save()
    return touched


def _write_dsf(path, existing, mb_details, genres, genre_primary, genre_secondary,
               mood, conf, vector, ab, lrc, mx):
    """
    Escritura de tags en archivos DSF (DSD Stream File).
    DSF embebe un bloque ID3 — mismo formato que MP3 pero accedido
    vía mutagen.dsf.DSF. DFF (DSDIFF) no soporta tags editables.
    """
    touched = {}
    std = existing.get("standard", {})

    dsf_obj = DSFFile(path)
    if dsf_obj.tags is None:
        dsf_obj.add_tags()
    id3 = dsf_obj.tags

    def _set_if_empty(easy_key, id3_cls, frame_key, value):
        if not value: return
        if _tag_empty(std.get(easy_key, "")):
            id3.delall(frame_key)
            id3.add(id3_cls(encoding=3, text=[str(value)]))
            _note(touched, easy_key, "wrote", value)
        else:
            _note(touched, easy_key, "skipped_exists", std.get(easy_key, ""))

    # MusicBrainz metadata
    if mb_details:
        _set_if_empty("album",        TALB, "TALB", mb_details.get("album"))
        _set_if_empty("date",         TDRC, "TDRC", mb_details.get("date"))
        _set_if_empty("tracknumber",  TRCK, "TRCK", mb_details.get("tracknumber"))
        _set_if_empty("discnumber",   TPOS, "TPOS", mb_details.get("discnumber"))
        _set_if_empty("organization", TPUB, "TPUB", mb_details.get("label"))
        if mb_details.get("isrc") and not id3.getall("TSRC"):
            id3.add(TSRC(encoding=3, text=[mb_details["isrc"]]))
            _note(touched, "isrc", "wrote", mb_details["isrc"])

    # BPM e Initial Key
    if ab:
        if ab.get("bpm"):
            _set_if_empty("bpm", TBPM, "TBPM", str(ab["bpm"]))
        if ab.get("key"):
            ks = f"{ab['key']} {ab.get('mode','')}" .strip()
            id3.add(TXXX(encoding=3, desc="InitialKey", text=ks))
            _note(touched, "initialkey", "wrote", ks)

    # Género
    if genre_primary:
        id3.delall("TCON")
        id3.add(TCON(encoding=3, text=[genre_primary]))
        _note(touched, "genre", "updated", genre_primary)
    if genre_secondary:
        id3.add(TXXX(encoding=3, desc="genre_secondary",
                     text=";".join(genre_secondary[:3])))
        _note(touched, "genre_secondary", "updated", ", ".join(genre_secondary[:3]))
    elif genres and not genre_primary:
        id3.delall("TCON")
        id3.add(TCON(encoding=3, text=[genres[0]]))
        _note(touched, "genre", "updated", genres[0])

    # Mood + Vector
    id3.delall("COMM:Mood:eng")
    id3.delall("COMM:MoodConf:eng")
    id3.add(COMM(encoding=3, lang="eng", desc="Mood",     text=mood))
    id3.add(COMM(encoding=3, lang="eng", desc="MoodConf", text=f"{conf:.2f}"))
    id3.add(TXXX(encoding=3, desc="MoodVector",           text=json.dumps(vector)))
    _note(touched, "mood",            "updated", mood)
    _note(touched, "mood_confidence", "updated", f"{conf:.2f}")

    # MusicExplorer features
    for fx_k, fx_v in (mx or {}).items():
        if fx_v is not None:
            id3.add(TXXX(encoding=3, desc=f"MX_{fx_k.upper()}", text=str(fx_v)))

    # Letras sincronizadas (SYLT)
    has_sylt = existing.get("has_synced_lrc", False)
    has_uslt = existing.get("has_plain_lrc",  False)

    if lrc.get("synced") and not has_sylt:
        sylt_data = _lrc_to_sylt(lrc["synced"])
        if sylt_data:
            id3.add(SYLT(encoding=Encoding.UTF8, lang="eng",
                         format=2, type=1, desc="", text=sylt_data))
            _note(touched, "lyrics_synced", "wrote",
                  f"LRCLib id={lrc.get('lrclib_id')} score={lrc.get('score',0):.2f}")
    elif lrc.get("synced"):
        _note(touched, "lyrics_synced", "skipped_exists", "SYLT ya presente")

    if lrc.get("plain") and not has_uslt:
        id3.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=lrc["plain"]))
        _note(touched, "lyrics_plain", "wrote",
              f"LRCLib id={lrc.get('lrclib_id')} score={lrc.get('score',0):.2f}")
    elif lrc.get("plain"):
        _note(touched, "lyrics_plain", "skipped_exists", "USLT ya presente")

    dsf_obj.save()
    return touched

def _write_flac(path, existing, mb_details, genres, genre_primary, genre_secondary, mood, conf, vector, ab, lrc, mx):
    touched = {}
    std   = existing.get("standard", {})
    audio = FLAC(path)

    def _set_if_empty(key, value):
        if value and _tag_empty(std.get(key,"")):
            audio[key] = [str(value)]; _note(touched, key, "wrote", value)
        elif value:
            _note(touched, key, "skipped_exists", std.get(key,""))

    if mb_details:
        _set_if_empty("album",       mb_details.get("album"))
        _set_if_empty("date",        mb_details.get("date"))
        _set_if_empty("tracknumber", mb_details.get("tracknumber"))
        _set_if_empty("discnumber",  mb_details.get("discnumber"))
        _set_if_empty("organization",mb_details.get("label"))
        _set_if_empty("isrc",        mb_details.get("isrc"))
    if ab:
        if ab.get("bpm") and _tag_empty(std.get("bpm","")):
            audio["bpm"] = [str(ab["bpm"])]; _note(touched,"bpm","wrote",ab["bpm"])
        if ab.get("key"):
            ks = f"{ab['key']} {ab.get('mode','')}".strip()
            audio["initialkey"] = [ks]; _note(touched,"initialkey","wrote",ks)
    if genre_primary:
        audio["genre"] = [genre_primary]
        _note(touched, "genre", "updated", genre_primary)
    if genre_secondary:
        audio["genre_secondary"] = genre_secondary[:3]
        _note(touched, "genre_secondary", "updated", ", ".join(genre_secondary[:3]))
    elif genres and not genre_primary:
        audio["genre"] = genres[:1]
        _note(touched, "genre", "updated", genres[0])

    audio["mood"]            = [mood]
    audio["mood_confidence"] = [f"{conf:.2f}"]
    audio["mood_vector"]     = [json.dumps(vector)]
    _note(touched,"mood","updated",mood)
    _note(touched,"mood_confidence","updated",f"{conf:.2f}")

    for fx_k, fx_v in (mx or {}).items():
        if fx_v is not None:
            audio[f"mx_{fx_k}"] = [str(fx_v)]

    has_sync  = existing.get("has_synced_lrc", False)
    has_plain = existing.get("has_plain_lrc",  False)

    if lrc.get("plain") and not has_plain:
        audio["lyrics"] = [lrc["plain"]]
        _note(touched,"lyrics_plain","wrote",f"LRCLib score={lrc.get('score',0):.2f}")
    elif lrc.get("plain"):
        _note(touched,"lyrics_plain","skipped_exists","ya existía")

    if lrc.get("synced") and not has_sync:
        audio["syncedlyrics"] = [lrc["synced"]]
        _note(touched,"lyrics_synced","wrote",f"LRCLib score={lrc.get('score',0):.2f}")
    elif lrc.get("synced"):
        _note(touched,"lyrics_synced","skipped_exists","ya existía")

    audio.save()
    return touched

# ============================================================
# RENDERIZADORES HTML (RESTAURO TOTAL)
# ============================================================

def _bar(value, css_cls, label):
    v   = max(-1.0, min(1.0, value))
    if css_cls in ("bar-vp","bar-vn"):
        if v >= 0:
            pct, left, cls = round(v*50), 50, "bar-vp"
        else:
            pct, left, cls = round(abs(v)*50), round((1+v)*50), "bar-vn"
    else:
        pct, left, cls = round(max(0,v)*100), 0, css_cls
    return (f'<div class="kv"><span class="k">{label}</span>'
            f'<span style="width:55%"><div class="vector-bar">'
            f'<div class="vector-fill {cls}" style="left:{left}%;width:{pct}%"></div>'
            f'</div><span style="font-size:10px;color:#8b949e">{value:+.2f}</span>'
            f'</span></div>')

def _kv(k, v, cls=""):
    c = f' class="{cls}"' if cls else ""
    return f'<div class="kv"><span class="k">{k}</span><span class="v"{c}>{v}</span></div>'

def _ac(action):
    if "wrote"   in action: return "wrote"
    if "updated" in action: return "updated"
    if "skipped" in action: return "skipped"
    if "error"   in action: return "errv"
    return ""

def _lrc_html(lrc_text: Optional[str]) -> str:
    if not lrc_text: return '<div class="lrc-viewer">Sin letras</div>'
    pattern = re.compile(r"(\[\d{1,3}:\d{2}\.\d{2,3}\])(.*)")
    lines = []
    for line in lrc_text.splitlines():
        m = pattern.match(line)
        if m: lines.append(f'<span class="ts">{m.group(1)}</span><span class="txt">{m.group(2)}</span>')
        else: lines.append(f'<span class="txt">{line}</span>')
    return '<div class="lrc-viewer">' + "\n".join(lines) + '</div>'

def render_track_html(r: dict) -> str:
    tier     = r.get("mood_confidence_tier","review")
    mood     = r.get("mood","—")
    conf     = r.get("confidence",0)
    artist   = r.get("artist","?")
    title    = r.get("title","?")
    fpath    = r.get("file","")
    vec      = r.get("vector",{})
    sims     = r.get("top_moods",{})
    features = r.get("musicexplorer_features",{})
    touched  = r.get("tags_touched",{})
    sources  = r.get("sources",{})
    mb_det   = r.get("mb_details") or {}
    all_tags = r.get("all_tags",{})
    existing = r.get("existing_tags",{})
    std      = existing.get("standard",{})
    sync_lrc = r.get("synced_lrc_text") or r.get("lrc_result",{}).get("synced")
    plain_lrc= r.get("plain_lrc_text")  or r.get("lrc_result",{}).get("plain")
    has_sync = r.get("has_synced_lrc_final", False)
    has_plain= r.get("has_plain_lrc_final",  False)

    lrc_badge = ('<span class="lrc-ok">♪ sincronizada</span>' if has_sync else ('<span class="lrc-plain">♩ plana</span>' if has_plain else '<span class="lrc-none">✗ sin letra</span>'))
    lrc_match = r.get("lrc_result",{})
    if lrc_match.get("track_name_match"):
        lrc_badge += f' <span style="color:#8b949e;font-size:11px">→ {lrc_match["track_name_match"]}</span>'

    html = f'''<div class="track"><div class="track-header"><span class="mood-label">{mood}</span><span class="badge b-{tier}">{tier.upper()} {conf:.2f}</span>{lrc_badge}</div><div class="track-title">{artist} — {title}</div><div class="track-file">{fpath}</div><div class="grid-3">'''

    html += '<div class="section"><h4>Vector Mood 4D</h4>'
    html += _bar(vec.get("energy",0),  "bar-e",  "Energy")
    html += _bar(vec.get("valence",0), "bar-vp", "Valence")
    html += _bar(vec.get("tension",0), "bar-t",  "Tension")
    html += _bar(vec.get("depth",0),   "bar-d",  "Depth")
    html += '<h4 style="margin-top:10px">Top Moods</h4><table class="sims">'
    for mn, ms in list(sims.items())[:5]:
        html += (f'<tr><td style="color:#c9d1d9;width:40%">{mn}</td>'
                 f'<td><div class="sim-fill" style="width:{round(ms*100)}px"></div></td>'
                 f'<td style="color:#8b949e;text-align:right">{ms:.3f}</td></tr>')
    html += '</table></div>'

    # ── Sección Géneros ──────────────────────────────────────────────────────
    genre_primary_r   = r.get("genre_primary", "")
    genre_secondary_r = r.get("genre_secondary", [])
    html += '<div class="section"><h4>Géneros</h4>'
    html += _kv("🎸 Género principal", genre_primary_r or "—")
    html += _kv("🏷 Otros géneros", ", ".join(genre_secondary_r) if genre_secondary_r else "—")
    html += '</div>'

    # ── Sección Features MusicExplorer ───────────────────────────────────────
    html += '<div class="section"><h4>Features MusicExplorer</h4>'
    fx_labels = {"time_of_day":"⏰ Momento","energy_arc":"📈 Arco energía","lyric_theme":"💬 Tema lírico","danceability_score":"💃 Bailabilidad","harmonic_key":"🎵 Camelot","decade":"📅 Década","era_style":"🎸 Era","mood_confidence_tier":"⭐ Tier","language":"🌐 Idioma","bpm":"🥁 BPM","key_mode":"🎼 Tonalidad"}
    for fk, fl in fx_labels.items():
        val = features.get(fk)
        html += _kv(fl, str(val) if val is not None else "—")
    html += '<h4 style="margin-top:8px">Fuentes</h4>'
    for sk, sl in [("genres","Last.fm"),("mb","MusicBrainz"),("lyrics","NLP letras"),("context","Contexto/Era")]:
        ok = sources.get(sk, False)
        html += f'<div class="kv"><span class="k">{sl}</span><span class="v {"wrote" if ok else "skipped"}">{"✓" if ok else "✗"}</span></div>'
    html += '</div>'

    html += '<div class="section"><h4>Metadata escrita</h4>'
    tag_labels = {"album":"Álbum","date":"Año","tracknumber":"Pista","discnumber":"Disco","organization":"Sello","bpm":"BPM","initialkey":"Key","isrc":"ISRC","genre":"Género principal","genre_secondary":"Otros géneros","mood":"Mood","mood_confidence":"Confianza mood","lyrics_plain":"Letra plana","lyrics_synced":"Letra sincronizada"}
    wrote_any = False
    for tk, tl in tag_labels.items():
        if tk in touched:
            wrote_any = True
            info   = touched[tk]
            action = info.get("action","")
            value  = info.get("value","")
            label  = {"wrote":"✚ escribió","updated":"↺ actualizó","skipped_exists":"— ya existía","error":"✗ error"}.get(action, action)
            html  += f'<div class="kv"><span class="k">{tl}</span><span class="v {_ac(action)}" title="{value}">{label}</span></div>'
    if not wrote_any: html += '<div style="color:#6e7681;font-size:11px;padding:4px">En cola de revisión — no se escribió</div>'
    html += '</div></div><div class="grid-2"><div class="section"><h4>Metadata completa del archivo</h4>'

    std_labels = {"artist":"Artista","title":"Título","album":"Álbum","albumartist":"Álbum artista","date":"Año","tracknumber":"Pista","discnumber":"Disco","genre":"Género","composer":"Compositor","bpm":"BPM","comment":"Comentario"}
    for k, l in std_labels.items():
        v = std.get(k,"")
        if v: html += _kv(l, v)

    if mb_det:
        html += '<h4 style="margin-top:8px">MusicBrainz</h4>'
        for mk, ml in [("album","Álbum MB"),("date","Año MB"),("label","Sello"),("country","País"),("catalog","Catálogo"),("isrc","ISRC"),("tracknumber","Pista MB"),("discnumber","Disco MB")]:
            if mb_det.get(mk): html += _kv(ml, mb_det[mk])
        if mb_det.get("release_mbid"):
            mbid_link = f'<a href="https://musicbrainz.org/release/{mb_det["release_mbid"]}" target="_blank" style="color:#58a6ff">ver en MB ↗</a>'
            html += _kv("Release", mbid_link)

    if all_tags:
        html += '<details><summary>Ver todos los tags raw del archivo</summary><table class="all-tags" style="margin-top:6px">'
        for k, v in sorted(all_tags.items()):
            if len(str(v)) > 0: html += f'<tr><td class="tk">{k}</td><td class="tv">{str(v)[:150]}</td></tr>'
        html += '</table></details>'

    html += '</div><div class="section"><h4>Letras</h4>'

    lrc_res = r.get("lrc_result",{})
    if lrc_res.get("lrclib_id"):
        html += _kv("LRCLib ID", str(lrc_res["lrclib_id"]))
        html += _kv("Score match", f"{lrc_res.get('score',0):.3f}")
    if lrc_res.get("track_name_match"): html += _kv("Match encontrado", lrc_res["track_name_match"])
    if lrc_res.get("album_match"): html += _kv("Álbum match", lrc_res["album_match"])
    if lrc_res.get("candidates_found") is not None: html += _kv("Candidatos LRCLib", str(lrc_res["candidates_found"]))

    if sync_lrc:
        html += '<details open><summary>▶ Letras sincronizadas (LRC)</summary>'
        html += _lrc_html(sync_lrc)
        html += '</details>'
    elif plain_lrc:
        html += '<details><summary>▶ Letras planas</summary>'
        html += f'<div class="lrc-viewer">{plain_lrc[:2000]}</div></details>'
    else: html += '<div style="color:#6e7681;font-size:12px;padding:8px">Sin letras disponibles</div>'

    html += '</div></div></div>'
    return html

def render_summary_html(audit, elapsed, base):
    ok    = sum(1 for r in audit if not r.get("review") and r["status"]=="ok")
    rev   = sum(1 for r in audit if r.get("review"))
    err   = sum(1 for r in audit if r["status"]!="ok")
    syn   = sum(1 for r in audit if r.get("has_synced_lrc_final"))
    plain = sum(1 for r in audit if r.get("has_plain_lrc_final") and not r.get("has_synced_lrc_final"))
    no_l  = len(audit) - syn - plain
    avg   = sum(r.get("confidence",0) for r in audit if r["status"]=="ok") / max(ok+rev,1)
    tiers = {"gold":0,"silver":0,"bronze":0,"review":0}
    for r in audit: tiers[r.get("mood_confidence_tier","review")] = tiers.get(r.get("mood_confidence_tier","review"),0)+1
    mood_d, tod_d = {}, {}
    for r in audit:
        if r.get("mood"): mood_d[r.get("mood")] = mood_d.get(r.get("mood"),0)+1
        tod = (r.get("musicexplorer_features") or {}).get("time_of_day","?")
        tod_d[tod] = tod_d.get(tod,0)+1

    # Utiliza CSS importado en zona diccionarios
    h  = '<div class="summary">'
    h += f'<h2>Resumen — {datetime.now().strftime("%d/%m/%Y %H:%M")}</h2>'
    h += f'<p style="color:#8b949e">📁 {base} &nbsp;|&nbsp; ⏱ {elapsed:.0f}s &nbsp;|&nbsp; {len(audit)} archivos</p><div>'
    for lbl, val, col in [("Escritos ✅",ok,"#3fb950"),("Revisión ⚑",rev,"#e3b341"),("Errores ✗",err,"#f85149"),("Confianza Ø",f"{avg:.2f}","#79c0ff"),("Sync ♪",syn,"#58a6ff"),("Plain ♩",plain,"#e3b341"),("Sin letra",no_l,"#6e7681")]:
        h += f'<div class="stat"><span class="val" style="color:{col}">{val}</span><span class="lbl">{lbl}</span></div>'
    h += '</div><div style="margin-top:10px">'
    for tier, count in tiers.items(): h += f'<span class="badge b-{tier}" style="margin:3px">{tier.upper()} {count}</span>'
    h += '</div><div style="margin-top:14px"><h3 style="color:#8b949e;font-size:12px;margin-bottom:6px">MOODS</h3><div style="display:flex;flex-wrap:wrap;gap:5px">'
    for m, c in sorted(mood_d.items(), key=lambda x:-x[1]): h += f'<span class="badge b-silver">{m} <span style="color:#f0f6fc">{c}</span></span>'
    h += '</div></div><div style="margin-top:10px"><h3 style="color:#8b949e;font-size:12px;margin-bottom:6px">MOMENTO DEL DÍA</h3><div style="display:flex;flex-wrap:wrap;gap:5px">'
    for tod, c in sorted(tod_d.items(), key=lambda x:-x[1]): h += f'<span class="badge b-silver">{tod} <span style="color:#f0f6fc">{c}</span></span>'
    h += '</div></div></div>'
    return h


# ============================================================
# ORQUESTADOR PRINCIPAL (RESTAURO TOTAL DE procesar)
# ============================================================

def procesar(path: str, audit_log: list) -> dict:
    result = {"file":path,"status":"ok","mood":None,"confidence":0.0, "review":False,"timestamp":datetime.now().isoformat()}

    existing = leer_tags_completo(path)
    if not existing:
        result["status"] = "error_tags"; return result

    std      = existing["standard"]
    artist   = _clean_artist(std["artist"])
    title    = _clean_title(std["title"])
    album    = std.get("album","")
    # Duración raw del archivo (mutagen). Para DSF/DFF (DSD), mutagen puede reportar
    # la duración total del stream (álbum completo), no de la pista individual.
    # Sanity check: fuera del rango 30s–1200s (20 min) → descartar como no fiable.
    # La duración real de la pista se recuperará desde MusicBrainz más abajo.
    _raw_dur = existing.get("duration")
    duration: Optional[float] = _raw_dur if (_raw_dur and 30 <= _raw_dur <= 1200) else None
    if _raw_dur and not duration:
        log.debug("Duración del archivo descartada (%.1fs — probable stream DSD completo)", _raw_dur)

    year = None
    if std.get("date"):
        m = re.match(r"(\d{4})", std["date"])
        year = int(m.group(1)) if m else None

    log.info("🎵 %s — %s", artist or "?", title or "?")
    if existing["has_synced_lrc"]: log.info("  ✓ Ya tiene letras sincronizadas")
    elif existing["has_plain_lrc"]: log.info("  ✓ Ya tiene letras planas")

    # MusicBrainz primero; huella + AcoustID solo como respaldo (faltan artista/título
    # en los tags, o MusicBrainz no encontró la grabación). La huella decodifica 2 min
    # de audio (lento en DSD) y su resultado no se guarda en Orbyte.
    mbid = None
    faltan_tags = _tag_empty(std["artist"]) or _tag_empty(std["title"])
    mb_recording = None if faltan_tags else buscar_musicbrainz(artist, title)
    if faltan_tags or not mb_recording:
        fp, dur = get_fingerprint(path)
        if fp and dur and dur > 0:
            ac = buscar_acoustid(fp, dur)
            if ac:
                antes = (artist, title)
                if ac.get("artist") and _tag_empty(std["artist"]): artist = ac["artist"]
                if ac.get("title")  and _tag_empty(std["title"]):  title  = ac["title"]
                mbid = ac.get("mbid")
                if not mb_recording and (faltan_tags or (artist, title) != antes):
                    mb_recording = buscar_musicbrainz(artist, title)
        if faltan_tags and not mb_recording:
            mb_recording = buscar_musicbrainz(artist, title)
    if mb_recording:
        credits = mb_recording.get("artist-credit",[])
        if credits and _tag_empty(std["artist"]): artist = credits[0].get("name", artist)
        if _tag_empty(std["title"]): title = mb_recording.get("title", title)
        if not mbid: mbid = mb_recording.get("id")
        log.info("  ✓ MusicBrainz: mbid=%s", mbid)
        # Preferir duración de MB (fiable, por pista) sobre la del archivo de audio.
        # Crítico para DSF/DFF donde mutagen devuelve duración del álbum completo.
        mb_dur = _mb_duration_s(mb_recording)
        if mb_dur:
            if duration is None:
                # Archivo no tiene duración confiable (DSD, stream, etc.) → usar MB
                log.debug("Duración desde MusicBrainz (fallback): %.1fs", mb_dur)
                duration = mb_dur
            else:
                # Archivo tiene duración válida → comparar con MB
                diff_pct = abs(mb_dur - duration) / duration
                if diff_pct > 0.30:
                    # MB difiere >30%: probablemente MB apuntó a versión diferente
                    # (radio edit, extended, live). Conservar duración del archivo.
                    log.debug("MB duration %.1fs difiere %.0f%% del archivo %.1fs — preferir archivo",
                              mb_dur, diff_pct * 100, duration)
                else:
                    log.debug("Duración MB: %.1fs | archivo: %.1fs (diff %.1fs)",
                              mb_dur, duration, abs(mb_dur - duration))

    mb_details = None
    if mbid:
        mb_details = obtener_mb_release_details(mbid)
        if mb_details and mb_details.get("date") and not year:
            try: year = int(mb_details["date"])
            except ValueError: pass

    lastfm_tags  = obtener_generos_lastfm(artist)
    mb_genres    = obtener_generos_mb(mb_recording)
    genre_primary, genre_secondary = consenso_generos(lastfm_tags, mb_genres)
    genres_final = ([genre_primary] + genre_secondary) if genre_primary else genre_secondary

    ab_features = obtener_ab_features(mbid)

    # Lyrics logic
    lrc_result = {"synced":None,"plain":None,"lrclib_id":None,"score":0.0,"candidates_found":0}
    if not existing["has_synced_lrc"]:
        _lrc_raw = buscar_lrclib(title=title, artist=artist,
                                  album=album or (mb_details.get("album") if mb_details else ""),
                                  duration_s=duration)
        # Si ya tenemos letras planas, solo aceptamos el resultado si trae sinc.
        # Esto evita sobreescribir plain con otro plain sin ganar nada.
        if existing["has_plain_lrc"] and not _lrc_raw.get("synced"):
            log.info("  · LRC: Tiene letras planas — resultado sin sincronizar descartado (se conserva existente)")
        else:
            lrc_result = _lrc_raw
            lrc_status = "synced" if lrc_result.get("synced") else ("plain" if lrc_result.get("plain") else "none")
            log.info("  · LRC Status: %s (Score: %.2f)", lrc_status, lrc_result.get("score",0))
    else:
        log.info("  · LRC: Archivo ya posee letras sincronizadas.")

    plain_for_nlp = (lrc_result.get("plain") or existing.get("plain_lrc_text",""))
    lyric_delta   = analizar_letras(plain_for_nlp or "")
    log.info("  · NLP: lang=%s valence=%.2f depth=%.2f theme=%s", lyric_delta.get("lang"), lyric_delta.get("valence",0), lyric_delta.get("depth",0), lyric_delta.get("lyric_theme"))

    ctx_delta = ajuste_contexto(year)
    mood, confidence, debug_info = mood_engine(lastfm_tags, ab_features, lyric_delta, ctx_delta, mb_recording=mb_recording)
    log.info("  ★ Mood: %s  (conf: %.2f)", mood, confidence)

    needs_review = confidence < CONFIDENCE_MIN
    tier = get_mood_confidence_tier(confidence)

    mx = {
        "time_of_day":        get_time_of_day(debug_info["vector"], ab_features),
        "energy_arc":         get_energy_arc(plain_for_nlp or "", debug_info["vector"]),
        "lyric_theme":        lyric_delta.get("lyric_theme"),
        "danceability_score": get_danceability_score(debug_info["vector"], ab_features),
        "harmonic_key":       get_harmonic_key(ab_features),
        "decade":             (year//10*10) if year else None,
        "era_style":          get_era_style(year),
        "mood_confidence_tier": tier,
        "language":           lyric_delta.get("lang"),
        "bpm":                ab_features.get("bpm") if ab_features else None,
        "key_mode":           f"{ab_features['key']} {ab_features.get('mode','')}" if ab_features and ab_features.get("key") else None,
    }

    # needs_review es solo una bandera de auditoría para el reporte HTML.
    # NO bloquea la escritura: género (Last.fm), metadata (MusicBrainz) y letras
    # son independientes de la confianza del mood y siempre se escriben.
    # El tier "review" indica al usuario qué moods necesitan revisión manual.
    if needs_review:
        log.info("  ⚑ Mood en revisión (conf %.2f < %.2f) — se escribe todo igual", confidence, CONFIDENCE_MIN)

    tags_touched = escribir_tags(
        path=path, existing_tags=existing, mb_details=mb_details,
        genres=genres_final, genre_primary=genre_primary,
        genre_secondary=genre_secondary,
        mood=mood, confidence=confidence,
        vector=debug_info["vector"], ab_features=ab_features,
        lrc_result=lrc_result, mx_features=mx
    )

    has_sync_final  = existing["has_synced_lrc"] or bool(lrc_result.get("synced"))
    has_plain_final = existing["has_plain_lrc"]  or bool(lrc_result.get("plain"))

    result.update({
        "artist":               artist,
        "title":                title,
        "mbid":                 mbid,
        "mood":                 mood,
        "confidence":           confidence,
        "mood_confidence_tier": tier,
        "review":               needs_review,
        "vector":               debug_info["vector"],
        "top_moods":            debug_info["similarities"],
        "weights_used":         debug_info["weights_used"],
        "sources":              debug_info["sources"],
        "genres":               genres_final,
        "genre_primary":        genre_primary,
        "genre_secondary":      genre_secondary,
        "lang":                 lyric_delta.get("lang"),
        "has_synced_lrc_final": has_sync_final,
        "has_plain_lrc_final":  has_plain_final,
        "lrc_result":           lrc_result,
        "synced_lrc_text":      lrc_result.get("synced") or existing.get("synced_lrc_text"),
        "plain_lrc_text":       lrc_result.get("plain")  or existing.get("plain_lrc_text"),
        "ab_used":              ab_features is not None,  # siempre False (AB dado de baja 2022)
        "mb_used":              mb_recording is not None,
        "mb_details":           mb_details,
        "musicexplorer_features": mx,
        "tags_touched":         tags_touched,
        "existing_tags":        existing,
        "all_tags":             existing.get("all_tags",{}),
        "status":               "ok",
    })
    audit_log.append(result)
    return result

# ============================================================
# SELECCIÓN SELECTIVA DE PISTAS
# ============================================================

def leer_audit(album_path: Path) -> list:
    """
    Lee el audit JSON más reciente del álbum. Retorna [] si no existe o está corrupto.
    Siempre usa el archivo más reciente (orden desc por nombre = timestamp).
    """
    audits = sorted(album_path.glob("richmetapro_audit_*.json"), reverse=True)
    if not audits:
        return []
    try:
        with open(audits[0], encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        log.debug("Error leyendo audit %s: %s", audits[0], e)
        return []


def seleccionar_pistas(album_path: Path, mode: str) -> List[Path]:
    """
    Retorna la lista de archivos de audio del álbum a procesar según el modo.

    MODE_FULL    → todos los archivos del álbum (sin filtro)
    MODE_PENDING → archivos SIN registro en el audit más reciente
    MODE_LYRICS  → archivos CON registro pero con has_synced_lrc_final=False
    MODE_RESUME  → unión de pending + no_lyrics (el más útil para re-procesar)

    La comparación se hace por nombre de archivo (no por path completo)
    para tolerar que el álbum se haya movido de ubicación.
    """
    all_files = sorted(
        f for f in album_path.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED
    )
    if not all_files:
        return []
    if mode == MODE_FULL:
        return all_files

    audit = leer_audit(album_path)
    # Indexar por nombre de archivo (basename) para robustez ante paths cambiados
    audit_idx: dict = {Path(r.get("file", "")).name: r for r in audit}

    pending   = [f for f in all_files if f.name not in audit_idx]
    no_lyrics = [f for f in all_files
                 if f.name in audit_idx
                 and not audit_idx[f.name].get("has_synced_lrc_final", False)]

    if mode == MODE_PENDING:
        return pending
    if mode == MODE_LYRICS:
        return no_lyrics
    # MODE_RESUME: unión ordenada sin duplicados
    seen, result = set(), []
    for f in pending + no_lyrics:
        if f.name not in seen:
            seen.add(f.name)
            result.append(f)
    return sorted(result)


def merge_audit(existing: list, nuevo: list) -> list:
    """
    Fusiona dos listas de registros de audit.
    Los registros en 'nuevo' sobreescriben los de 'existing' por nombre de archivo.
    Los registros de 'existing' que no aparecen en 'nuevo' se conservan sin cambios.
    """
    nuevo_idx = {Path(r.get("file", "")).name: r for r in nuevo}
    merged = []
    existing_names = set()
    for rec in existing:
        name = Path(rec.get("file", "")).name
        existing_names.add(name)
        merged.append(nuevo_idx.get(name, rec))   # nuevo tiene prioridad
    # Agregar registros completamente nuevos (pistas recién añadidas al álbum)
    for r in nuevo:
        if Path(r.get("file", "")).name not in existing_names:
            merged.append(r)
    return merged


def album_ya_procesado(album_path: Path) -> bool:
    return any(album_path.glob("richmetapro_audit_*.json"))

def procesar_album(album_path: Path, mode: str = MODE_FULL, pbar_global=None) -> dict:
    """
    Procesa un álbum según el modo indicado.

    mode=MODE_FULL     → procesa todos los archivos del álbum
    mode=MODE_PENDING  → solo archivos sin registro en audit
    mode=MODE_LYRICS   → solo archivos con audit pero sin synced lyrics
    mode=MODE_RESUME   → pendientes + sin synced lyrics

    En modos selectivos, el audit resultante fusiona los registros nuevos
    con los existentes (merge_audit), preservando los registros no tocados.
    El HTML de reporte siempre refleja el estado completo del álbum.
    """
    files = seleccionar_pistas(album_path, mode)

    if not files:
        if pbar_global:
            pbar_global.update(1)
            pbar_global.set_postfix_str(f"SKIP {album_path.name[:30]}")
        return {"album": str(album_path), "status": "skipped", "tracks": 0}

    # Leer audit existente antes de procesar (para merge posterior)
    existing_audit = leer_audit(album_path) if mode != MODE_FULL else []

    new_audit: list = []
    t_start = time.time()
    # Pistas en paralelo (TRACK_WORKERS). Cada API mantiene su límite global, así que
    # más hilos no generan más consultas por segundo; solo se superponen las esperas.
    # Un error en una pista no aborta el álbum: se registra y queda pendiente para Reanudar.
    def _una(f):
        propio: list = []
        try:
            procesar(str(f), propio)
        except Exception as ex:
            log.error("Error en pista %s: %s", f.name, ex)
        return propio
    with ThreadPoolExecutor(max_workers=max(1, min(TRACK_WORKERS, len(files)))) as pool:
        for propio in pool.map(_una, files):   # map conserva el orden de las pistas
            new_audit.extend(propio)
    elapsed = time.time() - t_start

    # Fusionar con audit existente en modos selectivos
    if existing_audit:
        audit = merge_audit(existing_audit, new_audit)
    else:
        audit = new_audit

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    with open(album_path / f"richmetapro_audit_{ts}.json", "w", encoding="utf-8") as fh:
        json.dump(audit, fh, indent=2, ensure_ascii=False, default=str)

    review = [r for r in audit if r.get("review")]
    if review:
        with open(album_path / f"richmetapro_review_{ts}.json", "w", encoding="utf-8") as fh:
            json.dump(review, fh, indent=2, ensure_ascii=False, default=str)

    log_path = album_path / f"richmetapro_log_{ts}.html"
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(f'<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8"><title>RichMeta Pro v7.2 — {album_path.name}</title><style>{CSS}</style></head><body>')
            fh.write(f'<h1>🎧 {album_path.name}</h1>')
            fh.write(render_summary_html(audit, elapsed, str(album_path)))
            fh.write('<h2 style="color:#58a6ff;margin-top:24px">Detalle por pista</h2>')
            for r in audit: fh.write(render_track_html(r))
            fh.write('</body></html>')
    except Exception as e:
        log.error("No se pudo escribir HTML de reporte: %s", e)

    ok  = sum(1 for r in audit if r["status"] == "ok")
    syn = sum(1 for r in audit if r.get("has_synced_lrc_final"))
    avg = sum(r.get("confidence", 0) for r in audit if r["status"] == "ok") / max(ok, 1)

    if pbar_global:
        pbar_global.update(1)
        pbar_global.set_postfix_str(f"{album_path.name[:25]} (lrc:{syn}/{ok})")

    return {
        "album":       str(album_path),
        "status":      "ok",
        "mode":        mode,
        "tracks":      len(files),          # pistas procesadas en esta pasada
        "ok":          ok,
        "review":      len(review),
        "synced_lrc":  syn,
        "confidence":  round(avg, 3),
        "log_html":    str(log_path),
    }

def descubrir_albums(base: Path) -> List[Path]:
    albums = []
    for root, dirs, files in os.walk(base):
        dirs.sort()
        if any(f.lower().endswith(SUPPORTED) for f in files):
            albums.append(Path(root))
    return sorted(albums)

# ============================================================
# SELECTOR INTERACTIVO DE DIRECTORIO
# ============================================================

def listar_subdirs(path: Path) -> List[Path]:
    """Lista subdirectorios ordenados, ignorando ocultos."""
    return sorted(
        [p for p in path.iterdir() if p.is_dir() and not p.name.startswith('.')],
        key=lambda p: p.name.lower()
    )

def contar_audio(path: Path) -> int:
    """Cuenta archivos de audio en un directorio (no recursivo)."""
    return sum(1 for f in path.iterdir()
               if f.is_file() and f.suffix.lower() in SUPPORTED)

def es_album(path: Path) -> bool:
    """Un directorio es álbum si contiene archivos de audio directamente."""
    return contar_audio(path) > 0

def descubrir_albums(base: Path) -> List[Path]:
    """
    Descubre todos los directorios de álbum bajo base.
    Un álbum es cualquier directorio que contenga archivos de audio.
    Busca recursivamente.
    """
    albums = []
    for root, dirs, files in os.walk(base):
        dirs.sort()   # orden determinístico
        root_path = Path(root)
        if any(f.lower().endswith(SUPPORTED) for f in files):
            albums.append(root_path)
    return sorted(albums)

def menu_seleccion() -> Path:
    """
    Menú interactivo de 3 niveles:
      Nivel 1: letra (A/, B/, ..., #/)
      Nivel 2: artista
      Nivel 3: álbum (o "Todos los álbumes de este artista")
    Permite también ingresar un path directo.
    """
    print("\n" + "=" * 60)
    print("  RichMeta Pro v7.0 — Selector de directorio")
    print("=" * 60)
    print(f"  Biblioteca: {MUSIC_ROOT}")
    print()
    print("  [D] Ingresar path directamente")
    print("  [T] Procesar TODA la biblioteca")
    print("  [L] Seleccionar por Letra → Artista → Álbum")
    print()

    while True:
        opcion = input("  Opción: ").strip().upper()
        if opcion == "D":
            path = input("  Path: ").strip()
            p = Path(path)
            if p.exists():
                return p
            print(f"  ❌ No existe: {path}")

        elif opcion == "T":
            print(f"  ✓ Procesando toda la biblioteca: {MUSIC_ROOT}")
            return MUSIC_ROOT

        elif opcion == "L":
            return _menu_letra()

def _menu_letra() -> Path:
    """Nivel 1: elegir letra/carpeta raíz."""
    letras = listar_subdirs(MUSIC_ROOT)
    print()
    print("  === Letras disponibles ===")
    for i, l in enumerate(letras):
        n_artistas = len(listar_subdirs(l))
        print(f"  [{i:3d}] {l.name:4s}  ({n_artistas} artistas)")

    while True:
        sel = input("\n  Número (o ENTER para volver): ").strip()
        if not sel:
            return menu_seleccion()
        try:
            idx = int(sel)
            if 0 <= idx < len(letras):
                return _menu_artista(letras[idx])
        except ValueError:
            pass
        print("  ❌ Opción inválida")

def _menu_artista(letra_path: Path) -> Path:
    """Nivel 2: elegir artista."""
    artistas = listar_subdirs(letra_path)
    print()
    print(f"  === Artistas en {letra_path.name}/ ===")
    for i, a in enumerate(artistas):
        n_albums = len([p for p in listar_subdirs(a)])
        print(f"  [{i:3d}] {a.name[:50]:<52s} ({n_albums} álbumes)")

    print(f"  [ALL] Todos los artistas de {letra_path.name}/")

    while True:
        sel = input("\n  Número (o ENTER para volver): ").strip().upper()
        if not sel:
            return _menu_letra()
        if sel == "ALL":
            return letra_path
        try:
            idx = int(sel)
            if 0 <= idx < len(artistas):
                return _menu_album(artistas[idx])
        except ValueError:
            pass
        print("  ❌ Opción inválida")

def _menu_album(artista_path: Path) -> Path:
    """Nivel 3: elegir álbum específico o todos."""
    albums = descubrir_albums(artista_path)
    print()
    print(f"  === Álbumes de {artista_path.name} ===")
    for i, a in enumerate(albums):
        n_tracks = contar_audio(a)
        # Show relative path from artist for multi-disc albums
        rel = a.relative_to(artista_path)
        print(f"  [{i:3d}] {str(rel)[:55]:<57s} ({n_tracks} tracks)")

    print(f"  [ALL] Todos los álbumes de {artista_path.name}")

    while True:
        sel = input("\n  Número (o ENTER para volver): ").strip().upper()
        if not sel:
            return _menu_artista(artista_path.parent)
        if sel == "ALL":
            return artista_path
        try:
            idx = int(sel)
            if 0 <= idx < len(albums):
                return albums[idx]
        except ValueError:
            pass
        print("  ❌ Opción inválida")


_MODE_LABELS = {
    MODE_FULL:    "Completo (todas las pistas)",
    MODE_PENDING: "Pendientes (sin registro en audit)",
    MODE_LYRICS:  "Sin lyrics (tienen audit, faltan letras sincronizadas)",
    MODE_RESUME:  "Reanudar (pendientes + sin lyrics sincronizadas)",
}

def menu_modo() -> str:
    """
    Menú interactivo para seleccionar el modo de procesamiento.
    Se muestra antes de iniciar el batch.

    Retorna una de las constantes MODE_*.
    """
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │           RichMeta Pro v7.2 — Modo de proceso           │")
    print("  ├─────────────────────────────────────────────────────────┤")
    print("  │  [1]  Completo          Procesar todo desde cero        │")
    print("  │  [2]  Pendientes        Solo pistas sin registro audit  │")
    print("  │  [3]  Sin lyrics        Solo pistas sin synced lyrics   │")
    print("  │  [4]  Reanudar ★        Pendientes + sin synced lyrics  │")
    print("  └─────────────────────────────────────────────────────────┘")
    print()
    print("  ★ Recomendado para re-procesar después de actualizar el script")
    print()

    _map = {"1": MODE_FULL, "2": MODE_PENDING, "3": MODE_LYRICS, "4": MODE_RESUME}
    while True:
        sel = input("  Modo [1-4] (ENTER = Reanudar): ").strip()
        if sel == "":
            return MODE_RESUME
        if sel in _map:
            return _map[sel]
        print("  ❌ Opción inválida, elige entre 1 y 4")


def main_batch(base: Path, mode: str = MODE_FULL, workers: int = MAX_WORKERS):
    """
    Orquestador principal de procesamiento por lotes.

    mode controla qué pistas se procesan dentro de cada álbum:
      MODE_FULL    → todas las pistas
      MODE_PENDING → solo pistas sin registro en audit
      MODE_LYRICS  → solo pistas con audit pero sin synced lyrics
      MODE_RESUME  → unión de pending + no_lyrics (recomendado para re-procesar)
    """
    print(f"\n{'='*60}")
    print(f"  RichMeta Pro v7.2 — Batch")
    print(f"  Base     : {base}")
    print(f"  Modo     : {_MODE_LABELS.get(mode, mode)}")
    print(f"  Workers  : {workers}")
    if not LANGDETECT_OK:
        print(f"  ⚠️  langdetect NO disponible — el idioma se está determinando")
        print(f"      con el diccionario de 5 palabras clave, no con análisis real.")
        print(f"      Corré 'pip install langdetect' en este entorno y reintentá.")
    print(f"{'='*60}")

    all_albums = descubrir_albums(base)

    # Pre-filtrar álbumes que no tienen nada que hacer en este modo.
    # Para MODE_FULL siempre procesamos todo.
    # Para otros modos, seleccionar_pistas decide; si retorna [] → skip.
    if mode == MODE_FULL:
        albums = all_albums
    else:
        print("  Analizando biblioteca...", end="", flush=True)
        albums = [a for a in all_albums if seleccionar_pistas(a, mode)]
        skipped = len(all_albums) - len(albums)
        print(f" {len(albums)} álbumes con trabajo pendiente "
              f"({skipped} sin cambios pendientes)")

    if not albums:
        print("\n  ✅ Nada que procesar en este modo.")
        return

    # Desglose previo de pistas afectadas
    if mode != MODE_FULL:
        total_pistas = sum(len(seleccionar_pistas(a, mode)) for a in albums)
        print(f"  Pistas a procesar: {total_pistas} en {len(albums)} álbumes\n")

    resultados, errores = [], []
    print(f"  Iniciando con {workers} workers paralelos...\n")

    t_start = time.time()
    with tqdm(total=len(albums), unit="álbum", dynamic_ncols=True) as pbar:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(procesar_album, a, mode, pbar): a
                for a in albums
            }
            for future in as_completed(futures):
                try:
                    resultados.append(future.result())
                except Exception as ex:
                    errores.append(str(ex))
                    log.error("Excepción grave en hilo: %s", ex)

    elapsed = time.time() - t_start
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = base if base != MUSIC_ROOT else Path(".")
    session_log = session_dir / f"richmetapro_session_{ts}.json"

    with open(session_log, "w", encoding="utf-8") as fh:
        json.dump({
            "timestamp":  datetime.now().isoformat(),
            "base":       str(base),
            "mode":       mode,
            "workers":    workers,
            "elapsed_s":  round(elapsed, 1),
            "resultados": resultados,
            "errores":    errores,
        }, fh, indent=2, ensure_ascii=False, default=str)

    ok_albums  = [r for r in resultados if r["status"] == "ok"]
    total_trk  = sum(r.get("tracks", 0) for r in ok_albums)
    total_syn  = sum(r.get("synced_lrc", 0) for r in ok_albums)
    skipped_n  = sum(1 for r in resultados if r["status"] == "skipped")

    print(f"\n{'='*60}")
    print(f"  ✅  Álbumes procesados   : {len(ok_albums)}")
    print(f"  ⏭   Álbumes sin cambios  : {skipped_n}")
    print(f"  ❌  Álbumes con error    : {len(errores)}")
    print(f"  🎵  Tracks procesados    : {total_trk}")
    print(f"  ♪   Letras sincronizadas : {total_syn}")
    print(f"  ⏱   Tiempo total         : {elapsed:.1f}s")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="RichMeta Pro v7.2 — Tagger inteligente de audio",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "path", nargs="?",
        help="Ruta directa al álbum o directorio raíz (opcional; si se omite, se muestra el menú)",
    )
    parser.add_argument(
        "--mode", "-m",
        choices=[MODE_FULL, MODE_PENDING, MODE_LYRICS, MODE_RESUME],
        default=None,
        help=(
            "Modo de procesamiento:\n"
            f"  {MODE_FULL:10s} — Todas las pistas (desde cero)\n"
            f"  {MODE_PENDING:10s} — Solo pistas sin registro en audit\n"
            f"  {MODE_LYRICS:10s} — Solo pistas con audit pero sin synced lyrics\n"
            f"  {MODE_RESUME:10s} — Pendientes + sin synced lyrics [por defecto interactivo]"
        ),
    )
    parser.add_argument(
        "--workers", "-w",
        type=int, default=MAX_WORKERS,
        help=f"Hilos paralelos (default: {MAX_WORKERS})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Mostrar logs DEBUG en consola",
    )
    args = parser.parse_args()

    if args.verbose:
        console_handler.setLevel(logging.DEBUG)
    else:
        console_handler.setLevel(logging.INFO)

    # Resolver ruta base
    base = Path(args.path) if args.path else menu_seleccion()

    # Resolver modo: CLI tiene prioridad; si no se pasa, preguntar interactivamente
    if args.mode:
        mode = args.mode
    else:
        mode = menu_modo()

    main_batch(base, mode=mode, workers=args.workers)
