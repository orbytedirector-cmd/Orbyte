"""
Orbyte Ingesta — revisar el país de origen de los artistas de music.db.

  Recalculo     hilo que pasa por todos los artistas (MusicBrainz, verificado con sus discos: ver paises.py)
                y deja una propuesta «antes → después»; no escribe nada hasta que se aplica
  aplicar()     escribe las filas elegidas (más las que no cambian, para guardar el código y el ID de MusicBrainz)
  fijar()       corrección a mano (nationality_source='manual': ninguna búsqueda la vuelve a tocar)
  buscar()      artistas de music.db por nombre, con su país actual

La propuesta se guarda en <datos>/nacionalidades.json: si el servicio se reinicia a mitad, se puede continuar.
"""
import json
import os
import sqlite3
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
import paises  # noqa: E402

from . import edicion  # noqa: E402

TIPOS = ('cambia', 'nuevo', 'normaliza', 'sin_verificar', 'sin_datos', 'igual', 'manual')


def _con(db):
    c = sqlite3.connect(db, timeout=60)
    c.row_factory = sqlite3.Row
    return c


def _tipo(actual, fuente, codigo):
    actual_iso = paises.iso_de(actual) if actual and actual != paises.SIN_PAIS else None
    if fuente == 'manual':
        return 'manual'
    if not codigo:
        return 'sin_verificar' if actual_iso else 'sin_datos'
    if not actual_iso:
        return 'nuevo'
    if actual_iso != codigo:
        return 'cambia'
    if actual != paises.nombre_de(codigo):
        return 'normaliza'
    return 'igual'


def _fila(a, n_albumes, res):
    actual = a['nationality']
    return {'id': a['id'], 'nombre': a['name'], 'albumes': n_albumes,
            'actual': actual if actual and actual != paises.SIN_PAIS else None,
            'actual_iso': paises.iso_de(actual) if actual else None,
            'fuente_actual': a['nationality_source'],
            'codigo': res.get('codigo'), 'pais': res.get('pais'), 'metodo': res.get('metodo'),
            'mbid': res.get('mbid'), 'mb_nombre': res.get('mb_nombre'), 'detalle': res.get('detalle') or '',
            'tipo': _tipo(actual, a['nationality_source'], res.get('codigo')), 'aplicado': False}


def _mb(cliente):
    def llamar(ruta, **params):
        try:
            return cliente.mb(ruta, **params)
        except edicion.SinRed as e:
            raise RuntimeError(f'MusicBrainz no responde: {e}')
    return llamar


class Recalculo:
    def __init__(self, ruta_json):
        self.ruta = ruta_json
        self.lock = threading.Lock()
        self.detener = threading.Event()
        self.hilo = None
        self.d = {'estado': None, 'total': 0, 'hechos': 0, 'actual': None, 'inicio': None, 'fin': None,
                  'error': None, 'filas': []}
        try:
            with open(self.ruta) as f:
                self.d.update(json.load(f))
            if self.d.get('estado') == 'corriendo':
                self.d['estado'] = 'interrumpido'
        except (OSError, ValueError):
            pass

    def _guardar(self):
        tmp = self.ruta + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.d, f, ensure_ascii=False)
        os.replace(tmp, self.ruta)

    def estado(self):
        with self.lock:
            return json.loads(json.dumps(self.d))

    def corriendo(self):
        return bool(self.hilo and self.hilo.is_alive())

    def iniciar(self, db, cliente, continuar=False):
        if self.corriendo():
            return False
        self.detener.clear()
        with self.lock:
            if not continuar:
                self.d.update(filas=[], hechos=0)
            self.d.update(estado='corriendo', inicio=self.d.get('inicio') if continuar else time.time(),
                          fin=None, error=None, actual=None)
        self.hilo = threading.Thread(target=self._correr, args=(db, cliente), daemon=True, name='nacionalidades')
        self.hilo.start()
        return True

    def parar(self):
        self.detener.set()

    def _correr(self, db, cliente):
        mb = _mb(cliente)
        try:
            con = _con(db)
            paises.asegurar_columnas(con)
            artistas = con.execute('SELECT a.id, a.name, a.nationality, a.nationality_source, '
                                   '(SELECT COUNT(*) FROM albums al WHERE al.artist_id=a.id) AS n '
                                   'FROM artists a ORDER BY a.name COLLATE NOCASE').fetchall()
            artistas = [a for a in artistas if a['n']]
            hechos = {f['id'] for f in self.d['filas']}
            with self.lock:
                self.d['total'] = len(artistas)
            for i, a in enumerate(artistas):
                if self.detener.is_set():
                    break
                if a['id'] in hechos:
                    continue
                with self.lock:
                    self.d['actual'] = a['name']
                if a['nationality_source'] == 'manual':
                    res = {'codigo': paises.iso_de(a['nationality']), 'pais': a['nationality'],
                           'detalle': 'corregido a mano: no se busca'}
                else:
                    res = paises.resolver(mb, a['name'], paises.albumes_de(con, a['id']))
                with self.lock:
                    self.d['filas'].append(_fila(a, a['n'], res))
                    self.d['hechos'] = len(self.d['filas'])
                    if self.d['hechos'] % 10 == 0:
                        self._guardar()
            con.close()
            with self.lock:
                self.d.update(estado='detenido' if self.detener.is_set() else 'listo', fin=time.time(), actual=None)
                self._guardar()
        except Exception as e:
            with self.lock:
                self.d.update(estado='error', error=f'{type(e).__name__}: {e}', fin=time.time(), actual=None)
                self._guardar()

    def uno(self, db, cliente, artist_id):
        """Busca de nuevo un solo artista y devuelve su fila (también la deja en la propuesta)."""
        con = _con(db)
        try:
            paises.asegurar_columnas(con)
            a = con.execute('SELECT a.id, a.name, a.nationality, a.nationality_source, '
                            '(SELECT COUNT(*) FROM albums al WHERE al.artist_id=a.id) AS n FROM artists a WHERE a.id=?',
                            (artist_id,)).fetchone()
            if not a:
                return None
            res = paises.resolver(_mb(cliente), a['name'], paises.albumes_de(con, a['id']))
            fila = _fila(a, a['n'], res)
            if fila['tipo'] == 'manual':               # buscar de nuevo a pedido sí propone, aunque esté a mano
                fila['tipo'] = _tipo(a['nationality'], None, res.get('codigo'))
            self._reemplazar(fila)
            return fila
        finally:
            con.close()

    def _reemplazar(self, fila):
        with self.lock:
            self.d['filas'] = [f for f in self.d['filas'] if f['id'] != fila['id']] + [fila]
            if self.d.get('estado') in (None,):
                self.d['estado'] = 'parcial'
            self._guardar()

    def aplicar(self, db, ids):
        """Escribe las filas elegidas (ids) y además las «igual» (solo completan código e ID de MusicBrainz)."""
        ids = set(ids)
        n = 0
        con = _con(db)
        try:
            paises.asegurar_columnas(con)
            with self.lock:
                for f in self.d['filas']:
                    if f['aplicado'] or not f.get('codigo') or f['tipo'] == 'manual':
                        continue
                    if f['id'] in ids or f['tipo'] == 'igual':
                        paises.guardar(con, f['id'], f['codigo'], f['metodo'] or 'musicbrainz', f.get('mbid'))
                        f['aplicado'] = True
                        if f['id'] in ids:
                            n += 1
                con.commit()
                self._guardar()
        finally:
            con.close()
        return n

    def marcar_manual(self, artist_id, codigo):
        with self.lock:
            for f in self.d['filas']:
                if f['id'] == artist_id:
                    f.update(tipo='manual', aplicado=True, codigo=codigo, pais=paises.nombre_de(codigo),
                             actual=paises.nombre_de(codigo), actual_iso=codigo, fuente_actual='manual')
            self._guardar()


def fijar(db, artist_id, codigo):
    codigo = (codigo or '').lower() or None
    if codigo and codigo not in paises.PAISES:
        raise ValueError('País desconocido')
    con = _con(db)
    try:
        paises.asegurar_columnas(con)
        if not con.execute('SELECT 1 FROM artists WHERE id=?', (artist_id,)).fetchone():
            raise ValueError('Artista no encontrado')
        paises.guardar(con, artist_id, codigo, 'manual')
        con.commit()
    finally:
        con.close()


def buscar(db, q, limite=40):
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(artists)')}
        fuente = 'a.nationality_source' if 'nationality_source' in cols else 'NULL'
        filas = con.execute(f'SELECT a.id, a.name, a.nationality, {fuente} AS fuente, '
                            '(SELECT COUNT(*) FROM albums al WHERE al.artist_id=a.id) AS n '
                            'FROM artists a WHERE a.name LIKE ? ORDER BY n DESC, a.name LIMIT ?',
                            (f'%{q}%', limite)).fetchall()
        return [{'id': f['id'], 'nombre': f['name'], 'albumes': f['n'],
                 'actual': f['nationality'] if f['nationality'] and f['nationality'] != paises.SIN_PAIS else None,
                 'actual_iso': paises.iso_de(f['nationality']), 'fuente_actual': f['fuente']} for f in filas]
    finally:
        con.close()


def lista_paises():
    return sorted(({'codigo': k, 'nombre': v} for k, v in paises.PAISES.items()), key=lambda x: x['nombre'])
