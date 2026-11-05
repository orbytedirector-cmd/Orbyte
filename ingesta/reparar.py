"""
Orbyte Ingesta — reparar solo las pistas dañadas de un álbum (sin re-descargarlo entero ni reprocesarlo).

Por pista dañada (FLAC):
  1. la misma edición en Tidal (código de barras si el álbum lo trae, si no artista + título, y la discografía del
     artista si la búsqueda no la trae) y la misma pista: disco + número, título y duración (±2 s);
  2. se baja SOLO esa pista; se verifica que se decodifique sin errores, que sea FLAC y que dure lo mismo;
  3. recibe la metadata de la pista vieja (todos los tags, carátula incrustada, letra); el ReplayGain de la pista se
     recalcula porque el audio es otro. Si su calidad es distinta a la del resto del álbum, su título lleva al final
     la calidad nueva, p. ej. «Loverman (CD-16/44.1)»;
  4. reemplaza a la vieja en el mismo lugar y con el mismo nombre (la vieja se borra recién ahí): para Orbyte es el
     mismo archivo, así que escuchas, favoritos y playlists no se tocan; solo se releen sus datos técnicos.

Se hace de a una pista, con pausa, y espera mientras haya una descarga en curso (misma sesión de Tidal). Los trabajos
quedan en ingesta_datos/salud.db: un reinicio retoma los que estaban en cola.
"""
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

from . import analisis, rescate, tienda

log = logging.getLogger('ingesta.reparar')

PAUSA_S = 5
SUFIJO_RE = re.compile(r'\s*\((?:CD|HR)-\d+/[\d.]+\)\s*$')


class Fallo(Exception):
    pass


# ─────────────────────────── calidad ───────────────────────────

def calidad_archivo(ruta):
    """(bits, frecuencia) según ffprobe (la cabecera se lee aunque el audio esté dañado)."""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                            'stream=sample_rate,bits_per_raw_sample,bits_per_sample', '-of', 'json', ruta],
                           capture_output=True, text=True, timeout=60)
        st = json.loads(r.stdout)['streams'][0]
        bits = int(st.get('bits_per_raw_sample') or st.get('bits_per_sample') or 0) or None
        return bits, int(st.get('sample_rate') or 0) or None
    except Exception:  # noqa: BLE001
        return None, None


def etiqueta(bits, sr):
    """«CD-16/44.1», «HR-24/96» (misma regla que el LED: más de 48 kHz es HR)."""
    if not sr:
        return None
    return f'{"HR" if sr > 48000 else "CD"}-{bits or "?"}/{sr / 1000:g}'


def _mayoria(pares):
    pares = [p for p in pares if p and p[1]]
    return max(set(pares), key=pares.count) if pares else (None, None)


def calidad_album(carpeta, db=None, excluir=()):
    """(bits, frecuencia) de la mayoría de las pistas del álbum (sin contar las dañadas)."""
    audios = [a for a in tienda._audios(carpeta) if os.path.realpath(a) not in excluir]
    pares = []
    if db and os.path.isfile(db):
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
        try:
            for k in range(0, len(audios), 500):
                parte = [os.path.realpath(a) for a in audios[k:k + 500]]
                pares += [(b, s) for b, s in con.execute(
                    f'SELECT bit_depth, sample_rate FROM tracks WHERE file_path IN ({",".join("?" * len(parte))})', parte)]
        finally:
            con.close()
    if len(pares) < min(len(audios), 3):          # no está en Orbyte (Bandeja): se mide una muestra
        pares = [calidad_archivo(a) for a in audios[:40]]
    return _mayoria(pares)


def calidad_tidal(pista_id):
    """(bits, frecuencia) que entregaría Tidal para esa pista, sin descargarla."""
    c = tienda.cliente('tidal')
    p = c._get(f'tracks/{pista_id}/playbackinfopostpaywall', audioquality='HI_RES_LOSSLESS', playbackmode='STREAM',
               assetpresentation='FULL')
    bits, sr = p.get('bitDepth'), p.get('sampleRate')
    if not sr and p.get('manifest'):
        try:
            import base64
            mpd = base64.b64decode(p['manifest']).decode('utf-8', 'replace')
            m = re.search(r'audioSamplingRate="(\d+)"', mpd)
            sr = int(m.group(1)) if m else None
        except Exception:  # noqa: BLE001
            pass
    if not bits:
        bits = 24 if p.get('audioQuality') == 'HI_RES_LOSSLESS' else (16 if p.get('audioQuality') == 'LOSSLESS' else None)
    return bits, sr, p.get('audioQuality')


# ─────────────────────────── plan ───────────────────────────

def _tags(ruta):
    try:
        import mutagen
        f = mutagen.File(ruta, easy=True)
        t = (f.tags or {}) if f is not None else {}
        return {k: (t.get(k) or [None])[0] for k in ('artist', 'albumartist', 'album', 'barcode', 'upc')}
    except Exception:  # noqa: BLE001
        return {}


def _ediciones(artista, album, n_pistas, upc=None):
    c = tienda.cliente('tidal')
    cands = c.buscar_albumes(f'{artista} {album}', limite=25)
    if not any(v.get('n_pistas') == n_pistas and rescate._parecido(v.get('titulo'), album) >= 0.9 for v in cands):
        try:
            _n, disco = c.buscar_artista(artista)
            vistos = {v['id'] for v in cands}
            cands += [v for v in disco if v['id'] not in vistos]
        except Exception:  # noqa: BLE001
            pass
    upc = str(upc or '').lstrip('0') or None
    salida = []
    for v in cands:
        mismo_upc = bool(upc) and str(v.get('upc') or '').lstrip('0') == upc
        if mismo_upc or (rescate._parecido(v.get('titulo'), album) >= 0.85 and rescate._parecido(v.get('artista'), artista) >= 0.8):
            salida.append(dict(v, como='mismo código de barras' if mismo_upc else 'mismo artista y título'))
    salida.sort(key=lambda v: (v['como'] != 'mismo código de barras', abs((v.get('n_pistas') or 0) - (n_pistas or 0)),
                               not v.get('hires')))
    return salida


def plan(rutas, db=None, edicion_id=None):
    """Qué se haría con cada pista dañada. `rutas`: archivos dañados (se resuelven los enlaces de una unión)."""
    rutas = [os.path.realpath(r) for r in rutas if r]
    if not rutas:
        raise Fallo('No hay pistas para reparar')
    faltan = [r for r in rutas if not os.path.isfile(r)]
    if faltan:
        raise Fallo(f'No está el archivo {os.path.basename(faltan[0])}')
    carpeta = analisis.carpeta_album(os.path.dirname(rutas[0]))
    n_pistas = len(tienda._audios(carpeta))
    t = _tags(rutas[0])
    artista, album = t.get('albumartist') or t.get('artist') or '', t.get('album') or ''
    if not artista or not album:
        raise Fallo('La pista no trae artista o álbum en sus tags: no se puede buscar en Tidal')
    album_q = calidad_album(carpeta, db, excluir=set(rutas))
    try:
        tienda.preparar_tidal()                         # sesión PKCE renovada (la búsqueda también la usa)
        eds = _ediciones(artista, album, n_pistas, t.get('barcode') or t.get('upc'))
    except Exception as e:  # noqa: BLE001
        raise Fallo(f'No se pudo buscar en Tidal: {type(e).__name__}: {e}')
    if not eds:
        raise Fallo(f'Tidal no tiene «{artista} — {album}»')
    ed = next((e for e in eds if e['id'] == str(edicion_id)), None) if edicion_id else eds[0]
    if not ed:
        raise Fallo('Esa edición ya no aparece en Tidal')
    pistas_t = tienda.cliente('tidal').pistas(ed['id'])
    durs = {}
    if db and os.path.isfile(db):
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
        try:
            durs = {fp: d for fp, d in con.execute(
                f'SELECT file_path, duration FROM tracks WHERE file_path IN ({",".join("?" * len(rutas))})', rutas)}
        finally:
            con.close()
    items = []
    for r in rutas:
        f = rescate.ficha(r)
        f['duracion'] = durs.get(r) or f.get('duracion')
        b, s = calidad_archivo(r)
        it = {'ruta': r, 'archivo': os.path.relpath(r, carpeta), 'titulo': f.get('titulo'), 'disco': f.get('disco'),
              'numero': f.get('numero'), 'duracion': f.get('duracion'), 'bits': b, 'sr': s,
              'calidad': etiqueta(b, s), 'reparable': True, 'motivo': None, 'pareja': None}
        if os.path.splitext(r)[1].lower() != '.flac':
            it.update(reparable=False, motivo=f'es {os.path.splitext(r)[1].lstrip(".").upper()}: Tidal entrega FLAC; '
                                              're-descarga el álbum completo')
        else:
            p = rescate.emparejar(f, pistas_t)
            if not p:
                it.update(reparable=False, motivo='no hay una pista segura en esa edición (disco, número, título y duración)')
            else:
                try:
                    tb, ts, aq = calidad_tidal(p['id'])
                except Exception as e:  # noqa: BLE001
                    tb, ts, aq = None, None, f'{type(e).__name__}'
                if aq and aq not in ('HI_RES_LOSSLESS', 'LOSSLESS'):
                    it.update(reparable=False, motivo=f'Tidal la entrega con pérdida ({aq})')
                it['pareja'] = {'id': p['id'], 'titulo': p.get('titulo'), 'disco': p.get('disco'), 'numero': p.get('numero'),
                                'duracion': p.get('duracion'), 'bits': tb, 'sr': ts, 'calidad': etiqueta(tb, ts)}
                it['igual'] = (tb, ts) == tuple(album_q) if album_q[1] else (tb, ts) == (b, s)
        items.append(it)
    return {'carpeta': carpeta, 'artista': artista, 'album': album, 'n_pistas': n_pistas,
            'calidad_album': etiqueta(*album_q) if album_q[1] else None, 'bits_album': album_q[0], 'sr_album': album_q[1],
            'edicion': {k: ed.get(k) for k in ('id', 'titulo', 'artista', 'anio', 'n_pistas', 'hires', 'como', 'url')},
            'ediciones': [{k: e.get(k) for k in ('id', 'titulo', 'artista', 'anio', 'n_pistas', 'hires', 'como')} for e in eds[:15]],
            'pistas': items}


# ─────────────────────────── reparar una pista ───────────────────────────

def _replaygain(ruta):
    """(ganancia dB, pico) de la pista según el filtro replaygain de ffmpeg, o (None, None)."""
    r = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-nostats', '-i', ruta, '-map', '0:a:0', '-af', 'replaygain',
                        '-f', 'null', '-'], capture_output=True, text=True, timeout=1800, errors='replace')
    g = re.search(r'track_gain\s*=\s*([+-]?[\d.]+)\s*dB', r.stderr or '')
    p = re.search(r'track_peak\s*=\s*([\d.]+)', r.stderr or '')
    return (float(g.group(1)) if g else None), (float(p.group(1)) if p else None)


def copiar_tags(vieja, nueva, sufijo=None):
    """Todos los tags y la carátula de la vieja a la nueva; ReplayGain de pista recalculado; sufijo de calidad."""
    from mutagen.flac import FLAC
    try:
        v = FLAC(vieja)
    except Exception as e:  # noqa: BLE001
        raise Fallo(f'no se pudieron leer los tags de la pista vieja ({type(e).__name__}): no se reemplaza')
    n = FLAC(nueva)
    n.clear_pictures()
    if n.tags is not None:
        n.tags.clear()
    else:
        n.add_tags()
    tags = v.tags.as_dict() if v.tags is not None else {}
    if not tags.get('title'):
        raise Fallo('la pista vieja no trae título en sus tags: no se reemplaza')
    for k, vals in tags.items():
        n[k] = vals
    for pic in v.pictures:
        n.add_picture(pic)
    if any(k.lower().startswith('replaygain_track') for k in tags):
        g, p = _replaygain(nueva)
        if g is not None:
            n['replaygain_track_gain'] = [f'{g:+.2f} dB']
        if p is not None:
            n['replaygain_track_peak'] = [f'{p:.6f}']
    titulos = [SUFIJO_RE.sub('', t) for t in tags.get('title', [])]
    n['title'] = [f'{t} ({sufijo})' for t in titulos] if sufijo else titulos
    n.save()
    return n['title'][0]


def reparar_pista(it, job, aj, reg, salud=None):
    """Baja, verifica, pasa la metadata y reemplaza. Devuelve el título final; Fallo si algo no cuadra."""
    ruta = it['ruta']
    if not os.path.isfile(ruta):
        raise Fallo('la pista vieja ya no está')
    tmp = tempfile.mkdtemp(prefix='reparar-', dir=os.path.dirname(os.path.abspath(salud.ruta)) if salud else None)
    try:
        nuevo = rescate.bajar_pista('tidal', it['pareja']['id'], tmp)
        if not nuevo:
            raise Fallo('Tidal no entregó la pista (' + rescate.ultima_salida.get(str(it['pareja']['id']), '') + ')')
        codec = (tienda.codec(nuevo) or '').lower()
        if codec != 'flac':
            raise Fallo(f'llegó en {codec.upper() or "?"}, no en FLAC')
        ok, errores, _pico, seg = analisis.decodificar(nuevo)
        if not ok:
            raise Fallo('la pista nueva también trae errores: ' + '; '.join(errores[:2]))
        ref = it.get('duracion') or (it.get('pareja') or {}).get('duracion')
        if ref and seg is not None and abs(seg - float(ref)) > rescate.TOLERANCIA_S:
            raise Fallo(f'dura {seg:.0f} s y la vieja {float(ref):.0f} s: no es la misma pista')
        b, s = calidad_archivo(nuevo)
        album_q = (job.get('bits_album'), job.get('sr_album'))
        distinta = bool(album_q[1]) and (b, s) != album_q
        if distinta and not it.get('acepto_calidad'):
            raise Fallo(f'llegó en {etiqueta(b, s)} y el álbum es {etiqueta(*album_q)}: no aceptaste otra calidad')
        titulo = copiar_tags(ruta, nuevo, etiqueta(b, s) if distinta else None)
        # al lado de la vieja (mismo disco) y recién ahí reemplaza: la vieja se borra solo si todo lo anterior anduvo
        provisorio = ruta + '.reparando'
        shutil.copyfile(nuevo, provisorio)
        if os.path.getsize(provisorio) != os.path.getsize(nuevo):
            os.remove(provisorio)
            raise Fallo('la copia a la biblioteca quedó incompleta')
        os.replace(provisorio, ruta)
        reg(f'   ✓ {it["archivo"]}: reemplazada por la de Tidal ({etiqueta(b, s)}){" · título: " + titulo if distinta else ""}')
        _orbyte(ruta, aj, b, s, reg)
        if salud:
            st = os.stat(ruta)
            salud._guardar(ruta, None, st.st_size, st.st_mtime, 'ok', ['reparada con la pista de Tidal'], seg, ref)
        return titulo
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _orbyte(ruta, aj, bits, sr, reg):
    """Orbyte relee los datos técnicos de la pista (mismo ID: escuchas y favoritos intactos) y su LED."""
    db = aj.get('db_orbyte')
    if not db or not os.path.isfile(db):
        return
    con = sqlite3.connect(db, timeout=60)
    try:
        fila = con.execute('SELECT t.id, al.path FROM tracks t JOIN albums al ON al.id = t.album_id WHERE t.file_path=?',
                           (ruta,)).fetchone()
    finally:
        con.close()
    if not fila:
        return                                          # todavía no está en Orbyte (álbum en la Bandeja)
    from . import metadata
    try:
        metadata._correr('index', ['index_libraryV3.py', '--update', '--album', fila[1], '--db', db, '--root',
                                   aj.get('biblioteca') or '/mnt/musica', '--quiet'], reg, aj, metadata.LIMITE_S['index'])
    except Exception as e:  # noqa: BLE001
        reg(f'   ⚠ Orbyte no releyó el álbum ({e}); se verá al correr la metadata')
    con = sqlite3.connect(db, timeout=60)
    try:
        with con:                                       # el indexador no toca el LED (campo curado): FLAC PCM
            con.execute('UPDATE tracks SET led_color=? WHERE id=?', ('white' if (sr or 0) > 48000 else 'yellow', fila[0]))
    finally:
        con.close()


# ─────────────────────────── cola de trabajos ───────────────────────────

class Reparador:
    def __init__(self, salud, ajustes_fn, ocupado_tidal_fn, al_terminar=None, avisar_fn=None):
        self.salud, self.ajustes_fn, self.ocupado_tidal_fn = salud, ajustes_fn, ocupado_tidal_fn
        self.al_terminar, self.avisar_fn = al_terminar, avisar_fn
        self._ev = threading.Event()
        self.actual = None
        with salud._con() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS reparaciones(id INTEGER PRIMARY KEY AUTOINCREMENT, creado REAL,
                         terminado REAL, estado TEXT, titulo TEXT, carpeta TEXT, aid INTEGER, trabajo TEXT, registro TEXT)''')
            # lo que quedó «reparando» al apagarse vuelve a la cola (cada pista se rehace desde la descarga)
            c.execute("UPDATE reparaciones SET estado='en_cola' WHERE estado='reparando'")

    def encolar(self, p, elegidas, aid=None):
        """`p`: el plan; `elegidas`: {ruta: {'acepto_calidad': bool}} de las pistas que se reparan."""
        pistas = []
        for it in p['pistas']:
            if it['ruta'] in elegidas and it.get('reparable') and it.get('pareja'):
                pistas.append(dict(it, acepto_calidad=bool(elegidas[it['ruta']].get('acepto_calidad'))))
        if not pistas:
            raise Fallo('No hay pistas para reparar')
        trabajo = {'pistas': pistas, 'bits_album': p.get('bits_album'), 'sr_album': p.get('sr_album'),
                   'edicion': p.get('edicion')}
        with self.salud._con() as c:
            cur = c.execute('INSERT INTO reparaciones(creado, estado, titulo, carpeta, aid, trabajo, registro) '
                            "VALUES(?, 'en_cola', ?, ?, ?, ?, '')",
                            (time.time(), f'{p["artista"]} — {p["album"]}', p['carpeta'], aid, json.dumps(trabajo)))
            jid = cur.lastrowid
        self._ev.set()
        return jid

    def lista(self, aid=None, limite=30):
        with self.salud._con() as c:
            q = 'SELECT id, creado, terminado, estado, titulo, carpeta, aid, trabajo, registro FROM reparaciones'
            filas = c.execute(q + (' WHERE aid=?' if aid else '') + ' ORDER BY id DESC LIMIT ?',
                              ((aid, limite) if aid else (limite,))).fetchall()
        out = []
        for r in filas:
            t = json.loads(r['trabajo'] or '{}')
            out.append({'id': r['id'], 'creado': r['creado'], 'terminado': r['terminado'], 'estado': r['estado'],
                        'titulo': r['titulo'], 'carpeta': r['carpeta'], 'aid': r['aid'],
                        'pistas': [x['archivo'] for x in t.get('pistas', [])], 'registro': r['registro'] or ''})
        return out

    def quitar(self, jid):
        with self.salud._con() as c:
            c.execute("DELETE FROM reparaciones WHERE id=? AND estado != 'reparando'", (jid,))

    def start(self):
        threading.Thread(target=self._bucle, daemon=True, name='reparar').start()

    def _bucle(self):
        while True:
            try:
                espera = self._uno()
            except Exception:  # noqa: BLE001
                log.exception('Reparar: error inesperado')
                espera = 60
            self._ev.wait(espera)
            self._ev.clear()

    def _uno(self):
        with self.salud._con() as c:
            r = c.execute("SELECT * FROM reparaciones WHERE estado='en_cola' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return 60
        if self.ocupado_tidal_fn():
            return 30                                   # una descarga usa la misma sesión de Tidal
        jid, trabajo = r['id'], json.loads(r['trabajo'] or '{}')
        self.actual = jid
        registro = []

        def reg(m):
            registro.append(m)
            with self.salud._con() as c:
                c.execute('UPDATE reparaciones SET registro=? WHERE id=?', ('\n'.join(registro), jid))

        with self.salud._con() as c:
            c.execute("UPDATE reparaciones SET estado='reparando', registro='' WHERE id=?", (jid,))
        aj = self.ajustes_fn()
        ed = trabajo.get('edicion') or {}
        reg(f'Reparar {len(trabajo["pistas"])} pista(s) con Tidal · edición «{ed.get("titulo")}»'
            f'{" (" + str(ed.get("anio")) + ")" if ed.get("anio") else ""} · {ed.get("como") or ""}')
        bien, mal = 0, 0
        for i, it in enumerate(trabajo['pistas']):
            if i:
                time.sleep(PAUSA_S)
            reg(f'· {it["archivo"]} → Tidal {it["pareja"].get("disco") or 1}-{it["pareja"].get("numero")} '
                f'«{it["pareja"].get("titulo")}»')
            try:
                reparar_pista(it, trabajo, aj, reg, self.salud)
                bien += 1
            except Fallo as e:
                mal += 1
                reg(f'   ✗ no se reemplazó: {e}')
            except Exception as e:  # noqa: BLE001
                mal += 1
                log.exception('Reparar %s', it['ruta'])
                reg(f'   ✗ error: {type(e).__name__}: {e} (la pista vieja quedó como estaba)')
        estado = 'hecho' if not mal else ('parcial' if bien else 'error')
        reg(f'Listo: {bien} reemplazada(s){f", {mal} sin reemplazar" if mal else ""}')
        with self.salud._con() as c:
            c.execute('UPDATE reparaciones SET estado=?, terminado=? WHERE id=?', (estado, time.time(), jid))
        self.actual = None
        if self.avisar_fn:
            self.avisar_fn('ok' if not mal else 'revision', f'Reparar pistas: {r["titulo"]}',
                           f'{bien} reemplazada(s)' + (f', {mal} sin reemplazar' if mal else ''), r['aid'])
        if self.al_terminar and r['aid'] and bien:
            try:
                self.al_terminar(r['aid'])               # el álbum de la Bandeja se vuelve a revisar
            except Exception:  # noqa: BLE001
                log.exception('Reparar: no se pudo volver a revisar el álbum %s', r['aid'])
        return 1
