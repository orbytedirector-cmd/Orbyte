#!/usr/bin/env python3
"""
import_richmetapro.py — Importa datos de RichMetaPro a music.db
================================================================
QUÉ HACE:
  1. Busca todos los archivos richmetapro_audit_*.json en /mnt/musica
  2. Para cada track en cada JSON, busca el track_id en tracks
     (match por file_path — el mismo campo único que usa el indexer)
  3. Inserta/actualiza track_meta con todos los campos de enriquecimiento
  4. Agrega album_meta por álbum (promedios y distribuciones)

USO:
  python3 import_richmetapro.py               # procesar todo
  python3 import_richmetapro.py --stats       # solo mostrar estadísticas
"""

import os, sys, json, re, sqlite3, argparse, logging
from pathlib import Path
from datetime import datetime
from collections import Counter
from typing import Optional

MUSIC_ROOT = "/mnt/musica"
DB_PATH    = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "music.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("import_rm")

# ── JSON FIELD MAPPING ────────────────────────────────────────────────────────

def extract_track_fields(r: dict) -> dict:
    """
    Extrae y normaliza los campos de un registro del JSON de RichMetaPro.
    Ajustado específicamente a la estructura anidada del JSON v2/v3.
    """
    # Sub-diccionarios donde se guardan los datos enriquecidos
    me_features = r.get("musicexplorer_features") or {}
    vector      = r.get("vector") or r.get("vectors") or {}
    lrc_res     = r.get("lrc_result") or {}
    sources     = r.get("sources") or {}

    # ── Mood ──────────────────────────────────────────────────────────────────
    mood            = r.get("mood") or r.get("mood_label")
    mood_confidence = r.get("confidence") or r.get("mood_confidence")

    top_moods = r.get("top_moods") or []
    if isinstance(top_moods, dict):
        top_moods = [{"mood": k, "score": v} for k, v in top_moods.items()]
    top_moods_json = json.dumps(top_moods, ensure_ascii=False) if top_moods else None

    # ── Vectores 4D ───────────────────────────────────────────────────────────
    energy  = _fget(r, vector, "energy")
    valence = _fget(r, vector, "valence")
    tension = _fget(r, vector, "tension")
    depth   = _fget(r, vector, "depth")

    # ── Contexto ──────────────────────────────────────────────────────────────
    momento      = _clean(me_features.get("time_of_day") or r.get("moment"))
    arco_energia = _clean(me_features.get("energy_arc"))
    bailabilidad = _int(me_features.get("danceability_score") or r.get("danceability"))

    # ── Identidad ─────────────────────────────────────────────────────────────
    idioma      = _clean(r.get("lang") or me_features.get("language"))
    tema_lirico = _clean(me_features.get("lyric_theme"))
    decada      = _decade(me_features.get("decade") or r.get("decade") or r.get("year"))
    era         = _clean(me_features.get("era_style") or r.get("era"))
    bpm         = _float(me_features.get("bpm") or r.get("bpm"))
    tonalidad   = _clean(me_features.get("harmonic_key") or me_features.get("key_mode"))

    # ── Letras ────────────────────────────────────────────────────────────────
    has_synced = int(bool(r.get("has_synced_lrc_final") or r.get("has_synced_lrc")))
    has_plain  = int(bool(r.get("has_plain_lrc_final") or r.get("has_plain_lrc")))
    has_lyrics = int(has_synced or has_plain)
    lrclib_id  = _int(lrc_res.get("lrclib_id"))

    # ── Calidad y Metadatos ───────────────────────────────────────────────────
    tier    = _clean(r.get("mood_confidence_tier") or me_features.get("mood_confidence_tier"))
    version = _clean(r.get("version") or "unknown")
    src_json = json.dumps(sources, ensure_ascii=False) if sources else None

    return {
        "mood":             mood,
        "mood_confidence":  _float(mood_confidence),
        "top_moods_json":   top_moods_json,
        "energy":           energy,
        "valence":          valence,
        "tension":          tension,
        "depth":            depth,
        "momento":          momento,
        "arco_energia":     arco_energia,
        "bailabilidad":     bailabilidad,
        "idioma":           idioma,
        "tema_lirico":      tema_lirico,
        "decada":           decada,
        "era":              era,
        "bpm":              bpm,
        "tonalidad":        tonalidad,
        "has_lyrics":       has_lyrics,
        "has_synced_lrc":   has_synced,
        "lrclib_id":        lrclib_id,
        "tier":             tier,
        "sources_json":     src_json,
        "richmetapro_ver":  version,
        # Géneros (nuevo — RichMetaPro >= 2026-05)
        "genre_primary":    _clean(r.get("genre_primary")),
        "genre_secondary":  json.dumps(r.get("genre_secondary") or [], ensure_ascii=False),
    }

# ── HELPERS ───────────────────────────────────────────────────────────────────

def _fget(r, d, key):
    v = r.get(key) or d.get(key)
    return _float(v)

def _float(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None

def _int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None

def _clean(v) -> Optional[str]:
    if not v:
        return None
    s = str(v).strip()
    return None if s in ("", "—", "-", "None", "null") else s

def _decade(v) -> Optional[int]:
    if not v:
        return None
    try:
        year = int(str(v)[:4])
        return (year // 10) * 10
    except (TypeError, ValueError):
        return None

def _majority(items: list) -> Optional[str]:
    clean = [x for x in items if x and x not in ("—", "-")]
    if not clean:
        return None
    return Counter(clean).most_common(1)[0][0]

# ── FIND JSON FILES ───────────────────────────────────────────────────────────

def find_audit_jsons(base: str, album_path: Optional[str] = None):
    search_root = album_path if album_path else base
    for root, dirs, files in os.walk(search_root):
        dirs.sort()
        for f in sorted(files):
            if f.startswith("richmetapro_audit_") and f.endswith(".json"):
                yield os.path.join(root, f), root

# ── DB HELPERS ────────────────────────────────────────────────────────────────

def find_track_id(cur, file_path: str) -> Optional[int]:
    # 1) Match exacto
    cur.execute("SELECT id FROM tracks WHERE file_path = ?", (file_path,))
    row = cur.fetchone()
    if row:
        return row[0]
    # 2) Match por nombre de archivo (basename) — tolerante a diferencias de raíz
    basename = os.path.basename(file_path)
    cur.execute("SELECT id FROM tracks WHERE file_path LIKE ?", (f"%{basename}",))
    row = cur.fetchone()
    if row:
        return row[0]
    # 3) Match por sufijo de path relativo (últimos 2 componentes)
    # ej: "Balls To The Wall/01-track.flac"
    parts = Path(file_path).parts
    if len(parts) >= 2:
        suffix = "/".join(parts[-2:])
        cur.execute("SELECT id FROM tracks WHERE file_path LIKE ?", (f"%{suffix}",))
        row = cur.fetchone()
        if row:
            return row[0]
    return None

def find_album_id(cur, album_dir: str) -> Optional[int]:
    # Normalizar: sin trailing slash
    norm = album_dir.rstrip("/")
    # 1) Match exacto (con y sin trailing slash)
    cur.execute("SELECT id FROM albums WHERE RTRIM(path, '/') = ?", (norm,))
    row = cur.fetchone()
    if row:
        return row[0]
    # 2) Match por nombre del álbum (basename del directorio)
    album_name = os.path.basename(norm)
    cur.execute("SELECT id FROM albums WHERE path LIKE ?", (f"%{album_name}%",))
    row = cur.fetchone()
    if row:
        return row[0]
    return None

def upsert_track_meta(cur, track_id: int, fields: dict, dry_run: bool):
    if dry_run:
        return
    fields["track_id"]     = track_id
    fields["processed_at"] = datetime.now().isoformat()

    cur.execute("""
        INSERT INTO track_meta (
            track_id, mood, mood_confidence, top_moods_json,
            energy, valence, tension, depth,
            momento, arco_energia, bailabilidad,
            idioma, tema_lirico, decada, era,
            bpm, tonalidad,
            has_lyrics, has_synced_lrc, lrclib_id,
            tier, sources_json, richmetapro_ver, processed_at,
            genre_primary, genre_secondary
        ) VALUES (
            :track_id, :mood, :mood_confidence, :top_moods_json,
            :energy, :valence, :tension, :depth,
            :momento, :arco_energia, :bailabilidad,
            :idioma, :tema_lirico, :decada, :era,
            :bpm, :tonalidad,
            :has_lyrics, :has_synced_lrc, :lrclib_id,
            :tier, :sources_json, :richmetapro_ver, :processed_at,
            :genre_primary, :genre_secondary
        )
        ON CONFLICT(track_id) DO UPDATE SET
            mood             = excluded.mood,
            mood_confidence  = excluded.mood_confidence,
            top_moods_json   = excluded.top_moods_json,
            energy           = excluded.energy,
            valence          = excluded.valence,
            tension          = excluded.tension,
            depth            = excluded.depth,
            momento          = excluded.momento,
            arco_energia     = excluded.arco_energia,
            bailabilidad     = excluded.bailabilidad,
            idioma           = excluded.idioma,
            tema_lirico      = excluded.tema_lirico,
            decada           = excluded.decada,
            era              = excluded.era,
            bpm              = excluded.bpm,
            tonalidad        = excluded.tonalidad,
            has_lyrics       = excluded.has_lyrics,
            has_synced_lrc   = excluded.has_synced_lrc,
            lrclib_id        = excluded.lrclib_id,
            tier             = excluded.tier,
            sources_json     = excluded.sources_json,
            richmetapro_ver  = excluded.richmetapro_ver,
            processed_at     = excluded.processed_at,
            genre_primary    = excluded.genre_primary,
            genre_secondary  = excluded.genre_secondary
    """, fields)

def upsert_album_meta(cur, album_id: int, track_ids: list, dry_run: bool):
    if dry_run or not track_ids:
        return

    placeholders = ",".join("?" * len(track_ids))
    rows = cur.execute(f"""
        SELECT mood, momento, idioma, era, decada,
               energy, valence, tension, depth, bailabilidad,
               has_lyrics, has_synced_lrc,
               genre_primary, genre_secondary
        FROM track_meta
        WHERE track_id IN ({placeholders})
    """, track_ids).fetchall()

    if not rows:
        return

    moods     = [r[0] for r in rows]
    momentos  = [r[1] for r in rows]
    idiomas   = [r[2] for r in rows]
    eras      = [r[3] for r in rows]
    decadas   = [r[4] for r in rows]
    # Géneros: primario del álbum = majority de los primarios de los tracks
    genres_primary = [r[12] for r in rows if r[12]]
    # Géneros secundarios: pool de todos los secundarios de los tracks (unique, ordenados por frecuencia)
    all_secondary = []
    for r in rows:
        if r[13]:
            try:
                all_secondary.extend(json.loads(r[13]))
            except Exception:
                pass
    genre_primary_album   = _majority(genres_primary)
    genre_secondary_album = [g for g, _ in Counter(all_secondary).most_common(5)
                             if g != genre_primary_album][:3]

    def avg(vals):
        clean = [v for v in vals if v is not None]
        return round(sum(clean)/len(clean), 4) if clean else None

    mood_dist    = dict(Counter(m for m in moods if m))
    momento_dist = dict(Counter(m for m in momentos if m))

    cur.execute("""
        INSERT INTO album_meta (
            album_id,
            mood_predominante, mood_distribution_json,
            momento_predominante, momento_distribution_json,
            avg_energy, avg_valence, avg_tension, avg_depth, avg_bailabilidad,
            idioma_principal, decada, era,
            tracks_con_letra, tracks_sincronizados, tracks_procesados,
            genre_primary, genre_secondary,
            processed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(album_id) DO UPDATE SET
            mood_predominante         = excluded.mood_predominante,
            mood_distribution_json    = excluded.mood_distribution_json,
            momento_predominante      = excluded.momento_predominante,
            momento_distribution_json = excluded.momento_distribution_json,
            avg_energy                = excluded.avg_energy,
            avg_valence               = excluded.avg_valence,
            avg_tension               = excluded.avg_tension,
            avg_depth                 = excluded.avg_depth,
            avg_bailabilidad          = excluded.avg_bailabilidad,
            idioma_principal          = excluded.idioma_principal,
            decada                    = excluded.decada,
            era                       = excluded.era,
            tracks_con_letra          = excluded.tracks_con_letra,
            tracks_sincronizados      = excluded.tracks_sincronizados,
            tracks_procesados         = excluded.tracks_procesados,
            genre_primary             = excluded.genre_primary,
            genre_secondary           = excluded.genre_secondary,
            processed_at              = excluded.processed_at
    """, (
        album_id,
        _majority(moods),
        json.dumps(mood_dist, ensure_ascii=False),
        _majority(momentos),
        json.dumps(momento_dist, ensure_ascii=False),
        avg([r[5] for r in rows]),
        avg([r[6] for r in rows]),
        avg([r[7] for r in rows]),
        avg([r[8] for r in rows]),
        avg([r[9] for r in rows]),
        _majority(idiomas),
        _majority([str(d) for d in decadas if d]),
        _majority(eras),
        sum(1 for r in rows if r[10]),
        sum(1 for r in rows if r[11]),
        len(rows),
        genre_primary_album,
        json.dumps(genre_secondary_album, ensure_ascii=False),
        datetime.now().isoformat()
    ))

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Importa datos de RichMetaPro a track_meta / album_meta"
    )
    parser.add_argument("--db",          default=DB_PATH,   help="Path a music.db")
    parser.add_argument("--music-root",  default=MUSIC_ROOT,help="Raíz biblioteca")
    parser.add_argument("--album",       metavar="PATH",    help="Solo este álbum")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--stats",       action="store_true",
                        help="Solo mostrar estadísticas, sin importar")
    parser.add_argument("--verbose",      action="store_true",
                        help="Log detallado (DEBUG)")
    parser.add_argument("--debug-match",  action="store_true",
                        help="Muestra paths del JSON vs DB para diagnosticar mismatches")
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ── Debug match mode ──────────────────────────────────────────────────────
    if args.debug_match:
        print("\n  ── DEBUG MATCH ──────────────────────────────────────────")
        jsons_dbg = list(find_audit_jsons(args.music_root, args.album))
        for json_path, album_dir in jsons_dbg[:3]:
            print(f"\n  JSON       : {json_path}")
            print(f"  album_dir  : {album_dir}")
            album_id = find_album_id(cur, album_dir)
            print(f"  album_id   : {album_id}  ({'✅ MATCH' if album_id else '❌ NO MATCH'})")
            # Muestra algunos paths de la DB cerca de ese directorio
            name = os.path.basename(album_dir.rstrip("/"))
            rows = cur.execute(
                "SELECT id, path FROM albums WHERE path LIKE ? LIMIT 5", (f"%{name}%",)
            ).fetchall()
            if rows:
                print(f"  DB albums LIKE '%{name}%':")
                for r in rows:
                    print(f"    id={r[0]}  path={r[1]}")
            else:
                # Muestra 3 paths de la DB para que veas el formato
                sample = cur.execute("SELECT id, path FROM albums LIMIT 3").fetchall()
                print(f"  DB albums (muestra):")
                for r in sample:
                    print(f"    id={r[0]}  path={r[1]}")
            # Lee el JSON y muestra los primeros file paths
            try:
                with open(json_path, encoding="utf-8") as f:
                    audit_dbg = json.load(f)
                if not isinstance(audit_dbg, list):
                    audit_dbg = audit_dbg.get("tracks", [audit_dbg])
                print(f"  Tracks en JSON: {len(audit_dbg)}")
                for rec in audit_dbg[:3]:
                    fp = rec.get("file") or rec.get("file_path") or rec.get("path") or ""
                    tid = find_track_id(cur, fp)
                    print(f"    JSON file   : {fp}")
                    print(f"    track_id    : {tid}  ({'✅' if tid else '❌'})")
                    if not tid:
                        bn = os.path.basename(fp)
                        sample_t = cur.execute(
                            "SELECT id, file_path FROM tracks WHERE file_path LIKE ? LIMIT 2", (f"%{bn}%",)
                        ).fetchall()
                        if sample_t:
                            print(f"    DB por basename: {sample_t}")
                        else:
                            sample_t2 = cur.execute("SELECT id, file_path FROM tracks LIMIT 2").fetchall()
                            print(f"    DB tracks (muestra): {sample_t2}")
            except Exception as ex:
                print(f"  Error: {ex}")
        print("\n  ── FIN DEBUG ────────────────────────────────────────────\n")
        conn.close()
        return

    # ── Stats mode ────────────────────────────────────────────────────────────
    if args.stats:
        total_tracks  = cur.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        total_albums  = cur.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        try:
            meta_tracks = cur.execute("SELECT COUNT(*) FROM track_meta").fetchone()[0]
            meta_albums = cur.execute("SELECT COUNT(*) FROM album_meta").fetchone()[0]
            pct_t = meta_tracks / total_tracks * 100
            pct_a = meta_albums / total_albums * 100
            print(f"\n  Tracks totales         : {total_tracks:,}")
            print(f"  Tracks con RichMetaPro : {meta_tracks:,}  ({pct_t:.1f}%)")
            print(f"  Albums totales         : {total_albums:,}")
            print(f"  Albums con RichMetaPro : {meta_albums:,}  ({pct_a:.1f}%)")

            # Muestra ahora los 5 campos enriquecidos principales
            for field, label in [("mood","Mood"), ("momento","Momento"), ("tema_lirico","Temáticas"),
                                ("idioma","Idioma"), ("decada","Décadas"), ("era","Eras"),
                                ("genre_primary","Género principal")]:
                print(f"\n  {label}:")
                for r in cur.execute(
                    f"SELECT {field}, COUNT(*) as c FROM track_meta "
                    f"WHERE {field} IS NOT NULL "
                    f"GROUP BY {field} ORDER BY c DESC LIMIT 8"
                ):
                    print(f"    {str(r[0]):25}: {r[1]:,}")

            # Géneros secundarios — explotar el JSON array
            print(f"\n  Otros géneros (secundarios):")
            sec_rows = cur.execute(
                "SELECT genre_secondary FROM track_meta WHERE genre_secondary IS NOT NULL"
            ).fetchall()
            sec_counter = Counter()
            for row in sec_rows:
                try:
                    for g in json.loads(row[0]):
                        if g:
                            sec_counter[g] += 1
                except Exception:
                    pass
            for g, c in sec_counter.most_common(10):
                print(f"    {g:25}: {c:,}")
            print()
        except sqlite3.OperationalError:
            print("  ⚠  track_meta no existe aún. Ejecutar migrate_add_richmetapro.sql primero.")
        conn.close()
        return

    # ── Import mode ───────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info("import_richmetapro.py (V2 - Mapeo Profundo)")
    log.info(f"  DB         : {args.db}")
    log.info(f"  Busca en   : {args.album or args.music_root}")
    log.info(f"  Dry run    : {args.dry_run}")
    log.info("=" * 60)

    jsons = list(find_audit_jsons(args.music_root, args.album))
    log.info(f"\n  JSON encontrados: {len(jsons)}")

    if not jsons:
        log.warning("  No se encontraron richmetapro_audit_*.json")
        conn.close()
        return

    total_tracks_ok = 0
    total_tracks_skip = 0
    total_albums_ok = 0
    errors = 0

    for json_path, album_dir in jsons:
        try:
            with open(json_path, encoding="utf-8") as f:
                audit = json.load(f)
        except Exception as ex:
            log.warning(f"  ⚠ Error leyendo {json_path}: {ex}")
            errors += 1
            continue

        if not isinstance(audit, list):
            audit = audit.get("tracks", [audit])

        album_id = find_album_id(cur, album_dir)
        if not album_id:
            total_tracks_skip += len(audit)
            continue

        track_ids_this_album = []

        for record in audit:
            # Match tolerante de rutas
            file_path = record.get("file") or record.get("file_path") or record.get("path") or ""
            if not file_path:
                total_tracks_skip += 1
                continue

            track_id = find_track_id(cur, file_path)
            if not track_id:
                total_tracks_skip += 1
                continue

            fields = extract_track_fields(record)
            upsert_track_meta(cur, track_id, fields, args.dry_run)
            track_ids_this_album.append(track_id)
            total_tracks_ok += 1

        if track_ids_this_album:
            upsert_album_meta(cur, album_id, track_ids_this_album, args.dry_run)
            total_albums_ok += 1

        if total_albums_ok % 50 == 0 and not args.dry_run:
            conn.commit()

    if not args.dry_run:
        conn.commit()

    conn.close()

    print(f"\n{'='*55}")
    print(f"  ✅  Importación completada con Mapeo Profundo")
    print(f"  📀  Albums importados  : {total_albums_ok:,}")
    print(f"  🎵  Tracks enriquecidos: {total_tracks_ok:,}")
    print(f"  ⏭   Tracks sin match   : {total_tracks_skip:,}")
    print(f"  ❌  Errores JSON       : {errors:,}")
    if args.dry_run:
        print(f"  ℹ   DRY RUN — nada fue escrito en la DB")
    print(f"{'='*55}\n")
    print(f"  Ver estadísticas: python3 import_richmetapro.py --stats\n")

if __name__ == "__main__":
    main()
