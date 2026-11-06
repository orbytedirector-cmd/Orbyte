"""
Orbyte Ingesta — Calidad y LED de la biblioteca.

El LED es la clasificación de Orbyte: dos pistas con los mismos bits y frecuencia pueden ser una MQA y la otra no.
El indexador decide el MQA por los tags, y Tidal dejó de ponerlos: hay MQA (el DAC los reconoce) que Orbyte ve
como CD/HR. Aquí se compara, pista por pista:

  · lo asignado en Orbyte (LED, MQA, frecuencia original, bits/frecuencia);
  · lo que dice el archivo (bits/frecuencia de la cabecera y los tags MQA);
  · lo que se detecta en el audio (mqa.detectar: firma MQA, Studio y frecuencia original);

y se calcula el LED que corresponde con la misma regla del indexador. Aplicar escribe los tags MQA en el archivo
(o los quita), reindexa el álbum y pone el LED (el indexador no lo actualiza: es un campo curado). El audio no se
toca.

La detección se guarda por la huella del audio del FLAC (MD5 de STREAMINFO): cambiar tags no obliga a volver a
analizar. La verificación de toda la biblioteca usa la misma maquinaria de Salud (de fondo, pausable, retoma donde
quedó) con su propia base, ingesta_datos/calidad.db.
"""
import json
import logging
import os
import sqlite3
import threading
import time

from . import mqa, salud as salud_mod

log = logging.getLogger('ingesta.calidad')

V = 'c3'                    # versión del criterio (c2: LED por la firma; c3: MQB coherente en tags y Orbyte)
DV = 'c1'                   # versión de la detección en caché (la firma no cambió: no se vuelve a decodificar)
ESTADOS = ('ok', 'distinto', 'duda', 'falta', 'omitida')
_COLS = ('t.id, t.file_path, t.album_id, t.title, t.track_number, t.disc_number, t.sample_rate, t.bit_depth, '
         't.is_mqa, t.mqa_type, t.mqa_original_sample_rate, t.led_color, t.is_dsd, t.dsd_rate, t.codec, '
         't.compression_mode')
_lock_aplicar = threading.Lock()


def _int(v):
    try:
        return int(str(v).strip()) if v not in (None, '') else None
    except ValueError:
        return None


# ─────────────────────────── una pista ───────────────────────────

def _archivo(ruta):
    """Cabecera del archivo: {'sr', 'bits', 'md5', 'flac'} (FLAC por mutagen; el resto, sin detalle)."""
    try:
        from mutagen.flac import FLAC
        f = FLAC(ruta)
        return {'flac': True, 'sr': f.info.sample_rate, 'bits': f.info.bits_per_sample,
                'md5': f'{f.info.md5_signature:032x}' if f.info.md5_signature else None,
                'canales': f.info.channels}
    except Exception:  # noqa: BLE001
        return {'flac': False}


class Detecciones:
    """Caché de mqa.detectar por la huella del audio (sobrevive a cambios de tags)."""

    def __init__(self, ruta_db):
        self.ruta = ruta_db
        with self._con() as c:
            c.execute('CREATE TABLE IF NOT EXISTS deteccion(clave TEXT PRIMARY KEY, v TEXT, resultado TEXT, cuando REAL)')

    def _con(self):
        c = sqlite3.connect(self.ruta, timeout=60)
        c.row_factory = sqlite3.Row
        return c

    def obtener(self, ruta, arch, forzar=False, prefijo=None):
        clave = arch.get('md5') or f'{ruta}:{os.path.getsize(ruta)}'
        if not forzar:
            c = self._con()
            try:
                r = c.execute('SELECT resultado FROM deteccion WHERE clave=? AND v=?', (clave, DV)).fetchone()
            finally:
                c.close()
            if r:
                return json.loads(r['resultado'])
        d = mqa.detectar(ruta, prefijo=prefijo)
        if not d.get('error'):
            c = self._con()
            try:
                with c:
                    c.execute('INSERT OR REPLACE INTO deteccion(clave, v, resultado, cuando) VALUES(?,?,?,?)',
                              (clave, DV, json.dumps(d), time.time()))
            finally:
                c.close()
        return d


def evaluar(fila, det_cache, forzar=False, prefijo=None):
    """Asignado (Orbyte) vs archivo vs detectado → estado, LED que corresponde y por qué."""
    ruta = fila['file_path']
    asignado = {'led': fila['led_color'], 'mqa': bool(fila['is_mqa']), 'studio': (fila['mqa_type'] or '') == 'studio',
                'mqb': bool(fila['is_mqa']) and (fila['mqa_type'] or '') == 'mqb',
                'sr_original': _int(fila['mqa_original_sample_rate']), 'sr': fila['sample_rate'],
                'bits': _int(fila['bit_depth'])}
    out = {'ruta': ruta, 'id': fila['id'], 'titulo': fila['title'], 'numero': fila['track_number'],
           'disco': fila['disc_number'], 'asignado': asignado, 'archivo': None, 'tags': None, 'detectado': None,
           'esperado': None, 'estado': 'ok', 'motivos': []}
    if not os.path.isfile(ruta):
        out.update(estado='falta', motivos=['el archivo no está'])
        return out
    lossy = (fila['compression_mode'] or '') == 'Lossy'
    if fila['is_dsd'] or lossy:
        led = mqa.led(False, False, fila['sample_rate'], None, fila['is_dsd'], fila['dsd_rate'])
        out['esperado'] = {'led': None if lossy else led, 'mqa': False}
        out['estado'] = 'omitida'
        out['motivos'] = ['con pérdida: no aplica' if lossy else 'DSD: no aplica MQA']
        return out
    arch = _archivo(ruta)
    out['archivo'] = arch
    out['tags'] = mqa.tags_mqa(ruta)
    if not arch.get('flac'):
        out.update(estado='omitida', motivos=['no es FLAC: el MQA solo se detecta en FLAC'])
        return out
    det = det_cache.obtener(ruta, arch, forzar=forzar, prefijo=prefijo)
    out['detectado'] = det
    sr = arch.get('sr') or fila['sample_rate']
    t = out['tags'] or {}
    # MQB (magenta) no se deduce del audio: se respeta cuando está marcado a mano (tags «MQB»), o cuando Orbyte lo
    # tiene en magenta sin firma MQA en el audio (puesto antes de existir la marca: se propone dejarlo coherente).
    mqb = bool(t.get('mqb')) or (asignado['led'] == 'magenta' and not det.get('mqa') and not t.get('mqa'))
    if mqb:
        esp = {'mqa': True, 'studio': False, 'mqb': True,
               'sr_original': t.get('sr_original') or asignado['sr_original'] or det.get('sr_original')}
        out['nota'] = 'MQB (magenta) marcado a mano: el LED magenta no se deduce del audio'
    elif det.get('mqa'):
        esp = {'mqa': True, 'studio': bool(det.get('studio')), 'mqb': False, 'sr_original': det.get('sr_original')}
    else:
        # sin firma en el audio el DAC lo muestra como PCM normal aunque los tags digan MQA (verificado con el iFi)
        esp = {'mqa': False, 'studio': False, 'mqb': False, 'sr_original': None}
    esp['led'] = mqa.led(esp['mqa'], esp['studio'], sr, esp['sr_original'], mqb=esp['mqb'])
    out['esperado'] = esp
    if mqb:
        if not t.get('mqb'):
            out['motivos'].append('MQB: falta la marca en los tags del archivo')
        if not asignado['mqb']:
            out['motivos'].append('MQB: Orbyte no lo tiene registrado como MQB')
    elif t.get('mqa') and not det.get('mqa'):
        out['motivos'].append(f'los tags dicen MQA, pero el audio no trae la firma (el DAC lo muestra como '
                              f'{mqa.LED_NOMBRE.get(esp["led"], esp["led"])})')
    if arch.get('sr') and arch['sr'] != fila['sample_rate']:
        out['motivos'].append(f'Orbyte tiene {fila["sample_rate"]} Hz y el archivo {arch["sr"]} Hz')
    if arch.get('bits') and asignado['bits'] and arch['bits'] != asignado['bits']:
        out['motivos'].append(f'Orbyte tiene {asignado["bits"]} bits y el archivo {arch["bits"]}')
    if esp['mqa'] and not mqb:
        # los tags son lo que lee el indexador: si no coinciden, un reindexado perdería el MQA o el Studio
        if not t.get('mqa'):
            out['motivos'].append('los tags del archivo no dicen MQA (al reindexar se perdería)')
        elif bool(t.get('studio')) != esp['studio'] or t.get('mqb'):
            out['motivos'].append(f'los tags dicen {mqa.texto(t)}')
    if esp['mqa'] and not asignado['mqa'] and not mqb:
        out['motivos'].append(f'el audio es {mqa.texto(det)} y Orbyte no lo tiene como MQA')
    elif asignado['mqa'] and not esp['mqa']:
        if not t.get('mqa'):
            out['motivos'].append('Orbyte lo tiene como MQA y ni el audio ni los tags lo son')
    elif esp['mqa'] and not mqb:
        if esp['studio'] != asignado['studio']:
            out['motivos'].append('es MQA Studio' if esp['studio'] else 'no es MQA Studio')
        if esp['sr_original'] and esp['sr_original'] != asignado['sr_original'] and esp['sr_original'] != sr:
            out['motivos'].append(f'frecuencia original {esp["sr_original"]} Hz (Orbyte: '
                                  f'{asignado["sr_original"] or "—"})')
    if (asignado['led'] or '') != esp['led']:
        out['motivos'].append(f'LED {mqa.LED_NOMBRE.get(asignado["led"], asignado["led"] or "—")} → '
                              f'{mqa.LED_NOMBRE.get(esp["led"], esp["led"])}')
    if out['motivos']:
        out['estado'] = 'distinto'
    return out


# ─────────────────────────── un álbum ───────────────────────────

def filas_album(db, album_ids=None, rutas=None):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        if album_ids:
            q = (f'SELECT {_COLS} FROM tracks t WHERE t.album_id IN ({",".join("?" * len(album_ids))}) '
                 'ORDER BY CAST(t.disc_number AS INTEGER), CAST(t.track_number AS INTEGER), t.file_path')
            return [dict(r) for r in con.execute(q, list(album_ids))]
        out = []
        for i in range(0, len(rutas or []), 500):
            lote = rutas[i:i + 500]
            out += [dict(r) for r in con.execute(f'SELECT {_COLS} FROM tracks t WHERE t.file_path IN '
                                                 f'({",".join("?" * len(lote))})', lote)]
        return out
    finally:
        con.close()


def revisar_album(db, album_ids, det_cache, forzar=False):
    pistas = [evaluar(f, det_cache, forzar=forzar) for f in filas_album(db, album_ids)]
    return {'pistas': pistas, 'resumen': resumen(pistas)}


def resumen(pistas):
    """LED asignado y el que corresponde (por mayoría) y cuántas pistas difieren."""
    def mayoria(vals):
        vals = [v for v in vals if v]
        return max(set(vals), key=vals.count) if vals else None
    return {'asignado': mayoria([p['asignado']['led'] for p in pistas]),
            'esperado': mayoria([(p['esperado'] or {}).get('led') for p in pistas]),
            'distintas': sum(1 for p in pistas if p['estado'] == 'distinto'),
            'dudas': sum(1 for p in pistas if p['estado'] == 'duda'),
            'mqa_detectado': sum(1 for p in pistas if (p['detectado'] or {}).get('mqa')),
            'n': len(pistas)}


# ─────────────────────────── aplicar ───────────────────────────

def aplicar(aj, decisiones, reg):
    """`decisiones`: {ruta: {'mqa': bool, 'studio': bool, 'sr_original': int|None}}. Escribe los tags, reindexa los
    álbumes tocados y pone el LED que corresponde. Devuelve {'cambiadas', 'avisos', 'errores'}."""
    from . import metadata
    db = aj.get('db_orbyte')
    if not decisiones:
        return {'cambiadas': 0, 'avisos': [], 'errores': []}
    if not _lock_aplicar.acquire(blocking=False):
        raise RuntimeError('Ya se está aplicando otro ajuste de calidad: espera a que termine')
    try:
        filas = {f['file_path']: f for f in filas_album(db, rutas=list(decisiones))}
        avisos, errores, carpetas = [], [], set()
        for ruta, d in decisiones.items():
            f = filas.get(ruta)
            if not f:
                errores.append(f'{os.path.basename(ruta)}: no está en Orbyte')
                continue
            if not ruta.lower().endswith('.flac'):
                errores.append(f'{os.path.basename(ruta)}: no es FLAC (los tags MQA solo se escriben en FLAC)')
                continue
            try:
                for a in mqa.escribir(ruta, bool(d.get('mqa')), bool(d.get('studio')), _int(d.get('sr_original')),
                                      mqb=bool(d.get('mqa') and d.get('mqb'))):
                    avisos.append(f'{os.path.basename(ruta)}: {a}')
                reg(f'· {os.path.basename(ruta)}: '
                    + (mqa.texto({'mqa': True, 'studio': d.get('studio'), 'mqb': d.get('mqb'),
                                  'sr_original': _int(d.get('sr_original'))}) if d.get('mqa') else 'sin MQA'))
                carpetas.add(f['album_id'])
            except Exception as e:  # noqa: BLE001
                errores.append(f'{os.path.basename(ruta)}: {type(e).__name__}: {e}')
        # reindexar cada álbum tocado (mismo ID de pista: escuchas y favoritos intactos)
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
        try:
            rutas_alb = [r[0] for r in con.execute(
                f'SELECT path FROM albums WHERE id IN ({",".join("?" * len(carpetas))})', list(carpetas))] if carpetas else []
        finally:
            con.close()
        for carpeta in rutas_alb:
            try:
                metadata._correr('index', ['index_libraryV3.py', '--update', '--album', carpeta, '--db', db, '--root',
                                           aj.get('biblioteca') or '/mnt/musica', '--quiet'], reg, aj,
                                 metadata.LIMITE_S['index'])
            except Exception as e:  # noqa: BLE001
                errores.append(f'Orbyte no releyó {carpeta}: {e}')
        # el LED (el indexador no lo toca en --update): mismo cálculo que hace al indexar por primera vez
        nuevas = filas_album(db, rutas=[r for r in decisiones if r in filas])
        con = sqlite3.connect(db, timeout=60)
        cambiadas = 0
        try:
            with con:
                for f in nuevas:
                    led = mqa.led(bool(f['is_mqa']), (f['mqa_type'] or '') == 'studio', f['sample_rate'],
                                  f['mqa_original_sample_rate'], f['is_dsd'], f['dsd_rate'],
                                  mqb=(f['mqa_type'] or '') == 'mqb')
                    con.execute('UPDATE tracks SET led_color=? WHERE id=?', (led, f['id']))
                    cambiadas += 1
        finally:
            con.close()
        reg(f'Listo: {cambiadas} pista(s) con su LED al día' + (f' · {len(errores)} con error' if errores else ''))
        return {'cambiadas': cambiadas, 'avisos': avisos, 'errores': errores}
    finally:
        _lock_aplicar.release()


def decisiones_detectadas(pistas):
    """Lo que hace «Aplicar lo detectado»: solo las pistas «distinto» (las dudas se deciden a mano)."""
    out = {}
    for p in pistas:
        if p['estado'] != 'distinto' or not p.get('esperado'):
            continue
        e = p['esperado']
        out[p['ruta']] = {'mqa': e['mqa'], 'studio': e.get('studio'), 'mqb': e.get('mqb'),
                          'sr_original': e.get('sr_original')}
    return out


# ─────────────────────────── toda la biblioteca ───────────────────────────

class Verificador(salud_mod.Salud):
    """La verificación de fondo: misma maquinaria que Salud (criterio, pausa, ritmo, retoma) con su propia base;
    el ritmo (pistas a la vez, pausa, horario, escuchas) es el mismo de Salud."""
    ARCHIVO = 'calidad.db'
    V = V
    PROBLEMAS = ('distinto', 'duda')
    NOMBRE = 'Calidad y LED'

    def __init__(self, datos, ajustes_fn, ocupado_fn, salud, avisar_fn=None):
        self.salud = salud
        super().__init__(datos, ajustes_fn, ocupado_fn, avisar_fn)
        self.det = Detecciones(self.ruta)

    def estado(self):
        st = super().estado()
        ritmo = self.salud.estado()
        for k in salud_mod.AJUSTES_DEFECTO:
            st[k] = ritmo.get(k)
        return st

    def _revisar(self, p):
        ruta = p['ruta']
        filas = filas_album(self._db_orbyte(), rutas=[ruta])
        if not filas:
            self._contar(p)
            return
        r = evaluar(filas[0], self.det, prefijo=salud_mod._prefijo())
        try:
            st = os.stat(ruta)
            tam, mt = st.st_size, st.st_mtime
        except OSError:
            tam = mt = None
        self._guardar(ruta, p['album_id'], tam, mt, r['estado'], [r], None, p['duracion'])
        self._contar(p)

    def actualizar_criterio(self):
        """Al cambiar el criterio (V): re-evalúa lo ya verificado con el criterio anterior. La detección sale
        de la caché (DV), así que no se vuelve a decodificar audio: solo se leen tags y cabeceras."""
        try:
            with self._con() as c:
                rutas = [r[0] for r in c.execute('SELECT ruta FROM pistas WHERE v IS NOT ? ORDER BY ruta', (self.V,))]
            for k in range(0, len(rutas), 200):
                self.reevaluar(rutas[k:k + 200])
            if rutas:
                log.info('Calidad y LED: %d pistas re-evaluadas con el criterio %s', len(rutas), self.V)
        except Exception:  # noqa: BLE001
            log.exception('Calidad y LED: re-evaluar con el criterio nuevo')

    def reevaluar(self, rutas):
        """Tras aplicar: vuelve a evaluar esas pistas (la detección sale de la caché)."""
        for f in filas_album(self._db_orbyte(), rutas=list(rutas)):
            r = evaluar(f, self.det)
            try:
                st = os.stat(f['file_path'])
                tam, mt = st.st_size, st.st_mtime
            except OSError:
                tam = mt = None
            self._guardar(f['file_path'], f['album_id'], tam, mt, r['estado'], [r], None, 0)

    def diferencias(self, ver_ignorados=False):
        """Álbumes con pistas cuyo LED/MQA no corresponde (o en duda), agrupados por carpeta."""
        with self._con() as c:
            filas = [dict(r) for r in c.execute(
                "SELECT ruta, album_id, estado, errores FROM pistas WHERE estado IN ('distinto', 'duda')")]
            ign = {r['album'] for r in c.execute('SELECT album FROM ignorados')}
        if not filas:
            return []
        db = self._db_orbyte()
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            ids = sorted({f['album_id'] for f in filas if f['album_id']})
            albumes = {}
            for k in range(0, len(ids), 500):
                parte = ids[k:k + 500]
                for r in con.execute(f'SELECT al.id, al.name, al.year, al.path, ar.name AS artista FROM albums al '
                                     f'LEFT JOIN artists ar ON ar.id = al.artist_id WHERE al.id IN '
                                     f'({",".join("?" * len(parte))})', parte):
                    albumes[r['id']] = dict(r)
            vigentes = set()
            rutas = [f['ruta'] for f in filas]
            for k in range(0, len(rutas), 500):
                parte = rutas[k:k + 500]
                vigentes |= {r[0] for r in con.execute(f'SELECT file_path FROM tracks WHERE file_path IN '
                                                       f'({",".join("?" * len(parte))})', parte)}
        finally:
            con.close()
        por = {}
        for f in filas:
            a = albumes.get(f['album_id'])
            if not a or f['ruta'] not in vigentes:
                continue
            if (a['path'] in ign) != bool(ver_ignorados):
                continue
            g = por.setdefault(a['path'], {'carpeta': a['path'], 'album_id': a['id'], 'artista': a['artista'],
                                           'album': a['name'], 'anio': a['year'], 'pistas': []})
            r = (json.loads(f['errores'] or '[]') or [{}])[0]
            g['pistas'].append({k: r.get(k) for k in ('ruta', 'titulo', 'numero', 'disco', 'asignado', 'esperado',
                                                      'detectado', 'estado', 'motivos')})
        out = list(por.values())
        for g in out:
            g['resumen'] = resumen(g['pistas'])
        out.sort(key=lambda g: ((g['artista'] or '').lower(), g['album'] or ''))
        return out

    def _terminar(self):
        criterio = json.loads(self.estado().get('criterio') or '{}')
        cache = self._cache({p['ruta'] for p in self.cola})
        malas = [c for c in cache.values() if c['estado'] in self.PROBLEMAS]
        self.poner(activa='0', pausada='0', terminada=time.time(),
                   resumen=json.dumps({'pistas': len(self.cola), 'malas': len(malas)}))
        self.cola_clave = None
        self.motivo, self.actual = 'Terminada', None
        if self.avisar_fn:
            alcance = ', '.join(f'{k}: {v}' for k, v in criterio.items() if k != 'modo') or 'toda la biblioteca'
            self.avisar_fn('ok' if not malas else 'revision', 'Calidad y LED: verificación terminada',
                           f'{len(self.cola)} pistas ({alcance}) · {len(malas)} con el LED por corregir o en duda')
