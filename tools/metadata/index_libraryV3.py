#!/usr/bin/env python3
# ============================================================
# INDEX LIBRARY v2.2
#   v2.1: MP3/AAC con bitrate real del stream y primary_format MP3/AAC
#   v2.2: tags con mutagen en MP3 (ID3 dañado hacía que ffprobe dejara de leer
#         y saliera "Unknown Artist") y como respaldo en el resto de formatos
# Indexador de biblioteca de audio para music.db
# ============================================================
#
# MODOS:
#   Incremental  → solo archivos nuevos (no en BD)
#   Actualizar   → re-leer metadata de existentes + agregar nuevos
#                  mantiene track_id → preserva track_meta/similar_*
#   Completo ★   → elimina el scope de la BD y re-indexa desde cero
#                  ⚠  borra track_meta del scope → re-ejecutar RichMeta + import
#
# ALCANCE:
#   Toda la biblioteca | Por letra | Por artista | Por álbum
#
# USO:
#   python3 index_library.py           → menú interactivo
#   python3 index_library.py --incremental --all
#   python3 index_library.py --update  --letter M
#   python3 index_library.py --full    --artist "Korn"
#   python3 index_library.py --full    --album  "/mnt/musica/K/Korn/The Path Of Totality (2011) [Vinyl]"
#   python3 index_library.py --stats
# ============================================================

import os, re, sys, time, json, sqlite3, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List

# ============================================================
# CONFIGURACIÓN
# ============================================================

MUSIC_ROOT       = "/mnt/musica/"
DB_PATH          = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "music.db")
AUDIO_EXTENSIONS = {'.flac', '.dsf', '.dff', '.wav', '.aiff', '.mp3'}

COVER_FILENAMES  = [
    'cover.jpg', 'cover.jpeg', 'cover.png', 'folder.jpg', 'folder.png',
    'front.jpg', 'front.png', 'albumart.jpg', 'artwork.jpg', 'thumb.jpg',
]

def dsd_fs_real(file_path, sample_rate_ffprobe=0):
    """Frecuencia DSD real (2822400 = DSD64) leída de la cabecera del .dsf/.dff.
    ffprobe informa fs/8 (DSD64 → 352800); el mapa antiguo la tomaba como fs/16 y
    marcaba todo un nivel más alto (DSD64 como DSD128, DSD128 como DSD256)."""
    try:
        with open(file_path, "rb") as f:
            cab = f.read(65536)
        if cab[:4] == b"DSD " and cab[28:32] == b"fmt ":
            return int.from_bytes(cab[56:60], "little")
        i = cab.find(b"FS  ")
        if i >= 0 and b"FRM8" in cab[:i]:
            return int.from_bytes(cab[i + 12:i + 16], "big")
    except OSError:
        pass
    return (sample_rate_ffprobe or 0) * 8


def dsd_nombre(fs):
    """2822400 → DSD64, 5644800 → DSD128… (también la familia de 48 kHz: 3072000 → DSD64)."""
    if not fs:
        return None
    base = 44100 if fs % 44100 == 0 else 48000
    return f"DSD{round(fs / base)}"

CHANNEL_LAYOUT_MAP = {
    "mono":        "Center",
    "stereo":      "Front: L R",
    "2.1":         "Front: L R, LFE",
    "3.0":         "Front: L C R",
    "3.0(back)":   "Front: L R, Back Center",
    "quad":        "Front: L R, Back: L R",
    "4.0":         "Front: L C R, Back Center",
    "5.0":         "Front: L C R, Side: L R",
    "5.1":         "Front: L C R, Side: L R, LFE",
    "5.1(side)":   "Front: L C R, Side: L R, LFE",
    "6.1":         "Front: L C R, Back: L R, Back C, LFE",
    "7.1":         "Front: L C R, Side: L R, Back: L R, LFE",
}

COMPRESSION_MODE = {
    'flac': 'Lossless', 'alac': 'Lossless', 'pcm_s16le': 'Lossless',
    'pcm_s24le': 'Lossless', 'pcm_s32le': 'Lossless', 'pcm_f32le': 'Lossless',
    'aiff': 'Lossless', 'dsd_lsbf': 'DSD', 'dsd_msbf': 'DSD',
    'dsd_lsbf_planar': 'DSD', 'dsd_msbf_planar': 'DSD',
    'mp3': 'Lossy', 'aac': 'Lossy', 'vorbis': 'Lossy',
    'opus': 'Lossy', 'wmav2': 'Lossy',
}

# ============================================================
# LED COLOR
# ============================================================

def get_led_color(is_dsd, dsd_rate, is_mqa, mqa_type,
                  sample_rate, mqa_original_sample_rate) -> str:
    if is_mqa and mqa_type == "studio":
        return "blue"
    if is_mqa and mqa_original_sample_rate:
        try:
            if int(mqa_original_sample_rate) != sample_rate:
                return "magenta"
        except (ValueError, TypeError):
            pass
    if is_mqa:
        return "green"
    if is_dsd:
        if dsd_rate in ("DSD64", "DSD128"): return "cyan"
        if dsd_rate == "DSD256":            return "red"
        if dsd_rate == "DSD512":            return "magenta"
    return "yellow" if sample_rate <= 48000 else "white"

# ============================================================
# EXTRACCIÓN DE METADATA
# ============================================================

def _parse_num_total(tag_value) -> tuple:
    """Parsea '03/12' → ('03', '12') o '03' → ('03', '')."""
    s = str(tag_value or "").strip()
    if "/" in s:
        parts = s.split("/", 1)
        return parts[0].strip(), parts[1].strip()
    return s, ""

# Tags que entrega mutagen (modo "easy") → nombre que usa este indexador
_MUTAGEN_KEYS = {
    "artist": "artist", "albumartist": "albumartist", "album": "album", "title": "title",
    "tracknumber": "tracknumber", "discnumber": "discnumber", "date": "date",
    "genre": "genre", "organization": "publisher", "copyright": "copyright",
    "encodedby": "encoded_by", "isrc": "isrc",
}

class _SoloTags:
    def __init__(self, tags):
        self.tags = tags


def _dff_id3(file_path: str):
    """Tags de un .dff: el bloque «ID3 » de primer nivel del FRM8 (como lo escriben foobar2000 y Ingesta)."""
    import io
    import struct
    from mutagen.id3 import ID3
    with open(file_path, "rb") as f:
        cab = f.read(16)
        if cab[:4] != b"FRM8" or cab[12:16] != b"DSD ":
            return None
        fin = 12 + struct.unpack(">Q", cab[4:12])[0]
        pos = 16
        while pos + 12 <= fin:
            f.seek(pos)
            h = f.read(12)
            if len(h) < 12:
                break
            tam = struct.unpack(">Q", h[4:12])[0]
            if h[:4] == b"ID3 ":
                return _SoloTags(ID3(io.BytesIO(f.read(tam))))
            pos += 12 + tam + (tam & 1)
    return None


_ID3_FRAMES = {
    "artist": "TPE1", "albumartist": "TPE2", "album": "TALB", "title": "TIT2", "tracknumber": "TRCK",
    "discnumber": "TPOS", "date": "TDRC", "genre": "TCON", "organization": "TPUB", "copyright": "TCOP",
    "encodedby": "TENC", "isrc": "TSRC",
}

def _tags_mutagen(file_path: str) -> dict:
    """Lee los tags con mutagen (más tolerante que ffprobe con ID3 dañados).
    Devuelve {} si mutagen no está o no puede leer el archivo."""
    try:
        if file_path.lower().endswith(".dff"):
            audio = _dff_id3(file_path)      # sin recorrer los cuadros DST (lento en disco de red)
        else:
            from mutagen import File as MutagenFile
            audio = MutagenFile(file_path, easy=True)
    except Exception:
        return {}
    if not audio or not getattr(audio, "tags", None):
        return {}
    out = {}
    # DSF / DFF: mutagen no tiene «easy» para ellos y devuelve los frames ID3 crudos (TIT2, TPE1…)
    crudo_id3 = hasattr(audio.tags, "getall") and not hasattr(audio.tags, "valid_keys")
    for k, destino in _MUTAGEN_KEYS.items():
        try:
            if crudo_id3:
                fr = audio.tags.get(_ID3_FRAMES.get(k, ""))
                v = [str(x) for x in fr.text] if fr is not None and getattr(fr, "text", None) else None
            else:
                v = audio.tags.get(k)
        except Exception:
            v = None
        if not v:
            continue
        if isinstance(v, (list, tuple)):
            v = "; ".join(str(x).strip() for x in v if str(x).strip())
        v = str(v).strip()
        if v:
            out[destino] = v
    return out

def extract_metadata(file_path: str) -> dict:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló: {file_path}")

    data   = json.loads(result.stdout)
    fmt    = data.get("format", {})
    tags   = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    # MP3: los tags de mutagen mandan (ffprobe deja de leer el ID3 si un frame
    # viene dañado). Otros formatos: mutagen solo completa lo que falte.
    _mt = _tags_mutagen(file_path)
    if _mt:
        if file_path.lower().endswith(".mp3"):
            tags.update(_mt)
        else:
            for k, v in _mt.items():
                if not tags.get(k):
                    tags[k] = v
    stream = next((s for s in data.get("streams", [])
                   if s["codec_type"] == "audio"), {})

    # ── Artista / álbum ──────────────────────────────────────
    artist       = (tags.get("artist") or tags.get("albumartist")
                    or tags.get("album_artist") or "Unknown Artist")
    album_artist = tags.get("albumartist") or tags.get("album_artist") or ""
    album        = tags.get("album") or "Unknown Album"

    # ── Numeración ──────────────────────────────────────────
    track_num, total_tracks = _parse_num_total(
        tags.get("tracknumber") or tags.get("track") or "0"
    )
    if not total_tracks:
        total_tracks = tags.get("tracktotal") or tags.get("totaltracks") or ""

    disc_num, total_discs = _parse_num_total(
        tags.get("discnumber") or tags.get("disc") or ""
    )
    if not total_discs:
        total_discs = tags.get("disctotal") or tags.get("totaldiscs") or ""

    # ── Año ──────────────────────────────────────────────────
    year_tag   = tags.get("date", "")
    year_match = re.search(r"(\d{4})", str(year_tag))
    year       = year_match.group(1) if year_match else None

    # ── Audio técnico ────────────────────────────────────────
    duration     = float(fmt.get("duration", 0))
    sample_rate  = int(stream.get("sample_rate", 0))
    bit_depth    = int(stream.get("bits_per_raw_sample") or
                       stream.get("bits_per_sample") or 0)
    channels     = int(stream.get("channels", 0))
    codec        = stream.get("codec_name", "unknown").lower()
    file_size    = os.path.getsize(file_path)

    raw_layout   = stream.get("channel_layout", "") or ""
    channel_layout = CHANNEL_LAYOUT_MAP.get(raw_layout, raw_layout or f"{channels}ch")

    compression  = COMPRESSION_MODE.get(codec, "Lossless" if codec == "flac" else "Unknown")

    # Con pérdida (MP3/AAC...): el bit_rate del contenedor incluye la carátula
    # embebida (un MP3 320 CBR figuraba como 321-343k). El del stream es el
    # real del audio; si ffprobe no lo trae (algunos VBR), se usa el del formato.
    if compression == "Lossy":
        bitrate_str = stream.get("bit_rate") or fmt.get("bit_rate") or "0"
    else:
        bitrate_str = fmt.get("bit_rate") or stream.get("bit_rate") or "0"
    bitrate      = int(float(bitrate_str)) if bitrate_str else 0

    # ── Fecha de modificación ────────────────────────────────
    mtime = os.path.getmtime(file_path)
    file_modified_date = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    # ── DSD ──────────────────────────────────────────────────
    ext    = Path(file_path).suffix.lower()
    is_dsd = ext in {'.dsf', '.dff'}
    dsd_rate = dsd_nombre(dsd_fs_real(file_path, sample_rate)) if is_dsd else None

    # ── MQA ──────────────────────────────────────────────────
    is_mqa   = False
    mqa_type = None
    mqa_orig_sr = tags.get("originalsamplerate")

    encoder   = tags.get("encoder", "").lower()
    tag_vals  = " ".join(str(v) for v in tags.values()).lower()

    if ("mqaencode" in encoder or "mqaencoder" in tags or mqa_orig_sr or
            "mqa" in tag_vals or "tidal masters" in tag_vals or
            "mqa" in Path(file_path).name.lower()):
        is_mqa = True

    if is_mqa:
        mqa_type = ("studio" if "studio" in encoder or
                    "mqa studio" in " ".join(str(v) for v in tags.values()).lower()
                    else "mqa")

    # ── sample_rate_real ─────────────────────────────────────
    try:
        sr_real = int(mqa_orig_sr) if (is_mqa and mqa_orig_sr) else sample_rate
    except (ValueError, TypeError):
        sr_real = sample_rate

    led_color = get_led_color(is_dsd, dsd_rate, is_mqa, mqa_type, sample_rate, mqa_orig_sr)

    return {
        "title":                    tags.get("title") or Path(file_path).stem,
        "artist":                   artist,
        "album_artist":             album_artist,
        "album":                    album,
        "track_number":             track_num,
        "total_tracks":             total_tracks,
        "disc_number":              disc_num,
        "total_discs":              total_discs,
        "year":                     year,
        "genre":                    tags.get("genre", ""),
        "publisher":                tags.get("publisher", ""),
        "copyright":                tags.get("copyright", ""),
        "encoded_by":               tags.get("encoded_by") or tags.get("encodedby") or "",
        "isrc":                     tags.get("isrc", ""),
        "duration":                 duration,
        "sample_rate":              sample_rate,
        "sample_rate_real":         sr_real,
        "bit_depth":                bit_depth,
        "channels":                 channels,
        "channel_layout":           channel_layout,
        "bitrate":                  bitrate,
        "codec":                    codec,
        "compression_mode":         compression,
        "file_size":                file_size,
        "file_path":                file_path,
        "file_modified_date":       file_modified_date,
        "is_dsd":                   int(is_dsd),
        "dsd_rate":                 dsd_rate,
        "is_mqa":                   int(is_mqa),
        "mqa_type":                 mqa_type,
        "mqa_original_sample_rate": mqa_orig_sr,
        "led_color":                led_color,
    }

# ============================================================
# COVER
# ============================================================

def find_cover(album_path: Path) -> Optional[str]:
    for p in [album_path, album_path.parent]:
        try:
            for f in os.listdir(p):
                if f.lower() in COVER_FILENAMES:
                    return str(p / f)
        except PermissionError:
            pass
    return None

# ============================================================
# BASE DE DATOS — MIGRACIÓN (sin DROP)
# ============================================================

TRACKS_SCHEMA = [
    # columna              tipo
    ("disc_number",                "TEXT"),
    ("total_tracks",               "TEXT"),
    ("total_discs",                "TEXT"),
    ("artist",                     "TEXT"),
    ("album_artist",               "TEXT"),
    ("year",                       "TEXT"),
    ("copyright",                  "TEXT"),
    ("encoded_by",                 "TEXT"),
    ("isrc",                       "TEXT"),
    ("file_modified_date",         "TEXT"),
    ("sample_rate_real",           "INTEGER"),
    ("channel_layout",             "TEXT"),
    ("bitrate",                    "INTEGER"),
    ("compression_mode",           "TEXT"),
]

def migrate_db(conn: sqlite3.Connection):
    """
    Crea tablas si no existen y agrega columnas faltantes.
    NUNCA hace DROP — seguro en BD con datos existentes.
    """
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS artists (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE,
            letter     TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS albums (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id      INTEGER,
            name           TEXT,
            year           TEXT,
            path           TEXT UNIQUE,
            cover_path     TEXT,
            primary_format TEXT,
            track_count    INTEGER,
            total_duration REAL,
            letter         TEXT,
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(artist_id) REFERENCES artists(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id                 INTEGER,
            track_number             TEXT,
            disc_number              TEXT,
            total_tracks             TEXT,
            total_discs              TEXT,
            title                    TEXT,
            artist                   TEXT,
            album_artist             TEXT,
            year                     TEXT,
            genre                    TEXT,
            publisher                TEXT,
            copyright                TEXT,
            encoded_by               TEXT,
            isrc                     TEXT,
            file_path                TEXT UNIQUE,
            file_size                INTEGER,
            file_modified_date       TEXT,
            duration                 REAL,
            sample_rate              INTEGER,
            sample_rate_real         INTEGER,
            bit_depth                INTEGER,
            channels                 INTEGER,
            channel_layout           TEXT,
            bitrate                  INTEGER,
            codec                    TEXT,
            compression_mode         TEXT,
            is_dsd                   INTEGER DEFAULT 0,
            dsd_rate                 TEXT,
            is_mqa                   INTEGER DEFAULT 0,
            mqa_type                 TEXT,
            mqa_original_sample_rate TEXT,
            led_color                TEXT,
            created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(album_id) REFERENCES albums(id)
        )
    """)

    # Migrar columnas faltantes si la tabla ya existía con schema antiguo
    existing_cols = {r[1] for r in cur.execute("PRAGMA table_info(tracks)").fetchall()}
    for col_name, col_type in TRACKS_SCHEMA:
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE tracks ADD COLUMN {col_name} {col_type}")
            print(f"  [migración] tracks.{col_name} añadida")

    # Índices
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_artists_letter    ON artists(letter)",
        "CREATE INDEX IF NOT EXISTS idx_albums_artist     ON albums(artist_id)",
        "CREATE INDEX IF NOT EXISTS idx_albums_path       ON albums(path)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_album      ON tracks(album_id)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_path       ON tracks(file_path)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_artist     ON tracks(artist)",
        "CREATE INDEX IF NOT EXISTS idx_tracks_alb_artist ON tracks(album_artist)",
    ]:
        cur.execute(sql)

    conn.commit()

# ============================================================
# HELPERS DE SCOPE — borrado seguro por IDs
# ============================================================

def _delete_album_scope(conn: sqlite3.Connection, album_ids: list) -> tuple:
    """
    Elimina albums + tracks + track_meta + album_meta por IDs.
    Retorna (n_albums, n_tracks, led_backup) donde led_backup={file_path:led_color}
    guardado ANTES de borrar para restaurarlo tras el re-insert en modo full.
    """
    if not album_ids:
        return 0, 0, {}
    cur = conn.cursor()
    ph  = ",".join("?" * len(album_ids))

    # Leer tracks ANTES de borrar para backup de led_color
    track_rows = cur.execute(
        f"SELECT id, file_path, led_color FROM tracks WHERE album_id IN ({ph})",
        album_ids
    ).fetchall()
    track_ids  = [r[0] for r in track_rows]
    led_backup = {r[1]: r[2] for r in track_rows if r[1] and r[2]}

    tph = ",".join("?" * len(track_ids)) if track_ids else "NULL"

    try:
        if track_ids:
            cur.execute(f"DELETE FROM track_meta WHERE track_id IN ({tph})", track_ids)
    except sqlite3.OperationalError:
        pass

    if track_ids:
        cur.execute(f"DELETE FROM tracks WHERE id IN ({tph})", track_ids)

    try:
        cur.execute(f"DELETE FROM album_meta WHERE album_id IN ({ph})", album_ids)
    except sqlite3.OperationalError:
        pass

    cur.execute(f"DELETE FROM albums WHERE id IN ({ph})", album_ids)

    cur.execute("""
        DELETE FROM artists
        WHERE id NOT IN (
            SELECT DISTINCT artist_id FROM albums WHERE artist_id IS NOT NULL
        )
    """)

    conn.commit()
    return len(album_ids), len(track_ids), led_backup

def _album_ids_for_paths(cur: sqlite3.Cursor, paths: List[str]) -> List[int]:
    ids = []
    for p in paths:
        norm = p.rstrip("/")
        rows = cur.execute(
            "SELECT id FROM albums WHERE RTRIM(path, '/') = ?", (norm,)
        ).fetchall()
        ids.extend(r[0] for r in rows)
    return ids

def _album_ids_for_artist_dir(cur: sqlite3.Cursor, artist_dir: str) -> List[int]:
    """Todos los albums cuyo path está dentro de artist_dir."""
    norm = artist_dir.rstrip("/")
    rows = cur.execute(
        "SELECT id FROM albums WHERE path LIKE ?", (f"{norm}/%",)
    ).fetchall()
    # También incluir si el path ES exactamente artist_dir (poco común)
    rows += cur.execute(
        "SELECT id FROM albums WHERE RTRIM(path, '/') = ?", (norm,)
    ).fetchall()
    return list({r[0] for r in rows})

# ============================================================
# INDEXACIÓN CORE
# ============================================================

def _upsert_artist(cur: sqlite3.Cursor, name: str) -> int:
    """
    Artista para la tabla artists: usa album_artist si disponible,
    si no usa artist del track. Retorna artist_id.
    """
    letter = name[0].upper() if name else "#"
    cur.execute(
        "INSERT OR IGNORE INTO artists (name, letter) VALUES (?, ?)",
        (name, letter)
    )
    return cur.execute("SELECT id FROM artists WHERE name = ?", (name,)).fetchone()[0]

def _upsert_album(cur: sqlite3.Cursor, artist_id: int, meta: dict,
                  album_dir: str) -> int:
    """Busca el album por path; si no existe lo inserta. Retorna album_id."""
    row = cur.execute("SELECT id FROM albums WHERE path = ?", (album_dir,)).fetchone()
    if row:
        return row[0]
    cover   = find_cover(Path(album_dir))
    letter  = meta["album"][0].upper() if meta["album"] else "#"
    cur.execute("""
        INSERT INTO albums (artist_id, name, year, path, cover_path, letter)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (artist_id, meta["album"], meta["year"], album_dir, cover, letter))
    return cur.lastrowid

def _insert_track(cur: sqlite3.Cursor, album_id: int, meta: dict):
    cur.execute("""
        INSERT INTO tracks (
            album_id, track_number, disc_number, total_tracks, total_discs,
            title, artist, album_artist, year, genre, publisher,
            copyright, encoded_by, isrc,
            file_path, file_size, file_modified_date,
            duration, sample_rate, sample_rate_real, bit_depth,
            channels, channel_layout, bitrate, codec, compression_mode,
            is_dsd, dsd_rate, is_mqa, mqa_type, mqa_original_sample_rate,
            led_color
        ) VALUES (
            ?,?,?,?,?,  ?,?,?,?,?,?,  ?,?,?,
            ?,?,?,  ?,?,?,?,  ?,?,?,?,?,  ?,?,?,?,?,  ?
        )
    """, (
        album_id,
        meta["track_number"], meta["disc_number"],
        meta["total_tracks"], meta["total_discs"],
        meta["title"], meta["artist"], meta["album_artist"],
        meta["year"], meta["genre"], meta["publisher"],
        meta["copyright"], meta["encoded_by"], meta["isrc"],
        meta["file_path"], meta["file_size"], meta["file_modified_date"],
        meta["duration"], meta["sample_rate"], meta["sample_rate_real"],
        meta["bit_depth"], meta["channels"], meta["channel_layout"],
        meta["bitrate"], meta["codec"], meta["compression_mode"],
        meta["is_dsd"], meta["dsd_rate"], meta["is_mqa"],
        meta["mqa_type"], meta["mqa_original_sample_rate"],
        meta["led_color"],
    ))

def _update_track(cur: sqlite3.Cursor, track_id: int, meta: dict):
    """
    Re-escribe metadata de un track existente preservando su ID (track_meta intacto).
    led_color NO se actualiza: campo curado manualmente.
    """
    cur.execute("""
        UPDATE tracks SET
            track_number=?, disc_number=?, total_tracks=?, total_discs=?,
            title=?, artist=?, album_artist=?, year=?, genre=?, publisher=?,
            copyright=?, encoded_by=?, isrc=?,
            file_size=?, file_modified_date=?,
            duration=?, sample_rate=?, sample_rate_real=?, bit_depth=?,
            channels=?, channel_layout=?, bitrate=?, codec=?, compression_mode=?,
            is_dsd=?, dsd_rate=?, is_mqa=?, mqa_type=?, mqa_original_sample_rate=?
        WHERE id=?
    """, (
        meta["track_number"], meta["disc_number"],
        meta["total_tracks"], meta["total_discs"],
        meta["title"], meta["artist"], meta["album_artist"],
        meta["year"], meta["genre"], meta["publisher"],
        meta["copyright"], meta["encoded_by"], meta["isrc"],
        meta["file_size"], meta["file_modified_date"],
        meta["duration"], meta["sample_rate"], meta["sample_rate_real"],
        meta["bit_depth"], meta["channels"], meta["channel_layout"],
        meta["bitrate"], meta["codec"], meta["compression_mode"],
        meta["is_dsd"], meta["dsd_rate"], meta["is_mqa"],
        meta["mqa_type"], meta["mqa_original_sample_rate"],
        track_id,
    ))

def _update_album_totals(cur: sqlite3.Cursor, album_id: int):
    tracks = cur.execute(
        "SELECT is_mqa, is_dsd, duration, compression_mode, codec FROM tracks WHERE album_id = ?",
        (album_id,)
    ).fetchall()
    if not tracks:
        return
    mqa_c = sum(1 for t in tracks if t[0])
    dsd_c = sum(1 for t in tracks if t[1])
    lossy = [(t[4] or "").lower() for t in tracks if t[3] == "Lossy"]
    total_dur = sum(t[2] or 0 for t in tracks)
    n = len(tracks)
    if mqa_c > dsd_c and mqa_c >= n - mqa_c - dsd_c:
        fmt = "MQA"
    elif dsd_c > mqa_c and dsd_c >= n - mqa_c - dsd_c:
        fmt = "DSD"
    elif len(lossy) * 2 > n:
        # Mayoría con pérdida → 'MP3' / 'AAC' / 'OGG' (antes quedaba 'FLAC')
        codec = max(set(lossy), key=lossy.count)
        fmt = {"vorbis": "OGG", "wmav1": "WMA", "wmav2": "WMA"}.get(codec, codec.upper() or "MP3")
    else:
        fmt = "FLAC"
    cur.execute(
        "UPDATE albums SET primary_format=?, track_count=?, total_duration=? WHERE id=?",
        (fmt, n, total_dur, album_id)
    )

def index_directories(conn: sqlite3.Connection, dirs: List[str],
                       mode: str, verbose: bool = True) -> dict:
    """
    Indexa recursivamente los directorios dados.

    mode:
      "incremental" → solo archivos nuevos (no en tracks.file_path)
      "update"      → UPDATE existentes + INSERT nuevos (preserva track_ids)
      "full"        → igual que incremental (el caller ya borró el scope)
    """
    cur = conn.cursor()

    # Cargar paths ya indexados (para incremental e update)
    indexed = {}
    if mode in ("incremental", "update"):
        rows = cur.execute("SELECT file_path, id FROM tracks").fetchall()
        indexed = {r[0]: r[1] for r in rows}

    # Recopilar todos los archivos de audio en los dirs
    all_files = []
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in sorted(files):
                if Path(f).suffix.lower() in AUDIO_EXTENSIONS:
                    all_files.append(os.path.join(root, f))

    total   = len(all_files)
    inserted = updated = skipped = errors = 0
    updated_albums = set()

    print(f"\n  {total} archivos de audio encontrados en {len(dirs)} directorio(s)")
    if total == 0:
        return {"inserted": 0, "updated": 0, "skipped": 0, "errors": 0}

    for i, file_path in enumerate(all_files, 1):
        # ── Incremental: saltar si ya existe ─────────────────
        if mode == "incremental" and file_path in indexed:
            skipped += 1
            continue

        try:
            meta       = extract_metadata(file_path)
            album_dir  = str(Path(file_path).parent)

            # Artista para tabla artists: album_artist > artist
            artists_name = (meta["album_artist"] or meta["artist"] or "Unknown Artist").strip()
            artist_id    = _upsert_artist(cur, artists_name)
            album_id     = _upsert_album(cur, artist_id, meta, album_dir)

            if mode == "update" and file_path in indexed:
                _update_track(cur, indexed[file_path], meta)
                updated += 1
            else:
                try:
                    _insert_track(cur, album_id, meta)
                    inserted += 1
                except sqlite3.IntegrityError:
                    # file_path UNIQUE ya existe (edge case en full mode)
                    if mode == "update":
                        tid = cur.execute(
                            "SELECT id FROM tracks WHERE file_path = ?", (file_path,)
                        ).fetchone()
                        if tid:
                            _update_track(cur, tid[0], meta)
                            updated += 1

            updated_albums.add(album_id)

            if verbose:
                status = "↺" if (mode == "update" and file_path in indexed) else "+"
                print(f"  [{i:5}/{total}] {status} {meta['artist'][:28]}"
                      f" — {Path(file_path).name[:42]}")

            if (i % 100) == 0:
                conn.commit()

        except Exception as e:
            errors += 1
            print(f"  [ERROR] {file_path}: {e}")

    # Actualizar totales de todos los albums tocados
    for aid in updated_albums:
        _update_album_totals(cur, aid)

    conn.commit()
    return {"inserted": inserted, "updated": updated,
            "skipped": skipped, "errors": errors}

# ============================================================
# ESTADÍSTICAS
# ============================================================

def print_stats(conn: sqlite3.Connection):
    cur  = conn.cursor()
    sep  = "=" * 55
    print(f"\n{sep}")
    print("  INDEX LIBRARY — Estado de la BD")
    print(sep)

    n_art = cur.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
    n_alb = cur.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
    n_trk = cur.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    print(f"\n  Artistas : {n_art:>7,}")
    print(f"  Álbumes  : {n_alb:>7,}")
    print(f"  Tracks   : {n_trk:>7,}")

    print("\n  Formatos:")
    for r in cur.execute(
        "SELECT primary_format, COUNT(*) c FROM albums "
        "WHERE primary_format IS NOT NULL GROUP BY primary_format ORDER BY c DESC"
    ):
        print(f"    {str(r[0]):8}: {r[1]:,} álbumes")

    print("\n  Codecs:")
    for r in cur.execute(
        "SELECT codec, COUNT(*) c FROM tracks "
        "WHERE codec IS NOT NULL GROUP BY codec ORDER BY c DESC LIMIT 8"
    ):
        print(f"    {str(r[0]):10}: {r[1]:,}")

    try:
        n_tm = cur.execute("SELECT COUNT(*) FROM track_meta").fetchone()[0]
        n_am = cur.execute("SELECT COUNT(*) FROM album_meta").fetchone()[0]
        print(f"\n  track_meta : {n_tm:>7,} / {n_trk:,} tracks")
        print(f"  album_meta : {n_am:>7,} / {n_alb:,} álbumes")
    except sqlite3.OperationalError:
        pass
    print(f"\n{sep}\n")

# ============================================================
# NAVEGACIÓN DEL SISTEMA DE ARCHIVOS
# ============================================================

def _subdirs(path: str) -> List[str]:
    try:
        return sorted(d for d in os.listdir(path)
                      if os.path.isdir(os.path.join(path, d))
                      and not d.startswith("."))
    except PermissionError:
        return []

def _count_audio(path: str) -> int:
    count = 0
    for _, _, files in os.walk(path):
        count += sum(1 for f in files if Path(f).suffix.lower() in AUDIO_EXTENSIONS)
    return count

# ============================================================
# MENÚ INTERACTIVO
# ============================================================

def menu_modo() -> str:
    """
    Retorna: 'incremental' | 'update' | 'full'
    """
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │         Index Library v2.2 — Modo de indexación         │")
    print("  ├─────────────────────────────────────────────────────────┤")
    print("  │  [1]  Incremental   Solo archivos nuevos (no en BD)     │")
    print("  │  [2]  Actualizar    Re-leer metadata + agregar nuevos   │")
    print("  │       mantiene track_id → preserva RichMeta data        │")
    print("  │  [3]  Completo ★    Eliminar scope y re-indexar         │")
    print("  │       ⚠  borra track_meta → re-ejecutar RichMeta+import │")
    print("  └─────────────────────────────────────────────────────────┘")
    print()
    while True:
        sel = input("  Modo [1-3] (ENTER = Incremental): ").strip()
        if sel in ("", "1"): return "incremental"
        if sel == "2":       return "update"
        if sel == "3":       return "full"
        print("  ❌ Opción inválida")

def menu_scope() -> dict:
    """
    Retorna dict con keys: type, dirs (list de paths a indexar),
    label (para mostrar), album_ids_to_delete (para modo full).
    """
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │         Alcance de indexación                           │")
    print("  ├─────────────────────────────────────────────────────────┤")
    print("  │  [T]  Toda la biblioteca                                │")
    print("  │  [L]  Por letra         (A/, B/, ...)                   │")
    print("  │  [A]  Por artista                                       │")
    print("  │  [U]  Por álbum específico                              │")
    print("  └─────────────────────────────────────────────────────────┘")
    print()
    while True:
        sel = input("  Alcance [T/L/A/U]: ").strip().upper()
        if sel == "T": return _scope_all()
        if sel == "L": return _scope_letra()
        if sel == "A": return _scope_artista()
        if sel == "U": return _scope_album()
        print("  ❌ Opción inválida")

def _scope_all() -> dict:
    n = _count_audio(MUSIC_ROOT)
    return {"type": "all", "dirs": [MUSIC_ROOT],
            "label": f"Toda la biblioteca ({n:,} archivos)"}

def _scope_letra() -> dict:
    letras = _subdirs(MUSIC_ROOT)
    print()
    print("  === Letras disponibles ===")
    for i, l in enumerate(letras):
        n_art = len(_subdirs(os.path.join(MUSIC_ROOT, l)))
        print(f"  [{i:3d}]  {l:4s}  ({n_art} artistas)")
    print("\n  [ALL]  Toda la biblioteca")
    while True:
        sel = input("\n  Número (ENTER para volver): ").strip().upper()
        if sel == "": return menu_scope()
        if sel == "ALL": return _scope_all()
        try:
            idx = int(sel)
            if 0 <= idx < len(letras):
                letra     = letras[idx]
                letra_dir = os.path.join(MUSIC_ROOT, letra)
                return _scope_dentro_letra(letra, letra_dir)
        except ValueError: pass
        print("  ❌ Inválido")

def _scope_dentro_letra(letra: str, letra_dir: str) -> dict:
    artistas = _subdirs(letra_dir)
    print()
    print(f"  === Artistas en {letra}/ ===")
    for i, a in enumerate(artistas):
        n = _count_audio(os.path.join(letra_dir, a))
        print(f"  [{i:3d}]  {a[:50]:<52}({n} tracks)")
    print(f"\n  [ALL]  Todos los artistas de {letra}/")
    while True:
        sel = input("\n  Número (ENTER para volver): ").strip().upper()
        if sel == "": return _scope_letra()
        if sel == "ALL":
            return {"type": "letter", "dirs": [letra_dir],
                    "label": f"Letra {letra}/ ({len(artistas)} artistas)"}
        try:
            idx = int(sel)
            if 0 <= idx < len(artistas):
                artista     = artistas[idx]
                artista_dir = os.path.join(letra_dir, artista)
                return _scope_dentro_artista(letra, artista, artista_dir)
        except ValueError: pass
        print("  ❌ Inválido")

def _scope_dentro_artista(letra: str, artista: str, artista_dir: str) -> dict:
    albums = _subdirs(artista_dir)
    print()
    print(f"  === Álbumes de {artista} ===")
    for i, a in enumerate(albums):
        n = _count_audio(os.path.join(artista_dir, a))
        print(f"  [{i:3d}]  {a[:50]:<52}({n} tracks)")
    print(f"\n  [ALL]  Todos los álbumes de {artista}/")
    while True:
        sel = input("\n  Número (ENTER para volver): ").strip().upper()
        if sel == "": return _scope_dentro_letra(letra, os.path.join(MUSIC_ROOT, letra))
        if sel == "ALL":
            return {"type": "artist", "dirs": [artista_dir],
                    "label": f"Artista {artista} ({len(albums)} álbumes)"}
        try:
            idx = int(sel)
            if 0 <= idx < len(albums):
                album     = albums[idx]
                album_dir = os.path.join(artista_dir, album)
                n         = _count_audio(album_dir)
                return {"type": "album", "dirs": [album_dir],
                        "label": f"{artista} / {album} ({n} tracks)"}
        except ValueError: pass
        print("  ❌ Inválido")

def _scope_artista() -> dict:
    """Búsqueda de artista navegando letras → directa."""
    letras = _subdirs(MUSIC_ROOT)
    print()
    for i, l in enumerate(letras):
        print(f"  [{i:3d}]  {l}")
    while True:
        sel = input("\n  Letra (número, ENTER para volver): ").strip().upper()
        if sel == "": return menu_scope()
        try:
            idx = int(sel)
            if 0 <= idx < len(letras):
                letra     = letras[idx]
                letra_dir = os.path.join(MUSIC_ROOT, letra)
                return _scope_dentro_letra(letra, letra_dir)
        except ValueError: pass
        print("  ❌ Inválido")

def _scope_album() -> dict:
    """Ingresa el path del álbum directamente."""
    print()
    sel = input("  Path completo del álbum: ").strip()
    if not sel or not os.path.isdir(sel):
        print(f"  ❌ Directorio no encontrado: {sel}")
        return menu_scope()
    n = _count_audio(sel)
    return {"type": "album", "dirs": [sel],
            "label": f"Álbum: {sel} ({n} tracks)"}

def _confirm(modo: str, scope: dict) -> bool:
    modo_labels = {
        "incremental": "Incremental  (solo nuevos)",
        "update":      "Actualizar   (re-leer metadata + nuevos)",
        "full":        "Completo ★   (eliminar scope + re-indexar)",
    }
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print("  │  Resumen de ejecución                                   │")
    print("  ├─────────────────────────────────────────────────────────┤")
    ml = modo_labels[modo]
    print(f"  │  Modo   : {ml:<48}│")
    sl = scope["label"]
    print(f"  │  Alcance: {sl:<48}│")
    if modo == "full":
        print("  │  ⚠  track_meta del scope será eliminada                 │")
    print("  └─────────────────────────────────────────────────────────┘")
    print()
    sel = input("  ¿Confirmar? [S/n]: ").strip().lower()
    return sel not in ("n", "no")

# ============================================================
# EJECUCIÓN PRINCIPAL
# ============================================================

def run(conn: sqlite3.Connection, modo: str, scope: dict, verbose: bool = True):
    cur = conn.cursor()
    t0  = time.time()

    print()
    print("  " + "=" * 53)
    print(f"  Index Library v2.2 — {modo.capitalize()}")
    print(f"  Scope: {scope['label']}")
    print("  " + "=" * 53)

    # ── Modo completo: borrar scope antes de re-indexar ───────
    if modo == "full":
        print("\n  [1/2] Eliminando registros del scope...")
        if scope["type"] == "all":
            # Vaciar todas las tablas sin DROP
            for tbl in ["track_meta", "album_meta"]:
                try:    cur.execute(f"DELETE FROM {tbl}")
                except sqlite3.OperationalError: pass
            cur.execute("DELETE FROM tracks")
            cur.execute("DELETE FROM albums")
            cur.execute("DELETE FROM artists")
            conn.commit()
            print("  Toda la biblioteca eliminada de la BD")
        else:
            # Borrar solo los album_ids que corresponden al scope
            aid_list = []
            for d in scope["dirs"]:
                aid_list.extend(_album_ids_for_artist_dir(cur, d))
                aid_list.extend(_album_ids_for_paths(cur, [d]))
            aid_list = list(set(aid_list))
            n_alb, n_trk, led_backup = _delete_album_scope(conn, aid_list)
            print(f"  Eliminados: {n_alb} álbumes, {n_trk} tracks")
        print("\n  [2/2] Re-indexando...")
    else:
        print("\n  Indexando...")

    stats = index_directories(conn, scope["dirs"], modo, verbose)

    elapsed = time.time() - t0
    print()
    print("  " + "=" * 53)
    print(f"  ✅  Indexación completada en {elapsed:.1f}s")
    print(f"  ➕  Tracks insertados  : {stats['inserted']:>6,}")
    print(f"  ↺   Tracks actualizados: {stats['updated']:>6,}")
    print(f"  ⏭   Tracks sin cambios : {stats['skipped']:>6,}")
    print(f"  ❌  Errores            : {stats['errors']:>6,}")
    print("  " + "=" * 53)

    if modo == "full" and scope["type"] != "all":
        print("\n  ⚠  Re-ejecutar RichMeta.py + import_richmetapro.py")
        print("     sobre este scope para restaurar track_meta.")

def run_interactive(conn: sqlite3.Connection):
    modo  = menu_modo()
    scope = menu_scope()
    if not _confirm(modo, scope):
        print("  Cancelado.")
        return
    run(conn, modo, scope)

# ============================================================
# MAIN + ARGPARSE
# ============================================================

def main():
    global MUSIC_ROOT, DB_PATH
    parser = argparse.ArgumentParser(
        description="Index Library v2.0 — Indexador de biblioteca de audio",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--db",      default=DB_PATH,
                        help=f"Path a music.db (default: {DB_PATH})")
    parser.add_argument("--root",    default=MUSIC_ROOT,
                        help=f"Raíz de la biblioteca (default: {MUSIC_ROOT})")

    # Modos (CLI)
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--incremental", action="store_true",
                     help="Solo archivos nuevos")
    grp.add_argument("--update",      action="store_true",
                     help="Re-leer metadata + agregar nuevos")
    grp.add_argument("--full",        action="store_true",
                     help="Eliminar scope + re-indexar")
    grp.add_argument("--stats",       action="store_true",
                     help="Solo mostrar estadísticas")

    # Scope (CLI)
    parser.add_argument("--all",      action="store_true",
                        help="Toda la biblioteca (default si no se especifica scope)")
    parser.add_argument("--letter",   default=None,
                        help="Scope: letra  (ej: M)")
    parser.add_argument("--artist",   default=None,
                        help="Scope: carpeta de artista  (ej: 'Korn')")
    parser.add_argument("--album",    default=None,
                        help="Scope: path completo del álbum")
    parser.add_argument("--quiet",    action="store_true",
                        help="Sin output por archivo")

    args = parser.parse_args()
    MUSIC_ROOT = args.root
    DB_PATH    = args.db

    if not os.path.exists(os.path.dirname(os.path.abspath(DB_PATH)) or "."):
        print(f"❌ Directorio de BD no encontrado: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-32000")
    conn.row_factory = sqlite3.Row
    migrate_db(conn)

    has_action = any([args.incremental, args.update, args.full, args.stats])

    if not has_action:
        # ── Modo interactivo ─────────────────────────────────
        print()
        print("  ╔═════════════════════════════════════════════════════════╗")
        print("  ║       Index Library v2.2 — Modo interactivo             ║")
        print("  ╚═════════════════════════════════════════════════════════╝")
        print(f"  BD   : {DB_PATH}")
        print(f"  Root : {MUSIC_ROOT}")
        try:
            run_interactive(conn)
        except KeyboardInterrupt:
            print("\n\n  Interrumpido.")
            conn.commit()
        finally:
            conn.close()
        return

    # ── Modo CLI ─────────────────────────────────────────────
    if args.stats:
        print_stats(conn)
        conn.close()
        return

    # Resolver modo
    if args.full:         modo = "full"
    elif args.update:     modo = "update"
    else:                 modo = "incremental"

    # Resolver scope
    if args.album:
        album_dir = args.album.rstrip("/")
        if not os.path.isdir(album_dir):
            print(f"❌ Directorio no encontrado: {album_dir}")
            conn.close(); sys.exit(1)
        n = _count_audio(album_dir)
        scope = {"type": "album", "dirs": [album_dir],
                 "label": f"Álbum: {album_dir} ({n} tracks)"}

    elif args.artist:
        # Buscar carpeta del artista dentro de MUSIC_ROOT
        found = None
        for root, dirs, _ in os.walk(MUSIC_ROOT):
            for d in dirs:
                if d.lower() == args.artist.lower():
                    found = os.path.join(root, d)
                    break
            if found: break
        if not found:
            print(f"❌ Artista '{args.artist}' no encontrado bajo {MUSIC_ROOT}")
            conn.close(); sys.exit(1)
        n = _count_audio(found)
        scope = {"type": "artist", "dirs": [found],
                 "label": f"Artista: {args.artist} ({n} tracks)"}

    elif args.letter:
        letra_dir = os.path.join(MUSIC_ROOT, args.letter.upper())
        if not os.path.isdir(letra_dir):
            print(f"❌ Letra '{args.letter}' no encontrada en {MUSIC_ROOT}")
            conn.close(); sys.exit(1)
        n = _count_audio(letra_dir)
        scope = {"type": "letter", "dirs": [letra_dir],
                 "label": f"Letra {args.letter.upper()}/ ({n} tracks)"}

    else:
        n = _count_audio(MUSIC_ROOT)
        scope = {"type": "all", "dirs": [MUSIC_ROOT],
                 "label": f"Toda la biblioteca ({n:,} archivos)"}

    try:
        run(conn, modo, scope, verbose=not args.quiet)
    except KeyboardInterrupt:
        print("\n\n  Interrumpido — commit parcial...")
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    main()
