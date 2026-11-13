"""
Orbyte Ingesta — papelera (pedido de Niko, 13-11-2026).

Lo que se elimina desde las fichas (pistas o un álbum completo, de la Biblioteca o de la Bandeja) no se borra:
se mueve a «.orbyte-papelera/<id>/» en la raíz del mismo disco (la biblioteca o la carpeta de entrada), con su
ruta relativa, así que el movimiento es un rename instantáneo y «Restaurar» lo deja donde estaba. Se vacía sola
pasadas `papelera_horas` (Ajustes, 48 por defecto) o a mano desde Salud → Espacio.

Las carpetas con punto no las ve nadie: ni la revisión de la entrada, ni el indexador, ni la metadata.

Para lo que sale de Orbyte (music.db), el elemento guarda en `extra` lo necesario para devolverlo al restaurar:
las pistas (id viejo → ruta) y las filas que apuntaban a ellas y se quitaron (playlists, favoritos...). Al
restaurar, los archivos vuelven, el álbum se vuelve a indexar y `reponer()` (en cada vuelta del vigilante)
espera a que las pistas estén en music.db para devolverles el historial y esas filas.
"""
import json
import logging
import os
import shutil
import sqlite3
import threading
import time

log = logging.getLogger('ingesta')

NOMBRE = '.orbyte-papelera'
HORAS_DEFECTO = 48
_lock = threading.Lock()


def horas(ajustes):
    try:
        h = float(ajustes.get('papelera_horas') or HORAS_DEFECTO)
    except (TypeError, ValueError):
        h = HORAS_DEFECTO
    return min(max(h, 1), 24 * 90)


def _tamano(p):
    if os.path.isfile(p) or os.path.islink(p):
        try:
            return os.lstat(p).st_size
        except OSError:
            return 0
    n = 0
    for raiz, _dirs, archivos in os.walk(p):
        for a in archivos:
            try:
                n += os.lstat(os.path.join(raiz, a)).st_size
            except OSError:
                pass
    return n


def _dentro(p, raiz):
    p, raiz = os.path.realpath(p), os.path.realpath(raiz)
    return p != raiz and p.startswith(raiz + os.sep)


class FalloPapelera(Exception):
    pass


class Papelera:
    def __init__(self, datos):
        self.ruta = os.path.join(datos, 'papelera.db')
        with self._con() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS elementos(id INTEGER PRIMARY KEY AUTOINCREMENT, cuando REAL, raiz TEXT,
                    rels TEXT, tipo TEXT, origen TEXT, titulo TEXT, detalle TEXT, bytes INTEGER, extra TEXT);
                CREATE TABLE IF NOT EXISTS pendientes(id INTEGER PRIMARY KEY AUTOINCREMENT, desde REAL, extra TEXT);''')

    def _con(self):
        c = sqlite3.connect(self.ruta, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def carpeta(raiz, eid):
        return os.path.join(raiz, NOMBRE, str(eid))

    def mover(self, raiz, rutas, tipo, origen, titulo, detalle='', extra=None):
        """Mueve `rutas` (archivos o carpetas dentro de `raiz`) a la papelera. Todo o nada. → id del elemento."""
        raiz = os.path.realpath(raiz)
        rutas = [os.path.abspath(r) for r in rutas]
        if not rutas:
            raise FalloPapelera('Nada que mover')
        pap = os.path.join(raiz, NOMBRE)
        for r in rutas:
            if not os.path.lexists(r):
                raise FalloPapelera(f'No existe {os.path.basename(r)}')
            if not _dentro(r, raiz) or _dentro(r, pap) or os.path.realpath(r) == pap:
                raise FalloPapelera(f'{r} no está dentro de {raiz}')
        rels = [os.path.relpath(os.path.realpath(r), raiz) for r in rutas]
        with _lock:
            with self._con() as c:
                eid = c.execute('INSERT INTO elementos(cuando, raiz, rels, tipo, origen, titulo, detalle, bytes, extra) '
                                'VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?)',
                                (time.time(), raiz, json.dumps(rels, ensure_ascii=False), tipo, origen, titulo,
                                 detalle, json.dumps(extra or {}, ensure_ascii=False))).lastrowid
            destino = self.carpeta(raiz, eid)
            hechos, total = [], 0
            try:
                for r, rel in zip(rutas, rels):
                    d = os.path.join(destino, rel)
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    total += _tamano(r)
                    shutil.move(r, d)                 # mismo disco: rename
                    hechos.append((r, d))
            except Exception as e:
                for r, d in reversed(hechos):        # todo o nada
                    try:
                        os.makedirs(os.path.dirname(r), exist_ok=True)
                        shutil.move(d, r)
                    except Exception:  # noqa: BLE001
                        log.exception('Papelera: no se pudo devolver %s', d)
                shutil.rmtree(destino, ignore_errors=True)
                with self._con() as c:
                    c.execute('DELETE FROM elementos WHERE id=?', (eid,))
                raise FalloPapelera(f'No se pudo mover a la papelera: {e}')
            with self._con() as c:
                c.execute('UPDATE elementos SET bytes=? WHERE id=?', (total, eid))
        log.info('Papelera: %s «%s» (%d elemento(s), %.0f MB) → %s', tipo, titulo, len(rutas), total / 1048576, destino)
        return eid

    def poner_extra(self, eid, extra):
        with self._con() as c:
            c.execute('UPDATE elementos SET extra=? WHERE id=?', (json.dumps(extra or {}, ensure_ascii=False), eid))

    def elemento(self, eid):
        with self._con() as c:
            f = c.execute('SELECT * FROM elementos WHERE id=?', (eid,)).fetchone()
        return self._fila(f) if f else None

    @staticmethod
    def _fila(f):
        d = dict(f)
        d['rels'] = json.loads(d['rels'] or '[]')
        d['extra'] = json.loads(d['extra'] or '{}')
        return d

    def listar(self, horas_vida=HORAS_DEFECTO):
        with self._con() as c:
            filas = [self._fila(f) for f in c.execute('SELECT * FROM elementos ORDER BY cuando DESC')]
        for f in filas:
            f['vence'] = int(f['cuando'] + horas_vida * 3600)
            f['cuando'] = int(f['cuando'])
            f['n'] = len(f['rels'])
            f['existe'] = os.path.isdir(self.carpeta(f['raiz'], f['id']))
            f.pop('extra', None)                       # ids y filas de music.db: no hacen falta en la página
        return filas

    def vaciar(self, ids=None):
        """Borra para siempre los elementos `ids` (todos si None). → {'borrados', 'bytes', 'errores'}"""
        with self._con() as c:
            filas = [self._fila(f) for f in c.execute('SELECT * FROM elementos')]
        if ids is not None:
            pedidos = {int(i) for i in ids}
            filas = [f for f in filas if f['id'] in pedidos]
        out = {'borrados': 0, 'bytes': 0, 'errores': []}
        for f in filas:
            d = self.carpeta(f['raiz'], f['id'])
            if os.path.isdir(d):
                if not os.path.isdir(f['raiz']) or not os.listdir(f['raiz']):
                    out['errores'].append(f'{f["titulo"]}: el disco no está montado')
                    continue
                try:
                    shutil.rmtree(d)
                except OSError as e:
                    out['errores'].append(f'{f["titulo"]}: {e}')
                    continue
            with self._con() as c:
                c.execute('DELETE FROM elementos WHERE id=?', (f['id'],))
            out['borrados'] += 1
            out['bytes'] += f['bytes'] or 0
        if out['borrados']:
            log.info('Papelera: %d elemento(s) borrados (%.0f MB)', out['borrados'], out['bytes'] / 1048576)
        return out

    def purgar(self, horas_vida):
        """Lo que lleva más de `horas_vida` en la papelera se borra."""
        limite = time.time() - horas_vida * 3600
        with self._con() as c:
            ids = [r['id'] for r in c.execute('SELECT id FROM elementos WHERE cuando < ?', (limite,))]
        return self.vaciar(ids) if ids else {'borrados': 0, 'bytes': 0, 'errores': []}

    def restaurar(self, eid):
        """Devuelve los archivos a su lugar. → el elemento (con `extra`) para que quien llamó lo vuelva a sumar."""
        f = self.elemento(eid)
        if not f:
            raise FalloPapelera('Ese elemento ya no está en la papelera')
        d = self.carpeta(f['raiz'], eid)
        if not os.path.isdir(d):
            raise FalloPapelera('Sus archivos ya no están (¿disco desmontado?)')
        choques = [rel for rel in f['rels'] if os.path.lexists(os.path.join(f['raiz'], rel))]
        if choques:
            raise FalloPapelera(f'Ya hay algo en su lugar: {choques[0]}')
        for rel in f['rels']:
            o, dst = os.path.join(d, rel), os.path.join(f['raiz'], rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(o, dst)
        shutil.rmtree(d, ignore_errors=True)
        with self._con() as c:
            c.execute('DELETE FROM elementos WHERE id=?', (eid,))
        log.info('Papelera: restaurado «%s» (%d elemento(s))', f['titulo'], len(f['rels']))
        return f

    # ── devolver a music.db lo que se quitó, cuando el álbum restaurado ya está indexado ──
    def pendiente(self, extra):
        with self._con() as c:
            c.execute('INSERT INTO pendientes(desde, extra) VALUES(?, ?)',
                      (time.time(), json.dumps(extra, ensure_ascii=False)))

    def reponer(self, db, registro=None):
        """Para cada restauración pendiente: si sus pistas ya están en music.db, les devuelve el historial de
        escucha y las filas guardadas (playlists, favoritos...). Pasado un día se da por perdida."""
        with self._con() as c:
            filas = [dict(r) for r in c.execute('SELECT * FROM pendientes')]
        if not filas or not db or not os.path.isfile(db):
            return
        from . import eliminar
        for f in filas:
            extra = json.loads(f['extra'] or '{}')
            try:
                listo = eliminar.reponer(db, extra, registro or log.info)
            except Exception:  # noqa: BLE001
                log.exception('Papelera: no se pudo reponer %s', extra.get('titulo'))
                listo = False
            if listo or time.time() - f['desde'] > 86400:
                with self._con() as c:
                    c.execute('DELETE FROM pendientes WHERE id=?', (f['id'],))
