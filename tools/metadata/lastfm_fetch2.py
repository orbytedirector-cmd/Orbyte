#!/usr/bin/env python3
# ============================================================
# LastFM Fetch v2.0 — HiRes Browser
# ============================================================
# Fuentes de datos (según especificación):
#
#   Álbumes  → album.getinfo   → album_meta.lastfm_listeners / lastfm_playcount
#   Artistas → artist.getinfo  → artists.lastfm_listeners / lastfm_playcount
#   Tracks   → album.getinfo   → track_meta.lastfm_listeners / lastfm_playcount
#              (los tracks vienen GRATIS dentro de album.getinfo — 1 llamada
#               por álbum devuelve datos del álbum + todos sus tracks)
#
# Nota: track.getInfo individual para 66k tracks tomaría ~4h.
# album.getinfo incluye listeners/playcount por track en la misma respuesta.
#
# Cálculo de estrellas:
#   Álbumes/Artistas: basado en distribución logarítmica relativa al
#   máximo de playcount de toda la colección (no umbrales fijos).
#   Tracks: igual, basado en playcount del track relativo al máximo de tracks.
#
# Uso:
#   python3 lastfm_fetch.py              # menú interactivo
#   python3 lastfm_fetch.py --status     # ver pendientes sin procesar
#   python3 lastfm_fetch.py --pending    # procesar solo pendientes
#   python3 lastfm_fetch.py --scope letter:A
#   python3 lastfm_fetch.py --scope artist:42
#   python3 lastfm_fetch.py --scope album:1211
#   python3 lastfm_fetch.py --scope all
#   python3 lastfm_fetch.py --force      # re-procesar aunque ya tenga datos
# ============================================================

import os, sys, time, json, sqlite3, logging, unicodedata, re, argparse
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH     = "music.db"

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

LASTFM_KEY  = os.environ.get("LASTFM_API_KEY", "")
LASTFM_BASE = "http://ws.audioscrobbler.com/2.0/"
RATE_LIMIT  = 0.22   # ~4.5 req/s
TIMEOUT     = 8
MAX_RETRIES = 3

# ── Logging ───────────────────────────────────────────────────────────────────

log = logging.getLogger("lastfm_fetch")
log.setLevel(logging.DEBUG)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                                   datefmt="%H:%M:%S"))
_ch.setLevel(logging.INFO)
log.addHandler(_ch)
_fh = logging.FileHandler("lastfm_fetch.log", mode="a", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
log.addHandler(_fh)

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn(db_path=None):
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_schema(conn):
    """
    Crea o extiende tablas con columnas lastfm.
    Idempotente — se puede ejecutar múltiples veces sin problema.
    """
    changes = []

    # album_meta — listeners, playcount y timestamp del fetch
    existing = {c[1] for c in conn.execute("PRAGMA table_info(album_meta)")}
    for col, typ in [
        ("lastfm_listeners",  "INTEGER"),
        ("lastfm_playcount",  "INTEGER"),
        ("lastfm_fetched_at", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE album_meta ADD COLUMN {col} {typ}")
            changes.append(f"album_meta.{col}")

    # track_meta — listeners y playcount del track (obtenidos via album.getinfo)
    existing = {c[1] for c in conn.execute("PRAGMA table_info(track_meta)")}
    for col, typ in [
        ("lastfm_listeners", "INTEGER"),
        ("lastfm_playcount", "INTEGER"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE track_meta ADD COLUMN {col} {typ}")
            changes.append(f"track_meta.{col}")

    # artists — listeners, playcount y timestamp
    existing = {c[1] for c in conn.execute("PRAGMA table_info(artists)")}
    for col, typ in [
        ("lastfm_listeners",  "INTEGER"),
        ("lastfm_playcount",  "INTEGER"),
        ("lastfm_fetched_at", "TEXT"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE artists ADD COLUMN {col} {typ}")
            changes.append(f"artists.{col}")

    # Tablas auxiliares para el browser
    conn.execute("""
        CREATE TABLE IF NOT EXISTS album_pop_cache (
            album_id   INTEGER PRIMARY KEY,
            pop_score  REAL NOT NULL DEFAULT 0,
            stars      REAL NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_pop_cache (
            track_id   INTEGER PRIMARY KEY,
            pop_score  REAL NOT NULL DEFAULT 0,
            stars      REAL NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            track_id  INTEGER PRIMARY KEY,
            added_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY(track_id) REFERENCES tracks(id)
        )""")

    conn.commit()
    if changes:
        log.info("Columnas añadadidas: %s", ", ".join(changes))

# ── Last.fm API ───────────────────────────────────────────────────────────────

_last_req = 0.0

def _lfm(params: dict) -> dict:
    """GET a Last.fm con rate-limit y reintentos."""
    global _last_req
    params = dict(params, api_key=LASTFM_KEY, format="json")
    url    = LASTFM_BASE + "?" + urllib.parse.urlencode(params)
    for attempt in range(MAX_RETRIES):
        gap = RATE_LIMIT - (time.time() - _last_req)
        if gap > 0:
            time.sleep(gap)
        _last_req = time.time()
        try:
            resp = urllib.request.urlopen(url, timeout=TIMEOUT)
            data = json.loads(resp.read().decode("utf-8"))
            if "error" in data:
                log.debug("LFM err %s: %s", data["error"], data.get("message", ""))
                return {}
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                secs = 2 ** (attempt + 1)
                log.warning("Rate-limit 429 — esperando %ds", secs)
                time.sleep(secs)
            elif e.code in (403, 404):
                return {}
            else:
                log.debug("HTTP %s (intento %d)", e.code, attempt + 1)
                time.sleep(1)
        except Exception as ex:
            log.debug("fetch error (intento %d): %s", attempt + 1, ex)
            time.sleep(1)
    return {}


def api_album_getinfo(artist: str, album: str) -> dict:
    """
    album.getinfo → listeners y playcount del álbum.
    Retorna: {listeners: int, playcount: int}
    """
    # 1º Intento: Con el nombre exacto de la DB
    data = _lfm({"method": "album.getinfo",
                 "artist": artist, "album": album, "autocorrect": "1"})
    alb = data.get("album")

    # 2º Intento (FALLBACK): Si falla y el artista contiene mayúsculas, probamos forzando minúsculas
    if not alb and artist and not artist.islower():
        log.debug("Fallback minúsculas para álbum: %s - %s", artist, album)
        data = _lfm({"method": "album.getinfo",
                     "artist": artist.lower(), "album": album, "autocorrect": "1"})
        alb = data.get("album")

    if not alb:
        return {}
    return {
        "listeners": _toint(alb.get("listeners")),
        "playcount":  _toint(alb.get("playcount")),
    }


def api_track_getinfo(artist: str, track: str) -> dict:
    """
    track.getInfo → listeners y playcount del track.
    Retorna: {listeners: int, playcount: int}
    """
    # 1º Intento: Con el nombre exacto de la DB
    data = _lfm({"method": "track.getInfo",
                 "artist": artist, "track": track, "autocorrect": "1"})
    tr = data.get("track")

    # 2º Intento (FALLBACK): Si falla y el artista contiene mayúsculas, probamos forzando minúsculas
    if not tr and artist and not artist.islower():
        log.debug("Fallback minúsculas para track: %s - %s", artist, track)
        data = _lfm({"method": "track.getInfo",
                     "artist": artist.lower(), "track": track, "autocorrect": "1"})
        tr = data.get("track")

    if not tr:
        return {}
    return {
        "listeners": _toint(tr.get("listeners")),
        "playcount":  _toint(tr.get("playcount")),
    }


def api_artist_getinfo(artist: str) -> dict:
    """
    artist.getinfo — devuelve {listeners, playcount}
    """
    # 1º Intento: Con el nombre exacto de la DB
    data = _lfm({"method": "artist.getinfo",
                 "artist": artist, "autocorrect": "1"})
    ar = data.get("artist")

    # 2º Intento (FALLBACK): Si falla y el artista contiene mayúsculas, probamos forzando minúsculas
    if not ar and artist and not artist.islower():
        log.debug("Fallback minúsculas para artista: %s", artist)
        data = _lfm({"method": "artist.getinfo",
                     "artist": artist.lower(), "autocorrect": "1"})
        ar = data.get("artist")

    if not ar:
        return {}
    stats = ar.get("stats") or ar
    return {
        "listeners": _toint(stats.get("listeners")),
        "playcount":  _toint(stats.get("playcount")),
    }


def _toint(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None

# ── Normalización de nombres ─────────────────────────────────────────────────

def _norm(text: str) -> str:
    if not text: return ""
    text = text.lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

# ── Procesamiento individual ──────────────────────────────────────────────────

def process_album(conn, album_id: int, artist_name: str, album_name: str,
                  force: bool = False) -> str:
    """
    Llama album.getinfo.
    Persiste en album_meta: lastfm_listeners, lastfm_playcount, lastfm_fetched_at.
    Retorna: 'ok' | 'skip' | 'no_data'
    """
    if not force:
        row = conn.execute(
            "SELECT lastfm_fetched_at FROM album_meta WHERE album_id=?",
            (album_id,)
        ).fetchone()
        if row and row["lastfm_fetched_at"]:
            return "skip"

    data = api_album_getinfo(artist_name, album_name)
    now  = datetime.now().isoformat()

    if not data:
        conn.execute("""
            INSERT INTO album_meta (album_id, lastfm_fetched_at)
            VALUES (?,?)
            ON CONFLICT(album_id) DO UPDATE SET lastfm_fetched_at=excluded.lastfm_fetched_at
        """, (album_id, now))
        return "no_data"

    conn.execute("""
        INSERT INTO album_meta (album_id, lastfm_listeners, lastfm_playcount, lastfm_fetched_at)
        VALUES (?,?,?,?)
        ON CONFLICT(album_id) DO UPDATE SET
            lastfm_listeners  = excluded.lastfm_listeners,
            lastfm_playcount  = excluded.lastfm_playcount,
            lastfm_fetched_at = excluded.lastfm_fetched_at
    """, (album_id, data["listeners"], data["playcount"], now))
    return "ok"


def process_track(conn, track_id: int, artist_name: str, track_title: str,
                  force: bool = False) -> str:
    """
    Llama track.getInfo.
    Persiste en track_meta: lastfm_listeners, lastfm_playcount.
    Retorna: 'ok' | 'skip' | 'no_data'
    """
    if not force:
        row = conn.execute(
            "SELECT lastfm_listeners FROM track_meta WHERE track_id=?",
            (track_id,)
        ).fetchone()
        if row and row["lastfm_listeners"] is not None:
            return "skip"

    # Use only the primary artist (first before semicolon) for the API call
    primary_artist = artist_name.split(";")[0].strip() if artist_name else ""

    data = api_track_getinfo(primary_artist, track_title)
    if not data:
        return "no_data"

    conn.execute("""
        INSERT INTO track_meta (track_id, lastfm_listeners, lastfm_playcount)
        VALUES (?,?,?)
        ON CONFLICT(track_id) DO UPDATE SET
            lastfm_listeners = excluded.lastfm_listeners,
            lastfm_playcount = excluded.lastfm_playcount
    """, (track_id, data["listeners"], data["playcount"]))
    return "ok"


def process_artist(conn, artist_id: int, artist_name: str,
                   force: bool = False) -> str:
    """
    Llama artist.getinfo. Persiste:
      - artists: lastfm_listeners, lastfm_playcount, lastfm_fetched_at
    Retorna: 'ok' | 'skip' | 'no_data'
    """
    if not force:
        row = conn.execute(
            "SELECT lastfm_fetched_at FROM artists WHERE id=?", (artist_id,)
        ).fetchone()
        if row and row["lastfm_fetched_at"]:
            return "skip"

    data = api_artist_getinfo(artist_name)
    if not data:
        conn.execute("""
            UPDATE artists SET lastfm_fetched_at=? WHERE id=?
        """, (datetime.now().isoformat(), artist_id))
        return "no_data"

    conn.execute("""
        UPDATE artists
        SET lastfm_listeners=?, lastfm_playcount=?, lastfm_fetched_at=?
        WHERE id=?
    """, (data["listeners"], data["playcount"], datetime.now().isoformat(), artist_id))
    return "ok"

# ── Recalcular pop_score y stars ──────────────────────────────────────────────

def rebuild_pop_scores(conn):
    """
    Recalcula album_pop_cache y track_pop_cache usando datos reales de Last.fm.

    Estrellas (0.0 – 5.0, con fracciones):
      Se calculan en escala LOGARÍTMICA relativa al máximo de playcount
      en toda la colección. Así la distribución es proporcional y no hay
      umbrales fijos que queden obsoletos cuando crece la biblioteca.

      stars = 5.0 * log(playcount + 1) / log(max_playcount + 1)

    pop_score (0 – 100):
      Combina playcount (60%) + listeners (40%), ambos normalizados logarítmicamente.
    """
    # Máximos de referencia de toda la colección
    row = conn.execute(
        "SELECT MAX(lastfm_playcount) as mp, MAX(lastfm_listeners) as ml FROM album_meta"
    ).fetchone()
    max_alb_plays = max(row["mp"] or 1, 1)
    max_alb_list  = max(row["ml"] or 1, 1)

    row2 = conn.execute(
        "SELECT MAX(lastfm_playcount) as mp, MAX(lastfm_listeners) as ml FROM track_meta"
    ).fetchone()
    max_trk_plays = max(row2["mp"] or 1, 1)
    max_trk_list  = max(row2["ml"] or 1, 1)

    row3 = conn.execute(
        "SELECT MAX(lastfm_playcount) as mp, MAX(lastfm_listeners) as ml FROM artists"
    ).fetchone()
    max_ar_plays = max(row3["mp"] or 1, 1)
    max_ar_list  = max(row3["ml"] or 1, 1)

    print(f"  Máx. playcount  álbum  : {max_alb_plays:>12,}")
    print(f"  Máx. listeners  álbum  : {max_alb_list:>12,}")
    print(f"  Máx. playcount  track  : {max_trk_plays:>12,}")
    print(f"  Máx. playcount  artista: {max_ar_plays:>12,}")

    # ── Álbumes ───────────────────────────────────────────────────────────────
    conn.execute("DELETE FROM album_pop_cache")
    conn.execute(f"""
        INSERT INTO album_pop_cache (album_id, pop_score, stars, updated_at)
        SELECT
            al.id,
            -- pop_score 0-100: 60% playcount + 40% listeners (log-normalizado)
            ROUND(
              CASE WHEN am.lastfm_playcount  > 0
                   THEN 60.0 * LOG(am.lastfm_playcount  + 1) / LOG({max_alb_plays} + 1)
                   ELSE 0.0 END
              +
              CASE WHEN am.lastfm_listeners  > 0
                   THEN 40.0 * LOG(am.lastfm_listeners  + 1) / LOG({max_alb_list}  + 1)
                   ELSE 0.0 END
            , 2) as pop_score,
            -- stars 0.0-5.0 basadas en playcount relativo (con fracciones)
            ROUND(
              CASE WHEN am.lastfm_playcount  > 0
                   THEN 5.0 * LOG(am.lastfm_playcount  + 1) / LOG({max_alb_plays} + 1)
                   ELSE 0.0 END
            , 1) as stars,
            datetime('now')
        FROM albums al
        LEFT JOIN album_meta am ON am.album_id = al.id
    """)
    n_alb = conn.execute("SELECT COUNT(*) FROM album_pop_cache").fetchone()[0]

    # ── Tracks ────────────────────────────────────────────────────────────────
    conn.execute("DELETE FROM track_pop_cache")
    conn.execute(f"""
        INSERT INTO track_pop_cache (track_id, pop_score, stars, updated_at)
        SELECT
            t.id,
            ROUND(
              CASE WHEN tm.lastfm_playcount  > 0
                   THEN 60.0 * LOG(tm.lastfm_playcount  + 1) / LOG({max_trk_plays} + 1)
                   ELSE 0.0 END
              +
              CASE WHEN tm.lastfm_listeners  > 0
                   THEN 40.0 * LOG(tm.lastfm_listeners  + 1) / LOG({max_trk_list}  + 1)
                   ELSE 0.0 END
            , 2) as pop_score,
            ROUND(
              CASE WHEN tm.lastfm_playcount  > 0
                   THEN 5.0 * LOG(tm.lastfm_playcount  + 1) / LOG({max_trk_plays} + 1)
                   ELSE 0.0 END
            , 1) as stars,
            datetime('now')
        FROM tracks t
        LEFT JOIN track_meta tm ON tm.track_id = t.id
    """)
    n_trk = conn.execute("SELECT COUNT(*) FROM track_pop_cache").fetchone()[0]

    conn.commit()
    print(f"  pop_score álbumes      : {n_alb:,} recalculados")
    print(f"  pop_score tracks       : {n_trk:,} recalculados")

    # Verificación visual: top 5
    top = conn.execute("""
        SELECT al.name, ar.name as artist, apc.stars, apc.pop_score,
               am.lastfm_playcount, am.lastfm_listeners
        FROM album_pop_cache apc
        JOIN albums al  ON al.id  = apc.album_id
        JOIN artists ar ON ar.id  = al.artist_id
        LEFT JOIN album_meta am ON am.album_id = al.id
        ORDER BY apc.pop_score DESC LIMIT 8
    """).fetchall()
    print("\n  Top 8 álbumes por popularidad:")
    for r in top:
        plays = f"{r['lastfm_playcount']:,}" if r["lastfm_playcount"] else "—"
        lst   = f"{r['lastfm_listeners']:,}" if r["lastfm_listeners"] else "—"
        stars = "★" * int(r["stars"]) + ("½" if (r["stars"] % 1) >= 0.5 else "") + \
                "☆" * (5 - int(r["stars"]) - (1 if (r["stars"] % 1) >= 0.5 else 0))
        print(f"    {stars}  {r['pop_score']:5.1f}  "
              f"{r['artist'][:18]:18} — {r['name'][:30]:30}  "
              f"[{plays} plays / {lst} oyentes]")

# ── Status ────────────────────────────────────────────────────────────────────

def show_status(conn):
    print("\n" + "═"*65)
    print("  Estado Last.fm en la biblioteca")
    print("═"*65)

    total_alb = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    done_alb  = conn.execute(
        "SELECT COUNT(DISTINCT album_id) FROM album_meta WHERE lastfm_fetched_at IS NOT NULL"
    ).fetchone()[0]
    with_data = conn.execute(
        "SELECT COUNT(DISTINCT album_id) FROM album_meta WHERE lastfm_listeners IS NOT NULL"
    ).fetchone()[0]

    total_ar  = conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    done_ar   = conn.execute(
        "SELECT COUNT(*) FROM artists WHERE lastfm_fetched_at IS NOT NULL"
    ).fetchone()[0]

    total_trk = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    done_trk  = conn.execute(
        "SELECT COUNT(*) FROM track_meta WHERE lastfm_listeners IS NOT NULL"
    ).fetchone()[0]
    pend_trk  = total_trk - done_trk

    print(f"\n  {'Entidad':<12} {'Procesados':>12} {'Total':>8} {'Con datos':>10} {'Pendiente':>10}")
    print(f"  {'─'*12} {'─'*12} {'─'*8} {'─'*10} {'─'*10}")
    print(f"  {'Álbumes':<12} {done_alb:>12,} {total_alb:>8,} {with_data:>10,} {total_alb-done_alb:>10,}")
    print(f"  {'Artistas':<12} {done_ar:>12,} {total_ar:>8,} {'—':>10} {total_ar-done_ar:>10,}")
    print(f"  {'Tracks':<12} {done_trk:>12,} {total_trk:>8,} {done_trk:>10,} {pend_trk:>10,}")
    print(f"  (tracks via track.getInfo — ejecutar por álbum/artista/letra)")

    # Por letra
    print("\n  Pendientes por letra:")
    rows = conn.execute("""
        SELECT ar.letter,
               COUNT(DISTINCT al.id)  as total,
               SUM(CASE WHEN am.lastfm_fetched_at IS NOT NULL THEN 1 ELSE 0 END) as done
        FROM albums al
        JOIN artists ar ON al.artist_id = ar.id
        LEFT JOIN album_meta am ON am.album_id = al.id
        GROUP BY ar.letter ORDER BY ar.letter
    """).fetchall()
    pending_letters = [(r["letter"], r["total"]-r["done"]) for r in rows if r["total"]-r["done"] > 0]
    if pending_letters:
        for letter, pend in pending_letters:
            print(f"    {letter:3s}  {pend:4d} pendientes")
    else:
        print("    ✅ Todas las letras procesadas")

    eta = sum(p for _, p in pending_letters) * RATE_LIMIT
    if eta > 0:
        print(f"\n  ETA estimada: {int(eta//60)}m {int(eta%60):02d}s")
    print()


def show_pending_list(conn):
    print("\n  === Álbumes pendientes (primeros 80) ===")
    rows = conn.execute("""
        SELECT ar.letter, ar.name as artist, al.id, al.name as album
        FROM albums al
        JOIN artists ar ON al.artist_id = ar.id
        LEFT JOIN album_meta am ON am.album_id = al.id
        WHERE am.lastfm_fetched_at IS NULL
        ORDER BY ar.letter, ar.name, al.name
        LIMIT 80
    """).fetchall()
    for r in rows:
        print(f"    {r['letter']:3s} [{r['id']:5d}]  {r['artist'][:22]:22} — {r['album'][:40]}")

    total = conn.execute("""
        SELECT COUNT(*) FROM albums al
        LEFT JOIN album_meta am ON am.album_id = al.id
        WHERE am.lastfm_fetched_at IS NULL
    """).fetchone()[0]
    if total > 80:
        print(f"    ... y {total-80:,} más")
    print(f"\n  Total álbumes pendientes : {total:,}")

    print("\n  === Artistas pendientes ===")
    rows2 = conn.execute("""
        SELECT id, name FROM artists WHERE lastfm_fetched_at IS NULL
        ORDER BY letter, name LIMIT 50
    """).fetchall()
    for r in rows2:
        print(f"    [{r['id']:4d}]  {r['name']}")
    total_ar = conn.execute(
        "SELECT COUNT(*) FROM artists WHERE lastfm_fetched_at IS NULL"
    ).fetchone()[0]
    if total_ar > 50:
        print(f"    ... y {total_ar-50:,} más")
    print(f"  Total artistas pendientes: {total_ar:,}")

# ── Ejecución por alcance ─────────────────────────────────────────────────────

def run_scope(conn, scope: str, force: bool = False):
    """Fetch y persistencia para el alcance dado."""

    # Construir listas
    if scope == "all":
        albums  = conn.execute("""
            SELECT al.id, al.name, ar.id as artist_id, ar.name as artist_name
            FROM albums al JOIN artists ar ON al.artist_id=ar.id
            ORDER BY ar.letter, ar.name, al.name
        """).fetchall()
        artists = conn.execute(
            "SELECT id, name FROM artists ORDER BY letter, name"
        ).fetchall()

    elif scope.startswith("letter:"):
        letter = scope.split(":", 1)[1]
        albums  = conn.execute("""
            SELECT al.id, al.name, ar.id as artist_id, ar.name as artist_name
            FROM albums al JOIN artists ar ON al.artist_id=ar.id
            WHERE ar.letter=? ORDER BY ar.name, al.name
        """, (letter,)).fetchall()
        artists = conn.execute(
            "SELECT id, name FROM artists WHERE letter=? ORDER BY name", (letter,)
        ).fetchall()

    elif scope.startswith("artist:"):
        aid = int(scope.split(":", 1)[1])
        albums  = conn.execute("""
            SELECT al.id, al.name, ar.id as artist_id, ar.name as artist_name
            FROM albums al JOIN artists ar ON al.artist_id=ar.id
            WHERE ar.id=? ORDER BY al.name
        """, (aid,)).fetchall()
        artists = conn.execute("SELECT id, name FROM artists WHERE id=?", (aid,)).fetchall()

    elif scope.startswith("album:"):
        alb_id = int(scope.split(":", 1)[1])
        albums  = conn.execute("""
            SELECT al.id, al.name, ar.id as artist_id, ar.name as artist_name
            FROM albums al JOIN artists ar ON al.artist_id=ar.id
            WHERE al.id=?
        """, (alb_id,)).fetchall()
        artists = []

    else:
        print(f"  ❌ Scope inválido: {scope}")
        return

    # Filtrar pendientes si no es force
    if not force:
        def _alb_pending(a):
            r = conn.execute(
                "SELECT lastfm_fetched_at FROM album_meta WHERE album_id=?", (a["id"],)
            ).fetchone()
            return not (r and r["lastfm_fetched_at"])
        def _ar_pending(a):
            r = conn.execute(
                "SELECT lastfm_fetched_at FROM artists WHERE id=?", (a["id"],)
            ).fetchone()
            return not (r and r["lastfm_fetched_at"])
        albums  = [a for a in albums  if _alb_pending(a)]
        artists = [a for a in artists if _ar_pending(a)]

    if not albums and not artists:
        print("  ✅ Nada pendiente en este alcance.")
        if not force:
            print("     Usa --force para re-procesar aunque ya tenga datos.")
        return

    # Tracks: construir lista de tracks para los álbumes/artistas del scope
    fetch_tracks = scope != "all"   # 'all' = demasiados tracks, solo bajo demanda
    if fetch_tracks:
        if scope.startswith("album:"):
            alb_ids = [a["id"] for a in albums]
        elif scope.startswith("artist:"):
            alb_ids = [a["id"] for a in albums]
        elif scope.startswith("letter:"):
            alb_ids = [a["id"] for a in albums]
        else:
            alb_ids = []

        if alb_ids:
            placeholders = ",".join("?" * len(alb_ids))
            tracks_to_process = conn.execute(f"""
                SELECT t.id, t.title,
                       COALESCE(ar.name, t.artist) as artist
                FROM tracks t
                JOIN albums al ON al.id=t.album_id
                JOIN artists ar ON ar.id=al.artist_id
                WHERE t.album_id IN ({placeholders})
                ORDER BY t.album_id, t.track_number
            """, alb_ids).fetchall()
            if not force:
                tracks_to_process = [
                    tr for tr in tracks_to_process
                    if conn.execute(
                        "SELECT lastfm_listeners FROM track_meta WHERE track_id=?",
                        (tr["id"],)
                    ).fetchone() is None or
                    conn.execute(
                        "SELECT lastfm_listeners FROM track_meta WHERE track_id=?",
                        (tr["id"],)
                    ).fetchone()["lastfm_listeners"] is None
                ]
        else:
            tracks_to_process = []
    else:
        tracks_to_process = []

    total_items = len(albums) + len(artists) + len(tracks_to_process)
    eta_s       = total_items * RATE_LIMIT
    print(f"\n  {len(albums)} álbumes  +  {len(artists)} artistas  +  {len(tracks_to_process)} tracks")
    print(f"  ETA: ~{int(eta_s//60)}m{int(eta_s%60):02d}s")
    print("  (Ctrl+C para interrumpir — progreso guardado cada 25 registros)\n")

    ok_alb = nd_alb = 0
    ok_ar  = nd_ar  = 0
    t0 = time.time()

    # ── Fetch álbumes + tracks ─────────────────────────────────────────────────
    for i, al in enumerate(albums, 1):
        status = process_album(conn, al["id"], al["artist_name"], al["name"], force)
        if status == "ok":      ok_alb += 1
        elif status == "no_data": nd_alb += 1

        if i % 25 == 0:
            conn.commit()

        if i % 50 == 0 or i == len(albums):
            elapsed  = time.time() - t0
            rate     = i / elapsed if elapsed > 0 else 1
            eta_rest = (len(albums) - i) / rate
            print(f"  Álbumes {i:4d}/{len(albums):4d}  ✓{ok_alb}  ∅{nd_alb}  "
                  f"ETA: {int(eta_rest//60)}m{int(eta_rest%60):02d}s",
                  end="\r", flush=True)
    conn.commit()
    if albums:
        print()

    # ── Fetch artistas ─────────────────────────────────────────────────────────
    for i, ar in enumerate(artists, 1):
        status = process_artist(conn, ar["id"], ar["name"], force)
        if status == "ok":      ok_ar += 1
        elif status == "no_data": nd_ar += 1

        if i % 25 == 0:
            conn.commit()
        if i % 50 == 0 or i == len(artists):
            print(f"  Artistas {i:3d}/{len(artists):3d}  ✓{ok_ar}  ∅{nd_ar}",
                  end="\r", flush=True)
    conn.commit()
    if artists:
        print()

    # ── Fetch tracks ──────────────────────────────────────────────────────────
    ok_trk = nd_trk = sk_trk = 0
    if fetch_tracks and tracks_to_process:
        print(f"\n  {len(tracks_to_process)} tracks → track.getInfo...")
        t1 = time.time()
        for i, tr in enumerate(tracks_to_process, 1):
            status = process_track(conn, tr["id"], tr["artist"], tr["title"], force)
            if   status == "ok":       ok_trk += 1
            elif status == "no_data":  nd_trk += 1
            elif status == "skip":     sk_trk += 1

            if i % 25 == 0:
                conn.commit()

            if i % 100 == 0 or i == len(tracks_to_process):
                elapsed2  = time.time() - t1
                rate2     = i / elapsed2 if elapsed2 > 0 else 1
                eta_rest2 = (len(tracks_to_process) - i) / rate2
                print(f"  Tracks {i:5d}/{len(tracks_to_process):5d}  ✓{ok_trk}  ∅{nd_trk}  skip{sk_trk}  "
                      f"ETA: {int(eta_rest2//60)}m{int(eta_rest2%60):02d}s",
                      end="\r", flush=True)
        conn.commit()
        print()

    elapsed = time.time() - t0
    print(f"\n  ─── Resultado {'─'*45}")
    print(f"  Álbumes  : {ok_alb:4d} OK  |  {nd_alb:4d} no encontrados en Last.fm")
    print(f"  Artistas : {ok_ar:4d} OK  |  {nd_ar:4d} no encontrados en Last.fm")
    if fetch_tracks:
        print(f"  Tracks   : {ok_trk:4d} OK  |  {nd_trk:4d} no encontrados  |  {sk_trk} ya tenían datos")
    print(f"  Tiempo   : {elapsed:.1f}s")

    # Recalcular scores
    print("\n  Recalculando pop_score y estrellas...")
    rebuild_pop_scores(conn)

# ── Menú interactivo ──────────────────────────────────────────────────────────

def menu_main(conn, force: bool = False):
    while True:
        pending_alb = conn.execute("""
            SELECT COUNT(*) FROM albums al
            LEFT JOIN album_meta am ON am.album_id=al.id
            WHERE am.lastfm_fetched_at IS NULL
        """).fetchone()[0]
        pending_ar = conn.execute(
            "SELECT COUNT(*) FROM artists WHERE lastfm_fetched_at IS NULL"
        ).fetchone()[0]

        print()
        print("  ┌─────────────────────────────────────────────────────────────┐")
        print("  │          LastFM Fetch v2.0 — HiRes Browser                  │")
        print("  ├─────────────────────────────────────────────────────────────┤")
        print(f"  │  Pendientes: {pending_alb:4d} álbumes · {pending_ar:4d} artistas{' '*20}│")
        print("  ├─────────────────────────────────────────────────────────────┤")
        print("  │  [1]  Por Letra → Artista → Álbum                           │")
        print("  │  [2]  Buscar artista por nombre                              │")
        print("  │  [3]  Álbum por ID directo                                   │")
        print("  │  [4]  Toda la biblioteca                                     │")
        print("  ├─────────────────────────────────────────────────────────────┤")
        print("  │  [5]  Ver estado (resumen de pendientes)                     │")
        print("  │  [6]  Listar pendientes detallados                           │")
        print("  │  [7]  Recalcular pop_score / estrellas (sin fetch)           │")
        print("  ├─────────────────────────────────────────────────────────────┤")
        print("  │  [Q]  Salir                                                  │")
        print("  └─────────────────────────────────────────────────────────────┘")
        if force:
            print("  ⚠️  Modo FORCE activo")
        print()

        sel = input("  Opción: ").strip().upper()

        if sel == "Q":
            break
        elif sel == "1":
            scope = _menu_letra(conn)
            if scope:
                run_scope(conn, scope, force)
        elif sel == "2":
            scope = _menu_buscar_artista(conn)
            if scope:
                run_scope(conn, scope, force)
        elif sel == "3":
            alb_id = input("  ID del álbum: ").strip()
            if alb_id.isdigit():
                run_scope(conn, f"album:{alb_id}", force)
                print()
                c = input("  ¿Procesar también los tracks de este álbum con track.getInfo? [s/N]: ").strip().lower()
                if c == "s":
                    # tracks already processed inside run_scope for non-all scopes
                    pass  # already handled
        elif sel == "4":
            c = input("  ⚠️  Procesará TODA la biblioteca. Confirmar? [s/N]: ").strip().lower()
            if c == "s":
                run_scope(conn, "all", force)
        elif sel == "5":
            show_status(conn)
        elif sel == "6":
            show_pending_list(conn)
        elif sel == "7":
            print("\n  Recalculando...")
            rebuild_pop_scores(conn)
        else:
            print("  ❌ Opción inválida")


def _menu_letra(conn) -> str | None:
    letters = [r[0] for r in conn.execute(
        "SELECT DISTINCT letter FROM artists ORDER BY letter"
    ).fetchall()]

    print("\n  === Letras ===")
    for i, l in enumerate(letters):
        n  = conn.execute("SELECT COUNT(*) FROM artists WHERE letter=?", (l,)).fetchone()[0]
        pn = conn.execute("""
            SELECT COUNT(DISTINCT al.id) FROM albums al
            JOIN artists ar ON al.artist_id=ar.id
            LEFT JOIN album_meta am ON am.album_id=al.id
            WHERE ar.letter=? AND am.lastfm_fetched_at IS NULL
        """, (l,)).fetchone()[0]
        marker = f" ← {pn} pend." if pn else ""
        print(f"  [{i:2d}] {l:4s} {n:3d} artistas{marker}")
    print("  [ALL] Toda la biblioteca")

    sel = input("\n  Número / ALL (ENTER=cancelar): ").strip().upper()
    if not sel:
        return None
    if sel == "ALL":
        return "all"
    if sel.isdigit() and int(sel) < len(letters):
        return _menu_artista(conn, letters[int(sel)])
    return None


def _menu_artista(conn, letter: str) -> str | None:
    artists = conn.execute("""
        SELECT ar.id, ar.name,
               COUNT(DISTINCT al.id) as n_alb,
               SUM(CASE WHEN am.lastfm_fetched_at IS NULL THEN 1 ELSE 0 END) as pend
        FROM artists ar
        LEFT JOIN albums al ON al.artist_id=ar.id
        LEFT JOIN album_meta am ON am.album_id=al.id
        WHERE ar.letter=?
        GROUP BY ar.id ORDER BY ar.name
    """, (letter,)).fetchall()

    print(f"\n  === Artistas en '{letter}/' ===")
    for i, a in enumerate(artists):
        pend = f"  ({a['pend']} pend.)" if a["pend"] else ""
        print(f"  [{i:3d}] {a['name'][:52]:52}{pend}")
    print(f"  [ALL] Todos los artistas de '{letter}/'")

    sel = input("\n  Número / ALL (ENTER=cancelar): ").strip().upper()
    if not sel:
        return None
    if sel == "ALL":
        return f"letter:{letter}"
    if sel.isdigit() and int(sel) < len(artists):
        return _menu_album(conn, artists[int(sel)])
    return None


def _menu_album(conn, artist: dict) -> str | None:
    albums = conn.execute("""
        SELECT al.id, al.name,
               CASE WHEN am.lastfm_fetched_at IS NULL THEN '·' ELSE '✓' END as st,
               am.lastfm_listeners
        FROM albums al
        LEFT JOIN album_meta am ON am.album_id=al.id
        WHERE al.artist_id=?
        ORDER BY al.name
    """, (artist["id"],)).fetchall()

    print(f"\n  === Álbumes de {artist['name']} ===")
    for i, a in enumerate(albums):
        lst = f" [{a['lastfm_listeners']:,} oyentes]" if a["lastfm_listeners"] else ""
        print(f"  [{i:3d}] {a['st']} {a['name'][:52]:52}{lst}")
    print(f"  [ALL] Todos los álbumes de {artist['name']}")

    sel = input("\n  Número / ALL (ENTER=cancelar): ").strip().upper()
    if not sel:
        return None
    if sel == "ALL":
        return f"artist:{artist['id']}"
    if sel.isdigit() and int(sel) < len(albums):
        return f"album:{albums[int(sel)]['id']}"
    return None


def _menu_buscar_artista(conn) -> str | None:
    q = input("  Nombre del artista (parcial): ").strip()
    if not q:
        return None
    rows = conn.execute("""
        SELECT ar.id, ar.name, ar.letter,
               CASE WHEN ar.lastfm_fetched_at IS NOT NULL THEN '✓' ELSE '·' END as st
        FROM artists ar WHERE LOWER(name) LIKE LOWER(?)
        ORDER BY name LIMIT 20
    """, (f"%{q}%",)).fetchall()
    if not rows:
        print("  No se encontraron artistas.")
        return None
    print()
    for i, r in enumerate(rows):
        print(f"  [{i}] {r['st']} [{r['id']:4d}] {r['name']}")
    sel = input("\n  Número (ENTER=cancelar): ").strip()
    if sel.isdigit() and int(sel) < len(rows):
        ar = rows[int(sel)]
        return _menu_album(conn, {"id": ar["id"], "name": ar["name"]})
    return None

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LastFM Fetch v2.0 — captura listeners/playcount para HiRes Browser",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    # Dejamos DB_PATH como el valor por defecto por si no pasan el argumento
    parser.add_argument("--db",     default=str(DB_PATH), help="Ruta a music.db")
    parser.add_argument("--force",  action="store_true",  help="Re-procesar aunque ya tenga datos")
    parser.add_argument("--pending",action="store_true",  help="Procesar solo pendientes (no menú)")
    parser.add_argument("--status", action="store_true",  help="Mostrar estado y salir")
    parser.add_argument("--scope",  default=None,
                        help="all | letter:X | artist:ID | album:ID")
    parser.add_argument("--rebuild-scores", action="store_true",
                        help="Solo recalcular pop_score sin fetch")
    args = parser.parse_args()

    # SOLUCIÓN: Usamos una variable local db_file en lugar de modificar la global DB_PATH
    db_file = Path(args.db)
    if not db_file.exists():
        print(f"❌ No se encuentra: {db_file}")
        sys.exit(1)

    # Pasamos db_file explícitamente a get_conn
    conn = get_conn(db_file)
    ensure_schema(conn)
    print(f"  DB: {db_file}  ({db_file.stat().st_size // 1024 // 1024} MB)")

    if args.status:
        show_status(conn)
        show_pending_list(conn)
        conn.close()
        return

    if args.rebuild_scores:
        print("\n  Recalculando pop_score y estrellas...")
        rebuild_pop_scores(conn)
        conn.close()
        return

    if args.scope:
        run_scope(conn, args.scope, args.force)
        conn.close()
        return

    if args.pending:
        run_scope(conn, "all", force=False)
        conn.close()
        return

    menu_main(conn, force=args.force)
    conn.close()


if __name__ == "__main__":
    main()
