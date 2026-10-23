"""
Orbyte Ingesta — etapa 3: los 5 scripts de metadata para un álbum ya ubicado, y la validación final.

  1. index_libraryV3.py --incremental --album RUTA      (lo agrega a music.db)
  2. RichMetaDSD3.py RUTA --mode pending                 (ánimo, género, letras…)
  3. import_richmetapro.py --album RUTA                  (lo lleva a track_meta / album_meta)
  4. RichMetaRelations3.py --album ID --all              (nacionalidad, similares)
  5. lastfm_fetch2.py --scope album:ID                   (popularidad y estrellas)

Cada script corre con stdin cerrado (si alguno pidiera algo por teclado, falla en vez de quedarse colgado)
y su salida va al registro del álbum.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import time

from . import analisis

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA_SCRIPTS = os.path.join(BASE, 'tools', 'metadata')
DEPENDENCIAS = ('requests', 'tqdm', 'rapidfuzz', 'dotenv', 'mutagen')
OPCIONALES = ('numpy', 'langdetect')      # sin ellos los scripts funcionan, pero más lento / sin idioma de letras
LIMITE_S = {'index': 1800, 'richmeta': 7200, 'import': 1800, 'relations': 3600, 'lastfm': 1800}


def python_scripts(aj):
    p = (aj.get('python_scripts') or '').strip()
    return p or shutil.which('python3', path='/usr/local/bin:/usr/bin:/bin') or sys.executable


def probar_python(aj):
    """Qué dependencias faltan en el Python que corre los scripts."""
    py = python_scripts(aj)
    codigo = ('import importlib.util as u; print(",".join(m for m in %r if u.find_spec(m) is None))'
              % (DEPENDENCIAS + OPCIONALES,))
    try:
        r = subprocess.run([py, '-c', codigo], capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {'python': py, 'ok': False, 'error': str(e), 'faltan': []}
    todas = [m for m in r.stdout.strip().split(',') if m]
    faltan = [m for m in todas if m in DEPENDENCIAS]
    return {'python': py, 'ok': r.returncode == 0 and not faltan, 'faltan': faltan,
            'opcionales_faltan': [m for m in todas if m in OPCIONALES],
            'error': r.stderr.strip()[-300:] if r.returncode else None}


def _correr(nombre, args, reg, aj, limite):
    py = python_scripts(aj)
    script = os.path.join(CARPETA_SCRIPTS, args[0])
    cmd = [py, '-u', script] + args[1:]
    reg(f'▶ {args[0]} {" ".join(args[1:])}')
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1', TERM='dumb')
    # los scripts dejan sus .log en la carpeta de trabajo: que sea la de datos de la herramienta, no el repo
    trabajo = os.path.join(os.environ.get('INGESTA_DATOS') or os.path.join(BASE, 'ingesta_datos'), 'logs_scripts')
    os.makedirs(trabajo, exist_ok=True)
    p = subprocess.Popen(cmd, cwd=trabajo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors='replace', env=env)
    ultimas = []
    try:
        for linea in p.stdout:
            linea = linea.rstrip()
            # las barras de tqdm llegan como muchas líneas con \r: solo se guarda la última de cada tanda
            if '\r' in linea:
                linea = linea.split('\r')[-1]
            if not linea.strip():
                continue
            ultimas.append(linea)
            ultimas = ultimas[-40:]
            if '%|' not in linea:
                reg('   ' + linea[:300])
            if time.time() - t0 > limite:
                p.kill()
                raise RuntimeError(f'{args[0]} superó {limite // 60} minutos')
        p.wait(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    seg = time.time() - t0
    if p.returncode != 0:
        raise RuntimeError(f'{args[0]} terminó con código {p.returncode}: ' + ' | '.join(ultimas[-3:]))
    reg(f'✓ {args[0]} ({seg:.0f} s)')
    return ultimas


def album_id(db, ruta):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    try:
        r = con.execute('SELECT id FROM albums WHERE path=? OR path=?', (ruta, ruta.rstrip('/') + '/')).fetchone()
        return r[0] if r else None
    finally:
        con.close()


def correr(ruta, aj, reg):
    db = aj.get('db_orbyte')
    raiz = aj.get('biblioteca') or '/mnt/musica'
    if not db or not os.path.isfile(db):
        raise RuntimeError(f'No se encontró music.db ({db})')
    prueba = probar_python(aj)
    if not prueba['ok']:
        raise RuntimeError(f'Al Python de los scripts ({prueba["python"]}) le faltan módulos: '
                           f'{", ".join(prueba["faltan"]) or prueba.get("error")}')
    reg('Etapa 3: metadata')
    _correr('index', ['index_libraryV3.py', '--incremental', '--album', ruta, '--db', db, '--root', raiz, '--quiet'],
            reg, aj, LIMITE_S['index'])
    aid = album_id(db, ruta)
    if not aid:
        raise RuntimeError('El indexador terminó pero el álbum no aparece en music.db')
    reg(f'   álbum ID {aid} en Orbyte')
    _correr('richmeta', ['RichMetaDSD3.py', ruta, '--mode', 'pending'], reg, aj, LIMITE_S['richmeta'])
    _correr('import', ['import_richmetapro.py', '--album', ruta, '--db', db], reg, aj, LIMITE_S['import'])
    _correr('relations', ['RichMetaRelations3.py', '--album', str(aid), '--all', '--db', db], reg, aj,
            LIMITE_S['relations'])
    _correr('lastfm', ['lastfm_fetch2.py', '--scope', f'album:{aid}', '--db', db], reg, aj, LIMITE_S['lastfm'])
    res = validar(db, ruta, aid)
    reg('Validación: ' + ('OK' if res['ok'] else 'con errores') +
        (f' · {len(res["advertencias"])} advertencias' if res['advertencias'] else ''))
    for e in res['errores']:
        reg('   ✗ ' + e)
    for a in res['advertencias']:
        reg('   ⚠ ' + a)
    return res


def validar(db, ruta, aid):
    """Comprobaciones finales del álbum en music.db (spec §3.6)."""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        al = con.execute('SELECT al.*, ar.name AS artista, ar.nationality, ar.similar_artists_json '
                         'FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.id=?', (aid,)).fetchone()
        pistas = con.execute('SELECT t.id, t.title, t.artist, t.file_path, m.mood, m.genre_primary, m.energy, '
                             'm.valence, m.has_lyrics, m.has_synced_lrc, m.similar_tracks_json '
                             'FROM tracks t LEFT JOIN track_meta m ON m.track_id = t.id WHERE t.album_id=?',
                             (aid,)).fetchall()
        meta = con.execute('SELECT * FROM album_meta WHERE album_id=?', (aid,)).fetchone()
        pop = con.execute('SELECT * FROM album_pop_cache WHERE album_id=?', (aid,)).fetchone()
    finally:
        con.close()

    n_archivos = 0
    for raiz, dirs, archivos in os.walk(ruta):
        dirs[:] = [d for d in dirs if d != 'Extras']
        n_archivos += sum(1 for a in archivos if os.path.splitext(a)[1].lower() in analisis.AUDIO_EXT)
    errores, advertencias = [], []
    n = len(pistas)
    if n != n_archivos:
        errores.append(f'{n} pistas en la base y {n_archivos} archivos en la carpeta')
    desconocidas = [p['title'] for p in pistas
                    if not (p['artist'] or '').strip() or 'unknown' in (p['artist'] or '').lower()]
    if desconocidas:
        errores.append(f'{len(desconocidas)} pistas con artista «Unknown Artist» o vacío')
    if al and 'unknown' in (al['name'] or '').lower():
        errores.append('El álbum quedó como «Unknown Album»')
    sin_animo = [p for p in pistas if not p['mood']]
    if sin_animo:
        errores.append(f'{len(sin_animo)} pistas sin ánimo/vectores (RichMeta)')
    sin_genero = [p for p in pistas if not p['genre_primary']]
    if sin_genero:
        advertencias.append(f'{len(sin_genero)} pistas sin género')
    sinc = sum(1 for p in pistas if p['has_synced_lrc'])
    planas = sum(1 for p in pistas if p['has_lyrics'] and not p['has_synced_lrc'])
    sin_letra = n - sinc - planas
    if sin_letra:
        advertencias.append(f'{sin_letra} pistas sin letra')
    if any(not p['similar_tracks_json'] for p in pistas):
        advertencias.append('Pistas similares sin calcular en algunas pistas')
    if not meta or not meta['similar_albums_json']:
        advertencias.append('Álbumes similares sin calcular')
    if al and not al['nationality']:
        advertencias.append('Artista sin nacionalidad')
    if not pop:
        advertencias.append('Sin popularidad de Last.fm')
    if not al or not al['cover_path'] or not os.path.isfile(al['cover_path']):
        advertencias.append('Orbyte no encuentra la carátula')
    return {'ok': not errores, 'errores': errores, 'advertencias': advertencias, 'album_id': aid,
            'pistas': n, 'letras': {'sincronizadas': sinc, 'planas': planas, 'faltantes': sin_letra},
            'estrellas': pop['stars'] if pop else None}
