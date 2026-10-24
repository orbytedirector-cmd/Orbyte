"""Orbyte Ingesta — almacén propio (ingesta.db): ajustes, álbumes de la bandeja y avisos."""
import json
import os
import sqlite3
import threading
import time

_lock = threading.Lock()

AJUSTES_DEFECTO = {
    'carpeta_entrada': '',
    'biblioteca': '/mnt/musica',
    'db_orbyte': '',            # se completa al arrancar con el music.db del repo
    'espera_min': '2',          # minutos sin cambios para dar por terminada una copia
    'intervalo_seg': '30',      # cada cuánto se mira la carpeta de entrada
    'trabajadores': '3',        # decodificaciones en paralelo durante la revisión
    'musicbrainz': '1',         # identificar la edición exacta durante la revisión
    'desatendido': '0',         # álbumes sin decisiones pendientes se procesan solos
    'borrar_origen': '1',       # tras verificar la copia, borrar el original de la entrada (mover)
    'metadata': '1',            # etapa 3: correr los 5 scripts de metadata tras ubicar
    'python_scripts': '',       # Python que corre los 5 scripts (vacío = python3 del sistema)
}

PREVIOS = ('copiando', 'en_cola', 'analizando', 'revisado', 'requiere_revision', 'error', 'descartado')
POSTERIORES = ('por_ubicar', 'ubicando', 'ubicado', 'por_metadata', 'metadata', 'completado',
               'error_ubicar', 'error_metadata')

ESTADOS = ('copiando', 'en_cola', 'analizando', 'revisado', 'requiere_revision', 'error')


class Almacen:
    def __init__(self, ruta):
        self.ruta = ruta
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with self._con() as c:
            c.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT);
                CREATE TABLE IF NOT EXISTS albumes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ruta TEXT UNIQUE NOT NULL,
                    rel TEXT,
                    firma TEXT,
                    firma_desde REAL,
                    detectado REAL,
                    estado TEXT,
                    progreso TEXT,
                    analizado REAL,
                    artista TEXT, album TEXT, anio TEXT, calidad TEXT,
                    destino TEXT, n_pistas INTEGER, conteo TEXT,
                    reporte TEXT
                );
                CREATE TABLE IF NOT EXISTS avisos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, tipo TEXT, titulo TEXT, texto TEXT,
                    album_id INTEGER, leido INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS ix_avisos_leido ON avisos(leido, ts);
            ''')
            columnas = {r[1] for r in c.execute('PRAGMA table_info(albumes)')}
            for col, tipo in (('eleccion', 'TEXT'), ('cambios', 'TEXT'), ('registro', 'TEXT'),
                              ('silencioso', 'INTEGER'), ('ubicado_en', 'TEXT'), ('terminado', 'REAL'),
                              ('resultado', 'TEXT'), ('version_reporte', 'INTEGER')):
                if col not in columnas:
                    c.execute(f'ALTER TABLE albumes ADD COLUMN {col} {tipo}')

    def _con(self):
        c = sqlite3.connect(self.ruta, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    # ── ajustes ──
    def ajustes(self):
        a = dict(AJUSTES_DEFECTO)
        with self._con() as c:
            a.update({r['clave']: r['valor'] for r in c.execute('SELECT clave, valor FROM ajustes')})
        return a

    def guardar_ajustes(self, cambios):
        with _lock, self._con() as c:
            c.executemany('INSERT INTO ajustes(clave, valor) VALUES(?, ?) '
                          'ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor',
                          [(k, str(v)) for k, v in cambios.items()])

    # ── álbumes ──
    def albumes(self):
        with self._con() as c:
            return [dict(r) for r in c.execute(
                'SELECT id, ruta, rel, estado, progreso, detectado, analizado, artista, album, anio, '
                'calidad, destino, n_pistas, conteo, ubicado_en, terminado FROM albumes '
                'ORDER BY detectado DESC, rel')]

    def album(self, aid):
        with self._con() as c:
            r = c.execute('SELECT * FROM albumes WHERE id=?', (aid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d['reporte'] = json.loads(d['reporte']) if d.get('reporte') else None
        d['cambios'] = json.loads(d['cambios']) if d.get('cambios') else {}
        d['resultado'] = json.loads(d['resultado']) if d.get('resultado') else None
        return d

    def por_ruta(self):
        with self._con() as c:
            return {r['ruta']: dict(r) for r in c.execute(
                'SELECT id, ruta, firma, firma_desde, estado FROM albumes')}

    def nuevo_album(self, ruta, rel, firma):
        ahora = time.time()
        with _lock, self._con() as c:
            c.execute('INSERT OR IGNORE INTO albumes(ruta, rel, firma, firma_desde, detectado, estado) '
                      'VALUES(?, ?, ?, ?, ?, ?)', (ruta, rel, json.dumps(firma), ahora, ahora, 'copiando'))

    def actualizar(self, aid, **campos):
        if not campos:
            return
        if 'firma' in campos and not isinstance(campos['firma'], str):
            campos['firma'] = json.dumps(campos['firma'])
        if 'reporte' in campos and campos['reporte'] is not None and not isinstance(campos['reporte'], str):
            campos['reporte'] = json.dumps(campos['reporte'], ensure_ascii=False)
        for k in ('conteo', 'cambios', 'resultado'):
            if k in campos and campos[k] is not None and not isinstance(campos[k], str):
                campos[k] = json.dumps(campos[k], ensure_ascii=False)
        sets = ', '.join(f'{k}=?' for k in campos)
        with _lock, self._con() as c:
            c.execute(f'UPDATE albumes SET {sets} WHERE id=?', (*campos.values(), aid))

    def borrar_album(self, aid):
        with _lock, self._con() as c:
            c.execute('DELETE FROM albumes WHERE id=?', (aid,))
            c.execute('UPDATE avisos SET album_id=NULL WHERE album_id=?', (aid,))

    def registrar(self, aid, linea):
        sello = time.strftime('%H:%M:%S')
        with _lock, self._con() as c:
            r = c.execute('SELECT registro FROM albumes WHERE id=?', (aid,)).fetchone()
            previo = (r[0] if r and r[0] else '').splitlines()[-1500:]
            previo.append(f'{sello}  {linea}')
            c.execute('UPDATE albumes SET registro=? WHERE id=?', ('\n'.join(previo), aid))

    def siguiente(self, estado):
        with self._con() as c:
            r = c.execute('SELECT id FROM albumes WHERE estado=? ORDER BY detectado, rel LIMIT 1', (estado,)).fetchone()
        return r[0] if r else None

    def revisiones_antiguas(self, version):
        with self._con() as c:
            return [r[0] for r in c.execute(
                "SELECT id FROM albumes WHERE estado IN ('revisado', 'requiere_revision', 'error') "
                'AND COALESCE(version_reporte, 0) < ?', (version,))]

    def con_estado(self, *estados):
        with self._con() as c:
            return [dict(r) for r in c.execute(
                f'SELECT id, ruta, rel, estado FROM albumes WHERE estado IN ({",".join("?" * len(estados))})', estados)]

    def siguiente_en_cola(self):
        with self._con() as c:
            r = c.execute("SELECT id, ruta, rel FROM albumes WHERE estado='en_cola' "
                          'ORDER BY detectado, rel LIMIT 1').fetchone()
        return dict(r) if r else None

    def reiniciar_interrumpidos(self):
        with _lock, self._con() as c:
            c.execute("UPDATE albumes SET estado='en_cola', progreso=NULL WHERE estado='analizando'")

    # ── avisos ──
    def avisar(self, tipo, titulo, texto='', album_id=None):
        with _lock, self._con() as c:
            c.execute('INSERT INTO avisos(ts, tipo, titulo, texto, album_id) VALUES(?, ?, ?, ?, ?)',
                      (time.time(), tipo, titulo, texto, album_id))
            # se guardan los últimos 300
            c.execute('DELETE FROM avisos WHERE id NOT IN (SELECT id FROM avisos ORDER BY id DESC LIMIT 300)')

    def avisos(self, limite=100):
        with self._con() as c:
            return [dict(r) for r in c.execute('SELECT * FROM avisos ORDER BY id DESC LIMIT ?', (limite,))]

    def avisos_sin_leer(self):
        with self._con() as c:
            return c.execute('SELECT COUNT(*) FROM avisos WHERE leido=0').fetchone()[0]

    def marcar_leidos(self, ids=None):
        with _lock, self._con() as c:
            if ids:
                c.executemany('UPDATE avisos SET leido=1 WHERE id=?', [(i,) for i in ids])
            else:
                c.execute('UPDATE avisos SET leido=1 WHERE leido=0')
