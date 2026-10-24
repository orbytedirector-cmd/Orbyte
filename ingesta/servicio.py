"""
Orbyte Ingesta — servicio web (C-12, etapa 1).

Servicio aparte de Orbyte (puerto 5005 por defecto) para revisar el material
nuevo que llega a la carpeta de entrada. Se entra con una cuenta admin de
Orbyte y solo responde a la red de la casa / Tailscale.

Arranque:  ./ingesta/start.sh   (o  python -m ingesta  desde la raíz del repo)
Variables: INGESTA_PORT (5005) · INGESTA_DATOS (<repo>/ingesta_datos) · ORBYTE_DB (<repo>/music.db)
"""
import hashlib
import ipaddress
import json
import re
import logging
import os
import secrets
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.security import check_password_hash

from . import analisis
from .almacen import Almacen
from .trabajador import Procesador
from .vigilante import Vigilante
from . import metadata as metadata_mod
from . import procesar
from .almacen import PREVIOS
from . import recursos

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)
DATOS = os.environ.get('INGESTA_DATOS') or os.path.join(REPO, 'ingesta_datos')
PUERTO = int(os.environ.get('INGESTA_PORT') or 5005)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('ingesta')

os.makedirs(DATOS, exist_ok=True)
almacen = Almacen(os.path.join(DATOS, 'ingesta.db'))
if not almacen.ajustes().get('db_orbyte'):
    almacen.guardar_ajustes({'db_orbyte': os.environ.get('ORBYTE_DB') or os.path.join(REPO, 'music.db')})


def _clave_sesion():
    ruta = os.path.join(DATOS, 'secret_key')
    if not os.path.exists(ruta):
        with open(ruta, 'w') as f:
            f.write(secrets.token_hex(32))
        os.chmod(ruta, 0o600)
    with open(ruta) as f:
        return f.read().strip()


app = Flask(__name__, template_folder=os.path.join(BASE, 'templates'),
            static_folder=os.path.join(BASE, 'static'), static_url_path='/static')
app.secret_key = _clave_sesion()
app.config.update(
    # Orbyte (puerto 5001) usa la cookie «session» en el mismo host: nombre propio para no pisarla
    SESSION_COOKIE_NAME='ingesta_session',
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

vigilante = Vigilante(almacen)
procesador = Procesador(almacen)
vigilante.procesador = procesador


# ─────────────────────────── seguridad ───────────────────────────

def ip_permitida(ip):
    """Casa (LAN), el propio servidor o Tailscale (100.64.0.0/10) — mismo criterio que Orbyte."""
    try:
        ip = ipaddress.ip_address((ip or '').split('%')[0])
    except ValueError:
        return False
    if getattr(ip, 'ipv4_mapped', None):
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip in ipaddress.ip_network('100.64.0.0/10')


_fallos = {}


def _bloqueado(ip):
    ahora = time.time()
    lista = [t for t in _fallos.get(ip, []) if ahora - t < 600]
    _fallos[ip] = lista
    return len(lista) >= 5


@app.before_request
def _guardia():
    if not ip_permitida(request.remote_addr):
        abort(403)
    if request.endpoint in ('login', 'static'):
        return None
    if not session.get('uid'):
        if request.path.startswith('/api/'):
            return jsonify(error='sesion'), 401
        return redirect(url_for('login', siguiente=request.path))
    if request.method == 'POST':
        token = request.headers.get('X-CSRF') or request.form.get('csrf')
        if not token or not secrets.compare_digest(token, session.get('csrf', '')):
            abort(400, 'Token de seguridad inválido: recarga la página.')
    return None


@app.after_request
def _cabeceras(resp):
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp


@app.context_processor
def _contexto():
    return {'csrf': session.get('csrf', ''), 'usuario': session.get('email'),
            'sin_leer': almacen.avisos_sin_leer() if session.get('uid') else 0}


def _db_orbyte():
    return almacen.ajustes().get('db_orbyte')


# ─────────────────────────── formatos para plantillas ───────────────────────────

@app.template_filter('duracion')
def f_duracion(seg):
    seg = int(round(seg or 0))
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


@app.template_filter('tamano')
def f_tamano(b):
    b = float(b or 0)
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if b < 1024 or u == 'TB':
            return f'{b:.0f} {u}' if u in ('B', 'KB') else f'{b:.1f} {u}'
        b /= 1024


@app.template_filter('fecha')
def f_fecha(ts):
    if not ts:
        return '—'
    return datetime.fromtimestamp(ts).strftime('%d-%m-%Y %H:%M')


# ─────────────────────────── login ───────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        ip = request.remote_addr
        if _bloqueado(ip):
            error = 'Demasiados intentos. Espera unos minutos.'
        else:
            email = (request.form.get('email') or '').strip().lower()
            clave = request.form.get('clave') or ''
            fila = None
            db = _db_orbyte()
            try:
                con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)
                con.row_factory = sqlite3.Row
                fila = con.execute('SELECT id, email, password_hash, is_admin FROM users '
                                   'WHERE LOWER(email)=?', (email,)).fetchone()
                con.close()
            except Exception as e:
                log.error('No se pudo leer users de %s: %s', db, e)
                error = 'No se pudo leer la base de Orbyte (revisa la ruta de music.db en Ajustes).'
            if not error:
                if fila and fila['password_hash'] and check_password_hash(fila['password_hash'], clave):
                    if not fila['is_admin']:
                        error = 'Esta herramienta es solo para administradores de Orbyte.'
                    else:
                        session.clear()
                        session.permanent = True
                        session['uid'] = fila['id']
                        session['email'] = fila['email']
                        session['csrf'] = secrets.token_hex(16)
                        _fallos.pop(ip, None)
                        destino = request.args.get('siguiente') or '/'
                        if not destino.startswith('/') or destino.startswith('//'):
                            destino = '/'
                        return redirect(destino)
                else:
                    _fallos.setdefault(ip, []).append(time.time())
                    error = 'Correo o contraseña incorrectos.'
    return render_template('login.html', error=error)


@app.route('/salir', methods=['POST'])
def salir():
    session.clear()
    return redirect(url_for('login'))


# ─────────────────────────── páginas ───────────────────────────

@app.route('/')
def bandeja():
    return render_template('bandeja.html', pagina='bandeja')


@app.route('/album/<int:aid>')
def album(aid):
    a = almacen.album(aid)
    if not a:
        flash('Ese álbum ya no está en la bandeja.')
        return redirect(url_for('bandeja'))
    aj = almacen.ajustes()
    return render_template('album.html', pagina='bandeja', a=a, r=a.get('reporte'),
                           aj_borrar=aj.get('borrar_origen', '1') == '1', aj_metadata=aj.get('metadata', '1') == '1')


@app.route('/avisos')
def avisos():
    lista = almacen.avisos(200)
    almacen.marcar_leidos()
    return render_template('avisos.html', pagina='avisos', avisos=lista)


@app.route('/ajustes', methods=['GET', 'POST'])
def ajustes():
    aj = almacen.ajustes()
    errores, avisos_aj = [], []
    if request.method == 'POST':
        nuevo = {k: (request.form.get(k) or '').strip() for k in
                 ('carpeta_entrada', 'biblioteca', 'db_orbyte', 'espera_min', 'intervalo_seg')}
        nuevo['uso_cpu'] = request.form.get('uso_cpu') if request.form.get('uso_cpu') in recursos.NIVELES else 'medio'
        nuevo['musicbrainz'] = '1' if request.form.get('musicbrainz') else '0'
        for k in ('desatendido', 'borrar_origen', 'metadata'):
            nuevo[k] = '1' if request.form.get(k) else '0'
        nuevo['python_scripts'] = (request.form.get('python_scripts') or '').strip()
        if nuevo['python_scripts'] and not (os.path.isfile(nuevo['python_scripts'])
                                             and os.access(nuevo['python_scripts'], os.X_OK)):
            errores.append('El Python para los scripts no existe o no es ejecutable.')
        entrada = os.path.abspath(nuevo['carpeta_entrada']) if nuevo['carpeta_entrada'] else ''
        bib = os.path.abspath(nuevo['biblioteca'] or '/mnt/musica')
        nuevo['carpeta_entrada'], nuevo['biblioteca'] = entrada, bib
        if entrada:
            if not os.path.isdir(entrada):
                errores.append('La carpeta de entrada no existe.')
            else:
                re_e, re_b = os.path.realpath(entrada), os.path.realpath(bib)
                if re_e == re_b or re_e.startswith(re_b + os.sep):
                    errores.append('La carpeta de entrada no puede estar dentro de la biblioteca.')
                elif re_b.startswith(re_e + os.sep):
                    errores.append('La biblioteca no puede estar dentro de la carpeta de entrada.')
                elif not os.access(entrada, os.W_OK):
                    avisos_aj.append('El servicio no puede escribir en la carpeta de entrada: '
                                     'para revisar basta, pero la etapa 2 necesitará moverla.')
        if not os.path.isdir(bib):
            errores.append('La carpeta de la biblioteca no existe (¿está montado el disco?).')
        if not os.path.isfile(nuevo['db_orbyte']):
            errores.append('No se encontró el music.db de Orbyte en esa ruta.')
        for campo, mini, maxi in (('espera_min', 0, 120), ('intervalo_seg', 5, 3600)):
            try:
                v = float(nuevo[campo])
                if not mini <= v <= maxi:
                    raise ValueError
                nuevo[campo] = str(int(v)) if campo != 'espera_min' else f'{v:g}'
            except ValueError:
                errores.append(f'Valor fuera de rango en «{campo}» ({mini}–{maxi}).')
        if not errores:
            cambio_entrada = entrada != aj.get('carpeta_entrada')
            almacen.guardar_ajustes(nuevo)
            recursos.aplicar(nuevo)
            if cambio_entrada:
                almacen.avisar('info', 'Carpeta de entrada cambiada', entrada or '(ninguna)')
            vigilante.ya()
            for a in avisos_aj:
                flash(a)
            flash('Ajustes guardados.')
            return redirect(url_for('ajustes'))
        aj.update(nuevo)
    from .edicion import acoustid_disponible
    return render_template('ajustes.html', pagina='ajustes', aj=aj, errores=errores,
                           acoustid=acoustid_disponible(), n_cpus=len(recursos.ORIGINALES))


# ─────────────────────────── API ───────────────────────────

@app.route('/api/bandeja')
def api_bandeja():
    aj = almacen.ajustes()
    lista = almacen.albumes()
    for a in lista:
        if a.get('conteo'):
            a['conteo'] = json.loads(a['conteo'])
    est = vigilante.estado
    return jsonify(albumes=lista, entrada=aj.get('carpeta_entrada'), biblioteca=aj.get('biblioteca'),
                   ultimo_escaneo=est.get('ultimo_escaneo'), problema=est.get('problema_entrada'),
                   sueltos=est.get('sueltos', []), ahora=time.time(),
                   biblioteca_lista=procesador.biblioteca_lista(),
                   sin_leer=almacen.avisos_sin_leer())


@app.route('/api/album/<int:aid>/estado')
def api_album_estado(aid):
    a = almacen.album(aid)
    if not a:
        return jsonify(estado=None)
    registro = (a.get('registro') or '').splitlines()
    return jsonify(estado=a['estado'], progreso=a['progreso'], analizado=a['analizado'],
                   registro=registro[-400:])


def _requeue(aid, **campos):
    almacen.actualizar(aid, estado='en_cola', progreso=None, silencioso=1, **campos)
    vigilante.ya()


@app.route('/api/album/<int:aid>/cambios', methods=['POST'])
def api_cambios(aid):
    """Guarda lo que decides en pantalla (tags, carátula, decisiones aceptadas, año) y vuelve a revisar."""
    a = almacen.album(aid)
    if not a or a['estado'] not in PREVIOS or a['estado'] == 'analizando':
        return jsonify(ok=False, error='El álbum no se puede cambiar en este estado'), 409
    d = request.get_json(silent=True) or {}
    c = a.get('cambios') or {}
    if 'tags' in d:
        t = {k: str(v).strip()[:300] for k, v in (d['tags'] or {}).items()
             if k in ('artista', 'album', 'anio', 'genero') and str(v or '').strip()}
        if t.get('anio') and not re.fullmatch(r'(1[89]|20)\d\d', t['anio']):
            return jsonify(ok=False, error='Año inválido'), 400
        c['tags'] = t
    if 'pistas' in d:
        pistas = {}
        for k, v in (d['pistas'] or {}).items():
            v = {kk: str(vv).strip()[:300] for kk, vv in (v or {}).items()
                 if kk in ('titulo', 'artista', 'numero') and str(vv or '').strip()}
            if v:
                pistas[str(k)[:500]] = v
        c['pistas'] = pistas
    if 'caratula' in d:
        car = d['caratula']
        if car is None:
            c.pop('caratula', None)
        elif isinstance(car, dict) and car.get('tipo') in ('archivo', 'incrustada', 'caa', 'url'):
            if car['tipo'] == 'url':
                host = re.sub(r'^https?://([^/]+).*$', r'\1', car.get('url') or '')
                if not car.get('url', '').startswith('https://') or not any(
                        host == h or host.endswith('.' + h) for h in procesar.HOSTS_PORTADA):
                    return jsonify(ok=False, error='Origen de imagen no permitido'), 400
            c['caratula'] = {k: car.get(k) for k in ('tipo', 'archivo', 'mb', 'url', 'miniatura') if car.get(k)}
    if d.get('aceptar'):
        c['aceptadas'] = sorted(set(c.get('aceptadas') or []) | {d['aceptar']})
    if d.get('desaceptar'):
        c['aceptadas'] = sorted(set(c.get('aceptadas') or []) - {d['desaceptar']})
    _requeue(aid, cambios=c)
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/ubicar', methods=['POST'])
def api_ubicar(aid):
    a = almacen.album(aid)
    if not a or a['estado'] not in ('revisado', 'error_ubicar'):
        return jsonify(ok=False, error='Solo se ubican álbumes revisados sin decisiones pendientes'), 409
    if ((a.get('reporte') or {}).get('version') or 0) < analisis.VERSION_REPORTE:
        _requeue(aid)                         # revisión antigua: primero se actualiza
        return jsonify(ok=True, revisar=True)
    if a['estado'] == 'error_ubicar':        # tras un error se revisa de nuevo antes de reintentar
        _requeue(aid)
        return jsonify(ok=True, revisar=True)
    almacen.actualizar(aid, estado='por_ubicar', progreso='En cola para ubicar')
    procesador.ya()
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/metadata', methods=['POST'])
def api_metadata(aid):
    a = almacen.album(aid)
    if not a or a['estado'] not in ('error_metadata', 'completado', 'ubicado'):
        return jsonify(ok=False), 409
    almacen.actualizar(aid, estado='por_metadata', progreso='En cola para la metadata', registro=None)
    procesador.ya()
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/descartar', methods=['POST'])
def api_descartar(aid):
    a = almacen.album(aid)
    if not a or a['estado'] not in PREVIOS or a['estado'] == 'analizando':
        return jsonify(ok=False), 409
    almacen.actualizar(aid, estado='descartado', progreso=None)
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/restaurar', methods=['POST'])
def api_restaurar(aid):
    a = almacen.album(aid)
    if not a or a['estado'] != 'descartado':
        return jsonify(ok=False), 409
    _requeue(aid)
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/buscar_caratula')
def api_buscar_caratula(aid):
    a = almacen.album(aid)
    r = (a or {}).get('reporte') or {}
    if not r:
        return jsonify(resultados=[])
    ediciones = ((r.get('edicion_mb') or {}).get('candidatas') or [])
    artista = request.args.get('artista') or r.get('artista') or ''
    album_q = request.args.get('album') or r.get('album') or ''
    return jsonify(resultados=procesar.buscar_caratulas(artista, album_q, ediciones))


@app.route('/api/probar_python', methods=['POST'])
def api_probar_python():
    aj = almacen.ajustes()
    py = (request.get_json(silent=True) or {}).get('python')
    if py is not None:
        aj['python_scripts'] = py
    return jsonify(metadata_mod.probar_python(aj))


@app.route('/api/album/<int:aid>/revisar', methods=['POST'])
def api_revisar(aid):
    a = almacen.album(aid)
    if not a:
        return jsonify(ok=False), 404
    if a['estado'] in PREVIOS and a['estado'] != 'analizando':
        _requeue(aid)
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/edicion', methods=['POST'])
def api_edicion(aid):
    """Elegir (o quitar) la edición de MusicBrainz: solo se guarda la elección y se vuelve a revisar."""
    a = almacen.album(aid)
    if not a:
        return jsonify(ok=False), 404
    mbid = (request.get_json(silent=True) or {}).get('mbid')
    if mbid and not re.fullmatch(r'[0-9a-f-]{36}', mbid):
        return jsonify(ok=False, error='id inválido'), 400
    almacen.actualizar(aid, eleccion=json.dumps({'mbid': mbid}) if mbid else None, estado='en_cola', progreso=None)
    vigilante.ya()
    return jsonify(ok=True)


@app.route('/api/reintentar_errores', methods=['POST'])
def api_reintentar_errores():
    """Los «Error al ubicar» vuelven a revisarse (y luego se ubican con el botón o el modo desatendido)."""
    n = almacen.reintentar_errores_ubicar()
    vigilante.ya()
    return jsonify(ok=True, n=n)


@app.route('/api/escanear', methods=['POST'])
def api_escanear():
    vigilante.ya()
    return jsonify(ok=True)


@app.route('/api/avisos')
def api_avisos():
    return jsonify(sin_leer=almacen.avisos_sin_leer(), avisos=almacen.avisos(8))


@app.route('/api/avisos/leer', methods=['POST'])
def api_avisos_leer():
    almacen.marcar_leidos()
    return jsonify(ok=True)


@app.route('/api/carpetas')
def api_carpetas():
    """Navegador de carpetas del servidor (solo carpetas, nunca archivos)."""
    ruta = os.path.abspath(request.args.get('ruta') or '/mnt')
    while not os.path.isdir(ruta) and ruta != '/':
        ruta = os.path.dirname(ruta)
    carpetas = []
    try:
        with os.scandir(ruta) as it:
            for e in it:
                if e.name.startswith('.'):
                    continue
                try:
                    if e.is_dir():
                        carpetas.append(e.name)
                except OSError:
                    pass
    except PermissionError:
        return jsonify(ruta=ruta, padre=os.path.dirname(ruta) if ruta != '/' else None,
                       carpetas=[], error='Sin permiso para leer esta carpeta.')
    carpetas.sort(key=str.lower)
    return jsonify(ruta=ruta, padre=os.path.dirname(ruta) if ruta != '/' else None,
                   carpetas=carpetas[:500], escribible=os.access(ruta, os.W_OK),
                   truncado=len(carpetas) > 500)


@app.route('/album/<int:aid>/caratula')
def caratula(aid):
    """Carátula del álbum (o de un disco con ?disco=N); ?mini=1 = miniatura."""
    a = almacen.album(aid)
    if not a or not a.get('reporte') or not a['reporte'].get('caratula'):
        abort(404)
    r = a['reporte']
    car = r['caratula']
    if a.get('ubicado_en') and not request.args.get('r') and not request.args.get('disco'):
        final = os.path.join(a['ubicado_en'], 'Cover.jpg')     # ya está en la biblioteca: la definitiva
        if os.path.isfile(final):
            return send_file(_miniatura(final) or final if request.args.get('mini') else final, max_age=300)
        abort(404)
    if car.get('tipo') == 'caa' and car.get('mb') and not request.args.get('r') and not request.args.get('disco'):
        return redirect(f'https://coverartarchive.org/release/{car["mb"]}/front-250')
    raiz = os.path.realpath(a['ruta'])
    disco = request.args.get('disco', type=int)
    archivo = None
    if request.args.get('r'):                   # cualquier imagen candidata del álbum
        archivo = request.args.get('r')
        if not any(c.get('archivo') == archivo for c in car.get('candidatos', [])):
            abort(404)
    elif request.args.get('incrustada') and car.get('incrustada_en'):
        car = dict(car, tipo='incrustada', pista=car['incrustada_en'])
    elif disco:
        d = next((x for x in car.get('por_disco', []) if x.get('disco') == disco), None)
        archivo = d and d.get('archivo')
    elif car.get('tipo') == 'archivo':
        archivo = car.get('archivo')
    origen = None
    if archivo:
        origen = os.path.realpath(os.path.join(raiz, archivo))
        if not origen.startswith(raiz + os.sep) or not os.path.isfile(origen):
            abort(404)
    elif car.get('tipo') == 'incrustada' and not disco and not request.args.get('r'):
        pista = os.path.realpath(os.path.join(raiz, car['pista']))
        if not pista.startswith(raiz + os.sep) or not os.path.isfile(pista):
            abort(404)
        origen = _extraer_incrustada(pista)
    if not origen:
        abort(404)
    if request.args.get('mini'):
        origen = _miniatura(origen) or origen
    resp = send_file(origen, max_age=300)
    return resp


def _cache(nombre_base, ruta, ext):
    st = os.stat(ruta)
    h = hashlib.md5(f'{ruta}|{st.st_mtime}|{st.st_size}|{nombre_base}'.encode()).hexdigest()
    d = os.path.join(DATOS, 'cache')
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'{h}.{ext}')


def _extraer_incrustada(pista):
    destino = _cache('incrustada', pista, 'jpg')
    if not os.path.exists(destino):
        try:
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-i', pista, '-map', '0:v:0',
                            '-frames:v', '1', destino], timeout=60, capture_output=True)
        except Exception:
            return None
    return destino if os.path.exists(destino) else None


def _miniatura(origen):
    if analisis.Image is None:
        return None
    destino = _cache('mini', origen, 'jpg')
    if not os.path.exists(destino):
        try:
            with analisis.Image.open(origen) as im:
                im = im.convert('RGB')
                im.thumbnail((240, 240))
                im.save(destino, 'JPEG', quality=82)
        except Exception:
            return None
    return destino


def main():
    cpus = recursos.aplicar(almacen.ajustes())
    log.info('Uso del procesador: núcleos %s', cpus)
    vigilante.start()
    procesador.start()
    log.info('Orbyte Ingesta en el puerto %s · datos en %s', PUERTO, DATOS)
    app.run(host='0.0.0.0', port=PUERTO, threaded=True, debug=False, use_reloader=False)
