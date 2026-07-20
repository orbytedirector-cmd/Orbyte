import os
import re
import json
import tempfile
import hashlib
import time
from urllib.parse import quote
from flask import Flask, render_template, request, send_file, jsonify, Response, stream_with_context
from flask_cors import CORS
import sqlite3
import mimetypes
import subprocess
try:
    import requests as req_lib
except ImportError:
    req_lib = None

# Some systems' mimetypes DB doesn't know .flac — register it explicitly so
# /stream-dsd's cached output always gets served with the right Content-Type.
mimetypes.add_type('audio/flac', '.flac')

try:
    from mpd import MPDClient as _MPDClient
    _MPD_AVAILABLE = True
except ImportError:
    _MPD_AVAILABLE = False

# ── Favorites (persisted in DB favorites table) ────────────────────────────────
_favorites_set: set = set()   # in-memory cache of track_id ints

def _load_favorites():
    global _favorites_set
    try:
        conn = get_db_connection()
        rows = conn.execute('SELECT track_id FROM favorites').fetchall()
        _favorites_set = {r[0] for r in rows}
        conn.close()
    except Exception as e:
        app.logger.warning(f'Could not load favorites: {e}')

# ── Nationality / Language → ISO 3166-1 alpha-2 country code ─────────────────
# Used to build flagcdn.com image URLs — works on all OS/browser combos,
# unlike Unicode flag emoji which require Noto Color Emoji on Linux.
COUNTRY_ISO = {
    'United States':'us','United Kingdom':'gb','England':'gb','Scotland':'gb',
    'Wales':'gb','Northern Ireland':'gb','Pontypridd':'gb','Brighton':'gb','London':'gb',
    'Germany':'de','Saarbrücken':'de','Dortmund':'de',
    'France':'fr','Italy':'it','Savona':'it',
    'Spain':'es','Madrid':'es','Asturias':'es',
    'Sweden':'se','Arvika Municipality':'se',
    'Norway':'no','Finland':'fi','Denmark':'dk','Copenhagen':'dk',
    'Netherlands':'nl','Australia':'au','Canada':'ca',
    'Brazil':'br','Argentina':'ar','Mexico':'mx','Chile':'cl',
    'Colombia':'co','Venezuela':'ve',
    'Japan':'jp','South Korea':'kr','Ireland':'ie','Portugal':'pt',
    'Greece':'gr','Poland':'pl','Switzerland':'ch','Iceland':'is',
    'New Zealand':'nz','South Africa':'za','Jamaica':'jm','Cuba':'cu',
    'Dominican Republic':'do','Puerto Rico':'pr','Philippines':'ph',
    'Kazakhstan':'kz','Bali':'id','Indonesia':'id',
    'Boston':'us','Miami':'us','New York':'us','San Francisco':'us',
    'Seattle':'us','Texas':'us','Washington':'us','Raleigh':'us',
    'Arlington':'us','Volcano':'us',
}

# Language code → ISO country code (language → representative country)
LANG_ISO = {
    'en':'gb','es':'es','de':'de','fr':'fr','pt':'pt','it':'it',
    'ja':'jp','ko':'kr','nl':'nl','ru':'ru','sv':'se','no':'no',
    'fi':'fi','da':'dk','pl':'pl','zh':'cn','ar':'sa','tr':'tr',
    'cs':'cz','hu':'hu','ro':'ro','uk':'ua','el':'gr','he':'il',
}

# Language code → full name (Spanish)
LANG_LABELS = {
    'en':'Inglés','es':'Español','de':'Alemán','fr':'Francés','pt':'Portugués',
    'it':'Italiano','ja':'Japonés','ko':'Coreano','nl':'Holandés','ru':'Ruso',
    'sv':'Sueco','no':'Noruego','fi':'Finlandés','da':'Danés','pl':'Polaco',
    'zh':'Chino','ar':'Árabe','tr':'Turco','cs':'Checo','hu':'Húngaro',
    'ro':'Rumano','uk':'Ucraniano','el':'Griego','he':'Hebreo',
}

def _flag_img(iso, label='', size='20x15'):
    """Return a flagcdn.com <img> tag. Empty string when iso is unknown."""
    if not iso:
        return ''
    return (f'<img src="https://flagcdn.com/{size}/{iso}.png" '
            f'width="{size.split("x")[0]}" height="{size.split("x")[1]}" '
            f'alt="{label}" title="{label}" '
            f'style="border-radius:2px;vertical-align:middle;object-fit:cover">')

def nationality_flag(nat):
    """Return an <img> flag tag for a nationality/country name. Empty when not found."""
    if not nat or nat in ('Unknown', ''):
        return ''
    iso = COUNTRY_ISO.get(nat)
    return _flag_img(iso, nat) if iso else ''

def lang_flag(code):
    """Return an <img> flag tag for a 2-letter language ISO 639-1 code."""
    if not code:
        return ''
    iso = LANG_ISO.get(code.lower())
    return _flag_img(iso, code.upper(), '20x15') if iso else ''

def lang_label(code):
    """Return the full Spanish name for a 2-letter language ISO 639-1 code."""
    if not code:
        return ''
    return LANG_LABELS.get(code.lower(), code.upper())


app = Flask(__name__)
CORS(app)

# ── Diamond SVG helper ────────────────────────────────────────────────────────
_DIAMOND_SVG_TMPL = (
    '<svg width="{w}" height="{h}" viewBox="0 0 20 22" fill="none" '
    'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
    '<line x1="10" y1="1" x2="10" y2="3.5"/>'
    '<line x1="7" y1="1.8" x2="8.5" y2="3.5"/>'
    '<line x1="13" y1="1.8" x2="11.5" y2="3.5"/>'
    '<path d="M3 8 L7 4.5 L13 4.5 L17 8"/>'
    '<line x1="3" y1="8" x2="17" y2="8"/>'
    '<path d="M3 8 L10 19 L17 8"/>'
    '<line x1="7" y1="4.5" x2="10" y2="8"/>'
    '<line x1="13" y1="4.5" x2="10" y2="8"/>'
    '</svg>'
)
_DIAMOND_SIZES = {'sm': ('11','13'), 'md': ('13','15'), 'lg': ('16','18'), 'np': ('18','21')}

def diamond_svg(led_color, size='sm'):
    """Return a colored SVG diamond indicator for the given led_color tier."""
    color = (led_color or 'yellow').lower()
    w, h  = _DIAMOND_SIZES.get(size, ('11', '13'))
    svg   = _DIAMOND_SVG_TMPL.format(w=w, h=h)
    return f'<span class="led-diamond-wrap led-d-{color}">{svg}</span>'

# Register helpers in Jinja globals (must be after app is created)
app.jinja_env.globals['nationality_flag'] = nationality_flag
app.jinja_env.globals['lang_flag']        = lang_flag
app.jinja_env.globals['lang_label']       = lang_label
app.jinja_env.globals['favorites_set']    = lambda: _favorites_set
app.jinja_env.globals['diamond_svg']      = diamond_svg

# Make MOOD_LABELS available in all templates
@app.context_processor
def inject_globals():
    # ADV_* keys power the "Búsqueda Avanzada" filter capsules (see the
    # QUALITY_OPTIONS / *_BUCKETS constants defined near the advanced-search
    # routes below). Referencing them here is safe even though they are
    # defined later in the file: Flask only calls this function per-request,
    # long after the whole module has finished loading.
    return {'MOOD_LABELS': MOOD_LABELS, 'LED_LABELS': LED_LABELS,
            'ADV_QUALITY_OPTIONS': QUALITY_OPTIONS,
            'ADV_POP_BUCKETS': POP_BUCKETS,
            'ADV_ENERGY_BUCKETS': ENERGY_BUCKETS,
            'ADV_BAIL_BUCKETS': BAIL_BUCKETS}

MUSIC_ROOT = "/mnt/musica/"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "music.db")

# ── LED color definitions (iFi Zen DAC V2) ───────────────────────────────────
# Source of truth: tracks.led_color field in the DB. Never recompute.
LED_LABELS = {
    'yellow':  'PCM 44.1/48 kHz',
    'white':   'PCM 88.2/96/176.4/192/352.8/384 kHz',
    'cyan':    'DSD 64/128',
    'red':     'DSD 256',
    'green':   'MQA',
    'blue':    'MQA Studio',
    'magenta': 'Original Sample Rate (MQB)',
}
LED_ORDER = ['yellow', 'white', 'cyan', 'red', 'green', 'blue', 'magenta']

# Mood display labels — maps raw DB value → friendly UI label
MOOD_LABELS = {
    'Humorístico': 'De Buen Humor',
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_db_path(path):
    if not path:
        return path
    return path.strip("'\"")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Lazy migration: add website_url column if it doesn't exist yet
    try:
        conn.execute("ALTER TABLE artists ADD COLUMN website_url TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists
    # Lazy migration: expression index for the "vista de pistas" de-dupe
    # (_track_dedupe_condition, ver más abajo). Sin este índice, el
    # NOT EXISTS correlacionado que colapsa duplicados hace un scan
    # completo de tracks por cada fila candidata (porque LOWER(TRIM(...))
    # no puede usar un índice normal) — con una biblioteca grande esto
    # tarda minutos. Con el índice, SQLite resuelve el match Título+Artista
    # con una búsqueda indexada en vez de una comparación fila por fila.
    # CREATE INDEX IF NOT EXISTS es prácticamente gratis una vez creado
    # (solo una consulta al catálogo), así que es seguro dejarlo acá.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_dedupe_norm "
        "ON tracks (LOWER(TRIM(title)), LOWER(TRIM(artist)))"
    )
    conn.commit()
    return conn

def _fmt_seconds(seconds):
    if not seconds or seconds < 0:
        return "0:00"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

def _fmt_bitrate(bps):
    if not bps:
        return "N/A"
    kbps = bps / 1000
    if kbps >= 1000:
        label = f"{kbps/1000:.2f} Mbps"
    else:
        label = f"{int(kbps):,} kbps"
    if kbps <= 320:
        tag = "MP3-level"
    elif kbps <= 1412:
        tag = "CD Lossless"
    else:
        tag = "Hi-Res"
    return f"{label} · {tag}"

def _dsd_label(track):
    """Return clean DSD label: DSD64, DSD128, DSD256 etc."""
    rate = (track.get('dsd_rate') or '').strip()
    if rate:
        # Already in correct form: 'DSD64', 'DSD128', 'DSD256'
        return rate if rate.startswith('DSD') else 'DSD' + rate
    # Derive from sample_rate_real
    sr = track.get('sample_rate_real') or 0
    if   sr >= 11289600: return 'DSD256'
    elif sr >=  5644800: return 'DSD128'
    elif sr >=  2822400: return 'DSD64'
    elif sr >=  1411200: return 'DSD32'
    return 'DSD'

def _fmt_format(track):
    """Return (display_label, led_color) using DB led_color as sole truth."""
    led = (track.get('led_color') or 'yellow').lower()
    if track.get('is_dsd'):
        label = _dsd_label(track)
    elif track.get('is_mqa'):
        label = 'MQA Studio' if led == 'blue' else ('MQB' if led == 'magenta' else 'MQA')
    else:
        codec = (track.get('codec') or 'FLAC').upper()
        label = f'{codec} · Hi-Res' if led == 'white' else codec
    return label, led

def _led_for_album_tracks(conn, album_id):
    """Get dominant (highest-quality) LED color for an album from its tracks."""
    priority = {c: i for i, c in enumerate(reversed(LED_ORDER))}
    rows = conn.execute(
        'SELECT led_color, COUNT(*) as c FROM tracks WHERE album_id=? AND led_color IS NOT NULL GROUP BY led_color',
        (album_id,)
    ).fetchall()
    if not rows:
        return 'yellow'
    # Return highest-priority color that exists
    best = sorted(rows, key=lambda r: priority.get(r['led_color'], -1), reverse=True)
    return best[0]['led_color']

def track_to_json(t):
    """Serialize a track Row/dict with all display fields. led_color comes from DB, never recomputed."""
    d = dict(t)
    d['file_path']   = clean_db_path(d.get('file_path'))
    d['cover_path']  = clean_db_path(d.get('cover_path'))
    d['cover_url']   = cover_url_filter(d['cover_path'])
    d['audio_url']   = audio_url_filter(d['file_path'])
    d['duration_fmt'] = _fmt_seconds(d.get('duration'))
    fmt, led = _fmt_format(d)
    d['format_display'] = fmt
    d['format_color']   = led
    # led_color stays as-is from DB
    d['bitrate_fmt']     = _fmt_bitrate(d.get('bitrate'))
    sr = d.get('sample_rate_real')
    d['sample_rate_fmt'] = f"{sr/1000:.1f} kHz" if sr else "N/A"
    d['bit_depth_fmt']   = f"{d.get('bit_depth') or 24} bit"
    return d

# ── Jinja2 filters ────────────────────────────────────────────────────────────

@app.template_filter('format_size')
def format_size_filter(b):
    if not b: return "0 B"
    if b < 1024**2: return f"{b/1024:.1f} KB"
    if b < 1024**3: return f"{b/1024**2:.2f} MB"
    return f"{b/1024**3:.2f} GB"

@app.template_filter('format_duration')
def format_duration_filter(seconds):
    return _fmt_seconds(seconds)

@app.template_filter('cover_url')
def cover_url_filter(cover_path):
    if not cover_path: return ''
    path = clean_db_path(cover_path).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if path.startswith(root + '/'):
        path = path[len(root)+1:]
    elif path.startswith(root):
        path = path[len(root):]
    # Encode each segment so special chars like # don't break URLs
    encoded = '/'.join(quote(seg, safe='') for seg in path.split('/'))
    return f"/cover/{encoded}"

@app.template_filter('audio_url')
def audio_url_filter(file_path):
    if not file_path: return ''
    path = clean_db_path(file_path).lstrip('/')
    root = MUSIC_ROOT.strip('/')
    if path.startswith(root + '/'):
        path = path[len(root)+1:]
    elif path.startswith(root):
        path = path[len(root):]
    # Encode each segment so special chars like # don't break URLs
    encoded = '/'.join(quote(seg, safe='') for seg in path.split('/'))
    # DSD files (DSF/DFF) must be transcoded on the fly — browsers can't play raw DSD
    ext = os.path.splitext(file_path)[1].lower()
    if ext in ('.dsf', '.dff'):
        return f"/stream-dsd/{encoded}"
    return f"/audio/{encoded}"

def build_similar_artists(conn, similar_artists_json, limit=12):
    """Parsea artists.similar_artists_json y arma la lista enriquecida con
       cover_url (portada del álbum más popular de cada artista, si existe
       en la biblioteca). Usado por /artist/<id> (tab Similares) y por
       /api/track/<id>/similar-artists (NP overlay)."""
    similar_artists = []
    if not similar_artists_json:
        return similar_artists
    try:
        similar_raw = json.loads(similar_artists_json)
    except Exception:
        return similar_artists
    if not isinstance(similar_raw, list):
        return similar_artists
    for s in similar_raw[:limit]:
        name = s.get('name', '') if isinstance(s, dict) else (s if isinstance(s, str) else '')
        if not name:
            continue
        existing = conn.execute(
            'SELECT id FROM artists WHERE LOWER(name)=LOWER(?)', (name,)
        ).fetchone()
        cover_url = ''
        if existing:
            cov = conn.execute(
                '''SELECT al.cover_path FROM albums al
                   LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                   WHERE al.artist_id=?
                   ORDER BY COALESCE(apc.pop_score,0) DESC, al.year DESC
                   LIMIT 1''',
                (existing['id'],)
            ).fetchone()
            if cov and cov['cover_path']:
                cover_url = cover_url_filter(clean_db_path(cov['cover_path']))
        similar_artists.append({
            'name': name,
            'id': existing['id'] if existing else None,
            'cover_url': cover_url
        })
    return similar_artists

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    """Serve SW from root so it controls the entire app scope."""
    from flask import send_from_directory
    resp = send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/')
def home():
    conn = get_db_connection()
    try:
        total_artists  = conn.execute('SELECT COUNT(*) FROM artists a WHERE EXISTS (SELECT 1 FROM albums al WHERE al.artist_id=a.id)').fetchone()[0]
        total_albums   = conn.execute('SELECT COUNT(*) FROM albums').fetchone()[0]
        total_tracks   = conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]
        total_duration = conn.execute('SELECT COALESCE(SUM(duration),0) FROM tracks').fetchone()[0]
        total_size     = conn.execute('SELECT COALESCE(SUM(file_size),0) FROM tracks').fetchone()[0]

        # LED breakdown — direct from DB field, no computation
        led_rows = conn.execute(
            'SELECT led_color, COUNT(*) as c FROM tracks WHERE led_color IS NOT NULL GROUP BY led_color ORDER BY c DESC'
        ).fetchall()
        led_counts = {r['led_color']: r['c'] for r in led_rows}

        # Genre breakdown (top 8)
        genre_rows = conn.execute(
            'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" GROUP BY genre ORDER BY c DESC LIMIT 8'
        ).fetchall()
        genres = [(r['genre'], r['c']) for r in genre_rows]

        # Recently added albums
        recent_albums_raw = conn.execute('''
            SELECT al.id, al.name, al.cover_path, al.year, al.track_count, al.total_duration,
                   al.artist_id, al.primary_format, ar.name as artist_name
            FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
            ORDER BY al.created_at DESC LIMIT 20
        ''').fetchall()
        recent_albums = []
        for a in recent_albums_raw:
            d = {**dict(a), 'cover_path': clean_db_path(a['cover_path'])}
            led = conn.execute(
                """SELECT led_color FROM tracks WHERE album_id=?
                   ORDER BY CASE led_color
                     WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                     WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                     ELSE 6 END LIMIT 1""",
                (a['id'],)
            ).fetchone()
            d['album_led'] = led['led_color'] if led else 'yellow'
            recent_albums.append(d)

        # ── RichMetaPro data ──────────────────────────────────────────────────
        mood_rows = conn.execute(
            'SELECT mood, COUNT(*) as c FROM track_meta WHERE mood IS NOT NULL GROUP BY mood ORDER BY c DESC LIMIT 14'
        ).fetchall()
        moods = [(r['mood'], r['c']) for r in mood_rows]

        momento_rows = conn.execute(
            'SELECT momento, COUNT(*) as c FROM track_meta WHERE momento IS NOT NULL GROUP BY momento ORDER BY c DESC'
        ).fetchall()
        momentos = [(r['momento'], r['c']) for r in momento_rows]

        era_order = [
            'early_rock_era', 'british_invasion_era', 'classic_rock_era',
            'nwobhm_synth_era', 'grunge_alternative_era', 'post_millennial_era',
            'streaming_era', 'current_era'
        ]
        era_raw  = conn.execute('SELECT era, COUNT(*) as c FROM track_meta WHERE era IS NOT NULL GROUP BY era').fetchall()
        era_dict = {r['era']: r['c'] for r in era_raw}
        eras = [(e, era_dict[e]) for e in era_order if e in era_dict]

        tema_rows = conn.execute(
            'SELECT tema_lirico, COUNT(*) as c FROM track_meta WHERE tema_lirico IS NOT NULL GROUP BY tema_lirico ORDER BY c DESC LIMIT 10'
        ).fetchall()
        temas = [(r['tema_lirico'], r['c']) for r in tema_rows]

        tier_rows = conn.execute(
            'SELECT tier, COUNT(*) as c FROM track_meta WHERE tier IS NOT NULL GROUP BY tier ORDER BY c DESC'
        ).fetchall()
        tiers = {r['tier']: r['c'] for r in tier_rows}

        lrc = conn.execute('SELECT SUM(has_lyrics) as wl, SUM(has_synced_lrc) as syn FROM track_meta').fetchone()
        lyrics_stats = {'with_lyrics': lrc['wl'] or 0, 'synced': lrc['syn'] or 0}

        # Language (idioma) breakdown from track_meta
        lang_rows = conn.execute(
            'SELECT idioma, COUNT(*) as c FROM track_meta WHERE idioma IS NOT NULL AND idioma!="" GROUP BY idioma ORDER BY c DESC LIMIT 12'
        ).fetchall()
        languages = [(r['idioma'], r['c']) for r in lang_rows]

        # Genre primary breakdown from track_meta (enriched metadata)
        genre_primary_rows = conn.execute(
            'SELECT genre_primary, COUNT(*) as c FROM track_meta WHERE genre_primary IS NOT NULL AND genre_primary!="" GROUP BY genre_primary ORDER BY c DESC LIMIT 15'
        ).fetchall()
        genres_primary = [(r['genre_primary'], r['c']) for r in genre_primary_rows]

        # Unlimited version of genres_primary, dedicated to the Búsqueda
        # Avanzada Género capsule (see _advanced_search_options() for the
        # matching copy used on /busqueda-avanzada itself). Kept separate
        # from genres_primary above so this compact section's LIMIT 15 is
        # untouched.
        adv_genre_primary_rows = conn.execute(
            'SELECT genre_primary, COUNT(*) as c FROM track_meta WHERE genre_primary IS NOT NULL AND genre_primary!="" GROUP BY genre_primary ORDER BY c DESC'
        ).fetchall()
        adv_genres_primary = [(r['genre_primary'], r['c']) for r in adv_genre_primary_rows]

        # Max era count for proportional bars
        max_era_count = max((c for _, c in eras), default=1)

        # All genres for extended selector (sorted by count desc)
        all_genre_rows = conn.execute(
            'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" GROUP BY genre ORDER BY c DESC'
        ).fetchall()
        all_genres = [(r['genre'], r['c']) for r in all_genre_rows]

        # ── Búsqueda Avanzada support data ──────────────────────────────────
        # Distinct release years (for the "Año" filter) and artist
        # nationalities (for "País Origen"), both driven directly by existing
        # columns (albums.year, artists.nationality) — no new DB fields.
        year_rows = conn.execute(
            'SELECT DISTINCT year FROM albums WHERE year IS NOT NULL ORDER BY year DESC'
        ).fetchall()
        available_years = [r['year'] for r in year_rows]

        nat_rows = conn.execute(
            'SELECT nationality, COUNT(*) as c FROM artists '
            'WHERE nationality IS NOT NULL AND nationality!="" '
            'GROUP BY nationality ORDER BY c DESC'
        ).fetchall()
        nationalities = [(r['nationality'], r['c']) for r in nat_rows]

        return render_template('home.html',
            total_artists=total_artists, total_albums=total_albums,
            total_tracks=total_tracks,
            duration_hours=total_duration / 3600,
            size_tb=total_size / (1024**4),
            led_counts=led_counts,
            led_order=LED_ORDER, led_labels=LED_LABELS,
            genres=genres, all_genres=all_genres,
            genres_primary=genres_primary, adv_genres_primary=adv_genres_primary,
            languages=languages,
            recent_albums=recent_albums,
            moods=moods, momentos=momentos, eras=eras,
            max_era_count=max_era_count,
            temas=temas, tiers=tiers, lyrics_stats=lyrics_stats,
            available_years=available_years, nationalities=nationalities)
    finally:
        conn.close()


@app.route('/letter/<letter>')
def letter(letter):
    conn = get_db_connection()
    try:
        artists = conn.execute('''
            SELECT
              a.id, a.name, a.nationality,
              a.lastfm_listeners, a.lastfm_playcount,
              COUNT(DISTINCT al.id)                        AS album_count,
              COALESCE(MAX(apc.pop_score), 0)              AS pop_score,
              (SELECT t.genre
               FROM tracks t JOIN albums al2 ON t.album_id=al2.id
               WHERE al2.artist_id=a.id AND t.genre IS NOT NULL AND t.genre != ""
               GROUP BY t.genre ORDER BY COUNT(*) DESC LIMIT 1)  AS most_common_genre
            FROM artists a
            INNER JOIN albums al  ON al.artist_id = a.id
            LEFT  JOIN album_pop_cache apc ON apc.album_id = al.id
            WHERE a.letter = ?
            GROUP BY a.id
            HAVING album_count > 0
            ORDER BY a.name
        ''', (letter,)).fetchall()
        return render_template('letter.html', artists=[dict(r) for r in artists], letter=letter)
    finally:
        conn.close()


@app.route('/artist/<int:artist_id>')
def artist(artist_id):
    conn = get_db_connection()
    try:
        ar = conn.execute('SELECT * FROM artists WHERE id=?', (artist_id,)).fetchone()
        if not ar: return "Artist not found", 404
        # albums already have track_count and total_duration pre-computed
        albums_raw = conn.execute(
            'SELECT *, artist_id FROM albums WHERE artist_id=? ORDER BY year, name', (artist_id,)
        ).fetchall()
        total_tracks = sum(a['track_count'] or 0 for a in albums_raw)

        # Enrich each album with dominant led_color from its tracks
        albums = []
        for a in albums_raw:
            d = dict(a)
            d['cover_path'] = clean_db_path(d.get('cover_path'))
            # Dominant led_color: highest-quality color that appears most
            led_row = conn.execute(
                '''SELECT led_color FROM tracks
                   WHERE album_id=? AND led_color IS NOT NULL
                   GROUP BY led_color
                   ORDER BY CASE led_color
                     WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                     WHEN 'red'     THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                     ELSE 6 END
                   LIMIT 1''',
                (a['id'],)
            ).fetchone()
            d['album_led'] = led_row['led_color'] if led_row else 'yellow'
            albums.append(d)

        ar_data = dict(ar)
        ar_data['flag'] = nationality_flag(ar_data.get('nationality') or '')

        # Dominant genre across artist's tracks
        genre_row = conn.execute('''
            SELECT COALESCE(tm.genre_primary, t.genre) as g, COUNT(*) as c
            FROM tracks t
            JOIN albums al ON al.id=t.album_id
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            WHERE al.artist_id=?
              AND COALESCE(tm.genre_primary, t.genre) IS NOT NULL
              AND COALESCE(tm.genre_primary, t.genre) != ''
            GROUP BY g ORDER BY c DESC LIMIT 5
        ''', (artist_id,)).fetchall()
        genres = [r['g'] for r in genre_row]

        # Similar artists — enriquecidos con cover_url para el carrusel (tab Similares)
        similar_artists = build_similar_artists(conn, ar_data.get('similar_artists_json'), limit=12)

        # Top / most popular tracks for this artist — powers the "Populares" tab
        top_tracks_raw = conn.execute(
            '''SELECT t.*, al.name as album_name, al.cover_path as album_cover,
                      COALESCE(tpc.pop_score, 0) as pop_score,
                      COALESCE(tpc.stars, 0) as pop_stars
               FROM tracks t
               JOIN albums al ON al.id = t.album_id
               LEFT JOIN track_pop_cache tpc ON tpc.track_id = t.id
               WHERE al.artist_id = ?
               ORDER BY pop_score DESC, t.title
               LIMIT 10''',
            (artist_id,)
        ).fetchall()
        top_tracks = []
        for t in top_tracks_raw:
            d = dict(t)
            d['file_path'] = clean_db_path(d.get('file_path'))
            cover = clean_db_path(d.get('album_cover'))
            d['cover_path'] = cover
            d['cover_url'] = cover_url_filter(cover)
            d['audio_url'] = audio_url_filter(d['file_path'])
            fmt, led = _fmt_format(d)
            d['format_display'] = fmt
            d['format_color'] = led
            d['duration_fmt'] = _fmt_seconds(d.get('duration'))
            d['artist_id'] = artist_id
            top_tracks.append(d)

        return render_template('artist.html', artist=ar_data, albums=albums,
                               total_tracks=total_tracks, fav_ids=_favorites_set,
                               genres=genres, similar_artists=similar_artists,
                               top_tracks=top_tracks,
                               artist_nationality=ar_data.get('nationality', ''))
    finally:
        conn.close()


@app.route('/api/artist/<int:artist_id>/website')
def api_artist_website(artist_id):
    """Return (and cache) the official website URL for an artist via MusicBrainz."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT name, website_url FROM artists WHERE id=?', (artist_id,)
        ).fetchone()
        if not row:
            return jsonify({'url': None})

        # Return cached value if present
        if row['website_url']:
            # '-' means "looked up, nothing found" — don't retry
            return jsonify({'url': None if row['website_url'] == '-' else row['website_url']})

        # MusicBrainz search — no auth required, rate limit is 1 req/s
        import urllib.request, urllib.parse
        query = urllib.parse.quote(row['name'])
        mb_url = (
            f'https://musicbrainz.org/ws/2/artist/?query=artist:"{query}"'
            f'&limit=1&fmt=json'
        )
        req = urllib.request.Request(mb_url, headers={
            'User-Agent': 'Orbyte/1.0 (music-browser; contact@orbyte.local)'
        })
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())

        artists_mb = data.get('artists', [])
        official_url = None
        if artists_mb:
            mbid = artists_mb[0].get('id', '')
            if mbid:
                rel_url = f'https://musicbrainz.org/ws/2/artist/{mbid}?inc=url-rels&fmt=json'
                rel_req = urllib.request.Request(rel_url, headers={
                    'User-Agent': 'Orbyte/1.0 (music-browser; contact@orbyte.local)'
                })
                with urllib.request.urlopen(rel_req, timeout=4) as rr:
                    rdata = json.loads(rr.read())
                for rel in rdata.get('relations', []):
                    if rel.get('type') in ('official homepage', 'streaming', 'social network'):
                        if rel.get('type') == 'official homepage':
                            official_url = rel.get('url', {}).get('resource')
                            break
                        elif not official_url:
                            official_url = rel.get('url', {}).get('resource')

        # Store sentinel '-' when no URL found, to avoid re-querying MusicBrainz on every visit
        store_val = official_url if official_url else '-'
        conn.execute(
            'UPDATE artists SET website_url=? WHERE id=?', (store_val, artist_id)
        )
        conn.commit()

        return jsonify({'url': official_url})
    except Exception as e:
        app.logger.debug(f'[website lookup] artist {artist_id}: {e}')
        return jsonify({'url': None})
    finally:
        conn.close()


@app.route('/album/<int:album_id>')
def album(album_id):
    conn = get_db_connection()
    try:
        alb = conn.execute(
            'SELECT al.*, ar.name as artist_name FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id WHERE al.id=?',
            (album_id,)
        ).fetchone()
        if not alb: return "Album not found", 404

        tracks = conn.execute(
            'SELECT * FROM tracks WHERE album_id=? ORDER BY disc_number, CAST(track_number AS INTEGER)',
            (album_id,)
        ).fetchall()

        alb = dict(alb)
        alb['cover_path'] = clean_db_path(alb.get('cover_path'))

        fp = conn.execute(
            'SELECT publisher FROM tracks WHERE album_id=? AND publisher IS NOT NULL AND publisher!="" LIMIT 1',
            (album_id,)
        ).fetchone()
        alb['publisher'] = fp['publisher'] if fp else None

        am = conn.execute('SELECT * FROM album_meta WHERE album_id=?', (album_id,)).fetchone()
        # Always include all expected keys so Jinja2 "album_meta.field is not none" never raises
        _AM_DEFAULTS = {
            'mood_predominante': None, 'momento_predominante': None, 'era': None,
            'idioma_principal': None, 'avg_energy': None, 'avg_valence': None,
            'avg_tension': None, 'avg_depth': None, 'avg_bailabilidad': None,
            'tracks_con_letra': None, 'tracks_sincronizados': None,
            'genre_primary': None, 'genre_secondary': None,
            'lastfm_listeners': None, 'lastfm_playcount': None,
        }
        album_meta = {**_AM_DEFAULTS, **(dict(am) if am else {})}

        track_list = []
        for t in tracks:
            td = {**dict(t),
                  'file_path':  clean_db_path(dict(t).get('file_path')),
                  'cover_path': alb['cover_path'],
                  'album_name': alb.get('name'),
                  'artist_id':  alb.get('artist_id')}
            td['audio_url'] = audio_url_filter(td['file_path'])
            td['cover_url'] = cover_url_filter(td['cover_path'])
            fmt, led = _fmt_format(td)
            td['format_display'] = fmt
            td['format_color']   = led
            # led_color stays as-is from DB
            tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (t['id'],)).fetchone()
            if tm:
                for k in tm.keys():
                    if k != 'track_id':
                        td[f'meta_{k}'] = tm[k]
            track_list.append(td)

        disc_numbers = set(t['disc_number'] for t in track_list if t['disc_number'])
        total_discs  = len(disc_numbers) if disc_numbers else 1

        # Issue 10: genre fallback — use most common track genre if album_meta lacks it
        if not album_meta.get('genre_primary'):
            row = conn.execute(
                '''SELECT COALESCE(tm.genre_primary, t.genre) as g, COUNT(*) as c
                   FROM tracks t LEFT JOIN track_meta tm ON tm.track_id=t.id
                   WHERE t.album_id=? AND COALESCE(tm.genre_primary, t.genre) IS NOT NULL
                   GROUP BY g ORDER BY c DESC LIMIT 1''',
                (album_id,)
            ).fetchone()
            if row:
                album_meta['genre_primary'] = row['g']

        # Issue 11: artist nationality + flag
        artist_row = conn.execute(
            'SELECT nationality, similar_artists_json FROM artists WHERE id=?',
            (alb.get('artist_id'),)
        ).fetchone()
        artist_nationality = ''
        artist_flag        = ''
        if artist_row and artist_row['nationality']:
            artist_nationality = artist_row['nationality']
            artist_flag        = nationality_flag(artist_nationality)

        # Favorites set for this page
        fav_ids = _favorites_set

        return render_template('album.html', album=alb, tracks=track_list,
                               primary_format=alb.get('primary_format') or 'Unknown',
                               total_discs=total_discs, multi_disc=total_discs > 1,
                               album_meta=album_meta,
                               artist_nationality=artist_nationality,
                               artist_flag=artist_flag,
                               fav_ids=fav_ids)
    finally:
        conn.close()


@app.route('/track/<int:track_id>')
def track(track_id):
    conn = get_db_connection()
    try:
        t = conn.execute('''
            SELECT t.*, a.name as album_name, a.cover_path, a.year as album_year, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums a ON t.album_id=a.id
            LEFT JOIN artists ar ON a.artist_id=ar.id
            WHERE t.id=?
        ''', (track_id,)).fetchone()
        if not t: return "Track not found", 404
        t = dict(t)
        t['file_path']  = clean_db_path(t.get('file_path'))
        t['cover_path'] = clean_db_path(t.get('cover_path'))
        t['publisher']  = clean_db_path(t.get('publisher'))
        fmt, led = _fmt_format(t)
        # led_color already in t from DB
        sr = t.get('sample_rate_real')
        tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (track_id,)).fetchone()
        track_meta = dict(tm) if tm else {}
        pop_row = conn.execute(
            'SELECT pop_score FROM track_pop_cache WHERE track_id=?', (track_id,)
        ).fetchone()
        track_pop_score = int(pop_row['pop_score']) if pop_row else 0
        return render_template('track.html', track=t,
                               format_display=fmt, format_color=led,
                               sample_rate=f"{sr/1000:.1f} kHz" if sr else "N/A",
                               bit_depth=f"{t.get('bit_depth') or 24} bit",
                               bitrate_fmt=_fmt_bitrate(t.get('bitrate')),
                               track_meta=track_meta,
                               track_pop_score=track_pop_score,
                               led_labels=LED_LABELS)
    finally:
        conn.close()


@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    if not query: return home()
    conn = get_db_connection()
    try:
        like = f'%{query}%'
        artists = conn.execute(
            '''SELECT a.id, a.name, a.nationality, a.letter,
                      a.lastfm_listeners,
                      COUNT(DISTINCT al.id)                          AS album_count,
                      COALESCE(MAX(apc.pop_score), 0)                AS pop_score,
                      (SELECT t.genre FROM tracks t JOIN albums al2 ON t.album_id=al2.id
                       WHERE al2.artist_id=a.id AND t.genre IS NOT NULL AND t.genre != ""
                       GROUP BY t.genre ORDER BY COUNT(*) DESC LIMIT 1) AS most_common_genre
               FROM artists a
               LEFT JOIN albums al  ON al.artist_id=a.id
               LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
               WHERE a.name LIKE ?
                 AND EXISTS (SELECT 1 FROM albums al2 WHERE al2.artist_id=a.id)
               GROUP BY a.id
               ORDER BY a.name LIMIT 10''',
            (like,)
        ).fetchall()
        albums = conn.execute(
            '''SELECT al.id, al.name, al.cover_path, al.primary_format, al.year,
                      al.track_count, al.total_duration, al.artist_id, ar.name as artist_name,
                      (SELECT led_color FROM tracks WHERE album_id=al.id
                       ORDER BY CASE led_color
                         WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                         WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                         ELSE 6 END LIMIT 1) as album_led
               FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id
               WHERE al.name LIKE ? OR ar.name LIKE ? ORDER BY al.name LIMIT 20''',
            (like, like)
        ).fetchall()
        tracks = conn.execute(
            '''SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.is_mqa,
                      t.codec, t.duration, t.sample_rate_real,
                      a.id as album_id, a.name as album_name, a.cover_path,
                      tm.mood as meta_mood, tm.momento as meta_momento, tm.tier as meta_tier
               FROM tracks t
               LEFT JOIN albums a ON t.album_id=a.id
               LEFT JOIN track_meta tm ON tm.track_id=t.id
               WHERE t.title LIKE ? OR t.artist LIKE ? OR t.genre LIKE ?
               ORDER BY t.title LIMIT 50''',
            (like, like, like)
        ).fetchall()

        albums_out = [{**dict(a), 'cover_path': clean_db_path(a['cover_path'])} for a in albums]
        tracks_out = []
        for t in tracks:
            d = dict(t)
            d['cover_path'] = clean_db_path(d.get('cover_path'))
            _, led = _fmt_format(d)
            d['format_color'] = led
            tracks_out.append(d)

        return render_template('search.html',
                               artists=[dict(a) for a in artists],
                               albums=albums_out, tracks=tracks_out, query=query)
    finally:
        conn.close()

# ── Pagination helper ──────────────────────────────────────────────────────────

PAGE_SIZE = 30

# ── album_meta field mapping for api_meta_tracks ───────────────────────────────
# Maps the 'field' param values to their column names in the album_meta table.
# Fields NOT listed here are queried at track_meta level instead.
_ALBUM_META_FIELD = {
    'mood':    'mood_predominante',
    'momento': 'momento_predominante',
    'era':     'era',
    'idioma':  'idioma_principal',
}

# ── Sort system ───────────────────────────────────────────────────────────────
# Whitelisted sort columns for albums and tracks (prevents SQL injection)

ALBUM_SORT_MAP = {
    'nombre':      'al.name',
    'artista':     'ar.name',
    'año':         'al.year',
    'pistas':      'al.track_count',
    'popularidad': 'COALESCE(apc.pop_score, 0)',   # pre-computed score: quality + tier + metadata
    'random':      'RANDOM()',
}
TRACK_SORT_MAP = {
    'artista':      't.artist',
    'titulo':       't.title',
    'año':          'a.year',
    'bpm':          'tm.bpm',
    'energia':      'tm.energy',
    'bailabilidad': 'tm.bailabilidad',
    'tier':         "CASE tm.tier WHEN 'silver' THEN 0 WHEN 'bronze' THEN 1 ELSE 2 END",
    'popularidad':  'COALESCE(tpc.pop_score, 0)',
    'random':       'RANDOM()',
    # 'intercalar' is handled client-side, server sends 'random' for it
}

def _album_order(sort_key, direction):
    """Return validated ORDER BY clause for album queries."""
    col = ALBUM_SORT_MAP.get(sort_key, 'al.track_count')
    if col == 'RANDOM()':
        return 'RANDOM()'
    # Popularidad siempre DESC por defecto (más popular primero)
    if sort_key == 'popularidad' and direction == 'asc':
        direction = 'desc'
    dir_ = 'DESC' if direction == 'desc' else 'ASC'
    return f'{col} {dir_} NULLS LAST'

def _track_order(sort_key, direction):
    """Return validated ORDER BY clause for track queries."""
    col = TRACK_SORT_MAP.get(sort_key, 't.artist')
    if col == 'RANDOM()':
        return 'RANDOM()'
    # Popularidad siempre DESC
    if sort_key == 'popularidad' and direction == 'asc':
        direction = 'desc'
    dir_ = 'DESC' if direction == 'desc' else 'ASC'
    return f'{col} {dir_} NULLS LAST'

# ── Deduplicación de pistas repetidas (misma canción, distinto álbum) ──────────
# Issue: en la vista de pistas (Búsqueda Avanzada y las pestañas Pistas de
# mood/momento/era/tema/tier/idioma/género) una misma canción puede aparecer
# varias veces si vive en más de un álbum (ediciones deluxe, remasters,
# compilados...). Además de ensuciar el listado, esto rompe el botón
# "➕ Añadir a cola": trackRowHtml()/addTrackWithFeedback() (advanced_search.html)
# y su equivalente en browse.html indexan por posición dentro del array de
# resultados, así que cada copia agrega la pista correcta — el problema real
# es mostrar N copias visualmente indistinguibles (mismo título) donde el
# usuario esperaba una sola.
#
# Solución: colapsar a UNA fila por (Título, Artista), quedándonos con la de
# mejor score = 70% Calidad + 30% Popularidad (ambos normalizados 0-100).
# La Calidad reutiliza el mismo ranking LED que ya se usa en todo este
# archivo para elegir la versión "mejor" de un álbum (ver los "ORDER BY CASE
# led_color ... LIMIT 1" repetidos arriba y _led_for_album_tracks) en vez de
# inventar una escala nueva: magenta es la mejor calidad, amarillo/otros la
# peor. La Popularidad es el pop_score (0-100) ya precalculado en
# track_pop_cache, igual que en todos los sorts de "popularidad" existentes.
#
# Importante: el ticket pide filtrar duplicados "de los resultados obtenidos"
# — es decir, la comparación debe quedar acotada a los mismos filtros que ya
# está aplicando la vista, no a la biblioteca completa. Si no fuera así, un
# filtro "Calidad: CD" podría hacer desaparecer una canción entera de los
# resultados solo porque existe una copia DSD256 de mejor score en otro
# álbum que ni siquiera cumple ese filtro — justo lo opuesto de lo que el
# usuario pidió al filtrar. Por eso _track_dedupe_condition recibe el WHERE
# ya construido por la vista (extra_where) y lo reaplica, con los mismos
# alias re-prefijados con "dup_", a la copia candidata antes de compararla.
#
# Si el usuario quiere la otra versión, puede seguir encontrándola buscándola
# manualmente (álbum, vista de álbumes, etc.) — este filtro solo afecta la
# vista de pistas, tal como pide el ticket.
_TRACK_QUALITY_RANK_SQL = """(CASE {alias}.led_color
        WHEN 'magenta' THEN 100.0
        WHEN 'blue'    THEN 83.3333333333
        WHEN 'green'   THEN 66.6666666667
        WHEN 'red'     THEN 50.0
        WHEN 'cyan'    THEN 33.3333333333
        WHEN 'white'   THEN 16.6666666667
        ELSE 0.0
    END)"""

# Alias usados por las dos consultas que llaman a _track_dedupe_condition —
# se re-prefijan con "dup_" para poder unir una segunda copia de tracks/
# albums/artists/track_meta/track_pop_cache dentro de la subconsulta
# correlacionada sin chocar con los alias de la fila "exterior".
_DEDUPE_ALIASES = ('t', 'tm', 'al', 'ar', 'tpc')

def _track_dedupe_condition(extra_where='', track_alias='t', pop_alias='tpc'):
    """
    Fragmento SQL (sin el 'AND' inicial, mismo estilo que _build_adv_filters)
    para insertar en el WHERE de una consulta de pistas ya unida (JOIN) con
    albums/artists/track_meta/track_pop_cache. Excluye una pista si existe
    otra CON LOS MISMOS FILTROS ACTIVOS (mismo Título + Artista, comparación
    case/espacios-insensible) y mejor score; en empate de score se conserva
    el id menor, para un resultado estable entre requests.

    extra_where: el WHERE que la vista ya aplica (usando los alias t/al/ar/
    tm/tpc), para que la pista candidata deba cumplir EXACTAMENTE los mismos
    filtros que la fila que reemplaza — no cualquier copia de la canción en
    toda la biblioteca. Pasar '' cuando no hay filtros activos. Los mismos
    valores de `extra_where` deben añadirse DOS VECES a la lista de params
    de la consulta que llama a esta función (una para el WHERE exterior, una
    para la copia re-prefijada de aquí adentro) — ver comentarios en los
    call sites.
    """
    def _reprefix(sql):
        for alias in _DEDUPE_ALIASES:
            sql = re.sub(rf'\b{alias}\.', f'dup_{alias}.', sql)
        return sql

    dup_extra = f' AND {_reprefix(extra_where)}' if extra_where else ''
    score_self  = f"({_TRACK_QUALITY_RANK_SQL.format(alias=track_alias)} * 0.7 " \
                  f"+ COALESCE({pop_alias}.pop_score, 0) * 0.3)"
    score_other = f"({_TRACK_QUALITY_RANK_SQL.format(alias='dup_t')} * 0.7 " \
                  f"+ COALESCE(dup_tpc.pop_score, 0) * 0.3)"
    return f"""NOT EXISTS (
        SELECT 1 FROM tracks dup_t
        JOIN albums dup_al ON dup_al.id = dup_t.album_id
        LEFT JOIN artists dup_ar ON dup_ar.id = dup_al.artist_id
        LEFT JOIN track_meta dup_tm ON dup_tm.track_id = dup_t.id
        LEFT JOIN track_pop_cache dup_tpc ON dup_tpc.track_id = dup_t.id
        WHERE LOWER(TRIM(dup_t.title))  = LOWER(TRIM({track_alias}.title))
          AND LOWER(TRIM(dup_t.artist)) = LOWER(TRIM({track_alias}.artist))
          AND dup_t.id != {track_alias}.id
          {dup_extra}
          AND ({score_other} > {score_self}
               OR ({score_other} = {score_self} AND dup_t.id < {track_alias}.id))
    )"""

def _paginate(conn, count_sql, count_params, data_sql, data_params, page,
              order_by='al.name ASC'):
    """Run paginated query with dynamic ORDER BY."""
    total  = conn.execute(count_sql, count_params).fetchone()[0]
    offset = (page - 1) * PAGE_SIZE
    final_sql = data_sql + f' ORDER BY {order_by} LIMIT ? OFFSET ?'
    rows  = conn.execute(final_sql, list(data_params) + [PAGE_SIZE, offset]).fetchall()
    albums = []
    for a in rows:
        d = dict(a)
        d['cover_path'] = clean_db_path(d.get('cover_path'))
        d['album_led']  = d.get('album_led') or 'yellow'
        albums.append(d)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return albums, total, total_pages

# ── Búsqueda Avanzada (multi-filter search) ─────────────────────────────────────
# One page + one API that combine ANY subset of the filters already used
# individually by /mood, /genre, /led, /language, etc. Reuses the exact same
# conventions as the routes above (PAGE_SIZE, _paginate, _album_order,
# _track_order, track_to_json, get_db_connection) instead of introducing new
# ones — see AGENTE.md rule 2.

# Calidad values requested by the ticket. NOTE: 'DSD512' is intentionally left
# out — the DB only distinguishes DSD tiers up to DSD256 (see _dsd_label()
# above: sample_rate_real thresholds top out at 11289600 Hz = DSD256, and no
# track in the schema is tagged higher). Selecting a "DSD512" value would
# either silently relabel DSD256 files or match nothing; instead of inventing
# that convention, this is called out for a product decision — see chat reply.
QUALITY_OPTIONS = ['CD', 'HI-RES', 'DSD64', 'DSD128', 'DSD256', 'MQA', 'MQA Studio', 'OSR']

# Popularidad buckets mirror the 5-star scale already rendered in browse.html
# (Math.floor(pop_score / 20)), so a filter labelled "★★★★★" matches exactly
# what the user already sees as 5 stars elsewhere in the app.
POP_BUCKETS = {'1': (0, 19), '2': (20, 39), '3': (40, 59), '4': (60, 79), '5': (80, 100)}

# Energía is stored as a -1..1 float (see the (energy+1)/2*100 display math in
# browse.html / base.html's drawer bars). Bailabilidad is already 0..100 (see
# the bailBar width calc in browse.html). Bucket edges below are a first pass
# at "Baja / Media / Alta" — flagged in the chat reply as worth confirming.
ENERGY_BUCKETS = {'baja': (-1.0, -0.34), 'media': (-0.34, 0.34), 'alta': (0.34, 1.0)}
BAIL_BUCKETS   = {'baja': (0, 33), 'media': (34, 66), 'alta': (67, 100)}

def _quality_condition(value):
    """SQL boolean fragment (references alias t.) for a Calidad filter value."""
    v = (value or '').strip()
    if v == 'CD':         return "t.led_color = 'yellow'"
    if v == 'HI-RES':     return "t.led_color = 'white'"
    if v == 'DSD64':      return ("t.is_dsd=1 AND (UPPER(COALESCE(t.dsd_rate,'')) LIKE 'DSD64%' "
                                   "OR (COALESCE(t.dsd_rate,'')='' AND t.sample_rate_real>=2822400 "
                                   "AND t.sample_rate_real<5644800))")
    if v == 'DSD128':     return ("t.is_dsd=1 AND (UPPER(COALESCE(t.dsd_rate,'')) LIKE 'DSD128%' "
                                   "OR (COALESCE(t.dsd_rate,'')='' AND t.sample_rate_real>=5644800 "
                                   "AND t.sample_rate_real<11289600))")
    if v == 'DSD256':     return ("t.is_dsd=1 AND (UPPER(COALESCE(t.dsd_rate,'')) LIKE 'DSD256%' "
                                   "OR (COALESCE(t.dsd_rate,'')='' AND t.sample_rate_real>=11289600))")
    if v == 'MQA':        return "t.is_mqa=1 AND t.led_color = 'green'"
    if v == 'MQA Studio': return "t.is_mqa=1 AND t.led_color = 'blue'"
    if v == 'OSR':        return "t.led_color = 'magenta'"  # "Original Sample Rate" (MQB)
    return '0'  # unknown value → explicitly no matches, never silently ignored

def _range_condition(col, bucket_key, bucket_map):
    """Generic BETWEEN condition builder for popularidad / energia / bailabilidad."""
    bounds = bucket_map.get(bucket_key)
    if not bounds:
        return None, []
    return f'{col} BETWEEN ? AND ?', [bounds[0], bounds[1]]

def _build_adv_filters(args, pop_alias, for_albums):
    """
    Build (where_clauses, params) from every active Búsqueda Avanzada filter in
    request.args. Both the albums query and the tracks query alias tracks as
    't', track_meta as 'tm', albums as 'al' and artists as 'ar' — only the
    popularity-cache alias differs (apc for albums, tpc for tracks), hence the
    pop_alias parameter.

    Every field accepts ONE OR MORE values (repeat the query param, e.g.
    ?mood=Feliz&mood=Triste). Multiple values within the SAME field are
    combined with OR — a track only has a single mood/momento/era/etc, so
    combining them with AND would always return zero rows; OR ("cualquiera
    de estos") is the only combination that makes sense there. Different
    fields are still combined with AND, same as before.

    for_albums controls which table mood/momento/era/idioma/genero/energia/
    bailabilidad are matched against — this is the exact same distinction
    _ALBUM_META_FIELD already makes for the single-filter /mood, /momento,
    /era, /language routes: a single mismatched or outlier track's
    track_meta value shouldn't be enough to pull an otherwise-unrelated
    album into the Albums view, so that view matches against album_meta's
    predominant/average field instead. The Tracks view keeps matching
    per-track (tm.*/t.*), which is correct there.

    Fields intentionally NOT split this way:
    - tema (Categoría Letra): album_meta has no equivalent column — stays
      track_meta-only for both views.
    - pais, anio: already single-level (artists.nationality, albums.year),
      no track/album ambiguity to begin with.
    - popularidad: already correctly split via the pop_alias param (apc for
      albums, tpc for tracks) — both are pre-computed scores, not raw
      per-track values, so no change was needed here.
    - calidad: albums has a `primary_format` column, but its value domain
      hasn't been confirmed to line up with QUALITY_OPTIONS (CD/HI-RES/
      DSD64.../OSR), so it's left per-track for both views rather than
      guessing at the mapping.
    """
    clauses, params = [], []

    calidad_vals = args.getlist('calidad')
    if calidad_vals:
        sub = [_quality_condition(v) for v in calidad_vals]
        clauses.append('(' + ' OR '.join(sub) + ')')

    for field, tm_col in (('mood', 'mood'), ('momento', 'momento'), ('era', 'era'),
                           ('idioma', 'idioma')):
        vals = args.getlist(field)
        if vals:
            placeholders = ','.join('?' * len(vals))
            col = f'am.{_ALBUM_META_FIELD[field]}' if for_albums else f'tm.{tm_col}'
            clauses.append(f'{col} IN ({placeholders})')
            params += vals

    tema_vals = args.getlist('tema')  # Categoría Letra
    if tema_vals:
        placeholders = ','.join('?' * len(tema_vals))
        clauses.append(f'tm.tema_lirico IN ({placeholders})')
        params += tema_vals

    genero_vals = args.getlist('genero')
    if genero_vals:
        sub = []
        if for_albums:
            for g in genero_vals:
                sub.append('(am.genre_primary = ? OR am.genre_secondary = ?)')
                params += [g, g]
        else:
            for g in genero_vals:
                sub.append('(t.genre = ? OR tm.genre_primary = ? OR tm.genre_secondary = ?)')
                params += [g, g, g]
        clauses.append('(' + ' OR '.join(sub) + ')')

    pais_vals = args.getlist('pais')  # País Origen (artists.nationality)
    if pais_vals:
        placeholders = ','.join('?' * len(pais_vals))
        clauses.append(f'ar.nationality IN ({placeholders})')
        params += pais_vals

    anio_vals = [v for v in args.getlist('anio') if v.lstrip('-').isdigit()]  # Año(s) de lanzamiento
    if anio_vals:
        placeholders = ','.join('?' * len(anio_vals))
        clauses.append(f'al.year IN ({placeholders})')
        params += [int(v) for v in anio_vals]

    pop_vals = args.getlist('popularidad')
    if pop_vals:
        sub, p = [], []
        for v in pop_vals:
            cond, cp = _range_condition(f'COALESCE({pop_alias}.pop_score,0)', v, POP_BUCKETS)
            if cond:
                sub.append(cond)
                p += cp
        if sub:
            clauses.append('(' + ' OR '.join(sub) + ')')
            params += p

    energia_vals = args.getlist('energia')
    if energia_vals:
        sub, p = [], []
        energia_col = 'am.avg_energy' if for_albums else 'tm.energy'
        for v in energia_vals:
            cond, cp = _range_condition(energia_col, v, ENERGY_BUCKETS)
            if cond:
                sub.append(cond)
                p += cp
        if sub:
            clauses.append('(' + ' OR '.join(sub) + ')')
            params += p

    bail_vals = args.getlist('bailabilidad')
    if bail_vals:
        sub, p = [], []
        bail_col = 'am.avg_bailabilidad' if for_albums else 'tm.bailabilidad'
        for v in bail_vals:
            cond, cp = _range_condition(bail_col, v, BAIL_BUCKETS)
            if cond:
                sub.append(cond)
                p += cp
        if sub:
            clauses.append('(' + ' OR '.join(sub) + ')')
            params += p

    return clauses, params

def _advanced_search_options(conn):
    """
    Gather the same filter-option lists home() already computes for its own
    sections (moods, momentos, eras, temas, genres, languages) plus the two
    new ones added there (available_years, nationalities). Kept as its own
    function — rather than reusing home()'s inline queries — so this route
    can supply exactly what _advanced_search_modal.html needs without
    depending on (or risking a merge conflict with) the rest of home()'s
    unrelated stats/recent-albums queries.
    NOTE: this duplicates a handful of SELECTs that already exist in home().
    Once app.py is fully reviewed it would be worth factoring both into one
    shared helper — left as-is here to keep this diff minimal and safe.
    """
    moods = [(r['mood'], r['c']) for r in conn.execute(
        'SELECT mood, COUNT(*) as c FROM track_meta WHERE mood IS NOT NULL '
        'GROUP BY mood ORDER BY c DESC LIMIT 14').fetchall()]

    momentos = [(r['momento'], r['c']) for r in conn.execute(
        'SELECT momento, COUNT(*) as c FROM track_meta WHERE momento IS NOT NULL '
        'GROUP BY momento ORDER BY c DESC').fetchall()]

    era_order = ['early_rock_era', 'british_invasion_era', 'classic_rock_era',
                 'nwobhm_synth_era', 'grunge_alternative_era', 'post_millennial_era',
                 'streaming_era', 'current_era']
    era_dict = {r['era']: r['c'] for r in conn.execute(
        'SELECT era, COUNT(*) as c FROM track_meta WHERE era IS NOT NULL GROUP BY era').fetchall()}
    eras = [(e, era_dict[e]) for e in era_order if e in era_dict]

    temas = [(r['tema_lirico'], r['c']) for r in conn.execute(
        'SELECT tema_lirico, COUNT(*) as c FROM track_meta WHERE tema_lirico IS NOT NULL '
        'GROUP BY tema_lirico ORDER BY c DESC LIMIT 10').fetchall()]

    genres_primary = [(r['genre_primary'], r['c']) for r in conn.execute(
        'SELECT genre_primary, COUNT(*) as c FROM track_meta '
        'WHERE genre_primary IS NOT NULL AND genre_primary!="" '
        'GROUP BY genre_primary ORDER BY c DESC LIMIT 15').fetchall()]

    genres = [(r['genre'], r['c']) for r in conn.execute(
        'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" '
        'GROUP BY genre ORDER BY c DESC LIMIT 8').fetchall()]

    languages = [(r['idioma'], r['c']) for r in conn.execute(
        'SELECT idioma, COUNT(*) as c FROM track_meta WHERE idioma IS NOT NULL AND idioma!="" '
        'GROUP BY idioma ORDER BY c DESC LIMIT 12').fetchall()]

    # Unlimited genre lists — dedicated to the Búsqueda Avanzada Género capsule.
    # genres_primary/genres above are capped (15/8) for home.html's own compact
    # "Géneros / Subgéneros" section; the modal needs every option so it isn't
    # missing anything, per the ticket's point 2. Separate variables so fixing
    # this never risks changing that unrelated home.html section.
    adv_genres_primary = [(r['genre_primary'], r['c']) for r in conn.execute(
        'SELECT genre_primary, COUNT(*) as c FROM track_meta '
        'WHERE genre_primary IS NOT NULL AND genre_primary!="" '
        'GROUP BY genre_primary ORDER BY c DESC').fetchall()]

    all_genres = [(r['genre'], r['c']) for r in conn.execute(
        'SELECT genre, COUNT(*) as c FROM tracks WHERE genre IS NOT NULL AND genre!="" '
        'GROUP BY genre ORDER BY c DESC').fetchall()]

    available_years = [r['year'] for r in conn.execute(
        'SELECT DISTINCT year FROM albums WHERE year IS NOT NULL ORDER BY year DESC').fetchall()]

    nationalities = [(r['nationality'], r['c']) for r in conn.execute(
        'SELECT nationality, COUNT(*) as c FROM artists '
        'WHERE nationality IS NOT NULL AND nationality!="" '
        'GROUP BY nationality ORDER BY c DESC').fetchall()]

    return dict(moods=moods, momentos=momentos, eras=eras, temas=temas,
                genres_primary=genres_primary, genres=genres, languages=languages,
                adv_genres_primary=adv_genres_primary, all_genres=all_genres,
                available_years=available_years, nationalities=nationalities)

@app.route('/busqueda-avanzada')
def advanced_search_page():
    """
    Búsqueda Avanzada results shell. The album/track RESULTS come from
    /api/search/advanced client-side (so the same view works no matter which
    combination of filters was used) — this route only supplies the filter
    OPTION lists so the modal can be reopened ("Modificar filtros") without
    a round-trip back to home.
    """
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
    finally:
        conn.close()
    # Pass the query string down instead of making the client read
    # window.location.search: navigateTo() (base.html) runs this page's
    # <script> BEFORE it calls history.pushState(), so a client-side read of
    # window.location.search on an SPA navigation would still see the
    # PREVIOUS page's URL. Baking the real filters in here — same approach
    # browse.html uses for its single filter_type/filter_value — sidesteps
    # that race entirely.
    return render_template('advanced_search.html', initial_query=request.query_string.decode('utf-8'), **opts)

@app.route('/api/search/advanced')
def api_search_advanced():
    """
    Multi-filter search across every RichMetaPro + technical field at once.
    ?view=albums (default) or ?view=tracks selects which result set to return;
    the front end calls this twice (lazily, only when the user switches tabs)
    rather than computing both server-side on every request.
    """
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir', 'desc')
    view = request.args.get('view', 'albums')

    conn = get_db_connection()
    try:
        if view == 'tracks':
            clauses, params = _build_adv_filters(request.args, pop_alias='tpc', for_albums=False)
            # Colapsa duplicados (misma canción en distintos álbumes) — ver
            # _track_dedupe_condition. Se le pasan los filtros YA activos
            # (extra_where) para que la comparación quede acotada a "otras
            # copias que también cumplirían este mismo filtro/búsqueda", tal
            # como pide el ticket ("filtre de los resultados obtenidos") —
            # así un filtro de Calidad, por ejemplo, no hace desaparecer una
            # canción solo porque existe una copia mejor fuera de ese filtro.
            extra_where = ' AND '.join(clauses)
            dedupe_clause = _track_dedupe_condition(extra_where=extra_where, track_alias='t', pop_alias='tpc')
            clauses = clauses + [dedupe_clause]
            # dedupe_clause reincorpora una copia (re-prefijada dup_*) de los
            # mismos filtros — cada uno de sus '?' necesita su valor de
            # nuevo, en el mismo orden, así que los params se duplican.
            params = params + params
            where = (' AND ' + ' AND '.join(clauses)) if clauses else ''

            count_sql = f'''SELECT COUNT(*) FROM tracks t
                             JOIN albums al ON al.id=t.album_id
                             LEFT JOIN artists ar ON ar.id=al.artist_id
                             LEFT JOIN track_meta tm ON tm.track_id=t.id
                             LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                             WHERE 1=1{where}'''
            total = conn.execute(count_sql, params).fetchone()[0]

            order = _track_order(sort, dir_)
            offset = (page - 1) * PAGE_SIZE
            data_sql = f'''SELECT t.*, al.id as album_id, al.name as album_name,
                                  al.year as album_year, al.cover_path,
                                  ar.name as artist_name,
                                  tm.mood, tm.momento, tm.era, tm.tema_lirico, tm.idioma,
                                  tm.genre_primary, tm.genre_secondary, tm.bpm, tm.energy,
                                  tm.bailabilidad, tm.tier, COALESCE(tpc.pop_score,0) as pop_score
                           FROM tracks t
                           JOIN albums al ON al.id=t.album_id
                           LEFT JOIN artists ar ON ar.id=al.artist_id
                           LEFT JOIN track_meta tm ON tm.track_id=t.id
                           LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                           WHERE 1=1{where}
                           ORDER BY {order} LIMIT ? OFFSET ?'''
            rows = conn.execute(data_sql, params + [PAGE_SIZE, offset]).fetchall()

            tracks = []
            for r in rows:
                d = track_to_json(dict(r))
                d['album_id']      = r['album_id']
                d['album_name']    = r['album_name']
                d['album_year']    = r['album_year']
                d['artist_name']   = r['artist_name']
                d['mood']          = r['mood']
                d['momento']       = r['momento']
                d['era']           = r['era']
                d['tema_lirico']   = r['tema_lirico']
                d['idioma']        = r['idioma']
                d['genre_primary'] = r['genre_primary']
                d['bpm']           = r['bpm']
                d['energy']        = r['energy']
                d['bailabilidad']  = r['bailabilidad']
                d['tier']          = r['tier']
                d['pop_score']     = r['pop_score']
                tracks.append(d)

            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            return jsonify({'tracks': tracks, 'total': total, 'total_pages': total_pages, 'page': page})

        # view == 'albums'
        clauses, params = _build_adv_filters(request.args, pop_alias='apc', for_albums=True)
        where = (' AND ' + ' AND '.join(clauses)) if clauses else ''

        count_sql = f'''SELECT COUNT(DISTINCT al.id) FROM albums al
                         JOIN tracks t ON t.album_id=al.id
                         LEFT JOIN artists ar ON ar.id=al.artist_id
                         LEFT JOIN track_meta tm ON tm.track_id=t.id
                         LEFT JOIN album_meta am ON am.album_id=al.id
                         LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                         WHERE 1=1{where}'''
        data_sql = f'''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name,
                              (SELECT led_color FROM tracks WHERE album_id=al.id
                               ORDER BY CASE led_color
                                   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                                   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                                   ELSE 6 END LIMIT 1) as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN artists ar ON ar.id=al.artist_id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       LEFT JOIN album_meta am ON am.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE 1=1{where}'''
        order = _album_order(sort, dir_)
        albums, total, total_pages = _paginate(conn, count_sql, params, data_sql, params, page, order)
        # _paginate() only cleans cover_path — it never had to add cover_url
        # because every other caller renders albums through Jinja (which
        # applies the |cover_url filter at template time). This is a plain
        # JSON API, so the URL has to be computed here or the <img src>
        # ends up pointing at a raw filesystem path the browser can't load.
        for a in albums:
            a['cover_url'] = cover_url_filter(a.get('cover_path'))
            a['duration_fmt'] = _fmt_seconds(a.get('total_duration'))
        return jsonify({'albums': albums, 'total': total, 'total_pages': total_pages, 'page': page})
    finally:
        conn.close()

# ── Format browse ──────────────────────────────────────────────────────────────

@app.route('/format/<fmt>')
def browse_format(fmt):
    page  = max(1, request.args.get('page', 1, type=int))
    sort  = request.args.get('sort', 'popularidad')
    dir_  = request.args.get('dir',  'desc')
    conn  = get_db_connection()
    try:
        base = '''SELECT al.id, al.name, al.cover_path, al.primary_format, al.year,
                         al.track_count, al.total_duration, al.artist_id, ar.name as artist_name,
                         (SELECT led_color FROM tracks WHERE album_id=al.id
                          ORDER BY CASE led_color
                            WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2
                            WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5
                            ELSE 6 END LIMIT 1) as album_led
                  FROM albums al LEFT JOIN artists ar ON al.artist_id=ar.id'''
        if fmt == 'DSD':   where = ' WHERE al.primary_format="DSD"'
        elif fmt == 'MQA':  where = ' WHERE al.primary_format="MQA"'
        elif fmt == 'FLAC': where = ' WHERE (al.primary_format="FLAC" OR al.primary_format IS NULL)'
        else:               where = ''
        order = _album_order(sort, dir_)
        count_sql = f'SELECT COUNT(*) FROM albums al{where}'
        albums, total, total_pages = _paginate(conn, count_sql, [], base + where, [], page, order)
        return render_template('browse.html', albums=albums, title=f"Formato: {fmt}",
                               filter_type='format', filter_value=fmt,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_)
    finally:
        conn.close()

# ── LED browse ─────────────────────────────────────────────────────────────────

@app.route('/led/<color>')
def browse_led(color):
    if color not in LED_LABELS:
        return "Unknown LED color", 404
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        # Feeds the "Filtrar" button's preloaded Búsqueda Avanzada modal
        # (same helper /busqueda-avanzada uses) — without this, the modal
        # only has Calidad/Popularidad/Energía/Bailabilidad to show, since
        # those don't depend on any data from the route.
        opts = _advanced_search_options(conn)
        count_sql = '''SELECT COUNT(DISTINCT al.id)
                       FROM albums al JOIN tracks t ON t.album_id=al.id
                       WHERE t.led_color=?'''
        data_sql  = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, ? as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       JOIN artists ar ON al.artist_id=ar.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       WHERE t.led_color=?'''
        order = _album_order(sort, dir_)
        albums, total, total_pages = _paginate(
            conn, count_sql, [color], data_sql, [color, color], page, order)
        # Enrich albums with dominant dsd_rate so badges show DSD64/DSD128 correctly
        if color in ('cyan', 'red'):
            for alb in albums:
                row = conn.execute(
                    """SELECT dsd_rate, sample_rate_real FROM tracks
                       WHERE album_id=? AND is_dsd=1
                       ORDER BY sample_rate_real DESC LIMIT 1""",
                    (alb['id'],)
                ).fetchone()
                if row:
                    alb['primary_format'] = _dsd_label(dict(row))
        return render_template('browse.html', albums=albums,
                               title=f"LED {color.capitalize()} — {LED_LABELS[color]}",
                               filter_type='led', filter_value=color,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()

# ── Genre browse ───────────────────────────────────────────────────────────────

@app.route('/genre/<path:genre>')
def browse_genre(genre):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        # Match by genre_primary in track_meta OR classic genre field in tracks
        count_sql = '''SELECT COUNT(DISTINCT al.id) FROM albums al
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       WHERE t.genre=? OR tm.genre_primary=?'''
        data_sql  = '''SELECT DISTINCT al.id, al.name, al.cover_path, al.primary_format, al.year,
                              al.track_count, al.total_duration, al.artist_id,
                              ar.name as artist_name, 'yellow' as album_led,
                              COALESCE(apc.pop_score, 0) as pop_score
                       FROM albums al
                       LEFT JOIN artists ar ON al.artist_id=ar.id
                       LEFT JOIN album_pop_cache apc ON apc.album_id=al.id
                       JOIN tracks t ON t.album_id=al.id
                       LEFT JOIN track_meta tm ON tm.track_id=t.id
                       WHERE t.genre=? OR tm.genre_primary=?'''
        order = _album_order(sort, dir_)
        albums, total, total_pages = _paginate(conn, count_sql, [genre, genre], data_sql, [genre, genre], page, order)
        return render_template('browse.html', albums=albums, title=f"Género: {genre}",
                               filter_type='genre', filter_value=genre,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()

# ── RichMetaPro browse ─────────────────────────────────────────────────────────

def _meta_browse(conn, field, value, page, title, filter_type, sort='popularidad', dir_='desc'):
    # Map filter field to album_meta predominant column where available
    _AM_COL = {
        'mood':    'mood_predominante',
        'idioma':  'idioma_principal',
        'momento': 'momento_predominante',
        'era':     'era',
    }
    am_col = _AM_COL.get(field)

    if am_col:
        # Album-level predominant value — accurate, fast, no DISTINCT needed
        count_sql = (
            "SELECT COUNT(al.id) FROM albums al "
            "JOIN album_meta am ON am.album_id=al.id "
            "WHERE am.{col}=?".format(col=am_col)
        )
        data_sql = (
            "SELECT al.id, al.name, al.cover_path, al.primary_format, al.year, "
            "al.track_count, al.total_duration, al.artist_id, "
            "ar.name as artist_name, "
            "(SELECT led_color FROM tracks WHERE album_id=al.id "
            " ORDER BY CASE led_color "
            "   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2 "
            "   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5 "
            "   ELSE 6 END LIMIT 1) as album_led, "
            "COALESCE(apc.pop_score, 0) as pop_score "
            "FROM albums al "
            "LEFT JOIN artists ar ON al.artist_id=ar.id "
            "LEFT JOIN album_pop_cache apc ON apc.album_id=al.id "
            "JOIN album_meta am ON am.album_id=al.id "
            "WHERE am.{col}=?".format(col=am_col)
        )
    else:
        # Fallback for tier/tema_lirico: majority of tracks must match (>50%)
        count_sql = (
            "SELECT COUNT(*) FROM ("
            "  SELECT al.id FROM albums al "
            "  JOIN tracks t ON t.album_id=al.id "
            "  JOIN track_meta tm ON tm.track_id=t.id "
            "  WHERE tm.{f}=? "
            "  GROUP BY al.id "
            "  HAVING COUNT(*)*2 > (SELECT COUNT(*) FROM tracks t2 WHERE t2.album_id=al.id)"
            ")".format(f=field)
        )
        data_sql = (
            "SELECT al.id, al.name, al.cover_path, al.primary_format, al.year, "
            "al.track_count, al.total_duration, al.artist_id, "
            "ar.name as artist_name, "
            "(SELECT led_color FROM tracks WHERE album_id=al.id "
            " ORDER BY CASE led_color "
            "   WHEN 'magenta' THEN 0 WHEN 'blue' THEN 1 WHEN 'green' THEN 2 "
            "   WHEN 'red' THEN 3 WHEN 'cyan' THEN 4 WHEN 'white' THEN 5 "
            "   ELSE 6 END LIMIT 1) as album_led, "
            "COALESCE(apc.pop_score, 0) as pop_score "
            "FROM albums al "
            "LEFT JOIN artists ar ON al.artist_id=ar.id "
            "LEFT JOIN album_pop_cache apc ON apc.album_id=al.id "
            "JOIN tracks t ON t.album_id=al.id "
            "JOIN track_meta tm ON tm.track_id=t.id "
            "WHERE tm.{f}=? "
            "GROUP BY al.id "
            "HAVING COUNT(*)*2 > (SELECT COUNT(*) FROM tracks t2 WHERE t2.album_id=al.id)".format(f=field)
        )
    order = _album_order(sort, dir_)
    return _paginate(conn, count_sql, [value], data_sql, [value], page, order)


@app.route('/mood/<path:mood>')
def browse_mood(mood):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'mood', mood, page, mood, 'mood', sort, dir_)
        display = MOOD_LABELS.get(mood, mood)
        return render_template('browse.html', albums=albums, title=f"Mood: {display}",
                               filter_type='mood', filter_value=mood,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/momento/<path:momento>')
def browse_momento(momento):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'momento', momento, page, momento, 'momento', sort, dir_)
        momento_labels = {
            'morning': 'Mañana ☀️', 'evening': 'Tarde 🌅', 'night': 'Noche 🌙',
            'sleep': 'Para dormir 😴', 'party': 'Fiesta 🎉', 'workout': 'Ejercicio 💪',
            'focus': 'Concentración 🎯', 'anytime': 'Cualquier momento 🎵',
        }
        label = momento_labels.get(momento, momento.capitalize())
        return render_template('browse.html', albums=albums, title=f"Momento: {label}",
                               filter_type='momento', filter_value=momento,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/era/<path:era>')
def browse_era(era):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'era', era, page, era, 'era', sort, dir_)
        era_labels = {
            'early_rock_era':         'Early Rock (50s–60s)',
            'british_invasion_era':   'British Invasion (60s)',
            'classic_rock_era':       'Classic Rock (70s)',
            'nwobhm_synth_era':       'NWOBHM / Synth (80s)',
            'grunge_alternative_era': 'Grunge / Alternative (90s)',
            'post_millennial_era':    'Post-millennial (2000s)',
            'streaming_era':          'Streaming Era (2010s)',
            'current_era':            'Actualidad (2020s+)',
        }
        label = era_labels.get(era, era.replace('_', ' ').title())
        return render_template('browse.html', albums=albums, title=f"Era: {label}",
                               filter_type='era', filter_value=era,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/tema/<path:tema>')
def browse_tema(tema):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'tema_lirico', tema, page, tema, 'tema', sort, dir_)
        return render_template('browse.html', albums=albums, title=f"Tema lírico: {tema.capitalize()}",
                               filter_type='tema', filter_value=tema,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()


@app.route('/tier/<path:tier>')
def browse_tier(tier):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        albums, total, total_pages = _meta_browse(conn, 'tier', tier, page, tier, 'tier', sort, dir_)
        tier_labels = {'silver': 'Silver ⭐⭐', 'bronze': 'Bronze ⭐', 'review': 'Por revisar 🔍'}
        label = tier_labels.get(tier, tier.capitalize())
        return render_template('browse.html', albums=albums, title=f"Tier: {label}",
                               filter_type='tier', filter_value=tier,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_)
    finally:
        conn.close()


@app.route('/language/<path:lang>')
def browse_language(lang):
    page = max(1, request.args.get('page', 1, type=int))
    sort = request.args.get('sort', 'popularidad')
    dir_ = request.args.get('dir',  'desc')
    conn = get_db_connection()
    try:
        opts = _advanced_search_options(conn)
        albums, total, total_pages = _meta_browse(conn, 'idioma', lang, page, lang, 'language', sort, dir_)
        lang_labels = {
            'en': 'Inglés 🇬🇧', 'es': 'Español 🇪🇸', 'de': 'Alemán 🇩🇪',
            'fr': 'Francés 🇫🇷', 'pt': 'Portugués 🇵🇹', 'it': 'Italiano 🇮🇹',
            'ja': 'Japonés 🇯🇵', 'ko': 'Coreano 🇰🇷', 'nl': 'Holandés 🇳🇱',
        }
        label = lang_labels.get(lang.lower(), lang.upper())
        return render_template('browse.html', albums=albums, title=f"Idioma: {label}",
                               filter_type='language', filter_value=lang,
                               page=page, total_pages=total_pages, total=total,
                               sort=sort, sort_dir=dir_, **opts)
    finally:
        conn.close()

# ── JSON API ──────────────────────────────────────────────────────────────────

@app.route('/api/track/<int:track_id>')
def api_track(track_id):
    conn = get_db_connection()
    try:
        t = conn.execute('''
            SELECT t.*, a.name as album_name, a.cover_path, a.year as album_year, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums a ON t.album_id=a.id
            LEFT JOIN artists ar ON a.artist_id=ar.id
            WHERE t.id=?
        ''', (track_id,)).fetchone()
        if not t: return jsonify({'error': 'not found'}), 404
        result = track_to_json(t)
        tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (track_id,)).fetchone()
        if tm:
            for k in tm.keys():
                if k != 'track_id':
                    result[f'meta_{k}'] = tm[k]
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/track/<int:track_id>/similar')
def api_track_similar(track_id):
    """Pistas similares a la indicada — alimenta el modal 'Similares' del
       Now Playing. Usa track_meta.similar_tracks_json (ya rankeado por score)
       y las serializa con track_to_json para que sean reproducibles/encolables
       directamente, igual que /api/album/<id>/tracks."""
    conn = get_db_connection()
    try:
        tm = conn.execute(
            'SELECT similar_tracks_json FROM track_meta WHERE track_id=?', (track_id,)
        ).fetchone()
        if not tm or not tm['similar_tracks_json']:
            return jsonify([])
        try:
            similar_raw = json.loads(tm['similar_tracks_json'])
        except Exception:
            return jsonify([])
        if not isinstance(similar_raw, list):
            return jsonify([])
        ids = [s.get('track_id') for s in similar_raw[:10] if isinstance(s, dict) and s.get('track_id')]
        if not ids:
            return jsonify([])
        placeholders = ','.join('?' * len(ids))
        rows = conn.execute(
            f'''SELECT t.*, a.name as album_name, a.cover_path, a.year as album_year,
                       ar.id as artist_id, ar.name as artist_name
                FROM tracks t
                LEFT JOIN albums a ON t.album_id=a.id
                LEFT JOIN artists ar ON a.artist_id=ar.id
                WHERE t.id IN ({placeholders})''',
            ids
        ).fetchall()
        by_id = {r['id']: r for r in rows}
        result = []
        for s in similar_raw[:10]:
            row = by_id.get(s.get('track_id'))
            if not row:
                continue
            d = track_to_json(row)
            d['same_artist'] = bool(s.get('same_artist'))
            d['sim_score']   = s.get('score')
            result.append(d)
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/track/<int:track_id>/similar-artists')
def api_track_similar_artists(track_id):
    """Artistas similares al artista de la pista en reproducción — alimenta
       el botón 'banda' del Now Playing. Resuelve track -> álbum -> artista y
       reusa build_similar_artists(), la misma lógica que la tab Similares
       de /artist/<id>."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            '''SELECT ar.similar_artists_json
               FROM tracks t
               JOIN albums al ON al.id = t.album_id
               JOIN artists ar ON ar.id = al.artist_id
               WHERE t.id = ?''', (track_id,)
        ).fetchone()
        if not row:
            return jsonify([])
        return jsonify(build_similar_artists(conn, row['similar_artists_json'], limit=12))
    finally:
        conn.close()


@app.route('/api/album/<int:album_id>/tracks')
def api_album_tracks(album_id):
    conn = get_db_connection()
    try:
        alb = conn.execute('SELECT name, cover_path, artist_id FROM albums WHERE id=?', (album_id,)).fetchone()
        album_cover      = clean_db_path(alb['cover_path']) if alb else None
        album_artist_id  = alb['artist_id'] if alb else None
        album_name       = alb['name'] if alb else None
        tracks = conn.execute(
            'SELECT * FROM tracks WHERE album_id=? ORDER BY disc_number, CAST(track_number AS INTEGER)',
            (album_id,)
        ).fetchall()
        result = []
        for t in tracks:
            d = dict(t)
            d['file_path']  = clean_db_path(d.get('file_path'))
            d['cover_path'] = album_cover
            d['cover_url']  = cover_url_filter(album_cover)
            d['audio_url']  = audio_url_filter(d['file_path'])
            fmt, led = _fmt_format(d)
            d['format_display'] = fmt
            d['format_color']   = led
            d['duration_fmt']   = _fmt_seconds(d.get('duration'))
            d['artist_id']      = album_artist_id   # needed by player for navigation
            d['album_name']     = album_name        # needed by player for display/navigation
            tm = conn.execute('SELECT * FROM track_meta WHERE track_id=?', (t['id'],)).fetchone()
            if tm:
                for k in tm.keys():
                    if k != 'track_id':
                        d[f'meta_{k}'] = tm[k]
            result.append(d)
        return jsonify(result)
    finally:
        conn.close()


@app.route('/api/tracks/resolve', methods=['POST'])
def api_tracks_resolve():
    """
    Resolve a list of file_path values (as recovered from an imported M3U
    playlist, which only carries title/artist/duration/path) back into full
    track records from the DB — same shape as /api/track/<id> and
    /api/album/<id>/tracks — so the player can restore cover, LED quality
    indicator, format badge, artist/album links and (via the real track id)
    lyrics, exactly as it does for tracks loaded from any other page.
    Paths not found in the library are simply omitted from the response;
    the caller keeps the minimal M3U-only object for those.
    """
    data  = request.get_json(silent=True) or {}
    paths = data.get('file_paths')
    if not isinstance(paths, list) or not paths:
        return jsonify([])

    # Defensive cap — this endpoint is meant for playlist-sized batches
    cleaned = [clean_db_path(p) for p in paths[:2000] if p]
    if not cleaned:
        return jsonify([])

    conn = get_db_connection()
    try:
        placeholders = ','.join('?' * len(cleaned))
        rows = conn.execute(f'''
            SELECT t.*, al.name as album_name, al.cover_path, al.year as album_year,
                   al.artist_id, ar.name as artist_name
            FROM tracks t
            LEFT JOIN albums al ON t.album_id=al.id
            LEFT JOIN artists ar ON al.artist_id=ar.id
            WHERE t.file_path IN ({placeholders})
        ''', cleaned).fetchall()

        by_path = {}
        for r in rows:
            d = track_to_json(r)
            by_path[d['file_path']] = d
        # Return in the same order as the request, one entry per matched path
        result = [by_path[p] for p in cleaned if p in by_path]
        return jsonify(result)
    finally:
        conn.close()


def _read_embedded_lyrics(file_path):
    """
    Read SYNCEDLYRICS and LYRICS from any audio file.
    Uses manual case-insensitive key iteration — mutagen VComment.get() is NOT
    reliably case-insensitive across all versions and formats.
    Returns (synced_lrc: str|None, plain_lyrics: str|None).
    """
    import sys
    if not file_path or not os.path.isfile(file_path):
        return None, None
    try:
        from mutagen import File as MFile
        audio = MFile(file_path, easy=False)
        if audio is None:
            print(f"[lyrics] mutagen could not open: {file_path}", file=sys.stderr, flush=True)
            return None, None

        tags = audio.tags
        if tags is None:
            print(f"[lyrics] no tags block in file: {file_path}", file=sys.stderr, flush=True)
            return None, None

        # Dump ALL tag keys for diagnostics
        try:
            all_keys = list(tags.keys())
            print(f"[lyrics] tags in file: {all_keys}", file=sys.stderr, flush=True)
        except Exception as ke:
            print(f"[lyrics] could not list tag keys: {ke}", file=sys.stderr, flush=True)
            all_keys = []

        synced_tag = None
        plain_tag  = None

        # Manual case-insensitive iteration — works for VComment, ID3, MP4, etc.
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ('.flac', '.ogg', '.opus', '.oga'):
            # VorbisComment: keys() returns original-case strings
            # values accessed via tags[key] → list of strings
            for key in tags.keys():
                k = key.lower().strip()
                if k == 'syncedlyrics' and synced_tag is None:
                    vals = tags[key]
                    synced_tag = vals[0] if isinstance(vals, list) else str(vals)
                elif k == 'lyrics' and plain_tag is None:
                    vals = tags[key]
                    plain_tag = vals[0] if isinstance(vals, list) else str(vals)

        elif ext in ('.mp3', '.dsf'):
            # ID3: SYLT = Synchronised Lyrics, USLT = Unsynchronised Lyrics.
            # DSF (DSD Stream File) embeds the SAME ID3 block as MP3 — mutagen
            # just opens it via a different container class (DSF vs MP3), but
            # the tag keys (USLT/SYLT) are identical. DFF (DSDIFF) is excluded:
            # it has no editable tag standard and never reaches this function.
            for key in tags.keys():
                if key.startswith('USLT') and plain_tag is None:
                    v = tags[key]
                    plain_tag = getattr(v, 'text', str(v))
                elif key.startswith('SYLT') and synced_tag is None:
                    v = tags[key]
                    pairs = getattr(v, 'text', None)
                    if pairs:
                        lines = []
                        for lyric_text, ms in pairs:
                            m  = ms // 60000
                            s  = (ms % 60000) // 1000
                            cs = (ms % 1000) // 10
                            lines.append(f"[{m:02}:{s:02}.{cs:02}] {lyric_text}")
                        synced_tag = "\n".join(lines)

        elif ext in ('.m4a', '.aac', '.mp4', '.alac'):
            # MP4 atoms: ©lyr = lyrics
            lyr = tags.get('\xa9lyr')
            if lyr:
                plain_tag = lyr[0] if isinstance(lyr, list) else str(lyr)

        else:
            # Generic fallback for WAV, AIFF, DFF etc. (Vorbis-style comments,
            # used by formats that don't have a dedicated branch above)
            for key in tags.keys():
                k = key.lower().strip()
                if k == 'syncedlyrics' and synced_tag is None:
                    vals = tags[key]
                    synced_tag = vals[0] if isinstance(vals, list) else str(vals)
                elif k == 'lyrics' and plain_tag is None:
                    vals = tags[key]
                    plain_tag = vals[0] if isinstance(vals, list) else str(vals)

        if synced_tag:
            print(f"[lyrics] SYNCEDLYRICS found ({len(synced_tag)} chars)", file=sys.stderr, flush=True)
        elif plain_tag:
            print(f"[lyrics] LYRICS found ({len(plain_tag)} chars)", file=sys.stderr, flush=True)
        else:
            print(f"[lyrics] neither SYNCEDLYRICS nor LYRICS found among keys: {all_keys}", file=sys.stderr, flush=True)

        return synced_tag, plain_tag

    except ImportError:
        import sys
        print("[lyrics] mutagen not installed — pip install mutagen", file=sys.stderr, flush=True)
    except Exception as e:
        import sys
        print(f"[lyrics] mutagen error ({file_path}): {e}", file=sys.stderr, flush=True)
    return None, None


def _get_file_duration(file_path):
    """
    Read actual playback duration from audio stream via mutagen.
    More reliable than DB metadata for lrclib duration matching.
    """
    if not file_path or not os.path.isfile(file_path):
        return None
    try:
        from mutagen import File
        audio = File(file_path)
        if audio and hasattr(audio, 'info') and hasattr(audio.info, 'length'):
            return float(audio.info.length)
    except Exception:
        pass
    return None


@app.route('/api/lyrics')
def api_lyrics():
    artist   = request.args.get('artist',   '').strip()
    title    = request.args.get('title',    '').strip()
    track_id = request.args.get('track_id', None, type=int)
    version  = request.args.get('version',  None)

    import sys

    def _log(msg):
        print(f"[lyrics] {msg}", file=sys.stderr, flush=True)

    def _empty(msg=''):
        return jsonify({'has_lyrics': False, 'has_synced': False,
                        'lyrics': '', 'synced': '', 'alternatives': [], 'error': msg})

    def _ok_file(synced, plain, source):
        return jsonify({
            'has_lyrics': bool(synced or plain),
            'has_synced': bool(synced),
            'lyrics':     plain  or '',
            'synced':     synced or '',
            'alternatives': [],
            'source': source,
        })

    def _fmt_lrc(data):
        return {
            'id':         data.get('id'),
            'title':      data.get('trackName',    title),
            'artist':     data.get('artistName',   artist),
            'album':      data.get('albumName',    ''),
            'duration':   data.get('duration',     0),
            'lyrics':     data.get('plainLyrics',  '') or '',
            'synced':     data.get('syncedLyrics', '') or '',
            'has_lyrics': bool(data.get('plainLyrics')),
            'has_synced': bool(data.get('syncedLyrics')),
        }

    # ── Resolve file_path and album early (used across multiple steps) ─────────
    file_path  = None
    album_name = ''
    file_dur   = None

    if track_id:
        try:
            conn = get_db_connection()
            row = conn.execute(
                '''SELECT t.file_path, t.duration, a.name as album_name
                   FROM tracks t LEFT JOIN albums a ON t.album_id=a.id
                   WHERE t.id=?''',
                (track_id,)
            ).fetchone()
            conn.close()
            if row:
                file_path  = clean_db_path(row['file_path'])
                album_name = row['album_name'] or ''
                file_dur   = row['duration']
        except Exception as e:
            _log(f"DB resolve error: {e}")

    # ── 1. Embedded file tags (SYNCEDLYRICS / LYRICS Vorbis comment) ──────────
    if file_path:
        synced_tag, plain_tag = _read_embedded_lyrics(file_path)
        if synced_tag:
            _log(f"SYNCEDLYRICS found in file tags — track_id={track_id}")
            return _ok_file(synced_tag, plain_tag, 'embedded_tag')
        elif plain_tag:
            _log(f"LYRICS found in file tags — track_id={track_id}")
            return _ok_file(None, plain_tag, 'embedded_tag')
        else:
            _log(f"No embedded lyrics in file — track_id={track_id}, path={file_path}")

    # ── 2. Get actual file duration from audio stream (more reliable) ──────────
    if file_path and file_dur is None:
        file_dur = _get_file_duration(file_path)
    elif file_path and file_dur:
        actual = _get_file_duration(file_path)
        if actual: file_dur = actual   # prefer stream duration

    # ── 3. lrclib.net direct fetch by stored lrclib_id ────────────────────────
    if track_id and req_lib:
        try:
            conn = get_db_connection()
            tm = conn.execute(
                'SELECT lrclib_id, has_lyrics, has_synced_lrc FROM track_meta WHERE track_id=?',
                (track_id,)
            ).fetchone()
            conn.close()
            if tm and tm['lrclib_id']:
                _log(f"lrclib direct fetch — id={tm['lrclib_id']}")
                r = req_lib.get(f"https://lrclib.net/api/get/{tm['lrclib_id']}", timeout=6)
                if r.status_code == 200:
                    data = r.json()
                    if data.get('plainLyrics') or data.get('syncedLyrics'):
                        return jsonify({**_fmt_lrc(data), 'alternatives': [], 'source': 'lrclib_id'})
            else:
                _log(f"No lrclib_id in track_meta — track_id={track_id}")
        except Exception as e:
            _log(f"lrclib direct fetch error: {e}")

    if not artist or not title:
        return _empty('artist and title required')
    if not req_lib:
        return _empty('requests library not available')

    # ── 4. lrclib.net version override ────────────────────────────────────────
    try:
        from urllib.parse import quote

        if version:
            r = req_lib.get(f"https://lrclib.net/api/get/{version}", timeout=6)
            if r.status_code == 200:
                return jsonify({**_fmt_lrc(r.json()), 'alternatives': []})

        # ── 5. lrclib.net search — scored by duration + synced + name match ───
        # Strategy: try album+artist+title first, then artist+title, then title alone
        def _search(q):
            try:
                r = req_lib.get(
                    f"https://lrclib.net/api/search?q={quote(q)}",
                    timeout=6
                )
                return r.json() if r.status_code == 200 and isinstance(r.json(), list) else []
            except Exception:
                return []

        def _score(item):
            pts = 0
            # Synced lyrics are strongly preferred
            if item.get('syncedLyrics'): pts += 20
            # Duration match (using actual file duration)
            if file_dur and item.get('duration'):
                diff = abs(float(item['duration']) - file_dur)
                if diff < 2:   pts += 15
                elif diff < 5: pts += 8
                elif diff < 15: pts += 2
            # Title match
            if title.lower() in (item.get('trackName', '') or '').lower(): pts += 6
            # Artist match
            if artist.lower() in (item.get('artistName', '') or '').lower(): pts += 5
            # Album match
            if album_name and album_name.lower() in (item.get('albumName', '') or '').lower(): pts += 4
            return pts

        candidates = []

        # Search 1: album + artist + title (most specific)
        if album_name:
            q1 = f"{artist} {album_name} {title}"
            candidates = _search(q1)
            _log(f"lrclib search 1 '{q1[:60]}': {len(candidates)} results")

        # Search 2: artist + title (fallback)
        if not any(_score(c) >= 25 for c in candidates):
            q2 = f"{artist} {title}"
            more = _search(q2)
            _log(f"lrclib search 2 '{q2[:60]}': {len(more)} results")
            # Merge without duplicates
            seen_ids = {c.get('id') for c in candidates}
            candidates += [m for m in more if m.get('id') not in seen_ids]

        if candidates:
            candidates.sort(key=_score, reverse=True)
            best = candidates[0]
            top_score = _score(best)
            _log(f"lrclib best: '{best.get('trackName')}' score={top_score} synced={bool(best.get('syncedLyrics'))}")
            alts = [
                {'id': x.get('id'), 'title': x.get('trackName', ''),
                 'artist': x.get('artistName', ''), 'album': x.get('albumName', ''),
                 'has_synced': bool(x.get('syncedLyrics')), 'duration': x.get('duration', 0)}
                for x in candidates[:8]
            ]
            return jsonify({**_fmt_lrc(best), 'alternatives': alts, 'source': 'lrclib_search'})

        _log(f"No lyrics found for artist='{artist}' title='{title}'")
        return _empty()

    except Exception as e:
        _log(f"Unexpected error: {e}")
        return _empty(str(e))


@app.route('/api/meta/tracks')
def api_meta_tracks():
    field      = request.args.get('field',      '').strip()
    value      = request.args.get('value',      '').strip()
    page       = max(1, request.args.get('page', 1, type=int))
    sort       = request.args.get('sort',       'popularidad')
    dir_       = request.args.get('dir',        'desc')
    intercalar = request.args.get('intercalar', '0') == '1'

    ALLOWED_FIELDS = {'mood', 'momento', 'era', 'tema_lirico', 'tier', 'idioma', 'genre'}
    if field not in ALLOWED_FIELDS or not value:
        return jsonify({'error': 'invalid field or value'}), 400

    conn = get_db_connection()
    try:
        # This endpoint lists INDIVIDUAL tracks (the Pistas tab) — always
        # match each track's own tag. album_meta's predominant-field logic
        # belongs to _meta_browse() (the Álbumes tab) only; applying it here
        # too was pulling in every track from a mostly-matching album even
        # when that specific track didn't carry the tag itself (e.g. a
        # French-tagged track on an English-dominant album wouldn't count
        # here, while home.html's per-track total — and the "cualquiera de
        # estos" Búsqueda Avanzada Pistas view — both already counted it).
        #
        # Colapsa duplicados (misma canción en distintos álbumes) — ver
        # _track_dedupe_condition. Le pasamos el filtro que esta vista YA
        # aplica (base_where) para que la comparación de duplicados quede
        # acotada a "otras copias que también cumplirían este mismo
        # filtro" — así, por ejemplo, filtrar por tier no hace desaparecer
        # una canción solo porque existe una copia de otro tier en otro
        # álbum. count y rows deben compartir exactamente la misma
        # condición (mismo base_where + mismo dedupe) o la paginación
        # queda desalineada con el total.
        if field == 'genre':
            base_where  = '(t.genre=? OR tm.genre_primary=?)'
            base_params = (value, value)
        else:
            base_where  = f'tm.{field}=?'
            base_params = (value,)

        dedupe = _track_dedupe_condition(extra_where=base_where, track_alias='t', pop_alias='tpc')
        # dedupe reincorpora una copia (re-prefijada dup_*) de base_where —
        # sus '?' necesitan su valor otra vez, en el mismo orden.
        full_params = base_params + base_params

        if field == 'genre':
            # Same match as browse_genre: classic tracks.genre OR track_meta.genre_primary
            count = conn.execute(f'''
                SELECT COUNT(*) FROM tracks t
                LEFT JOIN track_meta tm ON tm.track_id=t.id
                LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                WHERE {base_where} AND {dedupe}
            ''', full_params).fetchone()[0]
        else:
            count = conn.execute(
                f'''SELECT COUNT(*) FROM track_meta tm
                    JOIN tracks t ON t.id=tm.track_id
                    LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
                    WHERE {base_where} AND {dedupe}''', full_params
            ).fetchone()[0]

        # For intercalar mode: fetch a larger batch (up to 500) with random ordering
        # client will do round-robin by artist
        if intercalar:
            limit  = min(500, count)
            offset = 0
            order  = 'RANDOM()'
        else:
            limit  = PAGE_SIZE
            offset = (page - 1) * PAGE_SIZE
            order  = _track_order(sort, dir_)

        # Build the WHERE clause — per-track always (see comment above)
        where_clause = f'{base_where} AND {dedupe}'
        extra_join   = ''
        where_params = full_params

        rows = conn.execute(f'''
            SELECT t.id, t.title, t.artist, t.led_color, t.is_dsd, t.is_mqa, t.codec,
                   t.duration, t.track_number, t.file_path,
                   a.id as album_id, a.name as album_name, a.cover_path, a.year as album_year,
                   ar.name as artist_name,
                   tm.mood, tm.momento, tm.tier, tm.bpm, tm.tonalidad,
                   tm.energy, tm.bailabilidad, tm.lrclib_id, tm.has_lyrics, tm.has_synced_lrc,
                   tm.tema_lirico, tm.idioma, tm.genre_primary,
                   COALESCE(tpc.pop_score, 0) as pop_score
            FROM tracks t
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            LEFT JOIN albums a ON a.id=t.album_id
            LEFT JOIN artists ar ON ar.id=a.artist_id
            LEFT JOIN track_pop_cache tpc ON tpc.track_id=t.id
            {extra_join}
            WHERE {where_clause}
            ORDER BY {order}
            LIMIT ? OFFSET ?
        ''', (*where_params, limit, offset)).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d['cover_path'] = clean_db_path(d.get('cover_path'))
            d['cover_url']  = cover_url_filter(d['cover_path'])
            d['file_path']  = clean_db_path(d.get('file_path'))
            d['audio_url']  = audio_url_filter(d['file_path'])
            fmt, _ = _fmt_format(d)
            d['format_display'] = fmt
            d['duration_fmt']   = _fmt_seconds(d.get('duration'))
            result.append(d)

        total_pages = 1 if intercalar else max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        return jsonify({
            'tracks':      result,
            'total':       count,
            'page':        page,
            'total_pages': total_pages,
            'intercalar':  intercalar,
        })
    finally:
        conn.close()


@app.route('/api/debug/tags/<int:track_id>')
def api_debug_tags(track_id):
    """Return all mutagen tags for a track — for diagnosing embedded lyrics."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT file_path, title, artist FROM tracks WHERE id=?', (track_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'track not found'}), 404

    file_path = clean_db_path(row['file_path'])
    result = {
        'track_id':  track_id,
        'title':     row['title'],
        'artist':    row['artist'],
        'file_path': file_path,
        'file_exists': os.path.isfile(file_path),
        'tags': {},
        'has_syncedlyrics': False,
        'has_lyrics': False,
        'syncedlyrics_len': 0,
        'error': None,
    }
    if not result['file_exists']:
        result['error'] = 'File not found on disk'
        return jsonify(result)
    try:
        from mutagen import File as MFile
        audio = MFile(file_path, easy=False)
        if audio is None:
            result['error'] = 'mutagen could not open file'
            return jsonify(result)
        if audio.tags is None:
            result['error'] = 'No tags block in file'
            return jsonify(result)
        # Collect all tags (truncate large values for display)
        for key in audio.tags.keys():
            try:
                vals = audio.tags[key]
                v = vals[0] if isinstance(vals, list) else str(vals)
                k_lower = key.lower().strip()
                # Vorbis-style (FLAC/OGG): 'syncedlyrics' / 'lyrics'
                # ID3-style (MP3/DSF):    'SYLT...'      / 'USLT...'
                is_synced_key = (k_lower == 'syncedlyrics') or key.startswith('SYLT')
                is_plain_key  = (k_lower == 'lyrics')       or key.startswith('USLT')
                if is_synced_key or is_plain_key:
                    result['tags'][key] = f"[{len(str(v))} chars — first 200: {str(v)[:200]}]"
                else:
                    result['tags'][key] = str(v)[:300]
                if is_synced_key:
                    result['has_syncedlyrics'] = True
                    result['syncedlyrics_len'] = len(str(v))
                if is_plain_key:
                    result['has_lyrics'] = True
            except Exception as ke:
                result['tags'][key] = f'[error reading: {ke}]'
    except ImportError:
        result['error'] = 'mutagen not installed'
    except Exception as e:
        result['error'] = str(e)
    return jsonify(result)


def api_health():
    root_ok  = os.path.isdir(MUSIC_ROOT)
    root_contents = []
    if root_ok:
        try: root_contents = sorted(os.listdir(MUSIC_ROOT))[:10]
        except: pass
    db_ok = False
    track_count = 0
    try:
        c = get_db_connection()
        track_count = c.execute('SELECT COUNT(*) FROM tracks').fetchone()[0]
        c.close()
        db_ok = True
    except: pass
    return jsonify({
        'music_root':     MUSIC_ROOT,
        'music_root_ok':  root_ok,
        'music_root_top': root_contents,
        'db_ok':          db_ok,
        'track_count':    track_count,
    })

@app.route('/api/outputs')
def api_outputs():
    try:
        result = subprocess.run(['mpc', 'outputs'], capture_output=True, text=True, timeout=3)
        outputs = []
        for line in result.stdout.strip().split('\n'):
            if 'Output' in line:
                parts = line.split()
                enabled = 'enabled' in line
                name = ' '.join(parts[1:-2]) if len(parts) > 3 else (parts[1] if len(parts) > 1 else 'Output')
                try: idx = int(parts[0].replace('Output','').strip())
                except: idx = 1
                outputs.append({'id': idx, 'name': name, 'enabled': enabled})
        return jsonify(outputs)
    except Exception:
        return jsonify([{'id': 1, 'name': 'Default Output', 'enabled': True}])


@app.route('/api/output/toggle', methods=['POST'])
def api_output_toggle():
    data = request.get_json() or {}
    try:
        cmd = 'enable' if data.get('enable', True) else 'disable'
        result = subprocess.run(
            ['mpc', 'output', cmd, str(data.get('id', 1))],
            capture_output=True, timeout=3
        )
        if result.returncode == 0:
            return jsonify({'status': 'ok'})
        err = result.stderr.decode(errors='replace').strip()
        return jsonify({'status': 'error', 'message': err or 'mpc failed'}), 200
    except FileNotFoundError:
        return jsonify({'status': 'error', 'message': 'mpc not installed'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 200

# ── Files / MPD ───────────────────────────────────────────────────────────────

@app.route('/audio/<path:filepath>')
def audio_file(filepath):
    absolute_path = os.path.join(MUSIC_ROOT, filepath.lstrip('/'))

    # Try exact path
    if os.path.isfile(absolute_path):
        return _serve_audio(absolute_path)

    # Try with different audio extensions (flac → dsf, wav, etc.)
    base, ext = os.path.splitext(absolute_path)
    for alt_ext in ('.flac', '.dsf', '.dff', '.wav', '.aiff', '.mp3', '.m4a'):
        if alt_ext != ext.lower():
            alt = base + alt_ext
            if os.path.isfile(alt):
                return _serve_audio(alt)

    # Log for diagnostics
    app.logger.warning(f"Audio 404: {absolute_path}")
    return "File not found", 404


def _serve_audio(absolute_path):
    file_size = os.path.getsize(absolute_path)
    rh = request.headers.get('Range')
    if rh:
        start, end = parse_range_header(rh, file_size)
        end = end if end is not None else file_size - 1
        length = end - start + 1
        with open(absolute_path, 'rb') as f:
            f.seek(start)
            data = f.read(length)
        mime, _ = mimetypes.guess_type(absolute_path)
        resp = Response(data, status=206, mimetype=mime or 'application/octet-stream')
        resp.headers['Content-Range']  = f'bytes {start}-{end}/{file_size}'
        resp.headers['Accept-Ranges']  = 'bytes'
        resp.headers['Content-Length'] = length
        return resp
    return send_file(absolute_path, conditional=True)

def parse_range_header(rh, file_size):
    if not rh.startswith('bytes='): return (0, None)
    parts = rh[6:].split('-')
    start = int(parts[0])
    end   = int(parts[1]) if len(parts) == 2 and parts[1] else file_size - 1
    return (start, end)

COVER_NAMES = [
    'Cover.jpg','cover.jpg','folder.jpg','Folder.jpg',
    'Front.jpg','front.jpg','AlbumArt.jpg','albumart.jpg',
    'Artwork.jpg','artwork.jpg','Art.jpg','art.jpg',
    'Cover.png','cover.png','Cover.webp','cover.webp',
    'thumb.jpg','Thumb.jpg','back.jpg','Back.jpg',
]

@app.route('/cover/<path:filepath>')
def cover_file(filepath):
    # Try exact path first
    absolute_path = os.path.join(MUSIC_ROOT, filepath.lstrip('/'))
    if os.path.isfile(absolute_path):
        mime, _ = mimetypes.guess_type(absolute_path)
        return send_file(absolute_path, mimetype=mime or 'image/jpeg', max_age=86400)

    # Fallback: try common cover filenames in same directory
    directory = os.path.dirname(absolute_path)
    if os.path.isdir(directory):
        for name in COVER_NAMES:
            alt = os.path.join(directory, name)
            if os.path.isfile(alt):
                mime, _ = mimetypes.guess_type(alt)
                return send_file(alt, mimetype=mime or 'image/jpeg', max_age=86400)
        # Last resort: first image file found in directory
        try:
            for fname in sorted(os.listdir(directory)):
                if fname.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
                    alt = os.path.join(directory, fname)
                    if os.path.isfile(alt):
                        mime, _ = mimetypes.guess_type(alt)
                        return send_file(alt, mimetype=mime or 'image/jpeg', max_age=86400)
        except PermissionError:
            pass

    # Try parent directory (multi-disc albums store cover one level up)
    parent = os.path.dirname(directory)
    if parent != directory and os.path.isdir(parent):
        for name in COVER_NAMES:
            alt = os.path.join(parent, name)
            if os.path.isfile(alt):
                mime, _ = mimetypes.guess_type(alt)
                return send_file(alt, mimetype=mime or 'image/jpeg', max_age=86400)

    return "Cover not found", 404

# ── MPD helpers ───────────────────────────────────────────────────────────────

MPD_HOST = os.environ.get('MPD_HOST', 'localhost')
MPD_PORT = int(os.environ.get('MPD_PORT', 6600))
MPD_PASSWORD = os.environ.get('MPD_PASSWORD', None)

def _mpd_connect():
    """Return a connected MPDClient or raise ConnectionRefusedError."""
    client = _MPDClient()
    client.timeout = 5
    client.connect(MPD_HOST, MPD_PORT)
    if MPD_PASSWORD:
        client.password(MPD_PASSWORD)
    return client

def _to_mpd_relative(filepath):
    """Strip MUSIC_ROOT prefix to get the path relative to MPD's music_directory."""
    p = clean_db_path(filepath or '').strip()
    if p.startswith(MUSIC_ROOT):
        return p[len(MUSIC_ROOT):].lstrip('/')
    p = p.lstrip('/')
    for prefix in ('mnt/musica/', 'mnt/musica'):
        if p.startswith(prefix):
            return p[len(prefix):].lstrip('/')
    return p

@app.route('/play-mpd', methods=['POST'])
@app.route('/play-dsd', methods=['POST'])
def play_mpd():
    """
    Send a track to MPD for native hardware DSD playback.
    Uses python-mpd2 (socket protocol) for reliability.
    Falls back to mpc CLI if python-mpd2 is unavailable.
    Returns JSON so the client can decide whether to show/hide the MPD badge.
    """
    data     = request.get_json() or {}
    filepath = clean_db_path(data.get('path', ''))
    if not filepath:
        return jsonify({'status': 'error', 'message': 'No path provided'}), 400

    relative = _to_mpd_relative(filepath)
    app.logger.debug(f"[MPD] relative path: {relative!r}")

    # ── Strategy 1: python-mpd2 via socket ────────────────────────────────────
    if _MPD_AVAILABLE:
        try:
            client = _mpd_connect()
            try:
                client.clear()
                client.add(relative)
                client.play(0)
                status = client.status()
                return jsonify({
                    'status':  'ok',
                    'message': 'Playing via MPD (native)',
                    'path':    relative,
                    'mpd_state': status.get('state'),
                })
            except Exception as e:
                err = str(e)
                app.logger.warning(f"[MPD] add failed for {relative!r}: {err}")
                # Try to update the library then retry once
                try:
                    client.update(relative)
                    client.clear()
                    client.add(relative)
                    client.play(0)
                    return jsonify({'status': 'ok', 'message': 'Playing via MPD (after update)', 'path': relative})
                except Exception as e2:
                    return jsonify({'status': 'error', 'message': str(e2), 'path': relative}), 200
            finally:
                try: client.disconnect()
                except: pass
        except ConnectionRefusedError:
            return jsonify({'status': 'error', 'message': 'MPD not running', 'path': relative}), 200
        except Exception as e:
            return jsonify({'status': 'error', 'message': str(e), 'path': relative}), 200

    # ── Strategy 2: mpc CLI fallback ──────────────────────────────────────────
    try:
        subprocess.run(['mpc', 'clear'], capture_output=True, text=True, timeout=5)
        r_add = subprocess.run(['mpc', 'add', relative], capture_output=True, text=True, timeout=5)
        if r_add.returncode != 0 or 'error' in r_add.stderr.lower():
            subprocess.run(['mpc', 'update', '--wait'], capture_output=True, timeout=30)
            subprocess.run(['mpc', 'clear'], capture_output=True, timeout=5)
            subprocess.run(['mpc', 'add', relative], capture_output=True, text=True, check=True, timeout=5)
        subprocess.run(['mpc', 'play'], capture_output=True, check=True, timeout=5)
        return jsonify({'status': 'ok', 'message': 'Playing via MPD (mpc)', 'path': relative})
    except FileNotFoundError:
        return jsonify({'status': 'error', 'message': 'MPD not available', 'path': relative}), 200
    except subprocess.CalledProcessError as e:
        return jsonify({'status': 'error', 'message': getattr(e, 'stderr', str(e)), 'path': relative}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e), 'path': relative}), 200

# ── DSD transcode cache ───────────────────────────────────────────────────────
# The previous version of this endpoint computed an exact Content-Length for
# a synthetic WAV and padded it with silence if ffmpeg came up short — that
# solved "browser doesn't know the length" but NOT "browser can't
# resume/seek", because the response still declared Accept-Ranges: none. To
# iOS's background media pipeline that's still indistinguishable from a
# live/infinite stream: it periodically aborts and reconnects the request
# (visible in server logs as repeated restarts from start=0 every few
# seconds), and each reconnect threw away all progress on the track. The
# player's retry budget ran out long before a multi-minute track could get
# through like that, so playback on a DSD track reached mid-playlist would
# just stop.
#
# Regular (non-DSD) files never had this problem because they're already
# served through _serve_audio() with real Content-Length + byte-range
# support — exactly like every other progressively downloadable audio file
# iOS expects.
#
# Fix: transcode the whole track to a cached .flac file on disk first, then
# serve that file through the same _serve_audio() path used for everything
# else. First playback of a given DSD track pays the one-time transcode cost
# (ffmpeg at -compression_level 0 is much faster than realtime); every
# repeat request — including the reconnect-from-lastPos calls the player
# already makes on a network hiccup, and the watchdog's stall recovery —
# hits the cache and is served instantly.
_DSD_CACHE_DIR      = os.path.join(tempfile.gettempdir(), 'orbyte_dsd_cache')
_DSD_CACHE_MAX_AGE  = 4 * 3600   # seconds — long enough for a listening session

def _dsd_cache_path(absolute_path):
    """Deterministic cache filename for a DSD source: hash of path + mtime."""
    os.makedirs(_DSD_CACHE_DIR, exist_ok=True)
    try:
        mtime = os.path.getmtime(absolute_path)
    except OSError:
        mtime = 0
    key = hashlib.sha1(f'{absolute_path}:{mtime}'.encode('utf-8')).hexdigest()
    return os.path.join(_DSD_CACHE_DIR, f'{key}.flac')

def _dsd_cache_cleanup():
    """Best-effort removal of stale cache entries. Cheap enough to run per-request
    at personal-library scale; swap for a cron job if the library gets huge."""
    try:
        now = time.time()
        for name in os.listdir(_DSD_CACHE_DIR):
            fp = os.path.join(_DSD_CACHE_DIR, name)
            try:
                if now - os.path.getmtime(fp) > _DSD_CACHE_MAX_AGE:
                    os.remove(fp)
            except OSError:
                pass
    except FileNotFoundError:
        pass

@app.route('/stream-dsd/<path:filepath>')
def stream_dsd(filepath):
    """
    Transcode a DSD file (DSF/DFF) to FLAC and serve it exactly like a normal
    audio file (Content-Length + Accept-Ranges: bytes via _serve_audio), so
    iOS Safari can buffer/seek/resume it in the background the same way it
    already does for every non-DSD track. See _DSD_CACHE_DIR comment above.
    """
    absolute_path = os.path.join(MUSIC_ROOT, filepath.lstrip('/'))
    if not os.path.isfile(absolute_path):
        app.logger.warning(f"stream-dsd 404: {absolute_path}")
        return "File not found", 404

    _dsd_cache_cleanup()
    cache_path = _dsd_cache_path(absolute_path)
    cache_hit  = os.path.isfile(cache_path)

    if not cache_hit:
        tmp_path = f'{cache_path}.{os.getpid()}.tmp'
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-i', absolute_path,
            '-vn',                        # drop embedded cover art (DSF stores album art)
            '-ar', '176400',              # 176.4 kHz — universal browser FLAC support
            '-sample_fmt', 's32',         # 32-bit integer, full DSD dynamic range
            '-c:a', 'flac',
            '-compression_level', '0',    # fastest encode
            '-f', 'flac',                 # explicit — tmp_path ends in .tmp, not .flac,
                                           # so ffmpeg can't infer the muxer from the extension
            tmp_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            if result.returncode != 0 or not os.path.isfile(tmp_path):
                app.logger.error(
                    f"[stream-dsd] ffmpeg failed for {absolute_path}: "
                    f"{result.stderr.decode(errors='replace')[:500]}"
                )
                return "Transcode failed", 500
            os.replace(tmp_path, cache_path)
        finally:
            if os.path.isfile(tmp_path):
                try: os.remove(tmp_path)
                except OSError: pass

    resp = _serve_audio(cache_path)
    resp.headers['Content-Type']  = 'audio/flac'
    resp.headers['X-DSD-Source']  = os.path.basename(absolute_path)
    resp.headers['X-DSD-Rate']    = '176400'
    resp.headers['X-DSD-Cache']   = 'hit' if cache_hit else 'miss'
    return resp


@app.route('/api/favorites', methods=['GET'])
def api_favorites_list():
    """Return all favorited tracks with full metadata."""
    try:
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT t.id, t.title, t.artist, t.duration, t.led_color, t.file_path,
                   t.codec, t.is_dsd, t.dsd_rate, t.is_mqa, t.sample_rate_real,
                   al.name as album_name, al.cover_path,
                   tm.tier, f.added_at
            FROM favorites f
            JOIN tracks t ON t.id=f.track_id
            JOIN albums al ON al.id=t.album_id
            LEFT JOIN track_meta tm ON tm.track_id=t.id
            ORDER BY f.added_at DESC
        ''').fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/favorites/toggle', methods=['POST'])
def api_favorites_toggle():
    """Add or remove a track from favorites."""
    global _favorites_set
    data = request.get_json() or {}
    tid  = data.get('track_id')
    if not tid:
        return jsonify({'error': 'track_id required'}), 400
    try:
        conn = get_db_connection()
        if tid in _favorites_set:
            conn.execute('DELETE FROM favorites WHERE track_id=?', (tid,))
            _favorites_set.discard(tid)
            action = 'removed'
        else:
            conn.execute('INSERT OR IGNORE INTO favorites (track_id) VALUES (?)', (tid,))
            _favorites_set.add(tid)
            action = 'added'
        conn.commit()
        conn.close()
        return jsonify({'action': action, 'track_id': tid, 'total': len(_favorites_set)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/favorites/rebuild-cache', methods=['POST'])
def api_rebuild_pop_cache():
    """Rebuild popularity cache — call after bulk metadata updates."""
    try:
        conn = get_db_connection()
        conn.execute('DELETE FROM album_pop_cache')
        conn.execute('''INSERT INTO album_pop_cache (album_id, pop_score)
            SELECT al.id,
              COALESCE((SELECT CASE MAX(
                CASE led_color WHEN "red" THEN 700 WHEN "cyan" THEN 680 WHEN "white" THEN 600
                  WHEN "blue" THEN 500 WHEN "green" THEN 440 WHEN "magenta" THEN 400 ELSE 200 END)
                WHEN 700 THEN 40 WHEN 680 THEN 38 WHEN 600 THEN 30 WHEN 500 THEN 25
                WHEN 440 THEN 22 WHEN 400 THEN 20 ELSE 10 END FROM tracks WHERE album_id=al.id), 10)
              + COALESCE((SELECT AVG(CASE tm.tier WHEN "silver" THEN 30 WHEN "bronze" THEN 20 ELSE 8 END)
                FROM tracks t JOIN track_meta tm ON tm.track_id=t.id WHERE t.album_id=al.id), 8)
              + COALESCE((SELECT 10 + ROUND(10.0 * am.tracks_con_letra / NULLIF(am.tracks_procesados,0))
                FROM album_meta am WHERE am.album_id=al.id), 0)
            FROM albums al''')
        conn.execute('DELETE FROM track_pop_cache')
        conn.execute('''INSERT INTO track_pop_cache (track_id, pop_score)
            SELECT t.id,
              CASE t.led_color WHEN "red" THEN 40 WHEN "cyan" THEN 38 WHEN "white" THEN 30
                WHEN "blue" THEN 25 WHEN "green" THEN 22 WHEN "magenta" THEN 20 ELSE 10 END
              + COALESCE(CASE tm.tier WHEN "silver" THEN 30 WHEN "bronze" THEN 20 ELSE 8 END, 8)
              + COALESCE(CASE WHEN tm.has_synced_lrc=1 THEN 20 WHEN tm.has_lyrics=1 THEN 12 ELSE 0 END, 0)
              + COALESCE(ROUND(tm.mood_confidence * 10), 0)
            FROM tracks t LEFT JOIN track_meta tm ON tm.track_id=t.id''')
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/favorites')
def favorites_page():
    """Favorites page — shows all bookmarked tracks."""
    try:
        conn = get_db_connection()
        rows = conn.execute('''
            SELECT t.id, t.title, t.artist, t.duration, t.led_color, t.file_path,
                   t.codec, t.is_dsd, t.dsd_rate, t.is_mqa, t.sample_rate_real,
                   al.id as album_id, al.name as album_name, al.cover_path,
                   al.artist_id,
                   CASE WHEN t.is_dsd=1 THEN COALESCE(t.dsd_rate,'DSD')
                        WHEN t.is_mqa=1 THEN 'MQA'
                        ELSE UPPER(COALESCE(t.codec,'FLAC')) END as format_display,
                   f.added_at
            FROM favorites f
            JOIN tracks t ON t.id=f.track_id
            JOIN albums al ON al.id=t.album_id
            ORDER BY f.added_at DESC
        ''').fetchall()
        conn.close()
        tracks = [dict(r) for r in rows]
        # Build audio_url for each track
        for t in tracks:
            t['audio_url'] = audio_url_filter(t['file_path'])
            t['cover_url'] = cover_url_filter(t['cover_path'] or '')
        return render_template('favorites.html',
                               tracks=tracks,
                               tracks_json=json.dumps(tracks),
                               fav_ids=_favorites_set)
    except Exception as e:
        app.logger.error(f'favorites_page error: {e}')
        return render_template('favorites.html', tracks=[], tracks_json='[]', fav_ids=set())


@app.route('/api/debug-dsd')
def api_debug_dsd():
    """Diagnostic endpoint: shows what the server would do for a DSD track."""
    import shutil
    conn = get_db_connection()
    try:
        sample = conn.execute(
            "SELECT id, title, file_path, is_dsd, dsd_rate, sample_rate_real FROM tracks WHERE is_dsd=1 LIMIT 1"
        ).fetchone()
        result = {
            'ffmpeg_available':    bool(shutil.which('ffmpeg')),
            'mpc_available':       bool(shutil.which('mpc')),
            'python_mpd2':         _MPD_AVAILABLE,
            'music_root':          MUSIC_ROOT,
            'music_root_exists':   os.path.isdir(MUSIC_ROOT),
            'sample_dsd_track':    dict(sample) if sample else None,
        }
        if sample:
            fp = sample['file_path']
            result['sample_audio_url']  = audio_url_filter(fp)
            result['sample_file_exists'] = os.path.isfile(fp)
            result['mpd_relative']       = _to_mpd_relative(fp)
        return jsonify(result)
    finally:
        conn.close()


if __name__ == '__main__':
    _load_favorites()
    app.run(debug=True, host='0.0.0.0', port=5000)
