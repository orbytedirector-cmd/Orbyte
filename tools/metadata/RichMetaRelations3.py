#!/usr/bin/env python3
# ============================================================
# RICHMETARELATIONS v2.0
# Complemento de RichMetaPro — Relaciones y Contexto en BD
# ============================================================
#
# MÓDULOS:
#   1. Nacionalidad del artista    → artists.nationality
#   2. Artistas similares en BD   → artists.similar_artists_json
#   3. Álbumes similares en BD    → album_meta.similar_albums_json
#   4. Tracks similares en BD     → track_meta.similar_tracks_json
#
# MODOS DE USO:
#   a) Interactivo (sin argumentos): muestra menú de selección
#   b) CLI directo con flags:
#       python3 RichMetaRelations.py --all
#       python3 RichMetaRelations.py --nationality --similar-artists
#       python3 RichMetaRelations.py --similar-tracks --letter M
#       python3 RichMetaRelations.py --similar-albums --artist "Metallica"
#       python3 RichMetaRelations.py --similar-tracks --album 125
#       python3 RichMetaRelations.py --all --force
#       python3 RichMetaRelations.py --stats
#
# CRITERIOS DE SIMILITUD:
#   TRACKS  → Vector 4D (50%) + Género (20%) + BPM+Camelot (15%) + Era (10%) + Idioma (5%)
#   ÁLBUMES → Vector 4D (45%) + Género (25%) + Era (15%) + Bailabilidad (10%) + Década (5%)
#
# ALCANCE:
#   Toda la BD → procesa todos los artistas/álbumes/tracks
#   Por letra  → procesa artistas de esa letra y sus álbumes/tracks
#   Por artista → procesa un artista específico y sus álbumes/tracks
#   Por álbum  → procesa un álbum específico por su ID y sus tracks
# ============================================================

import os, re, sys, time, json, math, logging, argparse, sqlite3, threading
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Set
from collections import defaultdict, Counter

import requests
from tqdm import tqdm
from rapidfuzz import fuzz

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

# ============================================================
# CONFIGURACIÓN
# ============================================================

DB_PATH     = "music.db"
MB_BASE     = "https://musicbrainz.org/ws/2"
LASTFM_BASE = "http://ws.audioscrobbler.com/2.0/"

HEADERS_MB  = {"User-Agent": "RichMetaRelations/2.0 (opensource@local)"}
MB_RATE     = 1.1
LFM_RATE    = 0.5


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

LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")

SIMILAR_TRACKS_TOP_N     = 10
SIMILAR_TRACKS_MIN_SCORE = 0.65
SIMILAR_ALBUMS_TOP_N     = 8
SIMILAR_ALBUMS_MIN_SCORE = 0.60
SIMILAR_ARTISTS_TOP_N    = 10
ARTIST_FUZZY_THRESHOLD   = 82

W_TRACK = {"vector": 0.50, "genre": 0.20, "bpm": 0.15, "era": 0.10, "idioma": 0.05}
W_ALBUM = {"vector": 0.45, "genre": 0.25, "era": 0.15, "bailabilidad": 0.10, "decada": 0.05}

CAMELOT_ADJACENT = {}
for _n in range(1, 13):
    _prev = (((_n - 2) % 12) + 1)
    _next = ((_n % 12) + 1)
    for _l in ("A", "B"):
        _opp = "B" if _l == "A" else "A"
        CAMELOT_ADJACENT[f"{_n}{_l}"] = {f"{_prev}{_l}", f"{_next}{_l}", f"{_n}{_opp}"}

# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("richmetarelations")
log.setLevel(logging.DEBUG)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", "%H:%M:%S"))
log.addHandler(_ch)
_fh = logging.FileHandler("richmetarelations.log", mode="a", encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)

# ============================================================
# RATE LIMITERS
# ============================================================

_mb_last, _mb_lock   = 0.0, threading.Lock()
_lfm_last, _lfm_lock = 0.0, threading.Lock()

def _mb_wait():
    global _mb_last
    with _mb_lock:
        d = time.time() - _mb_last
        if d < MB_RATE: time.sleep(MB_RATE - d)
        _mb_last = time.time()

def _lfm_wait():
    global _lfm_last
    with _lfm_lock:
        d = time.time() - _lfm_last
        if d < LFM_RATE: time.sleep(LFM_RATE - d)
        _lfm_last = time.time()

# ============================================================
# HELPERS GENERALES
# ============================================================

def _norm_name(text: str) -> str:
    if not text: return ""
    t = text.lower()
    t = re.sub(r"^(the|los|las|les|die)\s+", "", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()

def _cosine(v1: dict, v2: dict, dims=("energy", "valence", "tension", "depth")) -> float:
    dot = sum(v1.get(d, 0) * v2.get(d, 0) for d in dims)
    n1  = math.sqrt(sum(v1.get(d, 0) ** 2 for d in dims))
    n2  = math.sqrt(sum(v2.get(d, 0) ** 2 for d in dims))
    if not n1 or not n2: return 0.0
    return max(-1.0, min(1.0, dot / (n1 * n2)))

def _safe_float(v) -> Optional[float]:
    try:
        f = float(v); return f if f == f else None
    except (TypeError, ValueError): return None

# ============================================================
# DB — MIGRACIÓN Y CONEXIÓN
# ============================================================

MIGRATIONS = [
    "ALTER TABLE artists    ADD COLUMN nationality          TEXT DEFAULT NULL",
    "ALTER TABLE artists    ADD COLUMN similar_artists_json TEXT DEFAULT NULL",
    "ALTER TABLE album_meta ADD COLUMN similar_albums_json  TEXT DEFAULT NULL",
    "ALTER TABLE track_meta ADD COLUMN similar_tracks_json  TEXT DEFAULT NULL",
]

def run_migrations(conn: sqlite3.Connection):
    cur = conn.cursor()
    for sql in MIGRATIONS:
        try: cur.execute(sql)
        except sqlite3.OperationalError: pass
    conn.commit()
    log.debug("Migraciones OK")

def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    return conn

# ============================================================
# SCOPE — Definición y helpers
# ============================================================
# Un "scope" delimita qué artistas/álbumes/tracks se procesan.
# Siempre se usa la BD completa como pool de candidatos para
# los módulos de similitud; el scope solo controla qué filas
# reciben el UPDATE al final.
#
# Estructura:
#   {"type": "all"}
#   {"type": "letter",  "letter":  "M"}
#   {"type": "artist",  "artist_id": 42, "artist_name": "Metallica"}
#   {"type": "album",   "album_id": 125, "album_name": "Master of Puppets"}
# ============================================================

SCOPE_ALL = {"type": "all"}

def _scope_artist_ids(conn: sqlite3.Connection, scope: dict) -> Optional[Set[int]]:
    """None = todos."""
    if scope["type"] == "all":
        return None
    elif scope["type"] == "album":
        row = conn.execute("SELECT artist_id FROM albums WHERE id = ?", (scope["album_id"],)).fetchone()
        return {row[0]} if row and row[0] is not None else set()
    elif scope["type"] == "letter":
        rows = conn.execute(
            "SELECT id FROM artists WHERE letter = ?", (scope["letter"],)
        ).fetchall()
        return {r[0] for r in rows}
    elif scope["type"] == "artist":
        return {scope["artist_id"]}
    return None

def _scope_album_ids(conn: sqlite3.Connection, scope: dict) -> Optional[Set[int]]:
    if scope["type"] == "all": return None
    if scope["type"] == "album": return {scope["album_id"]}
    aids = _scope_artist_ids(conn, scope)
    if not aids: return set()
    ph = ",".join("?" * len(aids))
    rows = conn.execute(
        f"SELECT id FROM albums WHERE artist_id IN ({ph})", list(aids)
    ).fetchall()
    return {r[0] for r in rows}

def _scope_track_ids(conn: sqlite3.Connection, scope: dict) -> Optional[Set[int]]:
    if scope["type"] == "all": return None
    album_ids = _scope_album_ids(conn, scope)
    if not album_ids: return set()
    ph = ",".join("?" * len(album_ids))
    rows = conn.execute(
        f"SELECT id FROM tracks WHERE album_id IN ({ph})", list(album_ids)
    ).fetchall()
    return {r[0] for r in rows}

def _scope_label(scope: dict) -> str:
    if scope["type"] == "all":    return "Toda la biblioteca"
    if scope["type"] == "letter": return f"Letra '{scope['letter']}'"
    if scope["type"] == "artist": return f"Artista '{scope['artist_name']}'"
    if scope["type"] == "album":  return f"Álbum '{scope['album_name']}' (ID: {scope['album_id']})"
    return "?"

# ============================================================
# MÓDULO 1 — NACIONALIDAD  (MusicBrainz)
# ============================================================

def _mb_artist_search(name: str) -> Optional[dict]:
    _mb_wait()
    try:
        r = requests.get(
            f"{MB_BASE}/artist",
            params={"query": f'artist:"{name}"', "fmt": "json", "limit": 5},
            headers=HEADERS_MB, timeout=12,
        )
        r.raise_for_status()
        for a in r.json().get("artists", []):
            if int(a.get("score", 0)) < 70: continue
            if fuzz.ratio(_norm_name(a.get("name", "")), _norm_name(name)) < 75: continue
            country   = a.get("country")
            area_name = (a.get("area") or {}).get("name")
            if country or area_name:
                return {"country": country, "area_name": area_name}
        return None
    except Exception as e:
        log.debug("MB artist error '%s': %s", name, e)
        return None

def process_nationalities(conn: sqlite3.Connection,
                           scope: dict = SCOPE_ALL,
                           force: bool = False):
    cur = conn.cursor()
    aids = _scope_artist_ids(conn, scope)

    if aids is not None:
        ph = ",".join("?" * len(aids))
        if force:
            artists = cur.execute(
                f"SELECT id, name FROM artists WHERE id IN ({ph}) ORDER BY name", list(aids)
            ).fetchall()
        else:
            artists = cur.execute(
                f"SELECT id, name FROM artists WHERE id IN ({ph}) AND nationality IS NULL ORDER BY name",
                list(aids),
            ).fetchall()
    else:
        if force:
            artists = cur.execute("SELECT id, name FROM artists ORDER BY name").fetchall()
        else:
            artists = cur.execute(
                "SELECT id, name FROM artists WHERE nationality IS NULL ORDER BY name"
            ).fetchall()

    log.info("  Nacionalidades: %d artistas a procesar", len(artists))
    ok = skipped = 0
    for row in tqdm(artists, desc="Nacionalidades", unit="artista"):
        result = _mb_artist_search(row["name"])
        nat = (result.get("area_name") or result.get("country") or "Unknown") if result else "Unknown"
        cur.execute("UPDATE artists SET nationality = ? WHERE id = ?", (nat, row["id"]))
        log.debug("  %s → %s", row["name"], nat)
        if result: ok += 1
        else: skipped += 1
        if (ok + skipped) % 50 == 0: conn.commit()
    conn.commit()
    log.info("  ✓ Nacionalidades OK: %d  |  Sin datos: %d", ok, skipped)

# ============================================================
# MÓDULO 2 — ARTISTAS SIMILARES  (Last.fm + filtro BD)
# ============================================================

def _lfm_similar_artists(name: str) -> List[dict]:
    _lfm_wait()
    try:
        r = requests.get(LASTFM_BASE, params={
            "method": "artist.getSimilar", "artist": name,
            "api_key": LASTFM_API_KEY, "format": "json", "limit": 30,
        }, timeout=10)
        r.raise_for_status()
        return [
            {"name": a["name"], "match": round(float(a.get("match", 0)), 4)}
            for a in r.json().get("similarartists", {}).get("artist", [])
            if _safe_float(a.get("match", 0)) is not None
        ]
    except Exception as e:
        log.debug("Last.fm similar error '%s': %s", name, e)
        return []

def _build_artist_index(conn: sqlite3.Connection) -> Dict[str, dict]:
    return {_norm_name(r["name"]): {"id": r["id"], "name": r["name"]}
            for r in conn.execute("SELECT id, name FROM artists").fetchall()}

def _match_to_db(similar_list, db_index):
    matched, seen = [], set()
    for item in similar_list:
        cand_norm = _norm_name(item["name"])
        entry = db_index.get(cand_norm)
        if not entry:
            best_score, best_key = 0, None
            for dn in db_index:
                s = fuzz.ratio(cand_norm, dn)
                if s > best_score: best_score, best_key = s, dn
            if best_score >= ARTIST_FUZZY_THRESHOLD and best_key:
                entry = db_index[best_key]
        if entry and entry["id"] not in seen:
            seen.add(entry["id"])
            matched.append({"artist_id": entry["id"], "name": entry["name"], "match": item["match"]})
    return sorted(matched, key=lambda x: -x["match"])[:SIMILAR_ARTISTS_TOP_N]

def process_similar_artists(conn: sqlite3.Connection,
                              scope: dict = SCOPE_ALL,
                              force: bool = False):
    cur = conn.cursor()
    aids = _scope_artist_ids(conn, scope)

    if aids is not None:
        ph = ",".join("?" * len(aids))
        if force:
            artists = cur.execute(
                f"SELECT id, name FROM artists WHERE id IN ({ph}) ORDER BY name", list(aids)
            ).fetchall()
        else:
            artists = cur.execute(
                f"SELECT id, name FROM artists WHERE id IN ({ph}) AND similar_artists_json IS NULL ORDER BY name",
                list(aids),
            ).fetchall()
    else:
        if force:
            artists = cur.execute("SELECT id, name FROM artists ORDER BY name").fetchall()
        else:
            artists = cur.execute(
                "SELECT id, name FROM artists WHERE similar_artists_json IS NULL ORDER BY name"
            ).fetchall()

    db_index = _build_artist_index(conn)
    log.info("  Artistas similares: %d a procesar", len(artists))
    ok = empty = 0
    for row in tqdm(artists, desc="Artistas similares", unit="artista"):
        similar_lfm = _lfm_similar_artists(row["name"])
        matched     = [m for m in _match_to_db(similar_lfm, db_index) if m["artist_id"] != row["id"]]
        result_json = json.dumps(matched, ensure_ascii=False)
        cur.execute("UPDATE artists SET similar_artists_json = ? WHERE id = ?",
                    (result_json, row["id"]))
        log.debug("  %s → %d en BD", row["name"], len(matched))
        if matched: ok += 1
        else: empty += 1
        if (ok + empty) % 50 == 0: conn.commit()
    conn.commit()
    log.info("  ✓ Con similares: %d  |  Sin match: %d", ok, empty)

# ============================================================
# MÓDULO 3 — ÁLBUMES SIMILARES  (100% local)
# ============================================================

def _load_album_data(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute("""
        SELECT a.id AS album_id, a.name AS album_name,
               ar.name AS artist_name,
               a.year, am.avg_energy, am.avg_valence,
               am.avg_tension, am.avg_depth, am.avg_bailabilidad,
               am.era, am.decada, am.genre_primary, am.mood_predominante
        FROM albums a
        JOIN album_meta am ON a.id = am.album_id
        LEFT JOIN artists ar ON a.artist_id = ar.id
        WHERE am.avg_energy IS NOT NULL
    """).fetchall()
    return [dict(r) for r in rows]

def _album_similarity(a: dict, b: dict) -> float:
    va = {k: a.get(f"avg_{k}", 0) or 0 for k in ("energy","valence","tension","depth")}
    vb = {k: b.get(f"avg_{k}", 0) or 0 for k in ("energy","valence","tension","depth")}
    vector_score = (1.0 + _cosine(va, vb)) / 2.0

    ga = (a.get("genre_primary") or "").lower().strip()
    gb = (b.get("genre_primary") or "").lower().strip()
    if ga and gb:
        genre_score = 1.0 if ga == gb else (0.4 if fuzz.partial_ratio(ga, gb) > 70 else 0.0)
    else:
        genre_score = 0.3

    ea, eb = a.get("era") or "", b.get("era") or ""
    da, db_ = a.get("decada") or 0, b.get("decada") or 0
    if ea and eb:
        era_score = 1.0 if ea == eb else (0.70 if (da and db_ and da == db_) else 0.0)
    else:
        era_score = 0.5

    ba = _safe_float(a.get("avg_bailabilidad"))
    bb = _safe_float(b.get("avg_bailabilidad"))
    bail_score = max(0.0, 1.0 - abs(ba - bb) / 50.0) if ba is not None and bb is not None else 0.5

    if da and db_:
        deca_score = max(0.0, 1.0 - abs(da - db_) / 10 * 0.3)
    else:
        deca_score = 0.5

    return (vector_score  * W_ALBUM["vector"] +
            genre_score   * W_ALBUM["genre"] +
            era_score     * W_ALBUM["era"] +
            bail_score    * W_ALBUM["bailabilidad"] +
            deca_score    * W_ALBUM["decada"])

def process_similar_albums(conn: sqlite3.Connection,
                            scope: dict = SCOPE_ALL,
                            force: bool = False):
    cur  = conn.cursor()
    all_albums = _load_album_data(conn)      # pool completo para candidatos
    log.info("  Álbumes similares: pool de %d álbumes", len(all_albums))

    # Determinar cuáles álbumes procesar según scope
    scoped_album_ids = _scope_album_ids(conn, scope)

    if force:
        to_process = (all_albums if scoped_album_ids is None
                      else [a for a in all_albums if a["album_id"] in scoped_album_ids])
    else:
        done_ids = {r[0] for r in cur.execute(
            "SELECT album_id FROM album_meta WHERE similar_albums_json IS NOT NULL"
        ).fetchall()}
        if scoped_album_ids is None:
            to_process = [a for a in all_albums if a["album_id"] not in done_ids]
        else:
            to_process = [a for a in all_albums
                          if a["album_id"] in scoped_album_ids and a["album_id"] not in done_ids]

    log.info("  → %d álbumes a calcular (scope: %s)", len(to_process), _scope_label(scope))

    batch = []
    for album in tqdm(to_process, desc="Álbumes similares", unit="álbum"):
        aid = album["album_id"]
        a_artist = album.get("artist_name", "")
        scored = []
        for cand in all_albums:
            cid = cand["album_id"]
            if cid == aid: continue
            score = _album_similarity(album, cand)
            if score >= SIMILAR_ALBUMS_MIN_SCORE:
                scored.append({
                    "album_id": cid, "album": cand.get("album_name",""),
                    "artist": cand.get("artist_name",""), "genre": cand.get("genre_primary",""),
                    "year": cand.get("year",""), "score": round(score, 3),
                    "same_artist": (a_artist != "" and
                                    cand.get("artist_name","").lower() == a_artist.lower()),
                })
        scored.sort(key=lambda x: -x["score"])
        batch.append((json.dumps(scored[:SIMILAR_ALBUMS_TOP_N], ensure_ascii=False), aid))
        if len(batch) >= 200:
            cur.executemany(
                "UPDATE album_meta SET similar_albums_json = ? WHERE album_id = ?", batch)
            conn.commit(); batch = []
    if batch:
        cur.executemany(
            "UPDATE album_meta SET similar_albums_json = ? WHERE album_id = ?", batch)
        conn.commit()
    log.info("  ✓ Álbumes escritos: %d", len(to_process))

# ============================================================
# MÓDULO 4 — TRACKS SIMILARES  (100% local)
# ============================================================

def _group_key(t: dict) -> str:
    gp = (t.get("genre_primary") or "").lower().strip()
    if gp: return gp
    mood = (t.get("mood") or "").lower().strip()
    return f"__mood_{mood}__" if mood else "__sin_clasificar__"

def _camelot_bonus(ka: Optional[str], kb: Optional[str]) -> float:
    if not ka or not kb: return 0.0
    if ka == kb: return 0.15
    return 0.10 if kb in CAMELOT_ADJACENT.get(ka, set()) else 0.0

def _load_track_data(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute("""
        SELECT t.id AS track_id, t.title, t.artist, t.album_id,
               tm.energy, tm.valence, tm.tension, tm.depth,
               tm.genre_primary, tm.genre_secondary, tm.bpm,
               tm.tonalidad, tm.era, tm.decada, tm.idioma, tm.mood, tm.tier
        FROM tracks t
        JOIN track_meta tm ON t.id = tm.track_id
        WHERE tm.energy IS NOT NULL AND tm.valence IS NOT NULL
          AND tm.tension IS NOT NULL AND tm.depth IS NOT NULL
    """).fetchall()
    return [dict(r) for r in rows]

def _track_score(base_cos: float, track: dict, cand: dict) -> float:
    ga = (track.get("genre_primary") or "").lower().strip()
    gb = (cand.get("genre_primary") or "").lower().strip()
    if ga and gb:
        genre_score = 1.0 if ga == gb else (0.5 if fuzz.partial_ratio(ga, gb) > 70 else 0.0)
    else:
        genre_score = 0.3

    bpm_a = _safe_float(track.get("bpm"))
    bpm_b = _safe_float(cand.get("bpm"))
    if bpm_a and bpm_b and bpm_a > 0 and bpm_b > 0:
        bpm_score = max(0.0, 1.0 - abs(bpm_a - bpm_b) / 30.0)
        bpm_score = min(1.0, bpm_score + _camelot_bonus(track.get("tonalidad"), cand.get("tonalidad")))
    else:
        bpm_score = 0.5

    ea, eb = track.get("era") or "", cand.get("era") or ""
    da, db_ = track.get("decada") or 0, cand.get("decada") or 0
    if ea and eb:
        era_score = 1.0 if ea == eb else (0.70 if (da and db_ and da == db_) else 0.0)
    else:
        era_score = 0.5

    ia, ib = track.get("idioma") or "", cand.get("idioma") or ""
    idioma_score = (1.0 if ia == ib else 0.0) if (ia and ib) else 0.5

    return (base_cos     * W_TRACK["vector"]  +
            genre_score  * W_TRACK["genre"]   +
            bpm_score    * W_TRACK["bpm"]     +
            era_score    * W_TRACK["era"]     +
            idioma_score * W_TRACK["idioma"])

def _compute_similar_tracks_numpy(tracks: List[dict],
                                   top_n: int = SIMILAR_TRACKS_TOP_N,
                                   min_score: float = SIMILAR_TRACKS_MIN_SCORE,
                                   ) -> Dict[int, List[dict]]:
    import numpy as np
    matrix = np.array(
        [[t.get("energy",0) or 0, t.get("valence",0) or 0,
          t.get("tension",0) or 0, t.get("depth",0) or 0] for t in tracks],
        dtype=np.float32,
    )
    norms  = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms  = np.where(norms < 1e-8, 1e-8, norms)
    normed = (matrix / norms).astype(np.float32)

    groups: Dict[str, List[int]] = defaultdict(list)
    for i, t in enumerate(tracks):
        groups[_group_key(t)].append(i)

    results: Dict[int, List[dict]] = {}
    CHUNK = 3000

    for _, indices in tqdm(groups.items(), desc="Sim. tracks", unit="grupo"):
        if len(indices) < 2: continue
        for ca_start in range(0, len(indices), CHUNK):
            chunk_a   = indices[ca_start:ca_start + CHUNK]
            normed_a  = normed[chunk_a]
            for cb_start in range(ca_start, len(indices), CHUNK):
                chunk_b  = indices[cb_start:cb_start + CHUNK]
                normed_b = normed[chunk_b]
                same     = (ca_start == cb_start)
                cos_mat  = np.clip(normed_a @ normed_b.T, -1.0, 1.0)

                for li, gi in enumerate(chunk_a):
                    track = tracks[gi]; tid = track["track_id"]; row = cos_mat[li]
                    cands = np.where(row > 0.30)[0]
                    if not len(cands): continue
                    new_scored = []
                    for lj in cands:
                        gj = chunk_b[lj]
                        if gj == gi: continue
                        score = _track_score(float(row[lj]), track, tracks[gj])
                        if score >= min_score:
                            c = tracks[gj]
                            new_scored.append({
                                "track_id": c["track_id"], "title": c.get("title",""),
                                "artist": c.get("artist",""), "album_id": c.get("album_id"),
                                "mood": c.get("mood",""), "score": round(score, 3),
                                "same_artist": (
                                    bool(track.get("artist")) and
                                    (track.get("artist","")).lower() == (c.get("artist","")).lower()
                                ),
                            })
                    if new_scored:
                        prev = results.get(tid, [])
                        merged = prev + new_scored
                        merged.sort(key=lambda x: -x["score"])
                        seen_ids, dedup = set(), []
                        for item in merged:
                            if item["track_id"] not in seen_ids:
                                seen_ids.add(item["track_id"]); dedup.append(item)
                        results[tid] = dedup[:top_n]

                if not same:
                    for lj, gj in enumerate(chunk_b):
                        track = tracks[gj]; tid = track["track_id"]; col = cos_mat[:, lj]
                        cands = np.where(col > 0.30)[0]
                        if not len(cands): continue
                        new_scored = []
                        for li in cands:
                            gi = chunk_a[li]
                            if gi == gj: continue
                            score = _track_score(float(col[li]), track, tracks[gi])
                            if score >= min_score:
                                c = tracks[gi]
                                new_scored.append({
                                    "track_id": c["track_id"], "title": c.get("title",""),
                                    "artist": c.get("artist",""), "album_id": c.get("album_id"),
                                    "mood": c.get("mood",""), "score": round(score, 3),
                                    "same_artist": (
                                        bool(track.get("artist")) and
                                        (track.get("artist","")).lower() == (c.get("artist","")).lower()
                                    ),
                                })
                        if new_scored:
                            prev = results.get(tid, [])
                            merged = prev + new_scored
                            merged.sort(key=lambda x: -x["score"])
                            seen_ids, dedup = set(), []
                            for item in merged:
                                if item["track_id"] not in seen_ids:
                                    seen_ids.add(item["track_id"]); dedup.append(item)
                            results[tid] = dedup[:top_n]
    return results

def _compute_similar_tracks_numpy_pending(tracks: List[dict], pending_ids: Set[int],
                                          top_n: int = SIMILAR_TRACKS_TOP_N,
                                          min_score: float = SIMILAR_TRACKS_MIN_SCORE,
                                          ) -> Dict[int, List[dict]]:
    """Igual que _compute_similar_tracks_numpy, pero solo para las pistas pendientes.

    La versión completa calcula la matriz de todo cada grupo (miles x miles) aunque
    se pidan 7 pistas; acá cada pista pendiente se compara contra todo su grupo
    (mismos candidatos, mismo puntaje, mismo orden), así que el resultado es igual
    y el tiempo depende de cuántas pistas haya que calcular, no del tamaño de la BD.
    """
    import numpy as np
    matrix = np.array(
        [[t.get("energy",0) or 0, t.get("valence",0) or 0,
          t.get("tension",0) or 0, t.get("depth",0) or 0] for t in tracks],
        dtype=np.float32,
    )
    norms  = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms  = np.where(norms < 1e-8, 1e-8, norms)
    normed = (matrix / norms).astype(np.float32)

    groups: Dict[str, List[int]] = defaultdict(list)
    for i, t in enumerate(tracks):
        groups[_group_key(t)].append(i)

    pend_por_grupo: Dict[str, List[int]] = defaultdict(list)
    for i, t in enumerate(tracks):
        if t["track_id"] in pending_ids:
            pend_por_grupo[_group_key(t)].append(i)

    results: Dict[int, List[dict]] = {}
    for g, filas in tqdm(pend_por_grupo.items(), desc="Sim. tracks", unit="grupo"):
        indices = groups[g]
        if len(indices) < 2: continue
        idx_arr = np.array(indices)
        for gi in filas:
            track = tracks[gi]; tid = track["track_id"]
            row = np.clip(normed[idx_arr] @ normed[gi], -1.0, 1.0)
            scored = []
            for lj in np.where(row > 0.30)[0]:
                gj = indices[lj]
                if gj == gi: continue
                score = _track_score(float(row[lj]), track, tracks[gj])
                if score >= min_score:
                    c = tracks[gj]
                    scored.append({
                        "track_id": c["track_id"], "title": c.get("title",""),
                        "artist": c.get("artist",""), "album_id": c.get("album_id"),
                        "mood": c.get("mood",""), "score": round(score, 3),
                        "same_artist": (
                            bool(track.get("artist")) and
                            (track.get("artist","")).lower() == (c.get("artist","")).lower()
                        ),
                    })
            if scored:
                scored.sort(key=lambda x: -x["score"])
                seen_ids, dedup = set(), []
                for item in scored:
                    if item["track_id"] not in seen_ids:
                        seen_ids.add(item["track_id"]); dedup.append(item)
                results[tid] = dedup[:top_n]
    return results

def _compute_similar_tracks_pure(tracks: List[dict],
                                  top_n: int = SIMILAR_TRACKS_TOP_N,
                                  min_score: float = SIMILAR_TRACKS_MIN_SCORE,
                                  ) -> Dict[int, List[dict]]:
    for t in tracks:
        e, v, tn, d = (t.get("energy",0) or 0, t.get("valence",0) or 0,
                       t.get("tension",0) or 0, t.get("depth",0) or 0)
        t["_vec"]  = (e, v, tn, d)
        t["_norm"] = math.sqrt(e*e + v*v + tn*tn + d*d)

    groups: Dict[str, List[dict]] = defaultdict(list)
    for t in tracks:
        groups[_group_key(t)].append(t)

    results: Dict[int, List[dict]] = {}
    for _, group in tqdm(groups.items(), desc="Sim. tracks (Python)", unit="grupo"):
        candidates = group if len(group) >= 5 else tracks
        for track in group:
            tid = track["track_id"]; vec = track["_vec"]; norm = track["_norm"]
            if norm < 1e-8: continue
            scored = []
            for cand in candidates:
                if cand["track_id"] == tid: continue
                if abs(cand["_vec"][0] - vec[0]) > 0.45: continue
                if abs(cand["_vec"][1] - vec[1]) > 0.55: continue
                cn = cand["_norm"]
                if cn < 1e-8: continue
                dot = sum(a*b for a,b in zip(vec, cand["_vec"]))
                cos = max(-1.0, min(1.0, dot / (norm * cn)))
                score = _track_score(cos, track, cand)
                if score >= min_score:
                    scored.append({
                        "track_id": cand["track_id"], "title": cand.get("title",""),
                        "artist": cand.get("artist",""), "album_id": cand.get("album_id"),
                        "mood": cand.get("mood",""), "score": round(score, 3),
                        "same_artist": (
                            bool(track.get("artist")) and
                            (track.get("artist","")).lower() == (cand.get("artist","")).lower()
                        ),
                    })
            if scored:
                scored.sort(key=lambda x: -x["score"])
                results[tid] = scored[:top_n]
    return results

def process_similar_tracks(conn: sqlite3.Connection,
                            scope: dict = SCOPE_ALL,
                            force: bool = False):
    cur = conn.cursor()
    all_tracks = _load_track_data(conn)
    log.info("  Tracks similares: pool de %d tracks", len(all_tracks))

    scoped_track_ids = _scope_track_ids(conn, scope)

    if force:
        pending_ids = ({t["track_id"] for t in all_tracks} if scoped_track_ids is None
                       else scoped_track_ids)
    else:
        done_ids = {r[0] for r in cur.execute(
            "SELECT track_id FROM track_meta WHERE similar_tracks_json IS NOT NULL"
        ).fetchall()}
        if scoped_track_ids is None:
            pending_ids = {t["track_id"] for t in all_tracks} - done_ids
        else:
            pending_ids = scoped_track_ids - done_ids

    log.info("  → %d pendientes (scope: %s, numpy: %s)",
             len(pending_ids), _scope_label(scope), HAS_NUMPY)

    if not pending_ids:
        log.info("  Nada que procesar — usa --force para recomputar")
        return

    # Mostrar distribución de grupos
    gd = Counter(_group_key(t) for t in all_tracks)
    log.info("  Top grupos: %s",
             " | ".join(f"{g}:{c}" for g,c in gd.most_common(5)))

    if HAS_NUMPY:
        results = _compute_similar_tracks_numpy_pending(all_tracks, pending_ids)
    else:
        results = _compute_similar_tracks_pure(all_tracks)

    results = {k: v for k, v in results.items() if k in pending_ids}
    log.info("  Tracks con similares (≥%.2f): %d", SIMILAR_TRACKS_MIN_SCORE, len(results))

    batch = []
    for tid, similar in results.items():
        batch.append((json.dumps(similar, ensure_ascii=False), tid))
        if len(batch) >= 500:
            cur.executemany(
                "UPDATE track_meta SET similar_tracks_json = ? WHERE track_id = ?", batch)
            conn.commit(); batch = []
    if batch:
        cur.executemany(
            "UPDATE track_meta SET similar_tracks_json = ? WHERE track_id = ?", batch)
        conn.commit()

    # Marcar con [] los pendientes sin resultado (para no repetirlos)
    sin_resultado = list(pending_ids - set(results.keys()))
    for i in range(0, len(sin_resultado), 1000):
        cur.executemany(
            "UPDATE track_meta SET similar_tracks_json = ? WHERE track_id = ?",
            [("[]", tid) for tid in sin_resultado[i:i+1000]])
    conn.commit()
    log.info("  ✓ Tracks escritos: %d  |  Sin similares: %d",
             len(results), len(sin_resultado))

# ============================================================
# ESTADÍSTICAS
# ============================================================

def print_stats(conn: sqlite3.Connection):
    cur = conn.cursor()
    SEP = "=" * 58
    print(f"\n{SEP}")
    print("  RICHMETARELATIONS — Estado de la BD")
    print(SEP)

    total_art = cur.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    nat_ok    = cur.execute("SELECT COUNT(*) FROM artists WHERE nationality IS NOT NULL AND nationality != 'Unknown'").fetchone()[0]
    sim_art   = cur.execute("SELECT COUNT(*) FROM artists WHERE similar_artists_json IS NOT NULL AND similar_artists_json != '[]'").fetchone()[0]
    nat_pend  = cur.execute("SELECT COUNT(*) FROM artists WHERE nationality IS NULL").fetchone()[0]
    sim_pend  = cur.execute("SELECT COUNT(*) FROM artists WHERE similar_artists_json IS NULL").fetchone()[0]
    print(f"\n  ARTISTAS ({total_art:,})")
    print(f"    Nacionalidad       : {nat_ok:>6,} ok  | {nat_pend:>5,} pendientes")
    print(f"    Artistas similares : {sim_art:>6,} ok  | {sim_pend:>5,} pendientes")
    print("\n  Top nacionalidades:")
    for r in cur.execute(
        "SELECT nationality, COUNT(*) c FROM artists WHERE nationality IS NOT NULL AND nationality != 'Unknown' GROUP BY nationality ORDER BY c DESC LIMIT 8"
    ):
        print(f"    {str(r[0]):32}: {r[1]}")

    total_alb = cur.execute("SELECT COUNT(*) FROM album_meta").fetchone()[0]
    sim_alb   = cur.execute("SELECT COUNT(*) FROM album_meta WHERE similar_albums_json IS NOT NULL AND similar_albums_json != '[]'").fetchone()[0]
    alb_pend  = cur.execute("SELECT COUNT(*) FROM album_meta WHERE similar_albums_json IS NULL").fetchone()[0]
    avg_alb   = cur.execute("SELECT AVG(json_array_length(similar_albums_json)) FROM album_meta WHERE similar_albums_json IS NOT NULL").fetchone()[0]
    print(f"\n  ÁLBUMES ({total_alb:,})")
    print(f"    Con similares      : {sim_alb:>6,} ok  | {alb_pend:>5,} pendientes")
    print(f"    Promedio similares : {(avg_alb or 0):.1f} por álbum")

    total_trk = cur.execute("SELECT COUNT(*) FROM track_meta").fetchone()[0]
    sim_trk   = cur.execute("SELECT COUNT(*) FROM track_meta WHERE similar_tracks_json IS NOT NULL AND similar_tracks_json != '[]'").fetchone()[0]
    trk_pend  = cur.execute("SELECT COUNT(*) FROM track_meta WHERE similar_tracks_json IS NULL").fetchone()[0]
    avg_trk   = cur.execute("SELECT AVG(json_array_length(similar_tracks_json)) FROM track_meta WHERE similar_tracks_json IS NOT NULL AND similar_tracks_json != '[]'").fetchone()[0]
    print(f"\n  TRACKS ({total_trk:,})")
    print(f"    Con similares      : {sim_trk:>6,} ok  | {trk_pend:>5,} pendientes")
    print(f"    Promedio similares : {(avg_trk or 0):.1f} por track")
    print(f"\n{SEP}\n")

# ============================================================
# MENÚ INTERACTIVO
# ============================================================

MODULE_LABELS = {
    "1": "Módulo 1  Nacionalidad del artista  (MusicBrainz)",
    "2": "Módulo 2  Artistas similares en BD  (Last.fm)",
    "3": "Módulo 3  Álbumes similares en BD   (vectores 4D)",
    "4": "Módulo 4  Tracks similares en BD    (vectores 4D + BPM)",
}

MODULE_FN = {
    "1": process_nationalities,
    "2": process_similar_artists,
    "3": process_similar_albums,
    "4": process_similar_tracks,
}

def menu_modulos() -> List[str]:
    """
    Menú de selección de módulos. Retorna lista de claves ["1","2","3","4"].
    """
    print()
    print("  ┌─────────────────────────────────────────────────────────────┐")
    print("  │          RichMetaRelations v2.0 — Módulos                   │")
    print("  ├─────────────────────────────────────────────────────────────┤")
    print("  │  [1]  Nacionalidad del artista     (MusicBrainz)            │")
    print("  │  [2]  Artistas similares en BD     (Last.fm + filtro BD)    │")
    print("  │  [3]  Álbumes similares en BD      (vectores 4D, 100% local)│")
    print("  │  [4]  Tracks similares en BD       (vectores 4D + BPM)      │")
    print("  │                                                             │")
    print("  │  [A]  Todos los módulos                                     │")
    print("  │  [S]  Ver estadísticas actuales                             │")
    print("  │  [Q]  Salir                                                 │")
    print("  └─────────────────────────────────────────────────────────────┘")
    print()
    print("  Puedes combinar módulos, ej: 1,3  o  1 2 4")
    print()

    while True:
        sel = input("  Módulos: ").strip().upper()
        if sel in ("Q", ""):
            sys.exit(0)
        if sel == "S":
            return ["stats"]
        if sel in ("A", "ALL"):
            return ["1", "2", "3", "4"]
        tokens = [t.strip() for t in re.split(r"[,\s]+", sel) if t.strip()]
        valid  = [t for t in tokens if t in MODULE_LABELS]
        if valid:
            return sorted(set(valid))
        print("  ❌ Opción inválida. Elige entre 1, 2, 3, 4 o A")

def menu_scope(conn: sqlite3.Connection) -> dict:
    print()
    print("  ┌─────────────────────────────────────────────────────────────┐")
    print("  │          Alcance de procesamiento                           │")
    print("  ├─────────────────────────────────────────────────────────────┤")
    print("  │  [T]  Toda la biblioteca        (todos los artistas)        │")
    print("  │  [L]  Por letra                 (A / B / C / ...)          │")
    print("  │  [A]  Artista específico        (buscar por nombre)         │")
    print("  │  [B]  Álbum específico          (por ID o búsqueda)        │")
    print("  └─────────────────────────────────────────────────────────────┘")
    print()
    while True:
        sel = input("  Alcance [T/L/A/B]: ").strip().upper()
        if sel == "T":
            return SCOPE_ALL
        elif sel == "L":
            return _menu_letra(conn)
        elif sel == "A":
            return _menu_artista_buscar(conn)
        elif sel == "B":
            return _menu_album_buscar(conn)
        else:
            print("  ❌ Opción inválida. Elige T, L, A o B")

def _menu_letra(conn: sqlite3.Connection) -> dict:
    """Nivel 1: selección de letra."""
    letras = [r[0] for r in conn.execute(
        "SELECT letter, COUNT(*) c FROM artists WHERE letter IS NOT NULL GROUP BY letter ORDER BY letter"
    ).fetchall()]
    conteos = {r[0]: r[1] for r in conn.execute(
        "SELECT letter, COUNT(*) FROM artists WHERE letter IS NOT NULL GROUP BY letter"
    ).fetchall()}

    print()
    print("  === Letras disponibles ===")
    for i, l in enumerate(letras):
        print(f"  [{i:3d}]  {l:4s}  ({conteos.get(l, 0)} artistas)")

    print("\n  [ALL]  Toda la biblioteca")
    while True:
        sel = input("\n  Número (ENTER para volver): ").strip().upper()
        if sel == "": return menu_scope(conn)
        if sel == "ALL": return SCOPE_ALL
        try:
            idx = int(sel)
            if 0 <= idx < len(letras):
                letra = letras[idx]
                return _menu_artista_en_letra(conn, letra)
        except ValueError: pass
        print("  ❌ Opción inválida")

def _menu_artista_en_letra(conn: sqlite3.Connection, letra: str) -> dict:
    """Nivel 2: artistas dentro de una letra."""
    artistas = conn.execute(
        "SELECT id, name FROM artists WHERE letter = ? ORDER BY name", (letra,)
    ).fetchall()

    print()
    print(f"  === Artistas en {letra}/ ===")
    for i, a in enumerate(artistas):
        n_alb = conn.execute(
            "SELECT COUNT(*) FROM albums WHERE artist_id = ?", (a["id"],)
        ).fetchone()[0]
        print(f"  [{i:3d}]  {a['name'][:52]:<54}({n_alb} álbumes)")

    print(f"\n  [ALL]  Todos los artistas de {letra}/")
    while True:
        sel = input("\n  Número (ENTER para volver): ").strip().upper()
        if sel == "": return _menu_letra(conn)
        if sel == "ALL":
            return {"type": "letter", "letter": letra}
        try:
            idx = int(sel)
            if 0 <= idx < len(artistas):
                a = artistas[idx]
                return {"type": "artist", "artist_id": a["id"], "artist_name": a["name"]}
        except ValueError: pass
        print("  ❌ Opción inválida")

def _menu_artista_buscar(conn: sqlite3.Connection) -> dict:
    """Búsqueda directa de artista por nombre (fuzzy)."""
    print()
    todos = conn.execute("SELECT id, name FROM artists ORDER BY name").fetchall()
    print(f"  ({len(todos)} artistas en la BD)")
    while True:
        term = input("  Buscar artista: ").strip()
        if not term:
            return menu_scope(conn)
        term_norm = _norm_name(term)
        matches = sorted(
            [(a, fuzz.ratio(_norm_name(a["name"]), term_norm)) for a in todos],
            key=lambda x: -x[1]
        )[:10]
        best = [m for m in matches if m[1] >= 40]
        if not best:
            print("  ❌ Sin coincidencias. Intenta otra búsqueda.")
            continue
        print()
        for i, (a, score) in enumerate(best):
            n_alb = conn.execute(
                "SELECT COUNT(*) FROM albums WHERE artist_id = ?", (a["id"],)
            ).fetchone()[0]
            print(f"  [{i}]  {a['name'][:52]:<54}(score {score}%  |  {n_alb} álbumes)")
        sel = input("\n  Número (ENTER para nueva búsqueda): ").strip()
        if sel == "": continue
        try:
            idx = int(sel)
            if 0 <= idx < len(best):
                a = best[idx][0]
                return {"type": "artist", "artist_id": a["id"], "artist_name": a["name"]}
        except ValueError: pass
        print("  ❌ Opción inválida")

def _menu_album_buscar(conn: sqlite3.Connection) -> dict:
    """Selección de álbum por ID directo o búsqueda por título."""
    print()
    while True:
        term = input("  Ingresa el ID del Álbum o parte de su nombre: ").strip()
        if not term:
            return menu_scope(conn)

        # Si ingresaron un número directo
        if term.isdigit():
            aid = int(term)
            row = conn.execute("SELECT id, name FROM albums WHERE id = ?", (aid,)).fetchone()
            if row:
                return {"type": "album", "album_id": row["id"], "album_name": row["name"]}
            print(f"  ❌ No existe ningún álbum con ID {aid}")
            continue

        # Búsqueda por texto (LIKE)
        rows = conn.execute(
            "SELECT a.id, a.name, ar.name as artist FROM albums a LEFT JOIN artists ar ON a.artist_id = ar.id WHERE a.name LIKE ? LIMIT 10",
            (f"%{term}%",)
        ).fetchall()

        if not rows:
            print("  ❌ No se encontraron álbumes.")
            continue

        print()
        for i, r in enumerate(rows):
            print(f"  [{i}]  [ID: {r['id']}] {r['name']} - {r['artist'] or 'Artista desconocido'}")

        sel = input("\n  Selecciona un número (ENTER para nueva búsqueda): ").strip()
        if sel == "": continue
        try:
            idx = int(sel)
            if 0 <= idx < len(rows):
                r = rows[idx]
                return {"type": "album", "album_id": r["id"], "album_name": r["name"]}
        except ValueError: pass
        print("  ❌ Opción inválida")

def menu_force() -> bool:
    print()
    sel = input("  ¿Forzar recomputo de registros ya existentes? [s/N]: ").strip().lower()
    return sel in ("s", "si", "sí", "y", "yes")

def _confirm(modules, scope, force) -> bool:
    print()
    print("  ┌─────────────────────────────────────────────────────────────┐")
    print("  │  Resumen de ejecución                                       │")
    print("  ├─────────────────────────────────────────────────────────────┤")
    mods_str = ", ".join(f"Módulo {m}" for m in modules)
    print(f"  │  Módulos : {mods_str:<48}│")
    scope_str = _scope_label(scope)
    print(f"  │  Alcance : {scope_str:<48}│")
    force_str = "Sí (recomputa todo)" if force else "No (solo pendientes)"
    print(f"  │  Forzar  : {force_str:<48}│")
    print("  └─────────────────────────────────────────────────────────────┘")
    print()
    sel = input("  ¿Confirmar? [S/n]: ").strip().lower()
    return sel not in ("n", "no")

def run_interactive(conn: sqlite3.Connection):
    """Flujo interactivo completo: módulos → scope → force → confirmar → ejecutar."""
    modules = menu_modulos()
    if modules == ["stats"]:
        print_stats(conn)
        return

    scope   = menu_scope(conn)
    force   = menu_force()

    if not _confirm(modules, scope, force):
        print("  Cancelado.")
        return

    _run_modules(conn, modules, scope, force)

def _run_modules(conn, modules, scope, force):
    t_start = time.time()
    log.info("=" * 58)
    log.info("  RichMetaRelations v2.0")
    log.info("  Módulos : %s", ", ".join(modules))
    log.info("  Alcance : %s", _scope_label(scope))
    log.info("  Forzar  : %s", force)
    log.info("  numpy   : %s", HAS_NUMPY)
    log.info("=" * 58)

    for m in modules:
        label = MODULE_LABELS.get(m, m)
        log.info("\n  ── %s", label)
        MODULE_FN[m](conn, scope, force)

    elapsed = time.time() - t_start
    log.info("\n  ✓ Completado en %.1fs (%.1f min)", elapsed, elapsed / 60)
    print_stats(conn)

# ============================================================
# MAIN + ARGPARSE
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="RichMetaRelations v2.0 — Enriquecimiento relacional de music.db",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--db",         default=DB_PATH,
                        help=f"Path a music.db (default: {DB_PATH})")
    parser.add_argument("--nationality",     action="store_true",
                        help="Módulo 1: Nacionalidad artistas")
    parser.add_argument("--similar-artists", action="store_true",
                        help="Módulo 2: Artistas similares")
    parser.add_argument("--similar-albums",  action="store_true",
                        help="Módulo 3: Álbumes similares")
    parser.add_argument("--similar-tracks",  action="store_true",
                        help="Módulo 4: Tracks similares")
    parser.add_argument("--all",         action="store_true",
                        help="Ejecutar todos los módulos")
    parser.add_argument("--force",       action="store_true",
                        help="Recomputar aunque ya existan datos")
    parser.add_argument("--stats",       action="store_true",
                        help="Solo mostrar estadísticas actuales")
    parser.add_argument("--letter", "-l", default=None,
                        help="Alcance: letra de artistas (ej: M, A, #)")
    parser.add_argument("--artist", "-a", default=None,
                        help="Alcance: nombre de artista específico (fuzzy)")
    parser.add_argument("--album", "--album-id", default=None, type=int,
                        help="Alcance: ID específico del álbum en music.db")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Log DEBUG en consola")
    args = parser.parse_args()

    if args.verbose:
        _ch.setLevel(logging.DEBUG)
    else:
        _ch.setLevel(logging.INFO)

    if not os.path.exists(args.db):
        log.error("No se encontró la base de datos: %s", args.db)
        sys.exit(1)

    conn = open_db(args.db)

    # Determinar si se usó CLI con flags de acción
    cli_modules_flags = [args.nationality, args.similar_artists,
                         args.similar_albums, args.similar_tracks, args.all]
    has_cli_action    = args.stats or any(cli_modules_flags)

    if not has_cli_action:
        # ── Modo interactivo ──────────────────────────────────────────────
        print()
        print("  ╔═════════════════════════════════════════════════════════════╗")
        print("  ║         RichMetaRelations v2.0 — Modo interactivo           ║")
        print("  ╚═════════════════════════════════════════════════════════════╝")
        print(f"  BD    : {args.db}")
        print(f"  numpy : {'disponible ✓' if HAS_NUMPY else 'no disponible (Python puro)'}")
        try:
            run_interactive(conn)
        except KeyboardInterrupt:
            print("\n\n  Interrumpido.")
            conn.commit()
        finally:
            conn.close()
        return

    # ── Modo CLI ─────────────────────────────────────────────────────────
    if args.stats:
        print_stats(conn)
        conn.close()
        return

    # Resolver scope desde CLI
    if args.album:
        row = conn.execute("SELECT id, name FROM albums WHERE id = ?", (args.album,)).fetchone()
        if row:
            scope = {"type": "album", "album_id": row["id"], "album_name": row["name"]}
            log.info("Álbum resuelto: %s (ID: %d)", row["name"], row["id"])
        else:
            log.error("No se encontró ningún álbum con ID %d", args.album)
            conn.close(); sys.exit(1)
    elif args.artist:
        todos = conn.execute("SELECT id, name FROM artists").fetchall()
        term_norm = _norm_name(args.artist)
        best = max(todos, key=lambda r: fuzz.ratio(_norm_name(r["name"]), term_norm),
                   default=None)
        if best and fuzz.ratio(_norm_name(best["name"]), term_norm) >= 50:
            scope = {"type": "artist", "artist_id": best["id"], "artist_name": best["name"]}
            log.info("Artista resuelto: %s", best["name"])
        else:
            log.error("No se encontró artista similar a '%s'", args.artist)
            conn.close(); sys.exit(1)
    elif args.letter:
        scope = {"type": "letter", "letter": args.letter.upper()}
    else:
        scope = SCOPE_ALL

    # Resolver módulos desde CLI
    run_all = args.all or not any([args.nationality, args.similar_artists,
                                   args.similar_albums, args.similar_tracks])
    modules = []
    if run_all or args.nationality:    modules.append("1")
    if run_all or args.similar_artists: modules.append("2")
    if run_all or args.similar_albums:  modules.append("3")
    if run_all or args.similar_tracks:  modules.append("4")

    try:
        _run_modules(conn, modules, scope, args.force)
    except KeyboardInterrupt:
        log.warning("\n  Interrumpido — haciendo commit parcial...")
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    main()
