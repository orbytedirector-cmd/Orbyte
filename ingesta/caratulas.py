"""Salud · Sin carátula (pedido de Niko, 08-11-2026).

Revisa los álbumes de music.db y separa los que no tienen una carátula usable en Orbyte:
- 'sin'         Orbyte no tiene ninguna y tampoco hay imagen en la carpeta ni incrustada → buscarla en su ficha.
- 'sin_asignar' Orbyte no tiene ninguna, pero hay una imagen en la carpeta o incrustada en las pistas → «Usar esta».
- 'rota'        Orbyte apunta a un archivo que ya no existe (si hay imagen disponible, también se ofrece).
- 'chica'       la que tiene mide menos de CHICA_PX de lado (se ve pixelada en la pantalla de reproducción): aviso.

Se corre en un hilo, a pedido o una vez al día (no decodifica audio: para las que tienen carátula solo lee la
cabecera de la imagen; las demás miran la carpeta y hasta 3 pistas). El resultado queda en
ingesta_datos/caratulas.json. Arreglar usa lo que ya existe: la ficha de Biblioteca (editor de carátula) y
POST /api/portada/b/<id> (escribe Cover.jpg, la incrusta en las pistas y reindexa).
"""
import json
import logging
import os
import sqlite3
import threading
import time

from . import analisis
from . import portada as portada_mod

log = logging.getLogger('ingesta.caratulas')

CHICA_PX = 500
CADA_S = 24 * 3600


def _dimensiones(ruta):
    if analisis.Image is None:
        return None
    try:
        with analisis.Image.open(ruta) as im:          # solo lee la cabecera
            return im.size
    except Exception:  # noqa: BLE001
        return None


def _candidata(ruta):
    """La imagen disponible para un álbum sin carátula en Orbyte: {'tipo', 'archivo'|'pista', ...} o None."""
    try:
        e = portada_mod.estado(ruta, None)
    except Exception as ex:  # noqa: BLE001
        log.warning('Carátulas: %s: %s', ruta, ex)
        return None
    if e.get('actual'):
        a = e['actual']
        return {'tipo': 'archivo', 'archivo': a['archivo'], 'ancho': a.get('ancho'), 'alto': a.get('alto')}
    buenas = [c for c in e.get('candidatas') or [] if not c.get('ilegible')]
    if buenas:
        c = buenas[0]
        return {'tipo': 'archivo', 'archivo': c['archivo'], 'ancho': c.get('ancho'), 'alto': c.get('alto')}
    if e.get('incrustada'):
        return {'tipo': 'incrustada', 'pista': e['incrustada']}
    return None


def revisar(db, avance=None):
    """→ {'albumes': [...], 'revisados': n, 'cuando': ts}. `avance(i, total)` opcional."""
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    try:
        filas = con.execute('SELECT al.id, al.name, al.year, al.path, al.cover_path, ar.name '
                            'FROM albums al LEFT JOIN artists ar ON ar.id = al.artist_id '
                            'ORDER BY ar.name COLLATE NOCASE, al.name COLLATE NOCASE').fetchall()
    finally:
        con.close()
    out = []
    total = len(filas)
    for n, (aid, nombre, anio, ruta, cover, artista) in enumerate(filas, 1):
        if avance and n % 50 == 0:
            avance(n, total)
        if not ruta or not os.path.isdir(ruta):
            continue                                    # fantasma: lo ve la página Fantasmas
        base = {'id': aid, 'album': nombre or os.path.basename(ruta.rstrip('/')), 'anio': anio,
                'artista': artista or '', 'ruta': ruta.rstrip('/')}
        if cover and os.path.isfile(cover):
            dim = _dimensiones(cover)
            if dim and min(dim) < CHICA_PX:
                out.append(dict(base, caso='chica', ancho=dim[0], alto=dim[1]))
            continue
        cand = _candidata(ruta)
        caso = 'rota' if cover else ('sin_asignar' if cand else 'sin')
        out.append(dict(base, caso=caso, candidata=cand))
    return {'albumes': out, 'revisados': total, 'cuando': int(time.time())}


class Caratulas:
    """Hilo de la revisión; guarda el último resultado en `archivo`."""

    def __init__(self, ajustes, archivo):
        self._ajustes = ajustes
        self._archivo = archivo
        self._lock = threading.Lock()
        self.revisando = False
        self.avance = None
        self.error = None

    def resultado(self):
        try:
            with open(self._archivo, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def quitar(self, oid):
        """Saca un álbum de la lista (ya se arregló) sin esperar a la próxima revisión."""
        with self._lock:
            r = self.resultado()
            if not r:
                return
            r['albumes'] = [a for a in r['albumes'] if a['id'] != oid]
            self._guardar(r)

    def _guardar(self, r):
        tmp = self._archivo + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(r, f, ensure_ascii=False)
        os.replace(tmp, self._archivo)

    def iniciar(self):
        """Revisión en segundo plano (si no hay otra en curso)."""
        with self._lock:
            if self.revisando:
                return False
            self.revisando = True
        threading.Thread(target=self._correr, name='caratulas', daemon=True).start()
        return True

    def si_hace_falta(self):
        r = self.resultado()
        if not r or time.time() - r.get('cuando', 0) > CADA_S:
            self.iniciar()

    def _correr(self):
        try:
            self.error = None
            aj = self._ajustes()
            r = revisar(aj.get('db_orbyte'), avance=lambda i, t: setattr(self, 'avance', (i, t)))
            with self._lock:
                self._guardar(r)
            log.info('Carátulas: %d álbumes revisados, %d en la lista', r['revisados'], len(r['albumes']))
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.exception('Carátulas')
        finally:
            self.revisando = False
            self.avance = None
