"""
Orbyte Ingesta — álbumes repetidos: el mismo álbum en varias ediciones o calidades.

  detectar()  agrupa ediciones del mismo artista + mismo álbum (los discos «CD 01/CD 02» cuentan como una edición),
              con datos técnicos, uso en Orbyte y espacio que se podría liberar
  detalle()   tags de la edición (sello, catálogo, país, fechas) leídos del primer archivo
  medir()     el master de cada edición: volumen integrado (LUFS), rango (LRA), pico real y PLR
  sugerir()   pide a la IA (Gemini → Groq, las mismas claves de Orbitron) qué edición conviene y por qué
  eliminar()  respaldo de music.db → escuchas/favoritos/playlists a la edición que se queda → borra los archivos
              del disco (irrecuperable) y la saca de Orbyte
"""
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import analisis
from .artistas import respaldo
from .fantasmas import _titulo, _tablas, borrar_album_db

LOSSY_SQL = ("(COALESCE(t.compression_mode,'')='Lossy' OR LOWER(COALESCE(t.codec,'')) IN ("
             + ','.join(f"'{c}'" for c in sorted(analisis.LOSSY_CODECS)) + "))")
DSD_NOMBRE = {2822400: 'DSD64', 5644800: 'DSD128', 11289600: 'DSD256', 22579200: 'DSD512'}
RANGO = {'Lossy': 0, 'CD': 1, 'HR': 2, 'MQA': 2, 'DSD': 3}


def _base_edicion(ruta):
    """Los discos «CD 01», «Disc 2»… son parte de la misma edición: su carpeta es la del álbum."""
    if ruta and analisis.dividir_por_disco(os.path.basename(ruta.rstrip('/'))) is not None:
        return os.path.dirname(ruta.rstrip('/'))
    return (ruta or '').rstrip('/')


def _formato(e):
    if e['dsd']:
        return DSD_NOMBRE.get(e['dsd_rate'] or 0, 'DSD')
    if e['lossy'] * 2 > e['n']:
        codec = (e['codecs'] or 'mp3').split(',')[0].upper()
        k = analisis.kbps_nominal(e['bitrate'], codec.lower()) if e['bitrate'] else None
        return f'{codec} {k} kbps' if k else codec
    bits = e['bits'] or 16
    if e['mqa']:
        return f'MQA {bits}/{(e.get("sr_real") or e["sr"] or 44100) / 1000:g}'
    codec = (e['codecs'] or 'flac').split(',')[0].upper()
    codec = {'PCM_S16LE': 'WAV', 'PCM_S24LE': 'WAV', 'PCM_S32LE': 'WAV', 'PCM_S16BE': 'AIFF',
             'PCM_S24BE': 'AIFF'}.get(codec, codec)
    return f'{codec} {bits}/{(e["sr"] or 44100) / 1000:g}'


def _nombre_limpio(nombre):
    """«The Wall [Japan SHM-CD]» → «The Wall»: sin lo que distingue a cada edición."""
    n = re.sub(r'\s*\[[^\]]*\]', '', nombre or '')
    n = re.sub(r'\s*\((?:[^)]*(?:remaster|deluxe|edition|edición|version|versión|expanded|anniversary|bonus|\d{4})[^)]*)\)',
               '', n, flags=re.I)
    return n.strip() or (nombre or '')


def detectar(db, revisados=None):
    """Grupos de ediciones repetidas, de mayor a menor espacio liberable."""
    revisados = revisados or {}
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        filas = con.execute(f'''
            SELECT al.id, al.name, al.year, al.path, al.cover_path, al.artist_id, ar.name AS artista,
                   COUNT(t.id) AS n, COALESCE(SUM(t.duration), 0) AS dur, COALESCE(SUM(t.file_size), 0) AS bytes,
                   MAX(t.sample_rate) AS sr, MAX(CAST(t.bit_depth AS INTEGER)) AS bits, MAX(CASE WHEN t.is_dsd=1 THEN t.sample_rate END) AS dsd_sr, MAX(t.sample_rate_real) AS sr_real,
                   SUM(CASE WHEN t.is_dsd=1 THEN 1 ELSE 0 END) AS dsd, SUM(CASE WHEN t.is_mqa=1 THEN 1 ELSE 0 END) AS mqa,
                   SUM(CASE WHEN {LOSSY_SQL} THEN 1 ELSE 0 END) AS lossy, MAX(t.bitrate) AS bitrate,
                   GROUP_CONCAT(DISTINCT LOWER(t.codec)) AS codecs
            FROM albums al JOIN artists ar ON ar.id = al.artist_id LEFT JOIN tracks t ON t.album_id = al.id
            GROUP BY al.id''').fetchall()
        escuchas = dict(con.execute('SELECT t.album_id, COUNT(*) FROM listening_events le '
                                    'JOIN tracks t ON t.id = le.track_id GROUP BY t.album_id').fetchall())
        en_playlists = dict(con.execute('SELECT t.album_id, COUNT(*) FROM playlist_tracks pt '
                                        'JOIN tracks t ON t.id = pt.track_id GROUP BY t.album_id').fetchall())
        favs = Counter()
        for tipo, item, alb in con.execute(
                "SELECT f.item_type, f.item_id, COALESCE(t.album_id, f.item_id) FROM user_item_favorites f "
                "LEFT JOIN tracks t ON f.item_type='track' AND t.id=f.item_id WHERE f.item_type IN ('album','track')"):
            favs[alb] += 1
    finally:
        con.close()

    ediciones = {}
    for f in filas:
        base = _base_edicion(f['path'])
        e = ediciones.setdefault(base, {
            'ruta': base, 'albumes': [], 'nombre': f['name'], 'anio': f['year'], 'artista': f['artista'],
            'artist_id': f['artist_id'], 'portada': f['cover_path'], 'n': 0, 'dur': 0.0, 'bytes': 0, 'sr': 0,
            'bits': 0, 'dsd': 0, 'mqa': 0, 'lossy': 0, 'bitrate': 0, 'codecs': f['codecs'], 'dsd_rate': 0,
            'escuchas': 0, 'playlists': 0, 'favoritos': 0})
        e['albumes'].append(f['id'])
        for k in ('n', 'dur', 'bytes', 'dsd', 'mqa', 'lossy'):
            e[k] += f[k] or 0
        e['sr'] = max(e['sr'], f['sr'] or 0)
        e['bits'] = max(e['bits'], f['bits'] or 0)
        e['bitrate'] = max(e['bitrate'], f['bitrate'] or 0)
        e['dsd_rate'] = max(e['dsd_rate'], f['dsd_sr'] or 0)
        e['sr_real'] = max(e.get('sr_real', 0), f['sr_real'] or 0)
        e['escuchas'] += escuchas.get(f['id'], 0)
        e['playlists'] += en_playlists.get(f['id'], 0)
        e['favoritos'] += favs.get(f['id'], 0)
        if not e['portada'] and f['cover_path']:
            e['portada'] = f['cover_path']

    grupos = {}
    for e in ediciones.values():
        if not e['n']:
            continue
        grupos.setdefault(f'{e["artist_id"]}:{analisis._norm_album(e["nombre"])}', []).append(e)

    salida = []
    for clave, eds in grupos.items():
        if len(eds) < 2:
            continue
        firma = '|'.join(sorted(e['ruta'] for e in eds))
        if revisados.get(clave) == firma:
            continue
        for e in eds:
            e['calidad'] = ('DSD' if e['dsd'] * 2 >= e['n'] else 'MQA' if e['mqa'] * 2 >= e['n']
                            else 'Lossy' if e['lossy'] * 2 > e['n'] else 'HR' if e['sr'] > 48000 else 'CD')
            e['formato'] = _formato(e)
            e['anidada_en'] = next((o['ruta'] for o in eds if o is not e and e['ruta'].startswith(o['ruta'] + '/')),
                                   None)
            e['contiene'] = [o['ruta'] for o in eds if o is not e and o['ruta'].startswith(e['ruta'] + '/')]
        eds.sort(key=lambda e: (-RANGO.get(e['calidad'], 0), -e['bits'], -e['sr'], -e['n']))
        total = sum(e['bytes'] for e in eds)
        calidades = {e['calidad'] for e in eds}
        salida.append({
            'clave': clave, 'firma': firma, 'artista': eds[0]['artista'],
            'album': min((_nombre_limpio(e['nombre']) for e in eds), key=lambda n: (len(n), n)),
            'ediciones': eds, 'total': total, 'liberable': total - max(e['bytes'] for e in eds),
            'misma_calidad': len(calidades) == 1,
        })
    salida.sort(key=lambda g: -g['liberable'])
    return salida


def _pistas(con, album_ids):
    m = ','.join('?' * len(album_ids))
    return con.execute(f'SELECT id, album_id, title, track_number, disc_number, duration, file_path FROM tracks '
                       f'WHERE album_id IN ({m}) ORDER BY disc_number, CAST(track_number AS INTEGER)',
                       album_ids).fetchall()


def detalle(db, grupo):
    """Agrega a cada edición: lista de pistas comparada y los tags de edición del primer archivo."""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        listas = {}
        for e in grupo['ediciones']:
            pistas = _pistas(con, e['albumes'])
            listas[e['ruta']] = pistas
            e['pistas_lista'] = [{'t': p['title'], 'd': round(p['duration'] or 0)} for p in pistas]
            e['tags'] = _tags_edicion(pistas[0]['file_path']) if pistas else {}
    finally:
        con.close()
    ref = max(grupo['ediciones'], key=lambda e: e['n'])
    ref_t = {_titulo(p['t']) for p in ref['pistas_lista']}
    for e in grupo['ediciones']:
        t = {_titulo(p['t']) for p in e['pistas_lista']}
        if e is ref:
            e['relacion'] = 'referencia'
        elif t == ref_t:
            e['relacion'] = 'mismas pistas'
        else:
            partes = [f'{len(t - ref_t)} pista(s) que las otras no tienen' if t - ref_t else '',
                      f'le faltan {len(ref_t - t)}' if ref_t - t else '']
            e['relacion'] = ' · '.join(x for x in partes if x)
    return grupo


def _tags_edicion(ruta):
    if not ruta or not os.path.isfile(ruta):
        return {}
    try:
        _, crudo = analisis.tags_mutagen(ruta)
        info = analisis._edicion(crudo)
        for k in ('comment', 'description', 'media', 'source', 'encodedby', 'version'):
            if crudo.get(k):
                info[k] = str(crudo[k])[:120]
        return info
    except Exception:
        return {}


# ─────────────────────────── medición del master ───────────────────────────

def _medir_pista(ruta):
    r = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-i', ruta, '-map', '0:a:0',
                        '-af', 'ebur128=peak=true', '-f', 'null', '-'],
                       capture_output=True, text=True, errors='replace', timeout=3600)
    txt = r.stderr[r.stderr.rfind('Summary:'):] if 'Summary:' in r.stderr else ''
    def num(patron):
        m = re.search(patron, txt)
        return float(m.group(1)) if m else None
    return num(r'I:\s*(-?[\d.]+) LUFS'), num(r'LRA:\s*(-?[\d.]+) LU'), num(r'Peak:\s*(-?[\d.]+) dBFS')


def medir(db, edicion, progreso=None, trabajadores=1):
    """Volumen integrado del álbum (LUFS), rango dinámico (LRA, mediana), pico real (máx.) y PLR."""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        pistas = [p for p in _pistas(con, edicion['albumes']) if p['file_path'] and os.path.isfile(p['file_path'])]
    finally:
        con.close()
    energia = dur_total = 0.0
    lras, picos, hechas = [], [], 0
    with ThreadPoolExecutor(max_workers=max(1, trabajadores)) as ex:
        futuros = {ex.submit(_medir_pista, p['file_path']): p for p in pistas}
        for fut in as_completed(futuros):
            p = futuros[fut]
            hechas += 1
            if progreso:
                progreso(hechas, len(pistas))
            try:
                lufs, lra, pico = fut.result()
            except Exception:
                continue
            d = p['duration'] or 0
            if lufs is not None and lufs > -70 and d:
                energia += d * 10 ** (lufs / 10)
                dur_total += d
            if lra is not None:
                lras.append(lra)
            if pico is not None:
                picos.append(pico)
    if not dur_total:
        return {'error': 'no se pudo medir'}
    lufs = 10 * math.log10(energia / dur_total)
    lras.sort()
    pico = max(picos) if picos else None
    return {'lufs': round(lufs, 1), 'lra': round(lras[len(lras) // 2], 1) if lras else None,
            'pico': round(pico, 1) if pico is not None else None,
            'plr': round(pico - lufs, 1) if pico is not None else None, 'pistas': len(pistas)}


# ─────────────────────────── sugerencia de la IA ───────────────────────────

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.6-flash')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b')


def _prompt(grupo, mediciones):
    lineas = [f'Artista: {grupo["artista"]}', f'Álbum: {grupo["album"]}', '', 'Ediciones que tengo:']
    letras = {}
    for i, e in enumerate(grupo['ediciones']):
        letra = chr(65 + i)
        letras[letra] = e['ruta']
        m = mediciones.get(e['ruta']) or {}
        t = e.get('tags') or {}
        datos = [f'carpeta «{os.path.basename(e["ruta"])}»', f'calidad {e["calidad"]} ({e["formato"]})',
                 f'{e["n"]} pistas', f'{round(e["dur"] / 60)} min', f'{e["bytes"] / 1e6:.0f} MB']
        if e.get('relacion') and e['relacion'] != 'referencia':
            datos.append(f'lista de pistas: {e["relacion"]}')
        for k, nombre in (('sello', 'sello'), ('catalogo', 'catálogo'), ('pais', 'país'),
                          ('fecha_edicion', 'fecha'), ('anio_original', 'original'), ('barcode', 'código de barras'),
                          ('mbid', 'MusicBrainz release'), ('comment', 'comentario')):
            if t.get(k):
                datos.append(f'{nombre} {t[k]}')
        if m.get('lufs') is not None:
            datos.append(f'medición: {m["lufs"]} LUFS, LRA {m["lra"]} LU, pico real {m["pico"]} dBFS, PLR {m["plr"]} dB')
        lineas.append(f'{letra}) ' + '; '.join(datos))
    lineas += ['', 'Soy audiófilo y quiero liberar espacio. Dime qué edición conviene conservar y cuáles se pueden '
                   'eliminar. Considera la calidad técnica, la masterización (rango dinámico: PLR y LRA más altos '
                   'suelen indicar menos compresión), la reputación conocida de esas ediciones o prensajes '
                   '(por ejemplo japonesas, remasters, MFSL) y si alguna trae pistas que las otras no. '
                   'Si dos valen la pena por motivos distintos, dilo.',
               'Responde SOLO con JSON: {"conservar": ["A"], "eliminar": ["B"], "recomendada": "A", '
               '"razon": "2 o 3 frases en español", "confianza": "alta|media|baja"}']
    return '\n'.join(lineas), letras


def _post(url, cuerpo, cabeceras, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), method='POST',
                                 headers=dict({'Content-Type': 'application/json'}, **cabeceras))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _json_de(texto):
    m = re.search(r'\{.*\}', texto or '', re.S)
    return json.loads(m.group(0)) if m else None


def sugerir(grupo, mediciones):
    prompt, letras = _prompt(grupo, mediciones)
    errores = []
    clave = os.environ.get('GEMINI_API_KEY')
    if clave:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
        for herramientas in ([{'google_search': {}}], None):      # con búsqueda web; si falla, sin ella
            cuerpo = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': {'temperature': 0.2}}
            if herramientas:
                cuerpo['tools'] = herramientas
            try:
                d = _post(url, cuerpo, {'x-goog-api-key': clave})
                texto = ''.join(p.get('text', '') for c in d.get('candidates', [])
                                for p in (c.get('content') or {}).get('parts', []))
                r = _json_de(texto)
                if r:
                    return _normalizar(r, letras, 'Gemini' + (' + búsqueda web' if herramientas else ''))
            except Exception as e:
                errores.append(f'Gemini: {e}')
    clave = os.environ.get('GROQ_API_KEY')
    if clave:
        try:
            d = _post('https://api.groq.com/openai/v1/chat/completions',
                      {'model': GROQ_MODEL, 'temperature': 0.2, 'response_format': {'type': 'json_object'},
                       'messages': [{'role': 'user', 'content': prompt}]},
                      {'Authorization': f'Bearer {clave}'})
            r = _json_de(d['choices'][0]['message']['content'])
            if r:
                return _normalizar(r, letras, 'Groq')
        except Exception as e:
            errores.append(f'Groq: {e}')
    if not os.environ.get('GEMINI_API_KEY') and not os.environ.get('GROQ_API_KEY'):
        return {'error': 'No hay GEMINI_API_KEY ni GROQ_API_KEY en el .env'}
    return {'error': '; '.join(errores) or 'La IA no devolvió una respuesta válida'}


def _normalizar(r, letras, fuente):
    def rutas(xs):
        return [letras[x.strip().upper()[:1]] for x in xs or [] if x and x.strip().upper()[:1] in letras]
    rec = (r.get('recomendada') or '').strip().upper()[:1]
    return {'conservar': rutas(r.get('conservar')), 'eliminar': rutas(r.get('eliminar')),
            'recomendada': letras.get(rec), 'razon': str(r.get('razon') or '')[:800],
            'confianza': r.get('confianza'), 'fuente': fuente,
            'letras': {v: k for k, v in letras.items()}}


# ─────────────────────────── eliminar ───────────────────────────

def _mapa(pistas_viejas, pistas_nuevas):
    por_num, por_tit, usados, mapa = {}, {}, set(), {}
    for o in pistas_nuevas:
        por_num.setdefault((str(o['disc_number'] or 1), str(o['track_number'] or '')), []).append(o)
        por_tit.setdefault(_titulo(o['title']), []).append(o)
    for p in pistas_viejas:
        t = _titulo(p['title'])
        elegido = next((o for o in por_num.get((str(p['disc_number'] or 1), str(p['track_number'] or '')), [])
                        if o['id'] not in usados and (not t or _titulo(o['title']) == t)), None)
        if not elegido and t:
            elegido = next((o for o in por_tit.get(t, []) if o['id'] not in usados), None)
        if elegido:
            mapa[p['id']] = elegido['id']
            usados.add(elegido['id'])
    return mapa


def eliminar(db, biblioteca, grupo, rutas_borrar, ruta_destino, registro):
    """Borra del disco y de Orbyte las ediciones `rutas_borrar`; lo usado pasa a `ruta_destino`. IRRECUPERABLE."""
    biblioteca = os.path.abspath(biblioteca)
    eds = {e['ruta']: e for e in grupo['ediciones']}
    if not rutas_borrar or any(r not in eds for r in rutas_borrar) or ruta_destino not in eds \
            or ruta_destino in rutas_borrar:
        raise ValueError('Selección inválida: debe quedar al menos una edición')
    for r in rutas_borrar:
        if not os.path.abspath(r).startswith(biblioteca + os.sep):
            raise ValueError(f'{r} está fuera de la biblioteca: no se borra')
    respaldo(db, registro)
    con = sqlite3.connect(db, timeout=60)
    con.row_factory = sqlite3.Row
    tablas = _tablas(con)
    destino = eds[ruta_destino]
    pistas_destino = _pistas(con, destino['albumes'])
    album_de_pista = {p['id']: p['album_id'] for p in pistas_destino}
    resumen = {'liberado': 0, 'ediciones': 0, 'archivos': 0, 'movidas': 0, 'quitadas': 0, 'avisos': []}
    try:
        for ruta in rutas_borrar:
            e = eds[ruta]
            pistas = _pistas(con, e['albumes'])
            carpetas = sorted(set(_rutas_albumes(con, e['albumes'])) | {ruta}, key=len, reverse=True)
            mapa = _mapa(pistas, pistas_destino)
            registro(f'«{os.path.relpath(ruta, biblioteca)}»: {len(mapa)}/{len(pistas)} pistas con equivalente '
                     f'en la edición que se queda')
            # 1) base: lo que apunta a sus pistas pasa a la edición que se queda; la edición sale de Orbyte
            with con:
                for aid in e['albumes']:
                    a = con.execute('SELECT id, name, path, artist_id FROM albums WHERE id=?', (aid,)).fetchone()
                    if not a:
                        continue
                    pids = [p['id'] for p in pistas if p['album_id'] == aid]
                    sub = {k: v for k, v in mapa.items() if k in pids}
                    dest = Counter(album_de_pista[v] for v in sub.values()).most_common(1)
                    mov, qui = borrar_album_db(con, tablas, a, pids, sub, dest[0][0] if dest else destino['albumes'][0])
                    resumen['movidas'] += mov
                    resumen['quitadas'] += qui
            # 2) disco: sus archivos de audio y luego sus carpetas
            otras = [r[0] for r in con.execute('SELECT path FROM albums WHERE path IS NOT NULL')]
            for p in pistas:
                f = p['file_path']
                if f and os.path.isfile(f):
                    resumen['liberado'] += os.path.getsize(f)
                    os.remove(f)
                    resumen['archivos'] += 1
            for carpeta in carpetas:
                if not os.path.isdir(carpeta):
                    continue
                rel = os.path.relpath(carpeta, biblioteca)
                ajenas = [o for o in otras if o == carpeta or o.startswith(carpeta.rstrip('/') + '/')]
                if not ajenas and len(rel.split(os.sep)) >= 3:      # Letra/Artista/Álbum… : la carpeta entera
                    resumen['liberado'] += _tamano(carpeta)
                    shutil.rmtree(carpeta)
                else:
                    sueltos = [n for n in os.listdir(carpeta) if os.path.isfile(os.path.join(carpeta, n))]
                    if ajenas and sueltos:
                        resumen['avisos'].append(f'«{rel}» contiene otra edición: se borró solo el audio de esta y '
                                                 f'quedan {len(sueltos)} archivo(s) sueltos ({", ".join(sueltos[:4])}'
                                                 f'{"…" if len(sueltos) > 4 else ""}); revísalos a mano')
                    _quitar_si_vacia(carpeta)
            _limpiar_padres(os.path.dirname(ruta), biblioteca)
            resumen['ediciones'] += 1
            registro(f'  eliminada · {resumen["liberado"] / 1e9:.2f} GB liberados hasta ahora')
    finally:
        con.close()
    registro(f'Listo: {resumen["ediciones"]} edición(es) eliminada(s), {resumen["liberado"] / 1e9:.2f} GB liberados, '
             f'{resumen["movidas"]} referencias pasadas a la edición que se queda')
    for a in resumen['avisos']:
        registro('⚠ ' + a)
    return resumen


def _quitar_si_vacia(carpeta):
    try:
        if not os.listdir(carpeta):
            os.rmdir(carpeta)
    except OSError:
        pass


def _rutas_albumes(con, ids):
    m = ','.join('?' * len(ids))
    return [r[0] for r in con.execute(f'SELECT path FROM albums WHERE id IN ({m})', ids) if r[0]]


def _tamano(carpeta):
    total = 0
    for raiz, _, archivos in os.walk(carpeta):
        for a in archivos:
            try:
                total += os.path.getsize(os.path.join(raiz, a))
            except OSError:
                pass
    return total


def _limpiar_padres(carpeta, biblioteca):
    """Borra carpetas vacías hacia arriba, sin llegar a la carpeta de la letra."""
    while carpeta.startswith(biblioteca + os.sep) and len(os.path.relpath(carpeta, biblioteca).split(os.sep)) >= 2:
        try:
            if os.listdir(carpeta):
                break
            os.rmdir(carpeta)
        except OSError:
            break
        carpeta = os.path.dirname(carpeta)
