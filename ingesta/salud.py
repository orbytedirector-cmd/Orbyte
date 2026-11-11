"""
Orbyte Ingesta — Salud de la biblioteca.

1) Discos por normalizar (se calcula al momento, desde music.db y las carpetas):
   - subcarpetas: «Álbum/CD 1», «Álbum/CD 2»… que Orbyte muestra como álbumes separados → Reprocesar los N discos;
   - separados: los discos de un mismo álbum en álbumes distintos, con evidencia de que son discos de uno (tags de
     disco 2+, total de discos en los tags o «CD 2»/«Disc 2» en el nombre) y del mismo año → Unir como discos.
     «Part 2», «Pt. 2» o «Vol. 2» no cuentan: suelen ser lanzamientos distintos;
   - numeración repetida: dos «01», dos «02»… sin tag de disco (discos mezclados en una carpeta) → Reprocesar;
   - discos faltantes: los tags dicen «disco 2 de 2» y no hay otro álbum con el resto.

2) Pistas dañadas: decodificación completa de cada pista (mismo criterio que la revisión de la Bandeja), en segundo
   plano y sin molestar:
   - un hilo de ffmpeg por pista, con prioridad mínima de CPU y de disco (nice 19 / ionice idle), y los hilos que
     elijas (1 por defecto) con una pausa entre pistas;
   - espera sola mientras la ingesta trabaja (revisando, ubicando, metadata, descargas), fuera del horario que elijas
     y, si quieres, mientras alguien escucha en Orbyte;
   - el resultado de cada pista se guarda en ingesta_datos/salud.db apenas termina (ruta + tamaño + fecha): pausar,
     detener, un error o reiniciar el servidor nunca hacen empezar de cero; solo se vuelve a decodificar lo que
     cambió;
   - se revisa por partes: texto (artista/álbum), letra, calidad, agregados desde una fecha, y «solo pendientes»,
     «todo de nuevo» o «solo las que dieron error».
"""
import contextlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import analisis, dsd, tags_mp3
from . import sanear as sanear_mod

log = logging.getLogger('ingesta.salud')

V = f'{analisis.DECODIFICAR_V}.1'      # versión del criterio: si cambia, lo revisado con la anterior queda pendiente
CALIDADES = ('CD', 'HR', 'DSD', 'MQA', 'Lossy')
MODOS = ('pendientes', 'todas', 'con_error')
PROBLEMAS = ('danada', 'errores', 'falta')
AJUSTES_DEFECTO = {'hilos': '1', 'pausa': '1', 'horario': '', 'escuchas': '1'}
LOSSY = {'mp3', 'aac', 'vorbis', 'opus', 'wmav1', 'wmav2', 'mp2'}


def _prefijo():
    p = []
    if shutil.which('ionice'):
        p += ['ionice', '-c', '3']
    if shutil.which('nice'):
        p += ['nice', '-n', '19']
    return p


def calidad_de(t):
    if t.get('is_dsd'):
        return 'DSD'
    if t.get('is_mqa'):
        return 'MQA'
    if (t.get('compression_mode') or '') == 'Lossy' or (t.get('codec') or '').lower() in LOSSY:
        return 'Lossy'
    return 'HR' if (t.get('sample_rate') or 0) > 48000 else 'CD'


CONFIRMADA = 'confirmado: el audio no coincide con la huella MD5 del FLAC'
PASAJERA = 'falla de lectura pasajera: el audio coincide con su huella MD5'


def huella_flac(ruta, prefijo=None):
    """¿El audio decodificado coincide con la huella MD5 que el FLAC guarda de su audio original?
    True / False; None si no se puede saber (no es FLAC, sin huella o profundidad rara)."""
    try:
        from mutagen.flac import FLAC
        info = FLAC(ruta).info
        md5, bits = info.md5_signature, info.bits_per_sample
    except Exception:  # noqa: BLE001
        return None
    if not md5 or bits not in (16, 24):
        return None
    cmd = list(prefijo or []) + ['ffmpeg', '-nostdin', '-v', 'quiet'] + (['-threads', '1'] if prefijo else []) + \
        ['-i', ruta, '-map', '0:a:0', '-c:a', f'pcm_s{bits}le', '-f', 'md5', '-']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception:  # noqa: BLE001
        return None
    salida = (r.stdout or '').strip()
    if not salida.startswith('MD5='):
        return False if r.returncode else None
    return salida[4:].lower() == f'{md5:032x}'


def confirmar(ruta, prefijo=None):
    """Segunda opinión sobre una pista con errores. → (True dañada | False sana | None sin saber, nota).
    FLAC con huella: se compara contra ella. Sin huella: otra decodificación; solo cuenta si los errores se repiten."""
    h = huella_flac(ruta, prefijo)
    if h is True:
        return False, PASAJERA
    if h is False:
        return True, CONFIRMADA
    ok, _e, _p, _s = analisis.decodificar(ruta, prefijo=prefijo)
    if ok:
        return False, 'falla de lectura pasajera: sin huella MD5; una segunda decodificación salió limpia'
    return True, 'confirmado: sin huella MD5 que comparar; los errores se repiten en una segunda decodificación'


def clasificar(ruta, ok, errores, segundos, duracion):
    """('ok' | 'errores' | 'danada', notas): «errores» = el audio se leyó completo pero con cuadros malos."""
    if ok:
        return 'ok', []
    completo = segundos is not None and bool(duracion) and segundos >= duracion - 0.5
    ext = os.path.splitext(ruta)[1].lower()
    if completo and ext == '.flac' and any(analisis.id3_en_flac(ruta)):
        return 'ok', ['tags ID3 pegados al FLAC (el audio está entero)']
    if completo and ext == '.dsf':
        try:
            if dsd.info(ruta).get('irregular'):
                return 'ok', ['DSF con datos de más tras el audio (el audio está entero)']
        except Exception:  # noqa: BLE001
            pass
    if not duracion and segundos:
        return 'errores', errores          # sin duración en music.db: no se puede saber si está incompleta
    return ('errores' if completo else 'danada'), errores


class Salud:
    ARCHIVO = 'salud.db'         # calidad.Verificador reutiliza esta maquinaria con otra base y otro criterio
    V = V
    PROBLEMAS = PROBLEMAS
    NOMBRE = 'Salud'

    def __init__(self, datos, ajustes_fn, ocupado_fn, avisar_fn=None):
        self.ruta = os.path.join(datos, self.ARCHIVO)
        self.ajustes_fn, self.ocupado_fn, self.avisar_fn = ajustes_fn, ocupado_fn, avisar_fn
        self._lock = threading.Lock()
        self._despertar = threading.Event()
        self.motivo = 'Detenida'
        self.actual = None
        self.cola, self.cola_clave, self.i, self.hechas = [], None, 0, 0
        self.seg_audio, self.seg_reloj = 0.0, 0.0          # ritmo de esta sesión (para el tiempo restante)
        self.restante_audio = 0.0
        with self._con() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS pistas(ruta TEXT PRIMARY KEY, album_id INTEGER, tamano INTEGER, mtime REAL,
                    v TEXT, estado TEXT, errores TEXT, segundos REAL, duracion REAL, revisado REAL);
                CREATE INDEX IF NOT EXISTS pistas_estado ON pistas(estado);
                CREATE TABLE IF NOT EXISTS ignorados(album TEXT PRIMARY KEY, desde REAL);
                CREATE TABLE IF NOT EXISTS estado(clave TEXT PRIMARY KEY, valor TEXT);
                CREATE TABLE IF NOT EXISTS medidas(ruta TEXT PRIMARY KEY, tamano INTEGER, mtime REAL, datos TEXT);''')

    # ── base propia ──
    @contextlib.contextmanager
    def _con(self):
        c = sqlite3.connect(self.ruta, timeout=60)
        c.row_factory = sqlite3.Row
        try:
            with c:              # confirma o deshace
                yield c
        finally:
            c.close()            # y cierra: si no, quedan abiertas hasta el recolector (Too many open files)

    def estado(self):
        with self._con() as c:
            d = {r['clave']: r['valor'] for r in c.execute('SELECT clave, valor FROM estado')}
        for k, v in AJUSTES_DEFECTO.items():
            d.setdefault(k, v)
        return d

    def poner(self, **kv):
        with self._lock, self._con() as c:
            for k, v in kv.items():
                c.execute('INSERT INTO estado(clave, valor) VALUES(?, ?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor',
                          (k, None if v is None else str(v)))
        self._despertar.set()

    def _guardar(self, ruta, album_id, tamano, mtime, estado, errores, segundos, duracion):
        with self._lock, self._con() as c:
            c.execute('INSERT OR REPLACE INTO pistas(ruta, album_id, tamano, mtime, v, estado, errores, segundos, duracion, '
                      'revisado) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                      (ruta, album_id, tamano, mtime, self.V, estado, json.dumps(errores[:4], ensure_ascii=False),
                       segundos, duracion, time.time()))

    # ── qué entra en una revisión ──
    def _db_orbyte(self):
        return self.ajustes_fn().get('db_orbyte')

    def pistas_de(self, criterio):
        """Pistas de music.db que cumplen el criterio, en orden de carpeta, disco y número."""
        aj = self.ajustes_fn()
        db = aj.get('db_orbyte')
        raiz = os.path.abspath(aj.get('biblioteca') or '/mnt/musica')
        criterio = criterio or {}
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            sql = ('SELECT t.file_path AS ruta, t.album_id, t.duration, t.sample_rate, t.is_dsd, t.is_mqa, t.codec, '
                   't.compression_mode, al.name AS album, al.path AS carpeta, ar.name AS artista '
                   'FROM tracks t JOIN albums al ON al.id = t.album_id LEFT JOIN artists ar ON ar.id = al.artist_id '
                   'WHERE t.file_path IS NOT NULL')
            params = []
            if criterio.get('desde'):
                sql += ' AND al.created_at >= ?'
                params.append(str(criterio['desde']))
            sql += ' ORDER BY al.path, CAST(t.disc_number AS INTEGER), CAST(t.track_number AS INTEGER)'
            filas = [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()
        texto = analisis._norm(criterio.get('texto') or '')
        palabras = texto.split()
        letra = (criterio.get('letra') or '').strip()
        cal = criterio.get('calidad') or ''
        out = []
        for f in filas:
            if palabras:
                h = analisis._norm(f'{f.get("artista") or ""} {f.get("album") or ""}')
                if not all(p in h for p in palabras):
                    continue
            if letra:
                rel = os.path.relpath(f['ruta'], raiz).split(os.sep) if f['ruta'].startswith(raiz + os.sep) else ['']
                if rel[0].upper() != letra.upper():
                    continue
            if cal and calidad_de(f) != cal:
                continue
            out.append({'ruta': f['ruta'], 'album_id': f['album_id'], 'duracion': f.get('duration') or 0})
        return out

    def estimar(self, criterio):
        ps = self.pistas_de(criterio)
        hechas = self._cache({p['ruta'] for p in ps})
        modo = (criterio or {}).get('modo') or 'pendientes'
        if modo == 'pendientes':
            pend = [p for p in ps if not (hechas.get(p['ruta']) and hechas[p['ruta']]['v'] == self.V)]
        elif modo == 'con_error':
            pend = [p for p in ps if hechas.get(p['ruta']) and hechas[p['ruta']]['estado'] in self.PROBLEMAS]
        else:
            pend = ps
        return {'pistas': len(ps), 'por_revisar': len(pend), 'horas_audio': round(sum(p['duracion'] for p in pend) / 3600, 1),
                'albumes': len({p['album_id'] for p in pend})}

    def _cache(self, rutas=None):
        with self._con() as c:
            filas = c.execute('SELECT ruta, tamano, mtime, v, estado, revisado FROM pistas').fetchall()
        return {r['ruta']: dict(r) for r in filas if rutas is None or r['ruta'] in rutas}

    # ── control ──
    def iniciar(self, criterio):
        criterio = {k: v for k, v in (criterio or {}).items() if k in ('texto', 'letra', 'calidad', 'desde', 'modo') and v}
        criterio.setdefault('modo', 'pendientes')
        if criterio['modo'] not in MODOS:
            raise ValueError('Modo desconocido')
        if criterio.get('calidad') and criterio['calidad'] not in CALIDADES:
            raise ValueError('Calidad desconocida')
        self.poner(criterio=json.dumps(criterio, ensure_ascii=False), activa='1', pausada='0', inicio=time.time(),
                   terminada=None, resumen=None)

    def pausar(self, si=True):
        self.poner(pausada='1' if si else '0')

    def detener(self):
        self.poner(activa='0', pausada='0')

    # ── por qué espera ──
    def _espera(self, st):
        if self.ocupado_fn():
            return 'Esperando: la ingesta está trabajando (revisión, ubicar, metadata o descarga)'
        h = (st.get('horario') or '').strip()
        m = re.fullmatch(r'(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})', h)
        if m:
            ahora = datetime.now().hour * 60 + datetime.now().minute
            ini, fin = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
            dentro = ini <= ahora < fin if ini <= fin else (ahora >= ini or ahora < fin)
            if not dentro:
                return f'Esperando: fuera del horario ({h})'
        if st.get('escuchas') == '1' and self._escuchando():
            return 'Esperando: alguien está escuchando en Orbyte'
        return None

    def _escuchando(self):
        try:
            con = sqlite3.connect(f'file:{self._db_orbyte()}?mode=ro', uri=True, timeout=10)
            try:
                return con.execute("SELECT 1 FROM listening_events WHERE created_at >= datetime('now', '-10 minutes') "
                                   'LIMIT 1').fetchone() is not None
            finally:
                con.close()
        except sqlite3.Error:
            return False

    # ── el trabajo ──
    def start(self):
        threading.Thread(target=self._bucle, daemon=True, name=self.ARCHIVO).start()

    def _bucle(self):
        while True:
            try:
                espera = self._ciclo()
            except Exception:  # noqa: BLE001 - la revisión de fondo nunca tumba la ingesta
                log.exception('Salud: error en la revisión (se reintenta en 1 minuto)')
                self.motivo, espera = 'Error: se reintenta en 1 minuto (detalle en el registro de la ingesta)', 60
            if espera:
                self._despertar.wait(espera)
                self._despertar.clear()

    def _ciclo(self):
        st = self.estado()
        if st.get('activa') != '1':
            self.motivo = 'Terminada' if st.get('terminada') else 'Detenida'
            self.actual = None
            return 30
        if st.get('pausada') == '1':
            self.motivo, self.actual = 'Pausada', None
            return 30
        motivo = self._espera(st)
        if motivo:
            self.motivo, self.actual = motivo, None
            return 20
        clave = (st.get('criterio'), st.get('inicio'))
        if clave != self.cola_clave:
            self.motivo = 'Preparando la lista de pistas…'
            criterio = json.loads(st.get('criterio') or '{}')
            self.cola = self.pistas_de(criterio)
            self.cola_clave, self.i, self.hechas = clave, 0, 0
            self.modo, self.inicio = criterio.get('modo') or 'pendientes', float(st.get('inicio') or 0)
            self.cache = self._cache({p['ruta'] for p in self.cola})
            self.restante_audio = sum(p['duracion'] for p in self.cola)
            self.seg_audio = self.seg_reloj = 0.0
        hilos = max(1, min(4, int(st.get('hilos') or 1)))
        lote = []
        while len(lote) < hilos:
            p = self._siguiente()
            if not p:
                break
            lote.append(p)
        if not lote:
            self._terminar()
            return 30
        self.motivo = 'Revisando'
        self.actual = lote[0]['ruta']
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=hilos) as ex:
            list(ex.map(self._revisar, lote))
        self.seg_reloj += time.time() - t0
        self.seg_audio += sum(p['duracion'] for p in lote)
        pausa = max(0.0, min(60.0, float(st.get('pausa') or 0)))
        return pausa or None

    def _siguiente(self):
        """Avanza hasta la próxima pista que hay que decodificar (las ya revisadas se saltan sin decodificar)."""
        while self.i < len(self.cola):
            p = self.cola[self.i]
            self.i += 1
            c = self.cache.get(p['ruta'])
            if self.modo == 'con_error':
                if not c or c['estado'] not in self.PROBLEMAS or (c['revisado'] or 0) >= self.inicio:
                    self._saltar(p)
                    continue
                return p
            if c and c['v'] == self.V and (self.modo == 'pendientes' or (c['revisado'] or 0) >= self.inicio):
                try:
                    st = os.stat(p['ruta'])
                    if c['tamano'] == st.st_size and abs((c['mtime'] or 0) - st.st_mtime) < 1:
                        self._saltar(p)
                        continue
                except OSError:
                    pass                 # ya no está: se revisa (queda como «falta»)
            return p
        return None

    def _saltar(self, p):
        self.hechas += 1
        self.restante_audio -= p['duracion']

    def _revisar(self, p):
        ruta = p['ruta']
        try:
            st = os.stat(ruta)
        except OSError:
            self._guardar(ruta, p['album_id'], None, None, 'falta', ['el archivo no está en la biblioteca'], None,
                          p['duracion'])
            self._contar(p)
            return
        ok, errores, _pico, seg = analisis.decodificar(ruta, prefijo=_prefijo())
        estado, notas = clasificar(ruta, ok, errores, seg, p['duracion'])
        if estado in ('danada', 'errores'):
            # solo las pocas con errores pagan una pasada más: ¿el daño está en el archivo o fue la lectura?
            danada, nota = confirmar(ruta, _prefijo())
            if danada is False:
                estado, notas = 'ok', [nota]
            else:
                notas = [nota] + list(notas)          # primero: se guardan solo las 4 primeras
        self._guardar(ruta, p['album_id'], st.st_size, st.st_mtime, estado, notas, seg, p['duracion'])
        self._contar(p)

    def _contar(self, p):
        with self._lock:
            self.hechas += 1
            self.restante_audio -= p['duracion']

    def _terminar(self):
        criterio = json.loads(self.estado().get('criterio') or '{}')
        rutas = {p['ruta'] for p in self.cola}
        cache = self._cache(rutas)
        malas = [c for c in cache.values() if c['estado'] in self.PROBLEMAS]
        resumen = {'pistas': len(self.cola), 'malas': len(malas)}
        self.poner(activa='0', pausada='0', terminada=time.time(), resumen=json.dumps(resumen))
        self.cola_clave = None
        self.motivo, self.actual = 'Terminada', None
        if self.avisar_fn:
            alcance = ', '.join(f'{k}: {v}' for k, v in criterio.items() if k != 'modo') or 'toda la biblioteca'
            self.avisar_fn('ok' if not malas else 'revision', 'Salud de la biblioteca: revisión terminada',
                           f'{len(self.cola)} pistas ({alcance}) · {len(malas)} con problemas')

    def progreso(self):
        st = self.estado()
        total = len(self.cola) if self.cola_clave == (st.get('criterio'), st.get('inicio')) else None
        ritmo = self.seg_audio / self.seg_reloj if self.seg_reloj > 30 else None
        return {'activa': st.get('activa') == '1', 'pausada': st.get('pausada') == '1', 'motivo': self.motivo,
                'criterio': json.loads(st.get('criterio') or '{}') if st.get('criterio') else None,
                'total': total, 'hechas': self.hechas if total is not None else None,
                'actual': self.actual, 'ritmo': round(ritmo, 1) if ritmo else None,
                'eta_s': round(max(0.0, self.restante_audio) / ritmo) if ritmo and total else None,
                'terminada': float(st['terminada']) if st.get('terminada') else None,
                'resumen': json.loads(st['resumen']) if st.get('resumen') else None,
                'ajustes': {k: st.get(k) for k in AJUSTES_DEFECTO}, 'revisadas': self._contar_revisadas()}

    def _contar_revisadas(self):
        with self._con() as c:
            return {r['estado']: r['n'] for r in c.execute('SELECT estado, COUNT(*) AS n FROM pistas GROUP BY estado')}

    # ── resultados ──
    def problemas_en(self, carpeta):
        """Rutas de la carpeta (y sus «CD N») que siguen dañadas o con errores y cuyo archivo existe."""
        base = os.path.realpath(carpeta).rstrip('/') + '/'
        with self._con() as c:
            filas = [r[0] for r in c.execute(
                "SELECT ruta FROM pistas WHERE ruta >= ? AND ruta < ? AND estado IN ('danada', 'errores') ORDER BY ruta",
                (base, base[:-1] + '0'))]
        return [r for r in filas if os.path.isfile(r)]

    # ── medir y sanear FLAC con cuadros dañados (sanear.py) ──
    def medidas(self, rutas):
        """{ruta: medida} de las que ya se midieron (y no cambiaron); las que faltan se encargan a `saneador`."""
        out, faltan = {}, []
        if not rutas:
            return out
        with self._con() as c:
            filas = {}
            for k in range(0, len(rutas), 500):
                parte = rutas[k:k + 500]
                for r in c.execute(f'SELECT ruta, tamano, mtime, datos FROM medidas WHERE ruta IN '
                                   f'({",".join("?" * len(parte))})', parte):
                    filas[r['ruta']] = r
        for ruta in rutas:
            r = filas.get(ruta)
            try:
                st = os.stat(ruta)
            except OSError:
                continue
            if r and r['tamano'] == st.st_size and abs((r['mtime'] or 0) - st.st_mtime) < 1:
                out[ruta] = json.loads(r['datos'])
            else:
                faltan.append(ruta)
        if faltan and getattr(self, 'saneador', None):
            self.saneador.medir(faltan)
            for ruta in faltan:
                out[ruta] = {'midiendo': True}
        return out

    def guardar_medida(self, ruta, datos):
        st = os.stat(ruta)
        with self._lock, self._con() as c:
            c.execute('INSERT OR REPLACE INTO medidas(ruta, tamano, mtime, datos) VALUES(?, ?, ?, ?)',
                      (ruta, st.st_size, st.st_mtime, json.dumps(datos, ensure_ascii=False)))

    def reparar_tags(self, rutas):
        """«Reparar tags»: reescribe la letra/comentario con BOM malo y vuelve a decodificar. → [{ruta, ok, texto}]"""
        out = []
        with self._con() as c:
            filas = {r['ruta']: dict(r) for r in c.execute(
                f'SELECT ruta, album_id, estado, errores, duracion FROM pistas WHERE ruta IN '
                f'({",".join("?" * len(rutas))})', rutas)} if rutas else {}
        for ruta in rutas:
            f = filas.get(ruta)
            if not f or not ruta.lower().endswith('.mp3') or not tags_mp3.solo_tags(json.loads(f['errores'] or '[]')):
                out.append({'ruta': ruta, 'ok': False, 'texto': 'no es un caso de solo tags'})
                continue
            try:
                r = tags_mp3.reparar(ruta)
            except Exception as e:  # noqa: BLE001
                log.exception('Salud: reparar tags %s', ruta)
                out.append({'ruta': ruta, 'ok': False, 'texto': f'no se pudo escribir: {e}'})
                continue
            ok, errores, _pico, seg = analisis.decodificar(ruta, prefijo=_prefijo())
            estado, notas = clasificar(ruta, ok, errores, seg, f['duracion'])
            if estado == 'ok':
                notas = [f'tags reparados: {r["rescatados"]} cuadro(s) reescrito(s) en UTF-8'
                         + (f', {r["quitados"]} sin texto quitado(s)' if r['quitados'] else '')]
            st = os.stat(ruta)
            self._guardar(ruta, f['album_id'], st.st_size, st.st_mtime, estado, notas, seg, f['duracion'])
            log.info('Salud: tags de %s: %s → %s', ruta, r, estado)
            out.append({'ruta': ruta, 'ok': estado == 'ok', 'texto': notas[0] if notas else estado})
        return out

    def olvidar(self, rutas):
        """Saca pistas de la lista de Salud (ya no están en Orbyte)."""
        with self._con() as c:
            for k in range(0, len(rutas), 500):
                parte = rutas[k:k + 500]
                c.execute(f'DELETE FROM pistas WHERE ruta IN ({",".join("?" * len(parte))})', parte)

    def ignorar(self, album, si=True):
        with self._lock, self._con() as c:
            if si:
                c.execute('INSERT OR REPLACE INTO ignorados(album, desde) VALUES(?, ?)', (album, time.time()))
            else:
                c.execute('DELETE FROM ignorados WHERE album=?', (album,))

    def ignorados_discos(self):
        with self._con() as c:
            return {r['album'][6:] for r in c.execute("SELECT album FROM ignorados WHERE album LIKE 'disco:%'")}

    def danadas(self, ver_ignorados=False):
        """Álbumes con pistas dañadas, con errores o que faltan (solo pistas que siguen en music.db)."""
        with self._con() as c:
            filas = [dict(r) for r in c.execute(
                f'SELECT ruta, album_id, estado, errores, segundos, duracion FROM pistas WHERE estado IN '
                f'({",".join("?" * len(PROBLEMAS))})', PROBLEMAS)]
            ign = {r['album'] for r in c.execute('SELECT album FROM ignorados')}
        if not filas:
            return []
        con = sqlite3.connect(f'file:{self._db_orbyte()}?mode=ro', uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            vigentes = {}
            rutas = [f['ruta'] for f in filas]
            for k in range(0, len(rutas), 500):
                parte = rutas[k:k + 500]
                for r in con.execute(f'SELECT file_path, album_id, disc_number, track_number, title FROM tracks '
                                     f'WHERE file_path IN ({",".join("?" * len(parte))})', parte):
                    vigentes[r['file_path']] = dict(r)
            ids = sorted({v['album_id'] for v in vigentes.values()})
            albumes = {}
            for k in range(0, len(ids), 500):
                parte = ids[k:k + 500]
                for r in con.execute(f'SELECT al.id, al.name, al.year, al.path, ar.name AS artista FROM albums al '
                                     f'LEFT JOIN artists ar ON ar.id = al.artist_id WHERE al.id IN '
                                     f'({",".join("?" * len(parte))})', parte):
                    albumes[r['id']] = dict(r)
        finally:
            con.close()
        por_album = {}
        for f in filas:
            t = vigentes.get(f['ruta'])
            if not t or t['album_id'] not in albumes:
                continue
            a = albumes[t['album_id']]
            carpeta = analisis.carpeta_album(a['path'])      # «Álbum/CD 2» se muestra como «Álbum»
            if (carpeta in ign) != bool(ver_ignorados):
                continue
            g = por_album.setdefault(carpeta, {'carpeta': carpeta, 'album_id': a['id'], 'artista': a['artista'],
                                               'album': a['name'], 'anio': a['year'], 'pistas': [],
                                               'danada': 0, 'errores': 0, 'falta': 0})
            g[f['estado']] += 1
            errs = json.loads(f['errores'] or '[]')
            tags = f['estado'] == 'errores' and f['ruta'].lower().endswith('.mp3') and tags_mp3.solo_tags(errs)
            g['tags'] = g.get('tags', 0) + (1 if tags else 0)
            g['pistas'].append({'solo_tags': tags,'archivo': os.path.basename(f['ruta']), 'ruta': f['ruta'], 'estado': f['estado'],
                                'errores': errs[:2], 'disco': t.get('disc_number'),
                                'numero': t.get('track_number'), 'titulo': t.get('title'),
                                'segundos': f['segundos'], 'duracion': f['duracion']})
        out = list(por_album.values())
        medidas = self.medidas([p['ruta'] for g in out for p in g['pistas'] if p['ruta'].lower().endswith('.flac')
                                and p['estado'] in ('errores', 'danada')])
        for g in out:
            g['saneables'] = 0
            for p in g['pistas']:
                m = medidas.get(p['ruta'])
                if m is not None:
                    p['medida'] = m
                    g['saneables'] += 1 if m.get('saneable') else 0
            g['pistas'].sort(key=lambda p: (_int(p['disco']), _int(p['numero'])))
        out.sort(key=lambda g: (-(g['danada'] + g['falta']), -g['errores'], (g['artista'] or '').lower(), g['album'] or ''))
        return out


def _int(v):
    try:
        return int(str(v or '0').split('/')[0])
    except ValueError:
        return 0


# ─────────────────────────── discos por normalizar ───────────────────────────

# solo «CD 2», «Disc 2», «Disco 2»: «Part 2» / «Pt. 2» / «Vol. 2» suelen ser álbumes distintos (otra fecha, otra edición)
DISCO_EN_NOMBRE = re.compile(r'[\s\-_,:(\[]*\b(?:cd|dis[ck]o?|disk)\s*0*(\d{1,3})\b[\])]?', re.I)
_CACHE = {'clave': None, 'datos': None}


def _base_nombre(nombre):
    from .repetidos import _nombre_limpio
    n = _nombre_limpio(nombre or '')
    n = DISCO_EN_NOMBRE.sub(' ', n)
    return analisis._norm_album(n)


def _detalle_separados(elegidos):
    if len(elegidos) <= 4:
        return ' + '.join(f'«{m["album"]}» (disco {e} · {m["n_pistas"]} pistas)' for m, e in elegidos)
    nums = [e for _m, e in elegidos]
    total = max([max(nums)] + [m['total_discos'] for m, _e in elegidos])
    txt = (f'{len(elegidos)} álbumes que son discos de uno: discos {", ".join(map(str, nums[:6]))}… {nums[-1]} · '
           f'{sum(m["n_pistas"] for m, _e in elegidos)} pistas')
    if total > len(set(nums)):
        txt += f' · faltan {total - len(set(nums))} de {total} discos'
    return txt


def _disco_nombre(m):
    """Número de disco escrito en el título o en la carpeta («Load (Disc 2)», «… CD2»), o None."""
    for txt in (m.get('album') or '', os.path.basename(m.get('ruta') or '')):
        x = DISCO_EN_NOMBRE.search(txt)
        if x:
            return int(x.group(1))
    return None


def discos_por_normalizar(db, biblioteca):
    """Casos de álbumes de varios discos que conviene normalizar (lista de dicts con tipo, miembros y acción)."""
    clave = (db, os.path.getmtime(db), biblioteca)
    if _CACHE['clave'] == clave:
        return _CACHE['datos']
    bib = analisis.biblioteca(db, biblioteca)
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    try:
        pistas = {}
        for aid, d, td, n in con.execute('SELECT album_id, disc_number, total_discs, track_number FROM tracks'):
            pistas.setdefault(aid, []).append((_int(d), _int(td), _int(n)))
        rutas = {i: (p or '').rstrip('/') for i, p in con.execute('SELECT id, path FROM albums')}
    finally:
        con.close()
    casos = []
    simples = []
    for g in bib['grupos']:
        ps = [x for i in g['ids'] for x in pistas.get(i, [])]
        discos = sorted({d for d, _t, _n in ps if d})
        total = max((t for _d, t, _n in ps), default=0)
        info = {'ruta': g['ruta'], 'id': g['ids'][0], 'album': g['album'], 'anio': g.get('anio'), 'artista': g['artista'],
                'n_pistas': g['pistas'], 'tag': g.get('tag'), 'discos': discos, 'total_discos': total}
        if len(g['ids']) > 1:
            carpetas = sorted({os.path.basename(rutas.get(i, '')) for i in g['ids']})
            casos.append({'tipo': 'subcarpetas', 'artista': g['artista'], 'titulo': g['album'], 'miembros': [info],
                          'detalle': f'{len(g["ids"])} discos en subcarpetas ({", ".join(carpetas[:4])}) que Orbyte '
                                     'muestra como álbumes separados'})
            continue
        simples.append(info)
        # numeración repetida sin tag de disco: discos mezclados en una carpeta
        if len(discos) <= 1:
            nums = [n for _d, _t, n in ps if n]
            rep = len(nums) - len(set(nums))
            if rep >= 2:
                casos.append({'tipo': 'numeracion', 'artista': g['artista'], 'titulo': g['album'], 'miembros': [info],
                              'detalle': f'{rep} números de pista repetidos sin tag de disco: parecen varios discos '
                                         'mezclados en una carpeta'})
    # discos de un mismo álbum en álbumes distintos: hace falta evidencia de que son discos de UNO (tags de disco 2+,
    # un total de discos en los tags o «CD 2» en el nombre); el mismo título en dos ediciones es un repetido, no un disco
    grupos = {}
    for s_ in simples:
        grupos.setdefault((analisis._norm(s_['artista']), _base_nombre(s_['album'])), []).append(s_)
    en_caso = set()
    for (_a, base), ms in grupos.items():
        if not base or len(ms) < 2:
            continue
        info = []
        for m in ms:
            n = _disco_nombre(m)
            t = m['discos'][0] if len(m['discos']) == 1 else None
            eff = n or t
            fuerte = n is not None or (t or 0) >= 2 or (t == 1 and m['total_discos'] >= 2)
            info.append((m, eff, fuerte))
        if not any(e and e >= 2 and f for _m, e, f in info):
            continue
        elegidos = [(m, e) for m, e, f in info if e and (f or e == 1)]
        if len({e for _m, e in elegidos}) < 2:
            continue
        anios = sorted(int(str(m['anio'])[:4]) for m, _e in elegidos if str(m.get('anio') or '')[:4].isdigit())
        if anios and anios[-1] - anios[0] > 1:
            continue                              # otra fecha: otro lanzamiento, no un disco del mismo
        elegidos.sort(key=lambda x: (x[1], x[0]['n_pistas']))
        ms = [m for m, _e in elegidos]
        en_caso.update(m['ruta'] for m in ms)
        casos.append({'tipo': 'separados', 'artista': ms[0]['artista'], 'titulo': ms[0]['album'], 'miembros': ms,
                      'detalle': _detalle_separados(elegidos)})
    # discos faltantes: los tags dicen más discos de los que hay y no hay otro álbum que los complete
    for m in simples:
        if m['ruta'] in en_caso or m['total_discos'] < 2 or not m['discos'] or m['total_discos'] > 20:
            continue
        faltan = [d for d in range(1, m['total_discos'] + 1) if d not in m['discos']]
        if faltan:
            casos.append({'tipo': 'faltantes', 'artista': m['artista'], 'titulo': m['album'], 'miembros': [m],
                          'detalle': f'Los tags dicen {m["total_discos"]} discos; en la biblioteca '
                                     f'{"están los discos" if len(m["discos"]) > 1 else "está el disco"} '
                                     f'{", ".join(map(str, m["discos"]))} y '
                                     f'{"faltan los discos" if len(faltan) > 1 else "falta el disco"} '
                                     f'{", ".join(map(str, faltan))}'})
    for c in casos:
        c['clave'] = c['tipo'] + '|' + '|'.join(sorted(m['ruta'] for m in c['miembros']))
    casos.sort(key=lambda c: (analisis._norm(c['artista']), analisis._norm(c['titulo'])))
    _CACHE.update(clave=clave, datos=casos)
    return casos


class Saneador:
    """Un hilo, una pista a la vez y con prioridad mínima: mide (dónde están los cuadros malos y cuántos ms se
    pierden) y sanea a pedido. Medir y sanear corren como proceso aparte (python -m ingesta.sanear)."""

    def __init__(self, salud, datos, repo):
        self.salud, self.tmp, self.repo = salud, os.path.join(datos, 'sanear-tmp'), repo
        self._cola = []             # [(accion, ruta)]
        self._lock = threading.Lock()
        self._ev = threading.Event()
        self.actual = None
        self.ultimos = []           # resultados de sanear recientes (para la página)
        salud.saneador = self
        threading.Thread(target=self._bucle, name='saneador', daemon=True).start()

    def _poner(self, accion, rutas):
        with self._lock:
            ya = set(self._cola)
            if self.actual:
                ya.add(self.actual)
            for r in rutas:
                if (accion, r) not in ya:
                    if accion in ('sanear', 'forzar'):
                        self._cola.insert(sum(1 for a, _ in self._cola if a != 'medir'), (accion, r))
                    else:
                        self._cola.append((accion, r))
        self._ev.set()

    def medir(self, rutas):
        self._poner('medir', rutas)

    def sanear(self, rutas, forzar=False):
        self._poner('forzar' if forzar else 'sanear', rutas)

    def estado(self):
        with self._lock:
            return {'actual': self.actual, 'medir': sum(1 for a, _ in self._cola if a == 'medir'),
                    'sanear': [r for a, r in self._cola if a != 'medir'], 'ultimos': self.ultimos[-30:]}

    def _correr(self, accion, ruta, *extra):
        os.makedirs(self.tmp, exist_ok=True)
        cmd = _prefijo() + [sys.executable, '-m', 'ingesta.sanear', accion, ruta, self.tmp, *extra]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200, cwd=self.repo, errors='replace')
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'ok': False, 'error': (r.stderr or 'sin respuesta').strip()[-300:]}

    def _bucle(self):
        while True:
            self._ev.wait(30)
            with self._lock:
                if not self._cola:
                    self._ev.clear()
                    continue
                self.actual = self._cola.pop(0)
            accion, ruta = self.actual
            try:
                if not os.path.isfile(ruta):
                    continue
                if accion == 'medir':
                    m = self._correr('medir', ruta)
                    if 'error' in m and 'ms' not in m:
                        m = {'fatal': m['error'], 'saneable': False}
                    self.salud.guardar_medida(ruta, m)
                else:
                    self._sanear(ruta, forzar=accion == 'forzar')
            except Exception:  # noqa: BLE001
                log.exception('Sanear: %s %s', accion, ruta)
            finally:
                with self._lock:
                    self.actual = None
                time.sleep(1)

    def _sanear(self, ruta, forzar=False):
        r = self._correr('sanear', ruta, *(['--forzar'] if forzar else []))
        nombre = os.path.basename(ruta)
        if not r.get('ok'):
            log.warning('Sanear: %s: %s', ruta, r.get('error'))
            self.ultimos.append({'ruta': ruta, 'archivo': nombre, 'ok': False, 'texto': r.get('error')})
            return
        ms = r['ms'] or 0
        ms_txt = f'{ms / 1000:.1f} s'.replace('.', ',') if ms >= 1000 else f'{ms:g} ms'.replace('.', ',')
        nota = (f'saneada: {ms_txt} en {r["tramos"]} punto{"s" if r["tramos"] != 1 else ""} rellenados; el '
                f'original quedó en {sanear_mod.RESPALDO}/')
        log.info('Sanear: %s → %s', ruta, nota)
        with self.salud._con() as c:
            f = c.execute('SELECT album_id, duracion FROM pistas WHERE ruta=?', (ruta,)).fetchone()
        dur = f['duracion'] if f else None
        ok, errores, _pico, seg = analisis.decodificar(ruta, prefijo=_prefijo())
        estado, notas = clasificar(ruta, ok, errores, seg, dur)
        st = os.stat(ruta)
        self.salud._guardar(ruta, f['album_id'] if f else None, st.st_size, st.st_mtime, estado,
                            [nota] if estado == 'ok' else [nota] + list(notas), seg, dur)
        self.salud.guardar_medida(ruta, dict(r, saneable=False, saneada=True))
        self.ultimos.append({'ruta': ruta, 'archivo': nombre, 'ok': estado == 'ok',
                             'texto': nota if estado == 'ok' else f'{nota}, pero ffmpeg todavía avisa: '
                                                                    f'{"; ".join(errores[:2])}'})
