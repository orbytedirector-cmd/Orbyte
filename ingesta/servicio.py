"""
Orbyte Ingesta — servicio web (C-12, etapa 1).

Servicio aparte de Orbyte (puerto 5005 por defecto) para revisar el material
nuevo que llega a la carpeta de entrada. Se entra con una cuenta admin de
Orbyte y solo responde a la red de la casa / Tailscale.

Arranque:  ./ingesta/start.sh   (o  python -m ingesta  desde la raíz del repo)
Variables: INGESTA_PORT (5005) · INGESTA_DATOS (<repo>/ingesta_datos) · ORBYTE_DB (<repo>/music.db)
"""
import contextlib
import difflib
import hashlib
import ipaddress
import json
import re
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.security import check_password_hash

from . import analisis
from .almacen import Almacen
from .trabajador import Procesador, borrar_union
from .vigilante import Vigilante
from . import metadata as metadata_mod
from . import procesar
from .almacen import PREVIOS, EN_BIBLIOTECA
from . import ajustar as ajustar_mod
from . import titulos as titulos_mod
from concurrent.futures import ThreadPoolExecutor
from . import recursos
from . import compat24
from . import caratulas as caratulas_mod
from . import artistas as artistas_mod
from . import fantasmas as fantasmas_mod
from . import repetidos as repetidos_mod
from . import autocorregir as autocorregir_mod
from . import sacd as sacd_mod
from . import portada as portada_mod
from . import tienda as tienda_mod
from . import nacionalidades as nacionalidades_mod
from . import popularidad as popularidad_mod
from . import salud as salud_mod
from . import calidad as calidad_mod
from . import reparar as reparar_mod
from . import papelera as papelera_mod
from . import eliminar as eliminar_mod
import threading
import traceback

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
descargador = tienda_mod.Descargador(almacen, vigilante, DATOS)
vigilante.procesador = procesador


def _ingesta_ocupada():
    """¿La ingesta está trabajando? (la revisión de salud espera para no competir con ella)."""
    if procesador.actual or descargador.actual:
        return True
    return bool(almacen.con_estado('analizando', 'ubicando', 'metadata', 'ajustando'))


salud = salud_mod.Salud(DATOS, almacen.ajustes, _ingesta_ocupada, almacen.avisar)
saneador = salud_mod.Saneador(salud, DATOS, REPO)
papelera = papelera_mod.Papelera(DATOS)


def _papelera_bucle():
    """Cada 2 min: se vacía lo vencido y se devuelve a music.db lo de los álbumes restaurados ya reindexados."""
    while True:
        time.sleep(120)
        try:
            aj = almacen.ajustes()
            if procesador.biblioteca_lista():
                papelera.purgar(papelera_mod.horas(aj))
                papelera.reponer(aj.get('db_orbyte'), log.info)
        except Exception:  # noqa: BLE001
            log.exception('Papelera')


threading.Thread(target=_papelera_bucle, name='papelera', daemon=True).start()
try:            # los que se juntaron antes de que hubiera límite (llegaron a 274 / 78 GB)
    if almacen.ajustes().get('db_orbyte'):
        artistas_mod.podar_respaldos(almacen.ajustes()['db_orbyte'], log.info)
except Exception:  # noqa: BLE001
    log.exception('No se pudieron podar los respaldos de la base')



def _salud_revisando():
    p = salud.progreso()
    return p['activa'] and not p['pausada'] and p['motivo'] == 'Revisando'


# Calidad y LED: misma maquinaria que Salud, espera también mientras Salud decodifica (no compiten por el disco).
verificador = calidad_mod.Verificador(DATOS, almacen.ajustes, lambda: _ingesta_ocupada() or _salud_revisando(),
                                      salud, almacen.avisar)
reparador = reparar_mod.Reparador(salud, almacen.ajustes, lambda: bool(descargador.actual),
                                  al_terminar=lambda aid: _revisar_tras_reparar(aid), avisar_fn=almacen.avisar)


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

@app.template_filter('prepend_ruta')
def _f_prepend_ruta(clave, ruta):
    return os.path.join(ruta, clave)


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
    lib = lib_error = None
    if a['estado'] in EN_BIBLIOTECA:          # ya ubicado: el editor muestra lo que dicen hoy los archivos
        try:
            lib = ajustar_mod.leer(a['ubicado_en'] or a['ruta'])
        except Exception as e:
            lib_error = str(e)
    dup_filas = _comparativa_dup(a) if (a.get('reporte') or {}).get('duplicados') else []
    actuales = ajustar_mod.pistas_actuales(a.get('reporte'), lib) if lib else {}
    portada = _portada_vista(a['ubicado_en'] or a['ruta']) if lib else None
    fmt = portada_mod.formatos(a['ubicado_en'] or a['ruta'], lib['plan']) if lib else {}
    sin_indexar = a['estado'] == 'error_metadata'          # «Borrar de la biblioteca y descartar»
    return render_template('album.html', pagina='bandeja', a=a, r=a.get('reporte'), lib=lib, lib_error=lib_error,
                           actuales=actuales, portada=portada, sin_indexar=sin_indexar, fmt=fmt,
                           dup_filas=dup_filas,
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
        try:
            nuevo['buscar_estrellas'] = str(max(0, min(50, int(request.form.get('buscar_estrellas') or 5))))
        except ValueError:
            nuevo['buscar_estrellas'] = '5'
        try:
            nuevo['papelera_horas'] = f'{max(1.0, min(24 * 90.0, float(request.form.get("papelera_horas") or 48))):g}'
        except ValueError:
            nuevo['papelera_horas'] = '48'
        nuevo['python_scripts'] = (request.form.get('python_scripts') or '').strip()
        nuevo['sacd_extract'] = (request.form.get('sacd_extract') or '').strip()
        if nuevo['sacd_extract'] and not (os.path.isfile(nuevo['sacd_extract'])
                                           and os.access(nuevo['sacd_extract'], os.X_OK)):
            errores.append('sacd_extract no existe o no es ejecutable.')
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


def _editable(a):
    """Antes de ubicar (o tras un error al ubicar: el original sigue intacto en la entrada)."""
    return a['estado'] in PREVIOS + ('error_ubicar',) and a['estado'] != 'analizando'


def _requeue(aid, **campos):
    almacen.actualizar(aid, estado='en_cola', progreso=None, silencioso=1, **campos)
    vigilante.ya()


@app.route('/api/album/<int:aid>/cambios', methods=['POST'])
def api_cambios(aid):
    """Guarda lo que decides en pantalla (tags, carátula, decisiones aceptadas, año) y vuelve a revisar."""
    a = almacen.album(aid)
    if not a or not _editable(a):
        return jsonify(ok=False, error='El álbum no se puede cambiar en este estado'), 409
    d = request.get_json(silent=True) or {}
    c = a.get('cambios') or {}
    if 'tags' in d:
        t = {k: str(v).strip()[:300] for k, v in (d['tags'] or {}).items()
             if k in ('artista', 'album', 'anio', 'genero', 'genero_secundario') and str(v or '').strip()}
        if t.get('anio') and not re.fullmatch(r'(1[89]|20)\d\d', t['anio']):
            return jsonify(ok=False, error='Año inválido'), 400
        c['tags'] = t
    if 'pistas' in d:
        pistas = {}
        for k, v in (d['pistas'] or {}).items():
            v = {kk: str(vv).strip()[:300] for kk, vv in (v or {}).items()
                 if kk in ('titulo', 'artista', 'numero', 'disco') and str(vv or '').strip()}
            if 'disco' in v and not (v['disco'].isdigit() and 1 <= int(v['disco']) <= 30):
                return jsonify(ok=False, error=f'Número de disco inválido: {v["disco"]}'), 400
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
    if d.get('sacd_area') in ('estereo', 'multicanal', 'ambos'):
        c['sacd_area'] = d['sacd_area']
    if d.get('aceptar'):
        c['aceptadas'] = sorted(set(c.get('aceptadas') or []) | {d['aceptar']})
    if d.get('desaceptar'):
        c['aceptadas'] = sorted(set(c.get('aceptadas') or []) - {d['desaceptar']})
    _requeue(aid, cambios=c)
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/letras', methods=['POST'])
def api_letras(aid):
    """«Buscar letras» de la ficha: si no, RichMeta no consulta LRCLib ni Tidal en la metadata de este álbum
    (no vuelve a revisar el álbum: solo cambia cómo se correrá la metadata)."""
    a = almacen.album(aid)
    if not a:
        return jsonify(ok=False, error='No existe'), 404
    cam = dict(a.get('cambios') or {})
    if (request.get_json(silent=True) or {}).get('buscar', True):
        cam.pop('sin_letras', None)
    else:
        cam['sin_letras'] = True
    almacen.actualizar(aid, cambios=cam)
    almacen.registrar(aid, 'Letras: ' + ('se buscarán en la metadata' if 'sin_letras' not in cam
                                         else 'no se buscarán en la metadata (LRCLib ni Tidal)'))
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/autocorregir', methods=['POST'])
def api_autocorregir(aid):
    """Propuesta de tags ordenados (no guarda nada: la pantalla la muestra y tú decides)."""
    a = almacen.album(aid)
    rep = (a or {}).get('reporte')
    if a and a['estado'] in EN_BIBLIOTECA:           # ya ubicado: se parte de lo que dicen hoy los archivos
        try:
            lib = ajustar_mod.leer(a['ubicado_en'] or a['ruta'])
        except ajustar_mod.FalloAjuste as e:
            return jsonify(ok=False, error=str(e)), 409
        rep = dict(lib, edicion_mb=(rep or {}).get('edicion_mb'), cambios={})
    elif not rep or not rep.get('plan'):
        return jsonify(ok=False, error='El álbum todavía no tiene revisión'), 409
    else:
        rep = dict(rep, cambios=a.get('cambios') or rep.get('cambios') or {})
    try:
        bib = analisis.biblioteca(almacen.ajustes().get('db_orbyte'), almacen.ajustes().get('biblioteca') or '/mnt/musica')
    except Exception:
        bib = None
    return jsonify(ok=True, **autocorregir_mod.proponer(rep, bib))


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
    if not a or not _editable(a):
        return jsonify(ok=False), 409
    if a.get('origen') == 'reproceso':          # cancelar: vuelve a como estaba en la biblioteca (nada se tocó)
        almacen.cancelar_reproceso(aid)
        union = (a.get('cambios') or {}).get('union')
        if union:                                # «Unir como discos»: solo se borran los enlaces armados
            borrar_union(almacen, a['ruta'])
            return jsonify(ok=True, ir=_ficha_de_ruta(union[0]))
        return jsonify(ok=True, ir=_ficha_de_ruta(a['ruta']))
    almacen.actualizar(aid, estado='descartado', progreso=None)
    return jsonify(ok=True)


def _en_music_db(ruta):
    """¿Orbyte tiene este álbum? (si la metadata falló en el indexador, la carpeta está en la biblioteca pero no en Orbyte)."""
    if not ruta:
        return False
    try:
        con = sqlite3.connect(f'file:{almacen.ajustes()["db_orbyte"]}?mode=ro', uri=True, timeout=20)
        try:
            return con.execute('SELECT 1 FROM albums WHERE path=? OR path=?',
                               (ruta.rstrip('/'), ruta.rstrip('/') + '/')).fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return True                  # ante la duda, no se ofrece borrar


@app.route('/api/album/<int:aid>/descartar_borrar', methods=['POST'])
def api_descartar_borrar(aid):
    """Antes de ubicar: se borra la carpeta de la entrada (p. ej. una descarga en AAC) y el álbum sale de la bandeja."""
    a = almacen.album(aid)
    if not a or not (_editable(a) or a['estado'] == 'descartado') or a.get('origen') == 'reproceso':
        return jsonify(ok=False, error='Solo para álbumes que aún no se ubican'), 409
    if a['estado'] == 'analizando' or procesador.actual == aid:
        return jsonify(ok=False, error='Se está revisando: espera a que termine'), 409
    ruta = (a.get('ruta') or '').rstrip('/')
    if ruta.endswith(sacd_mod.SUFIJO_MC):
        return jsonify(ok=False, error='Es la parte multicanal de un ISO: descarta la estéreo para borrar el ISO'), 409
    entrada = os.path.realpath((almacen.ajustes().get('carpeta_entrada') or '').strip() or '/nonexistent')
    real = os.path.realpath(ruta) if ruta else ''
    if not real or not real.startswith(entrada + os.sep):
        return jsonify(ok=False, error='La carpeta no está dentro de la carpeta de entrada'), 409
    if os.path.isdir(real):
        shutil.rmtree(real)
    elif os.path.isfile(real):
        os.remove(real)
    padre = os.path.dirname(real)                    # la carpeta que la contenía, solo si quedó vacía
    while padre.startswith(entrada + os.sep):
        try:
            os.rmdir(padre)
        except OSError:
            break
        padre = os.path.dirname(padre)
    almacen.borrar_album(aid)
    almacen.avisar('ok', f'Borrado de la entrada: {a.get("artista") or a.get("rel")} — {a.get("album") or ""}'.rstrip(' —'),
                   os.path.relpath(real, entrada))
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/descartar_ubicado', methods=['POST'])
def api_descartar_ubicado(aid):
    """Álbum ubicado cuya metadata falló antes de entrar a Orbyte: se borra su carpeta de la biblioteca y se descarta."""
    a = almacen.album(aid)
    if not a or a['estado'] != 'error_metadata':
        return jsonify(ok=False, error='Solo para álbumes con error en la metadata'), 409
    ruta = (a.get('ubicado_en') or '').rstrip('/')
    raiz = os.path.realpath(almacen.ajustes().get('biblioteca') or '/mnt/musica')
    if _en_music_db(ruta):
        # Orbyte alcanzó a indexarlo (en parte): la eliminación segura lo quita de la base y del disco, y su fila
        # de la Bandeja sale sola al terminar
        con = sqlite3.connect(f'file:{almacen.ajustes()["db_orbyte"]}?mode=ro', uri=True, timeout=20)
        try:
            r = con.execute('SELECT id FROM albums WHERE path=? OR path=?', (ruta, ruta + '/')).fetchone()
        finally:
            con.close()
        e, _db = _edicion_de_oid(r[0]) if r else (None, None)
        if not e:
            return jsonify(ok=False, error='No se encontró el álbum en Orbyte'), 409
        ok, res = _eliminar_seguro(e)
        if not ok:
            return jsonify(ok=False, error=res), 409
        return jsonify(ok=True, en_curso=True)
    if ruta and os.path.isdir(ruta):
        real = os.path.realpath(ruta)
        if not real.startswith(raiz + os.sep) or real.count(os.sep) - raiz.count(os.sep) < 3:
            return jsonify(ok=False, error='La carpeta no está dentro de la biblioteca'), 409
        shutil.rmtree(real)
        try:
            os.rmdir(os.path.dirname(real))          # la carpeta del artista, solo si quedó vacía
        except OSError:
            pass
    almacen.actualizar(aid, estado='descartado', progreso=None)
    almacen.avisar('ok', f'Descartado: {a.get("artista")} — {a.get("album")}', f'Se borró {ruta or "(sin carpeta)"}')
    return jsonify(ok=True)


# ─────────────────────── «Ya está en la biblioteca»: reemplazar, descartar o conservar ambos ───────────────────────

@app.route('/api/album/<int:aid>/duplicado', methods=['POST'])
def api_duplicado(aid):
    a = almacen.album(aid)
    rep = (a or {}).get('reporte') or {}
    if not a or not _editable(a) or not rep.get('duplicados'):
        return jsonify(ok=False, error='El álbum no se puede cambiar en este estado'), 409
    d = request.get_json(silent=True) or {}
    accion = d.get('accion')
    c = dict(a.get('cambios') or {})
    aceptadas = set(c.get('aceptadas') or [])
    rutas_dup = {x['ruta'].rstrip('/') for x in rep['duplicados']}
    if accion == 'reemplazar':
        rutas = [r.rstrip('/') for r in d.get('rutas') or [] if r and r.rstrip('/') in rutas_dup]
        if not rutas:
            return jsonify(ok=False, error='Marca qué edición(es) de la biblioteca se reemplazan'), 400
        c['reemplazar'] = rutas
        c['aceptadas'] = sorted(aceptadas | {'duplicado'})
    elif accion == 'ambos':
        c.pop('reemplazar', None)
        c['aceptadas'] = sorted(aceptadas | {'duplicado'})
    elif accion == 'deshacer':
        c.pop('reemplazar', None)
        c['aceptadas'] = sorted(aceptadas - {'duplicado'})
    elif accion == 'descartar' and a.get('origen') == 'reproceso':
        almacen.cancelar_reproceso(aid)
        return jsonify(ok=True, ir=_ficha_de_ruta(a['ruta']))
    elif accion == 'descartar':
        aj = almacen.ajustes()
        real, _ = sacd_mod.separar(a['ruta'])
        hermanos = [x for x in almacen.albumes() if x['id'] != aid and sacd_mod.separar(x['ruta'])[0] == real
                    and x['estado'] not in ('descartado',)]
        if hermanos:
            return jsonify(ok=False, error='La otra área de esta ISO sigue en la bandeja: descártala o ubícala primero'), 409
        try:
            procesar.borrar_de_entrada(real, aj.get('carpeta_entrada'))
        except Exception as e:
            return jsonify(ok=False, error=f'No se pudo borrar de la entrada: {e}'), 400
        sacd_mod.limpiar(DATOS, entrada=real)
        almacen.borrar_album(aid)
        almacen.avisar('ok', f'{rep.get("artista")} — {rep.get("album")}: descartado',
                       'Se dejó la edición que ya estaba en la biblioteca y se borró la nueva de la entrada')
        return jsonify(ok=True, ir='/')
    else:
        return jsonify(ok=False, error='Acción desconocida'), 400
    _requeue(aid, cambios=c)
    return jsonify(ok=True)


_ia_dup = {}        # álbum → {'estado': 'midiendo'|'consultando'|'listo'|'error', 'texto', 'avance'}
PREGUNTA_DUP = ('Estoy por agregar la edición marcada NUEVA y ya tengo las otras en mi biblioteca. Sobre todo por '
                'calidad de sonido y producción (masterización o remasterización, fuente, prensaje, reputación de la '
                'edición), dime cuál me conviene conservar: reemplazar las que tengo por la nueva, quedarme con la que '
                'tengo y descartar la nueva, o conservar ambas. Que el formato sea el mismo no significa que la '
                'calidad lo sea.')


def _grupo_ingesta(a):
    """La edición nueva (aún en la entrada) junto a las de la biblioteca, con la forma que usa repetidos."""
    rep = a['reporte']
    db = almacen.ajustes()['db_orbyte']
    dups = rep.get('duplicados') or []
    por_ruta = {e['ruta']: e for e in repetidos_mod.ediciones_de(db, [d['ruta'] for d in dups])}
    if por_ruta:
        repetidos_mod.detalle(db, {'ediciones': list(por_ruta.values())})
    # letras fijas: A = la nueva; B, C… = las de la biblioteca en el orden en que se muestran en la ficha
    existentes = []
    for i, d in enumerate(dups):
        e = por_ruta.get(d['ruta'].rstrip('/')) or {
            'ruta': d['ruta'].rstrip('/'), 'nombre': d['album'], 'anio': d.get('anio'), 'calidad': d.get('tag'),
            'formato': d.get('formato') or '', 'n': d.get('pistas') or 0, 'dur': 0, 'bytes': 0, 'albumes': d.get('ids') or []}
        e['letra'] = chr(66 + i)
        existentes.append(e)
    pistas = [p for d in rep.get('discos') or [] for p in d.get('pistas') or [] if p.get('legible')]
    el = (rep.get('edicion_mb') or {}).get('elegida') or {}
    ta = rep.get('tags_album') or {}
    nueva = {'ruta': rep['carpeta'], 'nueva': True, 'letra': 'A', 'nombre': rep.get('album'), 'anio': rep.get('anio'), 'calidad': (rep.get('calidad') or {}).get('tag'),
             'formato': (rep.get('calidad') or {}).get('formato') or '', 'n': rep.get('n_pistas') or len(pistas),
             'dur': rep.get('duracion') or 0, 'bytes': rep.get('tamano') or 0,
             'canales': max([p.get('canales') or 0 for p in pistas] + [0]),
             'pistas_lista': [{'t': x.get('titulo'), 'd': round(x.get('duracion_origen') or 0)} for x in rep.get('plan') or []],
             'tags': {k: v for k, v in {'sello': el.get('sello') or ta.get('sello'), 'catalogo': el.get('catalogo') or ta.get('catalogo'),
                                        'pais': el.get('pais') or ta.get('pais'), 'fecha_edicion': el.get('fecha'),
                                        'barcode': el.get('barcode') or ta.get('barcode'), 'mbid': el.get('id')}.items() if v},
             'archivos': [{'file_path': os.path.join(rep['carpeta'], p['archivo']), 'duration': p.get('duracion') or 0}
                          for p in pistas]}
    nueva['multicanal'] = repetidos_mod._multicanal(nueva)
    eds = [nueva] + existentes
    ref = max(eds, key=lambda e: e['n'])
    for e in eds:
        e['relacion'] = 'referencia' if e is ref else repetidos_mod._comparar(e.get('pistas_lista') or [],
                                                                               ref.get('pistas_lista') or [])
    return {'artista': rep.get('artista'), 'album': rep.get('album'), 'ediciones': eds}


def _comparativa_dup(a):
    """Filas para la tabla «Comparar ediciones» de la ficha: misma letra que ve la IA."""
    try:
        g = _grupo_ingesta(a)
    except Exception:
        log.error('Comparativa %s:\n%s', a['id'], traceback.format_exc())
        return []
    filas = []
    for e in g['ediciones']:
        t = e.get('tags') or {}
        m = almacen.medicion(e['ruta'], e['bytes']) if e.get('bytes') else None
        filas.append({'letra': e['letra'], 'nueva': bool(e.get('nueva')), 'ruta': e['ruta'],
                      'nombre': e.get('nombre'), 'anio': e.get('anio'), 'calidad': e.get('calidad'),
                      'formato': e.get('formato'), 'multicanal': e.get('multicanal'), 'n': e.get('n'),
                      'dur': e.get('dur'), 'bytes': e.get('bytes'), 'relacion': e.get('relacion'),
                      'edicion': ' · '.join(str(x) for x in (t.get('sello'), t.get('catalogo'), t.get('pais'),
                                                              t.get('fecha_edicion')) if x),
                      'medicion': m if m and m.get('lufs') is not None else None})
    return filas


def _firma_dup(rep):
    return '|'.join(sorted(d['ruta'] for d in rep.get('duplicados') or [])) + f':{rep.get("tamano")}'


def _ia_trabajo(aid):
    st = _ia_dup[aid]
    try:
        a = almacen.album(aid)
        g = _grupo_ingesta(a)
        aj = almacen.ajustes()
        mediciones = {}
        for e in g['ediciones']:
            m = almacen.medicion(e['ruta'], e['bytes'])
            if not m:
                st.update(estado='midiendo', texto=f'Midiendo el master: {"la nueva" if e.get("nueva") else os.path.basename(e["ruta"])}',
                          avance=[0, 0])
                aviso = lambda i, n: st.update(avance=[i, n])
                if e.get('nueva'):
                    m = repetidos_mod.medir_archivos([x for x in e['archivos'] if os.path.isfile(x['file_path'])],
                                                     aviso, recursos.nucleos(aj))
                else:
                    m = repetidos_mod.medir(aj['db_orbyte'], e, aviso, recursos.nucleos(aj))
                almacen.guardar_medicion(e['ruta'], e['bytes'], m)
            mediciones[e['ruta']] = m
        st.update(estado='consultando', texto='Consultando a la IA…', avance=[0, 0])
        r = repetidos_mod.sugerir(g, mediciones, PREGUNTA_DUP)
        if r.get('error'):
            st.update(estado='error', texto=r['error'])
            return
        r['nueva'] = g['ediciones'][0]['ruta']
        r['mediciones'] = mediciones
        almacen.guardar_sugerencia(f'ingesta:{aid}', _firma_dup(a['reporte']), r)
        st.update(estado='listo', texto='')
    except Exception as e:
        log.error('IA duplicado %s:\n%s', aid, traceback.format_exc())
        st.update(estado='error', texto=f'{type(e).__name__}: {e}')


@app.route('/api/album/<int:aid>/duplicado/ia', methods=['GET', 'POST'])
def api_duplicado_ia(aid):
    a = almacen.album(aid)
    rep = (a or {}).get('reporte') or {}
    if not a or not rep.get('duplicados'):
        return jsonify(ok=False, error='Este álbum no tiene ediciones en la biblioteca'), 404
    st = _ia_dup.get(aid) or {}
    if request.method == 'POST' and st.get('estado') not in ('midiendo', 'consultando'):
        if not (os.environ.get('GEMINI_API_KEY') or os.environ.get('GROQ_API_KEY')):
            return jsonify(ok=False, error='No hay GEMINI_API_KEY ni GROQ_API_KEY en el .env'), 409
        st = _ia_dup[aid] = {'estado': 'midiendo', 'texto': 'Preparando…', 'avance': [0, 0]}
        threading.Thread(target=_ia_trabajo, args=(aid,), daemon=True, name=f'ia-duplicado-{aid}').start()
    sug = almacen.sugerencia(f'ingesta:{aid}', _firma_dup(rep))
    if sug and sug.get('v') != repetidos_mod.VERSION_IA:
        sug = None
    return jsonify(ok=True, estado=st.get('estado'), texto=st.get('texto'), avance=st.get('avance'), sugerencia=sug)


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


@app.route('/api/probar_sacd', methods=['POST'])
def api_probar_sacd():
    ruta = ((request.get_json(silent=True) or {}).get('ruta') or '').strip() or None
    return jsonify(sacd_mod.probar(ruta))


@app.route('/api/album/<int:aid>/revisar', methods=['POST'])
def api_revisar(aid):
    a = almacen.album(aid)
    if not a:
        return jsonify(ok=False), 404
    if _editable(a):
        _requeue(aid)
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/sacar_de_cola', methods=['POST'])
def api_sacar_de_cola(aid):
    """«En cola para ubicar» → «Lista», para poder editarlo antes de que empiece a copiarse."""
    if not almacen.cambiar_estado(aid, 'por_ubicar', 'revisado', progreso=None):
        return jsonify(ok=False, error='Ya empezó a ubicarse: espera a que termine y ajústalo en la biblioteca'), 409
    return jsonify(ok=True)


@app.route('/api/album/<int:aid>/ajustar', methods=['POST'])
def api_ajustar(aid):
    """Tags de un álbum ya ubicado: se valida aquí y el procesador los escribe en la biblioteca."""
    d = request.get_json(silent=True) or {}
    error, codigo = _encolar_ajuste(almacen.album(aid), {'tags': d.get('tags') or {}, 'pistas': d.get('pistas') or {}})
    if error:
        return jsonify(ok=False, error=error), codigo
    procesador.ya()
    return jsonify(ok=True)


def _encolar_ajuste(a, pedido, lote=False):
    """Valida el pedido y deja el álbum «En cola: ajustar tags». Devuelve (error, código) o (None, None)."""
    if not a or a['estado'] not in EN_BIBLIOTECA:
        return 'Solo se ajustan álbumes ya ubicados y con la metadata terminada (o con error de metadata)', 409
    try:
        prop = ajustar_mod.propuesta(ajustar_mod.leer(a['ubicado_en'] or a['ruta']), pedido)
    except ajustar_mod.FalloAjuste as e:
        return str(e), 400
    if not prop['album_cambia'] and not any(p['cambio'] for p in prop['pistas']):
        return 'No hay cambios que guardar', 400
    cam = dict(a.get('cambios') or {}, biblioteca=dict(pedido, volver=a['estado'], lote=lote))
    if not almacen.cambiar_estado(a['id'], a['estado'], 'por_ajustar', progreso='En cola para ajustar tags',
                                  cambios=json.dumps(cam, ensure_ascii=False)):
        return 'El álbum cambió de estado: recarga la página', 409
    return None, None


# ─────────────────────────── recientes y revisión de títulos de la biblioteca ───────────────────────────

VENTANAS = {24: '24 h', 72: '72 h', 168: '7 días', 720: '30 días'}


def _titulos():
    db = almacen.ajustes().get('db_orbyte')
    return titulos_mod.detectar(db, almacen.titulos_ignorados()) if db and os.path.isfile(db) else \
        {'albumes': [], 'total': 0, 'ignorados': 0, 'por_ruta': {}}


@app.route('/recientes')
def recientes_pagina():
    try:
        h = int(request.args.get('h') or 72)
    except ValueError:
        h = 72
    h = h if h in VENTANAS else 72
    filas = almacen.recientes(time.time() - h * 3600)
    det = _titulos()
    ign = almacen.titulos_ignorados()
    for f in filas:
        x = det['por_ruta'].get((f['ubicado_en'] or '').rstrip('/'))
        f['reglas'] = x['reglas'] if x and x['id'] not in ign else {}
        f['oid'] = x['id'] if x else None
    return render_template('recientes.html', pagina='recientes', filas=filas, h=h, VENTANAS=VENTANAS,
                           REGLAS=titulos_mod.REGLAS)


@app.route('/biblioteca')
def biblioteca_pagina():
    return render_template('biblioteca.html', pagina='biblioteca', REGLAS=titulos_mod.REGLAS)


@app.route('/api/biblioteca/titulos')
def api_biblioteca_titulos():
    db = almacen.ajustes().get('db_orbyte')
    if not db or not os.path.isfile(db):
        return jsonify(error='No se encontró music.db (Ajustes)'), 409
    if request.args.get('ignorados'):
        ign = almacen.titulos_ignorados()
        todos = titulos_mod.detectar(db)['albumes']
        lista = [a for a in todos if a['id'] in ign]
    else:
        d = _titulos()
        lista = d['albumes']
    proc = almacen.estados_en_biblioteca()
    return jsonify(albumes=[dict({k: a[k] for k in ('id', 'artista', 'album', 'anio', 'ruta', 'n_pistas', 'reglas',
                                                     'ejemplos')}, proceso=proc.get((a['ruta'] or '').rstrip('/')))
                            for a in lista],
                   ignorados=len(almacen.titulos_ignorados()), raiz=almacen.ajustes().get('biblioteca') or '/mnt/musica')


@app.route('/api/biblioteca/buscar')
def api_biblioteca_buscar():
    """Álbumes de toda la biblioteca cuyo artista o título contiene el texto (sin acentos ni mayúsculas)."""
    q = (request.args.get('q') or '').strip()
    aj = almacen.ajustes()
    if len(q) < 2:
        return jsonify(albumes=[], mas=False)
    try:
        bib = analisis.biblioteca(aj.get('db_orbyte'), aj.get('biblioteca') or '/mnt/musica')
    except Exception as e:
        return jsonify(error=f'No se pudo leer music.db: {e}'), 409
    palabras = analisis._norm(q).split()
    hallados = [g for g in bib['grupos']
                if all(p in f'{g["n_artista"]} {analisis._norm(g["album"])}' for p in palabras)]
    hallados.sort(key=lambda g: (g['n_artista'], str(g.get('anio') or ''), g['n_album']))
    return jsonify(mas=len(hallados) > 200, albumes=[
        {'id': g['ids'][0], 'artista': g['artista'], 'album': g['album'], 'anio': g.get('anio'), 'ruta': g['ruta'],
         'n_pistas': g['pistas'], 'tag': g.get('tag'), 'formato': g.get('formato')} for g in hallados[:200]])


def _edicion_de_oid(oid):
    """(edición tal como la ve Repetidos, db) del álbum de Orbyte `oid` — la carpeta entera si es de varios discos."""
    o = _album_orbyte(oid)
    if not o or not o['ruta']:
        return None, None
    db = almacen.ajustes().get('db_orbyte')
    ruta = repetidos_mod._base_edicion(o['ruta'])
    eds = repetidos_mod.ediciones_de(db, [ruta])
    return (eds[0] if eds else None), db


def _limpio(nombre):
    n = repetidos_mod._nombre_limpio(nombre)
    n = re.sub(r'\s*\((?:live|en vivo|en directo|remastered|mono|stereo)[^)]*\)', '', n, flags=re.I)
    return analisis._norm(n)


@app.route('/api/biblioteca/album/<int:oid>/info')
def api_biblioteca_album_info(oid):
    """Datos técnicos de un álbum de la biblioteca para el modal «Más información»."""
    e, db = _edicion_de_oid(oid)
    if not e:
        return jsonify(error='El álbum ya no está en Orbyte'), 404
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute('PRAGMA table_info(tracks)')}
        extra = [c for c in ('sample_rate', 'bit_depth', 'bitrate', 'codec', 'file_size', 'channels', 'is_dsd', 'dsd_rate')
                 if c in cols]
        m = ','.join('?' * len(e['albumes']))
        pistas = [dict(p) for p in con.execute(
            f'SELECT title, track_number, disc_number, duration, file_path{"".join(", " + c for c in extra)} FROM tracks '
            f'WHERE album_id IN ({m}) ORDER BY disc_number, CAST(track_number AS INTEGER)', e['albumes'])]
    finally:
        con.close()
    for p in pistas:
        p['archivo'] = os.path.basename(p.pop('file_path') or '')
    tags = repetidos_mod._tags_edicion(os.path.join(e['ruta'], pistas[0]['archivo'])) if pistas else {}
    otros = []
    try:
        for n in sorted(os.listdir(e['ruta'])):
            if os.path.splitext(n)[1].lower() in analisis.IMAGEN_EXT | {'.pdf', '.cue', '.log', '.txt', '.nfo'} \
                    or os.path.isdir(os.path.join(e['ruta'], n)):
                otros.append(n + ('/' if os.path.isdir(os.path.join(e['ruta'], n)) else ''))
    except OSError:
        pass
    return jsonify(id=oid, artista=e['artista'], album=e['nombre'], anio=e['anio'], ruta=e['ruta'],
                   formato=e['formato'], calidad=e['calidad'], multicanal=e.get('multicanal'), n=e['n'], dur=e['dur'],
                   bytes=e['bytes'], sr=e['sr'], bits=e['bits'], bitrate=e['bitrate'], codecs=e['codecs'],
                   escuchas=e['escuchas'], playlists=e['playlists'], favoritos=e['favoritos'],
                   medicion=almacen.medicion(e['ruta'], e['bytes']), tags=tags, pistas=pistas, otros=otros[:40],
                   raiz=almacen.ajustes().get('biblioteca') or '/mnt/musica')


@app.route('/api/biblioteca/album/<int:oid>/eliminar', methods=['GET', 'POST'])
def api_biblioteca_album_eliminar(oid):
    """GET: qué se borra y a qué edición pueden pasar sus escuchas, favoritos y playlists.
    POST {destino: ruta|null, confirmacion: 'ELIMINAR'}: la misma eliminación segura de Repetidos."""
    e, db = _edicion_de_oid(oid)
    if not e:
        return jsonify(ok=False, error='El álbum ya no está en Orbyte'), 404
    todas = repetidos_mod._ediciones(db)
    yo = _limpio(e['nombre'])
    candidatos = []
    for x in todas.values():
        if x['ruta'] == e['ruta'] or x['artist_id'] != e['artist_id'] or not x['n']:
            continue
        sim = difflib.SequenceMatcher(None, yo, _limpio(x['nombre'])).ratio()
        candidatos.append((sim, repetidos_mod._calidad(x)))
    candidatos.sort(key=lambda c: -c[0])
    opciones = [{'ruta': x['ruta'], 'album': x['nombre'], 'anio': x['anio'], 'formato': x['formato'],
                 'calidad': x['calidad'], 'n': x['n'], 'similitud': round(sim, 2)} for sim, x in candidatos[:8]]
    if request.method == 'GET':
        return jsonify(ok=True, ruta=e['ruta'], album=e['nombre'], artista=e['artista'], n=e['n'], bytes=e['bytes'],
                       formato=e['formato'], escuchas=e['escuchas'], playlists=e['playlists'], favoritos=e['favoritos'],
                       opciones=opciones, sugerida=opciones[0]['ruta'] if opciones and opciones[0]['similitud'] >= 0.6 else None)
    d = request.get_json(silent=True) or {}
    if d.get('confirmacion') != 'ELIMINAR':
        return jsonify(ok=False, error='Falta la confirmación'), 400
    destino = d.get('destino') or None
    if destino and destino not in {o['ruta'] for o in opciones}:
        return jsonify(ok=False, error='Esa edición ya no está: recarga la página'), 409
    ok, res = _eliminar_seguro(e, destino, todas)
    return jsonify(ok=True, uid=res) if ok else (jsonify(ok=False, error=res), 409)


def _eliminar_seguro(e, destino=None, todas=None):
    """La eliminación segura de Repetidos (en segundo plano). (True, uid) o (False, motivo)."""
    if procesador.actual:
        return False, 'Hay un álbum procesándose: espera a que termine'
    if not procesador.biblioteca_lista():
        return False, 'La biblioteca no está montada'
    if not _unificando.acquire(blocking=False):
        return False, 'Hay otra operación sobre la base en curso'
    if todas is None:
        todas = {}
    aj = almacen.ajustes()
    titulo = f'{e["artista"]} — {e["nombre"]}'
    grupo = {'ediciones': [e] + ([todas[destino]] if destino else [])}
    uid = almacen.nueva_unificacion('repetidos', e['ruta'], titulo)

    def trabajo():
        try:
            res = repetidos_mod.eliminar(aj['db_orbyte'], aj.get('biblioteca') or '/mnt/musica', grupo, [e['ruta']],
                                         destino, lambda m: almacen.unificacion_log(uid, m), sin_destino=not destino)
            while True:                              # sus filas de Ingesta (si las ubicó ella) también salen
                fila = almacen.fila_de_ruta(e['ruta'])
                if not fila:
                    break
                almacen.borrar_album(fila)
            almacen.cerrar_unificacion(uid, 'ok', res)
            almacen.avisar('ok', f'Eliminado: {titulo}', f'{res["liberado"] / 1e9:.2f} GB liberados · '
                           + (f'{res["movidas"]} referencias pasadas a «{os.path.basename(destino)}»' if destino
                              else 'su historial no pasó a otra edición'))
        except Exception as ex:
            log.error('Eliminar %s:\n%s', titulo, traceback.format_exc())
            almacen.unificacion_log(uid, f'ERROR: {type(ex).__name__}: {ex}')
            almacen.cerrar_unificacion(uid, 'error', {'error': str(ex)})
            almacen.avisar('error', f'No se pudo eliminar: {titulo}', str(ex))
        finally:
            _cache_repetidos['clave'] = None
            _unificando.release()

    threading.Thread(target=trabajo, daemon=True, name='eliminar-biblioteca').start()
    return True, uid


def _album_orbyte(oid):
    db = almacen.ajustes().get('db_orbyte')
    if not db or not os.path.isfile(db):
        return None
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute('SELECT al.id, al.name AS album, al.year AS anio, al.path AS ruta, al.cover_path, '
                        'ar.name AS artista FROM albums al LEFT JOIN artists ar ON ar.id = al.artist_id '
                        'WHERE al.id=?', (oid,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _propuesta_lib(ruta, fila=None):
    lib = ajustar_mod.leer(ruta)
    rep = dict(lib, edicion_mb=((fila or {}).get('reporte') or {}).get('edicion_mb'), cambios={})
    aj = almacen.ajustes()
    try:
        bib = analisis.biblioteca(aj.get('db_orbyte'), aj.get('biblioteca') or '/mnt/musica')
    except Exception:
        bib = None
    return autocorregir_mod.proponer(rep, bib)


@app.route('/biblioteca/album/<int:oid>')
def biblioteca_album(oid):
    o = _album_orbyte(oid)
    if not o or not o['ruta']:
        flash('Ese álbum ya no está en Orbyte.')
        return redirect(url_for('biblioteca_pagina'))
    aid = almacen.fila_de_ruta(o['ruta'])
    a = almacen.album(aid) if aid else None
    if a and a.get('origen') == 'reproceso' and a['estado'] not in EN_BIBLIOTECA:
        return redirect(url_for('album', aid=aid))            # se está reprocesando: su ficha de Bandeja
    if a and a.get('reporte') and not a['reporte'].get('fallo') and 'discos' in a['reporte']:
        return redirect(url_for('album', aid=aid))            # lo ubicó Ingesta: su ficha completa
    lib = lib_error = None
    if not a or a['estado'] in EN_BIBLIOTECA:
        try:
            lib = ajustar_mod.leer(o['ruta'])
        except Exception as e:
            lib_error = str(e)
    x = _titulos()['por_ruta'].get(o['ruta'].rstrip('/'))
    portada = _portada_vista(o['ruta'], o.get('cover_path'))
    fmt = portada_mod.formatos(o['ruta'], lib['plan']) if lib else {}
    base = _base_multidisco(o['ruta'])
    n_discos = len([h for h in os.listdir(base) if os.path.isdir(os.path.join(base, h))
                    and analisis.dividir_por_disco(h) is not None]) if base != o['ruta'].rstrip('/') else 0
    return render_template('biblioteca_album.html', pagina='biblioteca', o=o, a=a, lib=lib, lib_error=lib_error,
                           portada=portada, fmt=fmt, n_discos=n_discos,
                           reglas=(x or {}).get('reglas') or {}, REGLAS=titulos_mod.REGLAS,
                           ignorado=oid in almacen.titulos_ignorados())


# ─────────────────────────── buscar y descargar (Tidal / Qobuz) ───────────────────────────

@app.route('/buscar')
def buscar_pagina():
    return render_template('buscar.html', pagina='buscar', q=request.args.get('q', ''),
                           modo=request.args.get('modo', 'album'))


@app.route('/descargas')
def descargas_pagina():
    return render_template('descargas.html', pagina='descargas')


@app.route('/api/tienda/estado')
def api_tienda_estado():
    return jsonify(tienda_mod.estado())


def _bib_tienda():
    aj = almacen.ajustes()
    try:
        return analisis.biblioteca(aj.get('db_orbyte'), aj.get('biblioteca') or '/mnt/musica')
    except Exception:
        return None


@app.route('/api/tienda/buscar')
def api_tienda_buscar():
    q = (request.args.get('q') or '').strip()
    modo = 'artista' if request.args.get('modo') == 'artista' else 'album'
    if len(q) < 2:
        return jsonify(grupos=[], errores={}, artista=None)
    res = tienda_mod.buscar(q, modo, _bib_tienda())
    if modo == 'artista' and res['grupos']:
        # popularidad: 0,7 Last.fm + 0,3 Tidal; dentro de cada sección, los más escuchados primero
        try:
            top = popularidad_mod.tidal_top(tienda_mod.cliente('tidal'), res.get('tidal_artista')) \
                if 'tidal' not in res['errores'] else []
        except Exception:  # noqa: BLE001
            top = []
        lfm = popularidad_mod.lastfm(almacen, res.get('artista') or q)
        try:
            estrellas = max(0, int(almacen.ajustes().get('buscar_estrellas') or 5))
        except ValueError:
            estrellas = 5
        popularidad_mod.anotar(res['grupos'], lfm, top, estrellas)
        res['popularidad'] = {'lastfm': bool(lfm and lfm.get('albumes')), 'tidal': bool(top), 'estrellas': estrellas}
        orden = {'album': 0, 'ep': 1, 'recopilatorio': 2, 'en_vivo': 3}
        res['grupos'].sort(key=lambda g: (orden.get(g['tipo'], 9),
                                          -(g['pop']['score'] if g['pop']['score'] is not None else -1),
                                          -(int(g['anio'] or 0))))
    res.pop('tidal_artista', None)
    # resolución de Tidal ya conocida (caché): se completa al tiro
    for g in res['grupos']:
        for v in g['versiones']:
            if v['servicio'] == 'tidal' and not v.get('resolucion_exacta'):
                c = almacen.sugerencia(f'tienda:res:tidal:{v["id"]}', tienda_mod.VERSION_CACHE)
                if c:
                    v.update(bits=c['bits'], sr=c['sr'], resolucion_exacta=True)
        tienda_mod.sugerir(g)
        g['medicion'] = None
        t = next((v for v in g['versiones'] if v['servicio'] == 'tidal'), None)
        qz = next((v for v in g['versiones'] if v['servicio'] == 'qobuz'), None)
        if t and qz:
            g['medicion'] = almacen.sugerencia(tienda_mod.clave_medicion(t['id'], qz['id']), tienda_mod.VERSION_CACHE)
    return jsonify(res)


@app.route('/api/tienda/resolucion', methods=['POST'])
def api_tienda_resolucion():
    ids = [str(i) for i in (request.get_json(silent=True) or {}).get('ids') or [] if str(i).isdigit()][:40]
    try:
        return jsonify(ok=True, resoluciones=tienda_mod.resolucion_tidal(almacen, ids))
    except tienda_mod.FalloTienda as e:
        return jsonify(ok=False, error=str(e)), 409


@app.route('/api/tienda/medir', methods=['GET', 'POST'])
def api_tienda_medir():
    d = (request.get_json(silent=True) or {}) if request.method == 'POST' else request.args
    t, q = str(d.get('tidal') or ''), str(d.get('qobuz') or '')
    if not t.isdigit() or not re.fullmatch(r'[0-9A-Za-z]{1,40}', q or ''):
        return jsonify(ok=False, error='Faltan las dos versiones'), 400
    if request.method == 'POST':
        tienda_mod.olvidar_error_medicion(t, q)
        st = tienda_mod.medir(almacen, DATOS, t, q)
    else:
        st = almacen.sugerencia(tienda_mod.clave_medicion(t, q), tienda_mod.VERSION_CACHE)
        st = dict(st, estado='listo') if st else (tienda_mod._mediciones.get(tienda_mod.clave_medicion(t, q))
                                                  or {'estado': None})
    return jsonify(ok=True, **st)


@app.route('/api/descargas', methods=['GET', 'POST'])
def api_descargas():
    if request.method == 'POST':
        n = 0
        for it in ((request.get_json(silent=True) or {}).get('items') or [])[:60]:
            s, i = it.get('servicio'), str(it.get('id') or '')
            if s not in ('tidal', 'qobuz') or not re.fullmatch(r'[0-9A-Za-z]{1,40}', i):   # Qobuz: IDs alfanuméricos
                continue
            url = f'https://tidal.com/browse/album/{i}' if s == 'tidal' else f'https://open.qobuz.com/album/{i}'
            _did, nueva = almacen.nueva_descarga(s, i, url, str(it.get('artista') or '')[:200],
                                                 str(it.get('album') or '')[:300], str(it.get('calidad') or '')[:40],
                                                 int(it['n_pistas']) if str(it.get('n_pistas') or '').isdigit() else None)
            n += nueva
        descargador.ya()
        return jsonify(ok=True, agregadas=n)
    lista = almacen.descargas()
    for d in lista:
        d['carpetas'] = json.loads(d['carpetas']) if d.get('carpetas') else []
        d['pendientes'] = json.loads(d['pendientes']) if d.get('pendientes') else []
        d['bandeja'] = None
        for c in d['carpetas']:
            aid = almacen.fila_de_ruta(c)
            if aid:
                fila = almacen.album(aid)
                d['bandeja'] = {'id': aid, 'estado': fila['estado']}
                break
    return jsonify(descargas=lista, pausa=sorted(vigilante.pausa_rutas))


@app.route('/api/descargas/limpiar', methods=['POST'])
def api_descargas_limpiar():
    """Quita de la lista las descargas terminadas bien (no toca archivos ni la Bandeja)."""
    return jsonify(ok=True, quitadas=almacen.limpiar_descargas())


@app.route('/api/descargas/<int:did>/<accion>', methods=['POST'])
def api_descarga_accion(did, accion):
    d = almacen.descarga(did)
    if not d:
        return jsonify(ok=False), 404
    if accion == 'cancelar':
        if d['estado'] == 'descargando':
            descargador.cancelar_actual(did)
        elif d['estado'] == 'en_cola':
            almacen.actualizar_descarga(did, estado='cancelada', fin=time.time())
    elif accion == 'reintentar' and d['estado'] in ('error', 'cancelada'):
        almacen.actualizar_descarga(did, estado='en_cola', error=None, progreso=None, ts=time.time())
        descargador.ya()
    elif accion == 'quitar' and d['estado'] not in ('descargando', 'decision'):
        almacen.borrar_descarga(did)
    elif accion == 'convertir' and d['estado'] == 'decision':
        formato = (request.get_json(silent=True) or {}).get('formato')
        if formato not in ('flac', 'mp3'):
            return jsonify(ok=False, error='Formato no válido'), 400
        almacen.actualizar_descarga(did, estado='convirtiendo', progreso='Convirtiendo…')
        threading.Thread(target=_convertir_descarga, args=(did, formato), daemon=True, name='convertir').start()
    elif accion == 'recuperar' and d['estado'] == 'decision':
        almacen.actualizar_descarga(did, estado='recuperando', progreso='Intentando de nuevo…')
        threading.Thread(target=_recuperar_descarga, args=(did,), daemon=True, name='recuperar').start()
    elif accion == 'descartar' and d['estado'] == 'decision':
        for c in json.loads(d.get('carpetas') or '[]'):
            real = os.path.realpath(c)
            base = os.path.realpath(tienda_mod.carpeta_destino(d['servicio']))
            if real.startswith(base + os.sep) and os.path.isdir(real):
                shutil.rmtree(real)
        almacen.actualizar_descarga(did, estado='cancelada', pendientes=None, progreso=None,
                                    error='Descartado: había pistas sin versión sin pérdida')
        vigilante.pausa_rutas = vigilante.pausa_rutas - {c.rstrip('/') for c in json.loads(d.get('carpetas') or '[]')}
    else:
        return jsonify(ok=False, error='No se puede en este estado'), 409
    return jsonify(ok=True)


def _recuperar_descarga(did):
    """«Intentar de nuevo» en una descarga que espera decisión: el mismo rescate, sin volver a bajar el álbum."""
    from . import rescate
    d = almacen.descarga(did)
    lineas = (d.get('registro') or '').splitlines() + ['— Intentando de nuevo —']
    carpetas = json.loads(d.get('carpetas') or '[]')

    def nota(t):
        lineas.append(t)
        almacen.actualizar_descarga(did, progreso=t[:160], registro='\n'.join(lineas[-160:]))
    try:
        tmp = os.path.join(DATOS, 'rescate')
        os.makedirs(tmp, exist_ok=True)
        pendientes, _notas = rescate.rescatar(d, carpetas, tmp, nota)
    except Exception as e:  # noqa: BLE001
        nota(f'Error: {type(e).__name__}: {e}')
        almacen.actualizar_descarga(did, estado='decision')
        return
    if pendientes:
        lista = [{'archivo': os.path.basename(f['ruta']), 'ruta': f['ruta'], 'codec': f['codec'],
                  'danada': bool(f.get('danada'))} for f in pendientes]
        almacen.actualizar_descarga(did, estado='decision', pendientes=json.dumps(lista, ensure_ascii=False),
                                    progreso=f'{len(lista)} pista(s) siguen sin recuperarse')
        return
    nota('Todas las pistas recuperadas sin pérdida')
    almacen.actualizar_descarga(did, estado='terminada', pendientes=None, fin=time.time(),
                                progreso='Pistas recuperadas: pasa a la Bandeja')
    vigilante.pausa_rutas = vigilante.pausa_rutas - {c.rstrip('/') for c in carpetas}
    vigilante.ya()


def _convertir_descarga(did, formato):
    from . import rescate
    d = almacen.descarga(did)
    pend = json.loads(d.get('pendientes') or '[]')
    lineas = (d.get('registro') or '').splitlines()
    errores = []
    for p in pend:
        try:
            nuevo = rescate.convertir(p['ruta'], formato)
            lineas.append(f'  ✓ {os.path.basename(nuevo)}: convertida a {"FLAC" if formato == "flac" else "MP3 320"} '
                          f'desde {(p.get("codec") or "?").upper()}')
        except Exception as e:  # noqa: BLE001
            errores.append(p['archivo'])
            lineas.append(f'  ✗ {p["archivo"]}: no se pudo convertir ({e})')
    if errores:
        almacen.actualizar_descarga(did, estado='decision', progreso=f'No se pudieron convertir {len(errores)} pista(s)',
                                    registro='\n'.join(lineas[-120:]))
        return
    carpetas = {c.rstrip('/') for c in json.loads(d.get('carpetas') or '[]')}
    almacen.actualizar_descarga(did, estado='terminada', pendientes=None, fin=time.time(),
                                progreso=f'{len(pend)} pista(s) convertidas a {"FLAC" if formato == "flac" else "MP3 320"} (Lossy)',
                                registro='\n'.join(lineas[-120:]))
    vigilante.pausa_rutas = vigilante.pausa_rutas - carpetas
    vigilante.ya()


# ─────────────────────────── reprocesar un álbum de la biblioteca ───────────────────────────

def _ficha_de_ruta(ruta):
    """Ficha de Biblioteca del álbum de Orbyte con esa carpeta (o la Bandeja si ya no está)."""
    db = almacen.ajustes().get('db_orbyte')
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
        try:
            r = con.execute('SELECT id FROM albums WHERE path=? OR path=?', (ruta.rstrip('/'), ruta.rstrip('/') + '/')).fetchone()
        finally:
            con.close()
        if r:
            return url_for('biblioteca_album', oid=r[0])
    except Exception:
        pass
    return '/'


def _base_multidisco(ruta):
    """Si la carpeta es un disco («CD 01», «Disc 2»…) de un álbum de varios discos, la carpeta del álbum."""
    ruta = ruta.rstrip('/')
    padre = os.path.dirname(ruta)
    if analisis.carpeta_album(ruta) == ruta or not os.path.isdir(padre):   # «Artista/Load (Disc 1)» es un álbum
        return ruta
    try:
        hijos = os.listdir(padre)
    except OSError:
        return ruta
    sueltos = [h for h in hijos if os.path.isfile(os.path.join(padre, h)) and os.path.splitext(h)[1].lower() in analisis.AUDIO_EXT]
    discos = [h for h in hijos if os.path.isdir(os.path.join(padre, h)) and analisis.dividir_por_disco(h) is not None]
    return padre if not sueltos and len(discos) >= 2 else ruta


@app.route('/api/reprocesar/<k>/<int:i>', methods=['POST'])
def api_reprocesar(k, i):
    """La carpeta del álbum en la biblioteca pasa por la revisión completa como si fuera nueva, sin moverla. Al
    ubicarla, la versión nueva reemplaza a esta cuando su metadata termina bien (escuchas, favoritos y playlists
    pasan a la nueva) y recién ahí se borra la carpeta anterior con sus audios y su .cue."""
    ruta, _cp, fila, _o = _lib_portada(k, i)
    if not ruta:
        return jsonify(ok=False, error='El álbum no está en la biblioteca'), 404
    ruta = _base_multidisco(ruta)                    # «…/Álbum/CD 01» → «…/Álbum»: se reprocesan todos los discos
    aj = almacen.ajustes()
    bib = os.path.realpath(aj.get('biblioteca') or '/mnt/musica')
    if not os.path.realpath(ruta).startswith(bib + os.sep):
        return jsonify(ok=False, error='La carpeta está fuera de la biblioteca'), 400
    if fila and fila['estado'] not in EN_BIBLIOTECA:
        return jsonify(ok=False, error='El álbum se está procesando: espera a que termine'), 409
    if fila and fila['ruta'].rstrip('/') != ruta and fila['estado'] in EN_BIBLIOTECA:
        fila = None                                   # (vino de una ISO: su fila sigue apuntando a la entrada)
    aid = almacen.iniciar_reproceso(ruta, os.path.relpath(ruta, bib), analisis.firma(ruta))
    almacen.registrar(aid, 'Reproceso: la carpeta de la biblioteca se revisa como un álbum nuevo (no se mueve nada)')
    vigilante.ya()
    return jsonify(ok=True, ir=url_for('album', aid=aid))


# ─────────────────────────── unir álbumes como discos de uno solo ───────────────────────────
# Dos (o más) álbumes de la biblioteca que en realidad son los discos de uno solo («Garage Inc» 11 pistas +
# «Garage Inc.» 15 pistas): se arma en ingesta_datos/unir una carpeta con una subcarpeta «CD N» por álbum (enlaces a
# sus archivos, no se copia ni se mueve nada) y se revisa como un reproceso. Al terminar la metadata del álbum unido,
# reemplaza a todos (escuchas, favoritos y playlists pasan al nuevo) y recién ahí se borran sus carpetas.

UNIR = os.path.join(DATOS, 'unir')
MAX_UNIR = 200          # cajas de 100 CD


def _grupos_bib():
    aj = almacen.ajustes()
    return analisis.biblioteca(aj.get('db_orbyte'), aj.get('biblioteca') or '/mnt/musica')


def _discos_tag(ids):
    """{album_id: disco más frecuente en sus tags (o None)} desde music.db."""
    if not ids:
        return {}
    con = sqlite3.connect(f'file:{almacen.ajustes()["db_orbyte"]}?mode=ro', uri=True, timeout=30)
    try:
        filas = con.execute(f'SELECT album_id, disc_number FROM tracks WHERE album_id IN ({",".join("?" * len(ids))})',
                            list(ids)).fetchall()
    finally:
        con.close()
    cuenta = {}
    for aid, d in filas:
        try:
            n = int(str(d or '').split('/')[0])
        except ValueError:
            continue
        cuenta.setdefault(aid, {}).setdefault(n, 0)
        cuenta[aid][n] += 1
    return {aid: max(c, key=c.get) for aid, c in cuenta.items() if c}


def _info_union(g, discos):
    d = [discos.get(i) for i in g['ids'] if discos.get(i)]
    return {'ruta': g['ruta'], 'id': g['ids'][0], 'album': g['album'], 'anio': g.get('anio'), 'artista': g['artista'],
            'n_pistas': g['pistas'], 'tag': g.get('tag'), 'formato': g.get('formato'),
            'disco_tag': max(set(d), key=d.count) if d else None,
            'disco_nombre': salud_mod._disco_nombre({'album': g['album'], 'ruta': g['ruta']})}


def _carpetas_de(ids):
    """{album_id: carpeta en music.db}."""
    if not ids:
        return {}
    con = sqlite3.connect(f'file:{almacen.ajustes()["db_orbyte"]}?mode=ro', uri=True, timeout=30)
    try:
        return {i: (p or '').rstrip('/') for i, p in con.execute(
            f'SELECT id, path FROM albums WHERE id IN ({",".join("?" * len(ids))})', list(ids))}
    finally:
        con.close()


def _subcarpetas_con_audio(ruta, excluir):
    """Subcarpetas de `ruta` con audio (sin contar las que también se unen): si hay, ya es de varios discos."""
    con_audio = []
    for raiz, dirs, archivos in os.walk(ruta):
        dirs[:] = [d for d in dirs if not d.startswith('.') and os.path.join(raiz, d).rstrip('/') not in excluir]
        if raiz != ruta and any(os.path.splitext(a)[1].lower() in analisis.AUDIO_EXT and not a.startswith('._')
                                for a in archivos):
            con_audio.append(os.path.relpath(raiz, ruta))
    return con_audio


@app.route('/api/unir/candidatos')
def api_unir_candidatos():
    """Álbumes del mismo artista para unir como discos con el álbum ?oid= (o ?ruta=), los de nombre parecido primero."""
    try:
        bib = _grupos_bib()
    except Exception as e:
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409
    oid = request.args.get('oid', type=int)
    ruta = (request.args.get('ruta') or '').rstrip('/')
    base = next((g for g in (bib or {}).get('grupos', [])
                 if (oid and oid in g['ids']) or (ruta and g['ruta'].rstrip('/') == ruta)), None)
    if not base:
        return jsonify(ok=False, error='Ese álbum no está en Orbyte'), 404
    cands = []
    for g in bib['grupos']:
        if g is base or analisis._parecido(base['n_artista'], g['n_artista']) < 0.88:
            continue
        cands.append((analisis._parecido(_limpio(base['album']), _limpio(g['album'])), g))
    cands.sort(key=lambda x: (-x[0], str(x[1].get('anio') or ''), x[1]['n_album']))
    cands = cands[:300]                              # una caja de 100 CD cabe entera
    discos = _discos_tag(base['ids'] + [i for _s, g in cands for i in g['ids']])
    return jsonify(ok=True, base=_info_union(base, discos), raiz=(almacen.ajustes().get('biblioteca') or '/mnt/musica'),
                   candidatos=[dict(_info_union(g, discos), parecido=round(sc, 2)) for sc, g in cands])


@app.route('/api/unir', methods=['POST'])
def api_unir():
    """{rutas: [...], discos?: [...]} en el orden de los discos → arma la carpeta de enlaces y la deja en la cola de
    revisión. `discos`: el número de cada disco (para conservar «CD 51» de una caja incompleta); si no, 1, 2, 3…"""
    d = request.get_json(silent=True) or {}
    rutas = [str(r).rstrip('/') for r in d.get('rutas') or []]
    if len(rutas) < 2 or len(set(rutas)) != len(rutas):
        return jsonify(ok=False, error='Elige al menos dos álbumes distintos'), 400
    if len(rutas) > MAX_UNIR:
        return jsonify(ok=False, error=f'Máximo {MAX_UNIR} discos'), 400
    numeros = list(range(1, len(rutas) + 1))
    if d.get('discos'):
        try:
            numeros = [int(x) for x in d['discos']]
        except (TypeError, ValueError):
            return jsonify(ok=False, error='Números de disco inválidos'), 400
        if len(numeros) != len(rutas) or len(set(numeros)) != len(numeros) or not all(1 <= x <= 999 for x in numeros):
            return jsonify(ok=False, error='Cada disco necesita un número distinto (1 a 999)'), 400
    try:
        bib = _grupos_bib()
    except Exception as e:
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409
    por_ruta = {g['ruta'].rstrip('/'): g for g in (bib or {}).get('grupos', [])}
    raiz = os.path.realpath(almacen.ajustes().get('biblioteca') or '/mnt/musica')
    for r in rutas:
        if r not in por_ruta:
            return jsonify(ok=False, error=f'«{os.path.basename(r)}» no está en Orbyte'), 404
    # carpeta real de cada álbum: la de Orbyte (un disco suelto en «Álbum/CD 02» se agrupa como «Álbum», pero sus
    # archivos están en «CD 02»); si un álbum ocupa varias carpetas ya es de varios discos
    carpetas = _carpetas_de([i for r in rutas for i in por_ruta[r]['ids']])
    fuentes = {}
    for r in rutas:
        cs = sorted({carpetas[i] for i in por_ruta[r]['ids'] if carpetas.get(i)})
        if len(cs) != 1:
            return jsonify(ok=False, error=f'«{os.path.basename(r)}» ya es de varios discos ({len(cs)} carpetas): '
                                           'reprocésalo primero para dejarlo en una carpeta'), 400
        fuentes[r] = cs[0]
    excluir = set(fuentes.values()) | set(rutas)
    for r in rutas:
        f = fuentes[r]
        if not os.path.realpath(f).startswith(raiz + os.sep) or not os.path.isdir(f):
            return jsonify(ok=False, error=f'«{os.path.basename(f)}» no está en la carpeta de la biblioteca'), 400
        for x in (r, f):
            fila = almacen.album(almacen.fila_de_ruta(x)) if almacen.fila_de_ruta(x) else None
            if fila and fila['estado'] not in EN_BIBLIOTECA:
                return jsonify(ok=False, error=f'«{os.path.basename(x)}» se está procesando: espera a que termine'), 409
        sub = _subcarpetas_con_audio(f, excluir)
        if sub:
            return jsonify(ok=False, error=f'«{os.path.basename(f)}» ya tiene discos en subcarpetas '
                                           f'({", ".join(sub[:3])}): reprocésalo primero para dejarlo en una carpeta'), 400
    for uid, otras in almacen.uniones_activas():
        if set(otras) & set(rutas):
            return jsonify(ok=False, error='Alguno de esos álbumes ya está en otra unión en curso',
                           ir=url_for('album', aid=uid)), 409
    # carpeta de enlaces: <unir>/<marca>/<nombre del disco 1>/CD N/… (los archivos de cada álbum, con sus subcarpetas
    # de escaneos; sin la carpeta de otro álbum que se une, si está anidada)
    tanda = os.path.join(UNIR, datetime.now().strftime('%Y%m%d-%H%M%S-') + secrets.token_hex(3))
    destino = os.path.join(tanda, analisis.nombre_seguro(os.path.basename(rutas[0])) or 'Album')
    try:
        for n, r in zip(numeros, rutas):
            cd = os.path.join(destino, f'CD {n}')
            os.makedirs(cd, exist_ok=True)
            f = fuentes[r]
            for raiz_w, dirs, archivos in os.walk(f):
                dirs[:] = [d for d in dirs if not d.startswith('.') and os.path.join(raiz_w, d).rstrip('/') not in excluir]
                for a in archivos:
                    if a.startswith('.'):
                        continue
                    origen = os.path.join(raiz_w, a)
                    dst = os.path.join(cd, os.path.relpath(origen, f))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.symlink(origen, dst)
    except OSError as e:
        shutil.rmtree(tanda, ignore_errors=True)
        return jsonify(ok=False, error=f'No se pudo armar la unión: {e}'), 500
    nombres = (' + '.join(f'«{os.path.basename(r)}»' for r in rutas) if len(rutas) <= 3 else
               f'«{os.path.basename(rutas[0])}» … «{os.path.basename(rutas[-1])}»')
    aid = almacen.iniciar_union(destino, f'Unir {len(rutas)} discos: {nombres}', analisis.firma(destino), rutas)
    almacen.registrar(aid, f'Unir como un álbum de {len(rutas)} discos (no se mueve nada hasta ubicar):')
    for n, r in zip(numeros, rutas):
        almacen.registrar(aid, f'  Disco {n}: {os.path.relpath(fuentes[r], raiz)} · {por_ruta[r]["pistas"]} pistas')
    vigilante.ya()
    return jsonify(ok=True, ir=url_for('album', aid=aid))


# ─────────────────────────── salud de la biblioteca ───────────────────────────

@app.route('/salud')
def salud_pagina():
    return render_template('salud.html', pagina='salud', CALIDADES=salud_mod.CALIDADES)


@app.route('/api/salud/discos')
def api_salud_discos():
    aj = almacen.ajustes()
    try:
        casos = salud_mod.discos_por_normalizar(aj.get('db_orbyte'), aj.get('biblioteca') or '/mnt/musica')
    except Exception as e:
        log.exception('Salud: discos por normalizar')
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409
    ign = salud.ignorados_discos()
    ver = request.args.get('ignorados') == '1'
    return jsonify(ok=True, casos=[c for c in casos if (c['clave'] in ign) == ver], n_ignorados=len(ign),
                   raiz=aj.get('biblioteca') or '/mnt/musica')


@app.route('/api/salud/discos/ignorar', methods=['POST'])
def api_salud_discos_ignorar():
    d = request.get_json(silent=True) or {}
    if not d.get('clave'):
        return jsonify(ok=False), 400
    salud.ignorar('disco:' + d['clave'], bool(d.get('si', True)))
    return jsonify(ok=True)


@app.route('/api/salud/estado')
def api_salud_estado():
    return jsonify(salud.progreso())


@app.route('/api/salud/estimar', methods=['POST'])
def api_salud_estimar():
    try:
        return jsonify(ok=True, **salud.estimar(request.get_json(silent=True) or {}))
    except Exception as e:
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409


@app.route('/api/salud/iniciar', methods=['POST'])
def api_salud_iniciar():
    try:
        salud.iniciar(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


@app.route('/api/salud/control', methods=['POST'])
def api_salud_control():
    accion = (request.get_json(silent=True) or {}).get('accion')
    if accion == 'pausar':
        salud.pausar(True)
    elif accion == 'reanudar':
        salud.pausar(False)
    elif accion == 'detener':
        salud.detener()
    else:
        return jsonify(ok=False, error='Acción desconocida'), 400
    return jsonify(ok=True)


@app.route('/api/salud/ajustes', methods=['POST'])
def api_salud_ajustes():
    d = request.get_json(silent=True) or {}
    cambios = {}
    if 'hilos' in d:
        cambios['hilos'] = max(1, min(4, int(d['hilos'] or 1)))
    if 'pausa' in d:
        cambios['pausa'] = max(0.0, min(60.0, float(d['pausa'] or 0)))
    if 'horario' in d:
        h = (d['horario'] or '').strip()
        if h and not re.fullmatch(r'\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}', h):
            return jsonify(ok=False, error='Horario: usa el formato 01:00-08:00 (o déjalo vacío)'), 400
        cambios['horario'] = h
    if 'escuchas' in d:
        cambios['escuchas'] = '1' if d['escuchas'] else '0'
    salud.poner(**cambios)
    return jsonify(ok=True)


# FLAC de 32 bits: copias 24 bit para los clientes que no los abren (ingesta/compat24.py, 07-11-2026)
compat = compat24.Compat24(almacen.ajustes)


@app.route('/api/salud/compat24')
def api_salud_compat24():
    aj = almacen.ajustes()
    try:
        r = compat24.estado(aj.get('db_orbyte'), aj.get('biblioteca') or '/mnt/musica')
    except Exception as e:
        log.exception('Salud: compat24')
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409
    return jsonify(ok=True, **r, trabajando=compat.actual, revisando=compat.revisando, hechas=compat.hechas,
                   error=compat.ultimo_error, raiz=aj.get('biblioteca') or '/mnt/musica')


# Sin carátula (ingesta/caratulas.py, 08-11-2026)
caratulas = caratulas_mod.Caratulas(almacen.ajustes, os.path.join(DATOS, 'caratulas.json'))


@app.route('/api/salud/caratulas')
def api_salud_caratulas():
    caratulas.si_hace_falta()
    r = (None if caratulas.revisando else caratulas.vigentes()) or caratulas.resultado() or {}
    return jsonify(ok=True, albumes=r.get('albumes') or [], revisados=r.get('revisados'), cuando=r.get('cuando'),
                   revisando=caratulas.revisando, avance=caratulas.avance, error=caratulas.error,
                   chica_px=caratulas_mod.CHICA_PX)


@app.route('/api/salud/caratulas/revisar', methods=['POST'])
def api_salud_caratulas_revisar():
    return jsonify(ok=True, iniciada=caratulas.iniciar())


@app.route('/api/salud/caratulas/<int:oid>/quitar', methods=['POST'])
def api_salud_caratulas_quitar(oid):
    caratulas.quitar(oid)
    return jsonify(ok=True)


@app.route('/api/salud/compat24/ahora', methods=['POST'])
def api_salud_compat24_ahora():
    compat.ahora()
    return jsonify(ok=True)


@app.route('/api/salud/danadas')
def api_salud_danadas():
    try:
        albumes = salud.danadas(request.args.get('ignorados') == '1')
        return jsonify(ok=True, albumes=albumes, raiz=almacen.ajustes().get('biblioteca') or '/mnt/musica',
                       revisando=len(saneador.estado().get('revisar') or []))
    except Exception as e:
        log.exception('Salud: pistas dañadas')
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409


# ─────────────────────────── calidad y LED ───────────────────────────

def _ids_album_orbyte(oid, ruta=None):
    """El álbum en Orbyte; si ese ID ya no existe (se reindexó), el que está en esa carpeta."""
    db = almacen.ajustes().get('db_orbyte')
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=30)
    try:
        if con.execute('SELECT 1 FROM albums WHERE id=?', (oid,)).fetchone():
            return [oid]
        if ruta:
            return [r[0] for r in con.execute('SELECT id FROM albums WHERE path=? OR path=?',
                                              (ruta.rstrip('/'), ruta.rstrip('/') + '/'))]
        return []
    finally:
        con.close()


@app.route('/api/calidad/album/<int:oid>')
def api_calidad_album(oid):
    aj = almacen.ajustes()
    ids = _ids_album_orbyte(oid, request.args.get('ruta'))
    if not ids:
        return jsonify(ok=False, error='Ese álbum ya no está en Orbyte con ese ID: ábrelo desde Biblioteca'), 404
    try:
        r = calidad_mod.revisar_album(aj.get('db_orbyte'), ids, verificador.det,
                                      forzar=request.args.get('forzar') == '1')
    except Exception as e:
        log.exception('Calidad: álbum %s', oid)
        return jsonify(ok=False, error=f'No se pudo revisar: {type(e).__name__}: {e}'), 409
    return jsonify(ok=True, oid=ids[0], **r)


# ─────────────────────────── ▶ En Desktop ───────────────────────────

def _token_interno():
    """Token compartido con Orbyte (mismo repo): lo crea la ingesta si falta."""
    ruta = os.path.join(REPO, '.token_interno')
    if not os.path.exists(ruta):
        fd = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32))
    with open(ruta) as f:
        return f.read().strip()


@app.route('/api/desktop/reproducir', methods=['POST'])
def api_desktop_reproducir():
    """{album_id (Orbyte), track_id?, cliente_id?} → Orbyte le manda la cola al Desktop abierto."""
    import urllib.error
    import urllib.request
    d = request.get_json(silent=True) or {}
    cuerpo = json.dumps({k: d.get(k) for k in ('album_id', 'track_id', 'cliente_id')}).encode()
    req = urllib.request.Request('http://127.0.0.1:5001/api/interno/desk/reproducir', data=cuerpo, method='POST',
                                 headers={'Content-Type': 'application/json', 'X-Orbyte-Interno': _token_interno()})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read() or b'{}')
        except ValueError:
            res = {}
        if e.code == 403:
            res = {'message': 'Orbyte no aceptó la orden: reinicia orbyte.service para que lea el token nuevo'}
        return jsonify(ok=False, error=res.get('message') or f'Orbyte respondió {e.code}')
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=False, error=f'No se pudo hablar con Orbyte: {e}')
    if res.get('status') == 'elegir':
        return jsonify(ok=False, elegir=res.get('desktops') or [])
    return jsonify(ok=res.get('status') == 'ok', error=res.get('message'), **{k: res.get(k) for k in (
        'desktop', 'salida', 'pista', 'pistas', 'album_id', 'titulo', 'artista', 'album', 'numero', 'disco',
        'formato', 'led', 'duracion')})


@app.route('/api/desktop/portada/<int:album_id>')
def api_desktop_portada(album_id):
    """Portada (miniatura) de un álbum de Orbyte, para el aviso «Ahora suena»."""
    con = sqlite3.connect(f'file:{almacen.ajustes()["db_orbyte"]}?mode=ro', uri=True, timeout=10)
    try:
        fila = con.execute('SELECT path, cover_path FROM albums WHERE id=?', (album_id,)).fetchone()
    finally:
        con.close()
    if not fila or not fila[1] or not os.path.isfile(fila[1]) \
            or not fila[1].lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        abort(404)
    return send_file(_miniatura(fila[1]) or fila[1], max_age=3600)


@app.route('/api/calidad/album/<int:oid>/aplicar', methods=['POST'])
def api_calidad_aplicar(oid):
    """modo «detectado»: corrige las pistas que no corresponden; modo «manual»: {rutas, mqa, studio, sr_original}."""
    d = request.get_json(silent=True) or {}
    aj = almacen.ajustes()
    db = aj.get('db_orbyte')
    ids = _ids_album_orbyte(oid)
    if not ids:
        return jsonify(ok=False, error='Ese álbum ya no está en Orbyte con ese ID: recarga la página'), 404
    pistas = calidad_mod.revisar_album(db, ids, verificador.det)['pistas']
    propias = {p['ruta'] for p in pistas}
    if d.get('modo') == 'manual':
        rutas = [r for r in (d.get('rutas') or []) if r in propias] or sorted(propias)
        sr = d.get('sr_original')
        try:
            sr = int(sr) if sr not in (None, '', 0, '0') else None
        except (TypeError, ValueError):
            return jsonify(ok=False, error='Frecuencia original inválida'), 400
        if d.get('mqb'):              # MQB (magenta) visto en el DAC: tags «MQB» + Orbyte + LED, todo coherente
            decisiones = {r: {'mqa': True, 'studio': False, 'mqb': True, 'sr_original': sr} for r in rutas}
        else:
            decisiones = {r: {'mqa': bool(d.get('mqa')), 'studio': bool(d.get('mqa') and d.get('studio')),
                              'sr_original': sr if d.get('mqa') else None} for r in rutas}
    else:
        decisiones = calidad_mod.decisiones_detectadas(pistas)
        if d.get('rutas'):
            decisiones = {k: v for k, v in decisiones.items() if k in set(d['rutas'])}
    if not decisiones:
        return jsonify(ok=False, error='No hay pistas que cambiar'), 400
    registro = []
    try:
        res = calidad_mod.aplicar(aj, decisiones, registro.append)
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 409
    verificador.reevaluar(list(decisiones))
    return jsonify(ok=True, registro=registro[-60:], **res)


@app.route('/api/calidad/estado')
def api_calidad_estado():
    return jsonify(verificador.progreso())


@app.route('/api/calidad/estimar', methods=['POST'])
def api_calidad_estimar():
    try:
        return jsonify(ok=True, **verificador.estimar(request.get_json(silent=True) or {}))
    except Exception as e:
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409


@app.route('/api/calidad/iniciar', methods=['POST'])
def api_calidad_iniciar():
    c = request.get_json(silent=True) or {}
    if c.get('modo') == 'con_error':
        c['modo'] = 'pendientes'
    try:
        verificador.iniciar(c)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


@app.route('/api/calidad/control', methods=['POST'])
def api_calidad_control():
    accion = (request.get_json(silent=True) or {}).get('accion')
    if accion == 'pausar':
        verificador.pausar(True)
    elif accion == 'reanudar':
        verificador.pausar(False)
    elif accion == 'detener':
        verificador.detener()
    else:
        return jsonify(ok=False, error='Acción desconocida'), 400
    return jsonify(ok=True)


@app.route('/api/calidad/diferencias')
def api_calidad_diferencias():
    try:
        return jsonify(ok=True, albumes=verificador.diferencias(request.args.get('ignorados') == '1'),
                       raiz=almacen.ajustes().get('biblioteca') or '/mnt/musica')
    except Exception as e:
        log.exception('Calidad: diferencias')
        return jsonify(ok=False, error=f'No se pudo leer music.db: {e}'), 409


@app.route('/api/calidad/ignorar', methods=['POST'])
def api_calidad_ignorar():
    d = request.get_json(silent=True) or {}
    if not d.get('carpeta'):
        return jsonify(ok=False), 400
    verificador.ignorar(d['carpeta'], bool(d.get('si', True)))
    return jsonify(ok=True)


@app.route('/api/salud/ignorar', methods=['POST'])
def api_salud_ignorar():
    d = request.get_json(silent=True) or {}
    if not d.get('carpeta'):
        return jsonify(ok=False), 400
    salud.ignorar(d['carpeta'], bool(d.get('si', True)))
    return jsonify(ok=True)


@app.route('/api/salud/sanear')
def api_salud_sanear_estado():
    return jsonify(ok=True, **saneador.estado())


def _forzable(m):
    return bool(m.get('forzable', m.get('tramos') and not m.get('fatal') and m.get('bits') in (16, 24, 32))) \
        and not m.get('saneada')


@app.route('/api/salud/sanear', methods=['POST'])
def api_salud_sanear():
    """Sanear FLAC con cuadros dañados (solo los que se midieron como saneables: menos de 1 s en total)."""
    rutas = [r for r in (request.get_json(silent=True) or {}).get('rutas') or [] if isinstance(r, str)]
    if not rutas:
        return jsonify(ok=False, error='Nada seleccionado'), 400
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    forzar = bool((request.get_json(silent=True) or {}).get('forzar'))
    med = salud.medidas(rutas)
    # forzar: también las de más de 1 s (lo decide Niko, pista por pista); nunca las ilegibles o de otros bits
    ok = [r for r in rutas if (med.get(r) or {}).get('saneable')
          or forzar and _forzable(med.get(r) or {})]
    saneador.sanear(ok, forzar=forzar)
    return jsonify(ok=True, en_cola=len(ok), saltadas=len(rutas) - len(ok))


@app.route('/api/salud/espacio')
def api_salud_espacio():
    """Lo que ocupa lo que la ingesta guarda por las dudas: respaldos de music.db y originales de Sanear."""
    db = _db_orbyte()
    try:
        bytes_db = os.path.getsize(db) if db else None
    except OSError:
        bytes_db = None
    resp = artistas_mod.respaldos(db) if db else []
    orig = salud.originales()
    horas = papelera_mod.horas(almacen.ajustes())
    pap = papelera.listar(horas)
    return jsonify(ok=True, db=db, bytes_db=bytes_db, respaldos=resp, conservar=artistas_mod.RESPALDOS_A_CONSERVAR,
                   bytes_respaldos=sum(r['bytes'] for r in resp), originales=orig,
                   bytes_originales=sum(o['bytes'] for o in orig), raiz=almacen.ajustes().get('biblioteca') or '',
                   papelera=pap, bytes_papelera=sum(x['bytes'] or 0 for x in pap), papelera_horas=horas)


@app.route('/api/salud/espacio/originales/borrar', methods=['POST'])
def api_salud_borrar_originales():
    """«Borrar los originales ya saneados»: todos, o los de `respaldos` (rutas dentro de .orbyte-respaldo/)."""
    d = request.get_json(silent=True) or {}
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    lista = d.get('respaldos')
    if lista is not None and not (isinstance(lista, list) and all(isinstance(r, str) for r in lista)):
        return jsonify(ok=False, error='Lista inválida'), 400
    if not d.get('todos') and not lista:
        return jsonify(ok=False, error='Nada seleccionado'), 400
    return jsonify(ok=True, **salud.borrar_originales(None if d.get('todos') else lista))


# ─────────────────────────── eliminar y papelera (pedido de Niko, 13-11-2026) ───────────────────────────

def _album_db_de_ruta(ruta):
    con = sqlite3.connect(f'file:{_db_orbyte()}?mode=ro', uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute('SELECT al.id, al.name, ar.name AS artista, (SELECT COUNT(*) FROM tracks t WHERE '
                        't.album_id=al.id) AS n FROM albums al LEFT JOIN artists ar ON ar.id=al.artist_id '
                        'WHERE al.path=? OR al.path=?', (ruta, ruta + '/')).fetchone()
        dentro = [dict(x) for x in con.execute('SELECT name, path FROM albums WHERE substr(path, 1, ?) = ? AND '
                                                'path<>? AND path<>?', (len(ruta) + 1, ruta + '/', ruta, ruta + '/'))]
        return (dict(r) if r else None), dentro
    finally:
        con.close()


def _elim_error(texto, codigo=409):
    return jsonify(ok=False, error=texto), codigo


def _eliminar_biblioteca(ruta, claves=None, todo=False):
    """Pistas (claves de ajustar.leer) o el álbum entero de la biblioteca → papelera, y fuera de music.db."""
    ruta = (ruta or '').rstrip('/')
    aj = almacen.ajustes()
    bib = aj.get('biblioteca') or '/mnt/musica'
    if not procesador.biblioteca_lista():
        return _elim_error('La biblioteca no está montada')
    if not os.path.isdir(ruta) or not papelera_mod._dentro(ruta, bib):
        return _elim_error('La carpeta del álbum no está en la biblioteca')
    aid = almacen.fila_de_ruta(ruta)
    a = almacen.album(aid) if aid else None
    if a and a['estado'] not in EN_BIBLIOTECA:
        return _elim_error('El álbum se está procesando: espera a que termine')
    db = _db_orbyte()
    al, dentro = _album_db_de_ruta(ruta)
    titulo = f'{al["artista"]} — {al["name"]}' if al else os.path.basename(ruta)

    def reg(m):
        log.info('%s', m)
        if aid:
            almacen.registrar(aid, m)

    if todo:
        if dentro:
            return _elim_error(f'Su carpeta contiene otro álbum de Orbyte («{dentro[0]["name"]}»): elimina sus pistas, '
                               'no el álbum entero')
        try:
            eid = papelera.mover(bib, [ruta], 'album', 'biblioteca', titulo,
                                 detalle=f'{al["n"]} pistas' if al else '', extra={'album_ruta': ruta})
        except papelera_mod.FalloPapelera as e:
            return _elim_error(str(e))
        try:
            extra = eliminar_mod.quitar_album(db, al['id'], reg) if al else {}
        except Exception as e:
            log.exception('Eliminar álbum %s', ruta)
            papelera.restaurar(eid)
            return _elim_error(f'No se pudo sacar de Orbyte (no se eliminó nada): {e}', 500)
        papelera.poner_extra(eid, dict(extra, album_ruta=ruta))
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(ruta))            # la carpeta del artista, si quedó vacía
        if aid:
            almacen.borrar_album(aid)
        return jsonify(ok=True, ir=url_for('biblioteca_pagina'), papelera=eid)

    try:
        lib = ajustar_mod.leer(ruta)
    except ajustar_mod.FalloAjuste as e:
        return _elim_error(str(e))
    plan = {x['clave']: x for x in lib['plan']}
    claves = [c for c in (claves or []) if isinstance(c, str)]
    if not claves:
        return _elim_error('Nada seleccionado', 400)
    if any(c not in plan for c in claves):
        return _elim_error('La lista de pistas cambió: recarga la página')
    rutas = [os.path.join(ruta, c) for c in claves]
    pistas = eliminar_mod.pistas_de(db, rutas)
    elim_rep = []
    if a and (a.get('reporte') or {}).get('discos'):      # la ficha de la Bandeja marca las de la revisión
        inv = {x['clave']: k for k, x in ajustar_mod.pistas_actuales(a['reporte'], lib).items()}
        elim_rep = [inv[c] for c in claves if c in inv]
    nombres = ', '.join(os.path.basename(c) for c in claves)
    try:
        eid = papelera.mover(bib, rutas, 'pistas', 'biblioteca', titulo,
                             detalle=nombres if len(nombres) < 400 else f'{len(claves)} pistas',
                             extra={'album_ruta': ruta})
    except papelera_mod.FalloPapelera as e:
        return _elim_error(str(e))
    try:
        extra = eliminar_mod.quitar_pistas(db, pistas, reg)
    except Exception as e:
        log.exception('Eliminar pistas %s', ruta)
        papelera.restaurar(eid)
        return _elim_error(f'No se pudo sacar de Orbyte (no se eliminó nada): {e}', 500)
    papelera.poner_extra(eid, dict(extra, album_ruta=ruta, aid=aid, eliminadas_rep=elim_rep))
    if elim_rep:
        cam = dict(a.get('cambios') or {})
        cam['eliminadas'] = sorted(set(cam.get('eliminadas') or []) | set(elim_rep))
        almacen.actualizar(aid, cambios=json.dumps(cam, ensure_ascii=False))
    return jsonify(ok=True, papelera=eid, n=len(claves))


def _eliminar_bandeja(a, archivos):
    """Pistas de un álbum en revisión (antes de ubicar) → papelera, y se vuelve a revisar."""
    if not _editable(a):
        return _elim_error('Solo antes de ubicar el álbum')
    if (a.get('cambios') or {}).get('union'):
        return _elim_error('Es un álbum unido: quita el disco desde «Unir como discos»')
    aj = almacen.ajustes()
    raiz = (aj.get('biblioteca') or '/mnt/musica') if a.get('origen') == 'reproceso' else aj.get('carpeta_entrada')
    if not raiz or not os.path.isdir(a['ruta']) or not papelera_mod._dentro(a['ruta'], raiz):
        return _elim_error('Este álbum no está en una carpeta que se pueda tocar (¿una ISO?)')
    archivos = [f for f in archivos or [] if isinstance(f, str)]
    if not archivos:
        return _elim_error('Nada seleccionado', 400)
    rutas = []
    for f in archivos:
        p = os.path.join(a['ruta'], f)
        if not papelera_mod._dentro(p, a['ruta']) or not os.path.isfile(p):
            return _elim_error(f'No está {f}: vuelve a revisar el álbum')
        rutas.append(p)
    rep = a.get('reporte') or {}
    titulo = f'{rep.get("artista") or a.get("artista") or "?"} — {rep.get("album") or a.get("album") or a["rel"]}'
    nombres = ', '.join(os.path.basename(f) for f in archivos)
    try:
        eid = papelera.mover(raiz, rutas, 'pistas', 'bandeja', titulo,
                             detalle=nombres if len(nombres) < 400 else f'{len(archivos)} pistas',
                             extra={'aid': a['id'], 'album_ruta': a['ruta']})
    except papelera_mod.FalloPapelera as e:
        return _elim_error(str(e))
    almacen.registrar(a['id'], f'{len(archivos)} pista(s) a la papelera: {nombres[:300]}')
    _requeue(a['id'])
    return jsonify(ok=True, papelera=eid, n=len(archivos))


@app.route('/api/album/<int:aid>/eliminar', methods=['POST'])
def api_album_eliminar(aid):
    a = almacen.album(aid)
    if not a:
        return jsonify(ok=False), 404
    d = request.get_json(silent=True) or {}
    if a['estado'] in EN_BIBLIOTECA:
        return _eliminar_biblioteca(a['ubicado_en'] or a['ruta'], d.get('claves'), bool(d.get('album')))
    return _eliminar_bandeja(a, d.get('archivos'))


@app.route('/api/biblioteca/<int:oid>/eliminar', methods=['POST'])
def api_biblioteca_eliminar(oid):
    o = _album_orbyte(oid)
    if not o or not o['ruta']:
        return jsonify(ok=False, error='Ese álbum ya no está en Orbyte'), 404
    d = request.get_json(silent=True) or {}
    return _eliminar_biblioteca(o['ruta'], d.get('claves'), bool(d.get('album')))


# ─────────────────────────── ficha: pistas dañadas (mismas acciones que Salud, 13-11-2026) ───────────────────────────

_san_pend = {}                 # aid de la Bandeja → rutas por sanear; al terminar la última se vuelve a revisar
_san_lock = threading.Lock()


def _al_sanear(ruta):
    listos = []
    with _san_lock:
        for aid, rutas in list(_san_pend.items()):
            rutas.discard(ruta)
            if not rutas:
                del _san_pend[aid]
                listos.append(aid)
    for aid in listos:
        a = almacen.album(aid)
        if a and _editable(a):
            almacen.registrar(aid, 'Sanear terminó: se vuelve a revisar')
            _requeue(aid)


saneador.al_terminar = _al_sanear
procesador.al_respaldo = salud.registrar_original


def _rutas_tocables(rutas):
    """Solo archivos de la biblioteca o de la carpeta de entrada (Sanear y Reparar tags los reescriben)."""
    aj = almacen.ajustes()
    raices = [r for r in (aj.get('biblioteca') or '/mnt/musica', aj.get('carpeta_entrada')) if r]
    out = []
    for r in rutas or []:
        if isinstance(r, str) and os.path.isfile(r) and any(papelera_mod._dentro(r, x) for x in raices) \
                and f'/{papelera_mod.NOMBRE}/' not in r:
            out.append(r)
    return out


def _sanear_estado_de(rutas, carpeta=None):
    """Lo del saneador que toca a esta ficha: sus pistas, o todo lo que está dentro de `carpeta`."""
    e = saneador.estado()
    nuestras = set(rutas)
    pref = carpeta.rstrip('/') + '/' if carpeta else None

    def es(r):
        return r in nuestras or bool(pref and r.startswith(pref))
    return {'actual': e['actual'] if e['actual'] and es(e['actual'][1]) else None,
            'seg': e['seg'], 'en_cola': [r for r in e['sanear'] if es(r)],
            'ultimos': [u for u in e['ultimos'] if es(u['ruta'])],
            'revisando': [r for r in e.get('revisar') or [] if es(r)]}


@app.route('/api/ficha/danadas', methods=['POST'])
def api_ficha_danadas():
    """Medición (ms perdidos, tramos) de las pistas de una ficha; con `carpeta` (Biblioteca) además la lista de
    pistas dañadas que dejó la revisión de Salud. Las que faltan medir se encargan."""
    d = request.get_json(silent=True) or {}
    pistas = []
    if d.get('carpeta'):
        carpeta = str(d['carpeta']).rstrip('/')
        aj = almacen.ajustes()
        if os.path.isdir(carpeta) and any(papelera_mod._dentro(carpeta, x) for x in
                                          (aj.get('biblioteca') or '/mnt/musica', aj.get('carpeta_entrada')) if x):
            pistas = salud.de_carpeta(carpeta)
        rutas = [p['ruta'] for p in pistas]
    else:
        rutas = _rutas_tocables(d.get('rutas'))
    flac = [r for r in rutas if r.lower().endswith('.flac')]
    cambiadas = [p['ruta'] for p in pistas if p.get('cambio')]
    if cambiadas:
        saneador.revisar(cambiadas)
    return jsonify(ok=True, pistas=pistas, medidas=salud.medidas(flac),
                   estado=_sanear_estado_de(rutas, d.get('carpeta') if pistas or d.get('carpeta') else None))


@app.route('/api/ficha/sanear', methods=['POST'])
def api_ficha_sanear():
    d = request.get_json(silent=True) or {}
    rutas = _rutas_tocables(d.get('rutas'))
    if not rutas:
        return jsonify(ok=False, error='Nada que sanear'), 400
    forzar = bool(d.get('forzar'))
    med = salud.medidas(rutas)
    ok = [r for r in rutas if (med.get(r) or {}).get('saneable') or forzar and _forzable(med.get(r) or {})]
    if not ok:
        return jsonify(ok=False, error='Ninguna se puede sanear (todavía se están midiendo o pierden demasiado)'), 409
    a = almacen.album(int(d['aid'])) if str(d.get('aid') or '').isdigit() else None
    if a and _editable(a):
        with _san_lock:
            _san_pend.setdefault(a['id'], set()).update(ok)
        almacen.registrar(a['id'], f'Sanear: {len(ok)} pista(s) en cola')
    saneador.sanear(ok, forzar=forzar)
    return jsonify(ok=True, en_cola=len(ok), saltadas=len(rutas) - len(ok))


@app.route('/api/ficha/tags', methods=['POST'])
def api_ficha_tags():
    """Reparar tags MP3 (letra/comentario UTF-16 con BOM malo) de una ficha."""
    d = request.get_json(silent=True) or {}
    rutas = [r for r in _rutas_tocables(d.get('rutas')) if r.lower().endswith('.mp3')]
    if not rutas:
        return jsonify(ok=False, error='Nada que reparar'), 400
    out = []
    for r in rutas:
        try:
            res = salud_mod.tags_mp3.reparar(r)
            out.append({'ruta': r, 'ok': True, 'texto': f'{res["rescatados"]} rescatados, {res["quitados"]} quitados'})
        except Exception as e:  # noqa: BLE001
            out.append({'ruta': r, 'ok': False, 'texto': str(e)})
    a = almacen.album(int(d['aid'])) if str(d.get('aid') or '').isdigit() else None
    if a and _editable(a):
        _requeue(a['id'])
    else:
        saneador.revisar([x['ruta'] for x in out if x['ok']])
    return jsonify(ok=True, resultados=out, revisar=bool(a and _editable(a)))


@app.route('/api/ficha/revisar', methods=['POST'])
def api_ficha_revisar():
    """Biblioteca: vuelve a decodificar las pistas del álbum (la misma revisión de Salud, sin esperar el ciclo)."""
    d = request.get_json(silent=True) or {}
    rutas = _rutas_tocables(d.get('rutas'))
    if rutas:
        saneador.revisar(rutas)
    return jsonify(ok=True, n=len(rutas))


@app.route('/api/papelera/<int:eid>/restaurar', methods=['POST'])
def api_papelera_restaurar(eid):
    if not procesador.biblioteca_lista():
        return _elim_error('La biblioteca no está montada')
    try:
        f = papelera.restaurar(eid)
    except papelera_mod.FalloPapelera as e:
        return _elim_error(str(e))
    extra = f.get('extra') or {}
    ir = None
    if f['origen'] == 'biblioteca':
        ruta = extra.get('album_ruta')
        aid = almacen.de_biblioteca(ruta)
        a = almacen.album(aid)
        if extra.get('eliminadas_rep') and a:
            cam = dict(a.get('cambios') or {})
            cam['eliminadas'] = [k for k in cam.get('eliminadas') or [] if k not in set(extra['eliminadas_rep'])]
            almacen.actualizar(aid, cambios=json.dumps(cam, ensure_ascii=False))
        if a and a['estado'] in ('completado', 'error_metadata', 'ubicado'):
            almacen.actualizar(aid, estado='por_metadata', progreso='En cola para la metadata (restaurado)')
            almacen.registrar(aid, f'Restaurado de la papelera: {f["detalle"] or f["titulo"]}')
            procesador.ya()
        if extra.get('pistas') or extra.get('albumes'):
            papelera.pendiente(dict(extra, titulo=f['titulo']))
        ir = url_for('album', aid=aid)
    else:
        a = almacen.album(extra.get('aid')) if extra.get('aid') else None
        if a and _editable(a):
            _requeue(a['id'])
            ir = url_for('album', aid=a['id'])
    return jsonify(ok=True, ir=ir)


@app.route('/api/papelera/vaciar', methods=['POST'])
def api_papelera_vaciar():
    d = request.get_json(silent=True) or {}
    ids = d.get('ids')
    if not d.get('todos') and not (isinstance(ids, list) and ids):
        return _elim_error('Nada seleccionado', 400)
    if ids is not None and not all(isinstance(i, int) for i in ids):
        return _elim_error('Lista inválida', 400)
    return jsonify(ok=True, **papelera.vaciar(None if d.get('todos') else ids))


@app.route('/api/salud/revisar', methods=['POST'])
def api_salud_revisar():
    """«Volver a revisar» pistas de la lista de Salud (sin re-escanear la biblioteca)."""
    rutas = [r for r in (request.get_json(silent=True) or {}).get('rutas') or [] if isinstance(r, str)]
    if not rutas:
        return jsonify(ok=False, error='Nada seleccionado'), 400
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    saneador.revisar(rutas[:2000])
    return jsonify(ok=True, en_cola=len(rutas[:2000]))


@app.route('/api/salud/tags/reparar', methods=['POST'])
def api_salud_tags_reparar():
    """«Reparar tags» de los MP3 cuyo único error es la letra/comentario con BOM malo (el audio no se toca)."""
    rutas = [r for r in (request.get_json(silent=True) or {}).get('rutas') or [] if isinstance(r, str)]
    if not rutas:
        return jsonify(ok=False, error='Nada seleccionado'), 400
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    r = salud.reparar_tags(rutas[:200])
    return jsonify(ok=True, resultado=r, reparadas=sum(1 for x in r if x['ok']))


@app.route('/api/salud/sin-archivo/quitar', methods=['POST'])
def api_salud_sin_archivo_quitar():
    """«Sin archivo» de Salud: quita esas filas de music.db (lo que apuntaba a ellas pasa a la pista equivalente)."""
    rutas = [r for r in (request.get_json(silent=True) or {}).get('rutas') or [] if isinstance(r, str)]
    if not rutas:
        return jsonify(ok=False, error='Nada seleccionado'), 400
    if procesador.actual:
        return jsonify(ok=False, error='Hay un álbum procesándose: espera a que termine'), 409
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    if not _unificando.acquire(blocking=False):
        return jsonify(ok=False, error='Hay otra operación sobre la base en curso'), 409
    lineas = []
    try:
        r = fantasmas_mod.limpiar_pistas(almacen.ajustes()['db_orbyte'], lineas.append, rutas=rutas)
    except Exception as e:  # noqa: BLE001
        log.exception('Salud: quitar filas sin archivo')
        return jsonify(ok=False, error=f'No se pudo: {e}'), 409
    finally:
        _unificando.release()
    for x in lineas:
        log.info('Salud: %s', x)
    salud.olvidar([x for x in rutas if not os.path.exists(x)])
    return jsonify(ok=True, registro=lineas, **r)


# ─────────────────────────── reparar solo las pistas dañadas ───────────────────────────

_PLANES = {}          # token → (momento, plan): lo que se mostró es lo que se repara


def _revisar_tras_reparar(aid):
    a = almacen.album(aid)
    if a and _editable(a):
        _requeue(aid)


def _ruta_reparable(r):
    """Archivo dentro de la biblioteca o de la entrada (los enlaces de una unión se resuelven a la biblioteca)."""
    aj = almacen.ajustes()
    real = os.path.realpath(r or '')
    raices = [os.path.realpath(x) for x in (aj.get('biblioteca') or '/mnt/musica', aj.get('carpeta_entrada') or '') if x]
    return real if os.path.isfile(real) and any(real.startswith(x + os.sep) for x in raices) else None


@app.route('/api/reparar/plan', methods=['POST'])
def api_reparar_plan():
    d = request.get_json(silent=True) or {}
    rutas = [_ruta_reparable(r) for r in d.get('rutas') or []]
    if not rutas or not all(rutas):
        return jsonify(ok=False, error='Alguna pista no está en la biblioteca ni en la entrada'), 400
    try:
        elegir = {_ruta_reparable(k): v for k, v in (d.get('elegir') or {}).items()}
        p = reparar_mod.plan(rutas, almacen.ajustes().get('db_orbyte'), d.get('edicion_id'),
                             busqueda=d.get('busqueda'), elegir={k: v for k, v in elegir.items() if k})
    except reparar_mod.Fallo as e:
        return jsonify(ok=False, error=str(e)), 409
    except Exception as e:
        log.exception('Reparar: plan')
        return jsonify(ok=False, error=f'{type(e).__name__}: {e}'), 500
    ahora = time.time()
    for k in [k for k, (t, _p) in _PLANES.items() if ahora - t > 3600]:
        _PLANES.pop(k, None)
    token = secrets.token_hex(8)
    _PLANES[token] = (ahora, p)
    return jsonify(ok=True, token=token, **p)


@app.route('/api/reparar', methods=['GET', 'POST'])
def api_reparar():
    if request.method == 'GET':
        return jsonify(ok=True, trabajos=reparador.lista(request.args.get('aid', type=int)), actual=reparador.actual)
    d = request.get_json(silent=True) or {}
    t = _PLANES.pop(d.get('token') or '', None)
    if not t:
        return jsonify(ok=False, error='La propuesta venció: ábrela de nuevo'), 409
    try:
        jid = reparador.encolar(t[1], {r: v or {} for r, v in (d.get('elegidas') or {}).items()}, d.get('aid'))
    except reparar_mod.Fallo as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, id=jid)


@app.route('/api/reparar/<int:jid>/quitar', methods=['POST'])
def api_reparar_quitar(jid):
    reparador.quitar(jid)
    return jsonify(ok=True)


# ─────────────────────────── carátula de un álbum ya en la biblioteca ───────────────────────────
# <k>: «b» = álbum de Orbyte (ficha de Biblioteca, id de music.db) · «a» = fila de Ingesta ya ubicada (ficha de Bandeja)

SUBIDAS = os.path.join(DATOS, 'subidas')
MAX_SUBIDA = 40 * 1024 * 1024


def _portada_vista(ruta, cover_path=None):
    try:
        return portada_mod.estado(ruta, cover_path)
    except Exception as e:
        log.warning('Carátula de %s: %s', ruta, e)
        return None


def _lib_portada(k, i):
    """(ruta del álbum, cover_path, fila de Ingesta o None, álbum de Orbyte o None); ruta None si no corresponde."""
    if k == 'b':
        o = _album_orbyte(i)
        if not o or not o['ruta'] or not os.path.isdir(o['ruta']):
            return None, None, None, None
        aid = almacen.fila_de_ruta(o['ruta'])
        return o['ruta'].rstrip('/'), o.get('cover_path'), (almacen.album(aid) if aid else None), o
    if k == 'a':
        a = almacen.album(i)
        if not a or a['estado'] not in EN_BIBLIOTECA and a['estado'] not in ('por_ajustar', 'ajustando') \
                or not (a['ubicado_en'] or a['ruta']):
            return None, None, None, None
        return (a['ubicado_en'] or a['ruta']).rstrip('/'), None, a, None
    return None, None, None, None


@app.route('/portada/<k>/<int:i>/img')
def portada_img(k, i):
    """?actual=1 · ?r=<imagen de la carpeta> · ?incrustada=<pista> ; &mini=1 = miniatura."""
    ruta, cover_path, _fila, _o = _lib_portada(k, i)
    if not ruta:
        abort(404)
    base = os.path.realpath(ruta)
    if request.args.get('actual'):
        full = os.path.join(base, 'Cover.jpg')
        if not os.path.isfile(full) and cover_path:
            full = os.path.realpath(cover_path)
    elif request.args.get('incrustada'):
        pista = os.path.realpath(os.path.join(base, request.args['incrustada']))
        if not pista.startswith(base + os.sep) or not os.path.isfile(pista):
            abort(404)
        full = _cache('incrustada-lib', pista, 'jpg')
        if not os.path.exists(full):
            datos = portada_mod.imagen_de_pista(pista)
            if not datos:
                abort(404)
            with open(full, 'wb') as f:
                f.write(datos)
    else:
        full = os.path.realpath(os.path.join(base, request.args.get('r') or ''))
        if os.path.splitext(full)[1].lower() not in analisis.IMAGEN_EXT:
            abort(404)
    if not full.startswith(base + os.sep) and not request.args.get('incrustada') or not os.path.isfile(full):
        abort(404)
    if request.args.get('mini'):
        full = _miniatura(full) or full
    return send_file(full, max_age=60)


@app.route('/portada/subida/<nombre>')
def portada_subida(nombre):
    full = os.path.join(SUBIDAS, os.path.basename(nombre))
    if not os.path.isfile(full):
        abort(404)
    return send_file(full, mimetype='image/jpeg', max_age=60)


@app.route('/api/portada/<k>/<int:i>/buscar')
def api_portada_buscar(k, i):
    ruta, _cp, fila, _o = _lib_portada(k, i)
    if not ruta:
        return jsonify(resultados=[])
    ediciones = (((fila or {}).get('reporte') or {}).get('edicion_mb') or {}).get('candidatas') or []
    return jsonify(resultados=procesar.buscar_caratulas(request.args.get('artista') or '',
                                                         request.args.get('album') or '', ediciones))


@app.route('/api/portada/<k>/<int:i>/subir', methods=['POST'])
def api_portada_subir(k, i):
    """Guarda la imagen subida (convertida a JPEG) hasta que se elija «Usar esta carátula»."""
    if not _lib_portada(k, i)[0]:
        return jsonify(ok=False, error='El álbum no está en la biblioteca'), 404
    if (request.content_length or 0) > MAX_SUBIDA:
        return jsonify(ok=False, error='La imagen pesa más de 40 MB'), 413
    f = request.files.get('imagen')
    if not f:
        return jsonify(ok=False, error='No llegó ninguna imagen'), 400
    try:
        jpg, tam = portada_mod.a_jpeg(f.read(MAX_SUBIDA + 1))
    except portada_mod.FalloPortada as e:
        return jsonify(ok=False, error=str(e)), 400
    os.makedirs(SUBIDAS, exist_ok=True)
    ahora = time.time()
    for viejo in os.listdir(SUBIDAS):                      # las que nunca se usaron, fuera al día
        try:
            if ahora - os.path.getmtime(os.path.join(SUBIDAS, viejo)) > 86400:
                os.remove(os.path.join(SUBIDAS, viejo))
        except OSError:
            pass
    nombre = secrets.token_hex(12) + '.jpg'
    with open(os.path.join(SUBIDAS, nombre), 'wb') as out:
        out.write(jpg)
    return jsonify(ok=True, subida=nombre, ancho=tam[0], alto=tam[1], tamano=len(jpg),
                   url=url_for('portada_subida', nombre=nombre))


@app.route('/api/portada/<k>/<int:i>', methods=['POST'])
def api_portada_usar(k, i):
    """Deja el álbum «En cola: ajustar» con la carátula elegida; el procesador la escribe e incrusta."""
    ruta, _cp, fila, o = _lib_portada(k, i)
    if not ruta:
        return jsonify(ok=False, error='El álbum no está en la biblioteca'), 404
    d = request.get_json(silent=True) or {}
    tipo = d.get('tipo')
    if tipo == 'archivo':
        pedido = {'tipo': 'archivo', 'archivo': str(d.get('archivo') or '')}
    elif tipo == 'incrustada':
        pedido = {'tipo': 'incrustada', 'pista': str(d.get('pista') or '')}
    elif tipo == 'url':
        url = str(d.get('url') or '')
        host = (re.match(r'https?://([^/:]+)', url) or [None, ''])[1]
        if not any(host == h or host.endswith('.' + h) for h in procesar.HOSTS_PORTADA):
            return jsonify(ok=False, error=f'No se descargan imágenes desde {host or "ese sitio"}'), 400
        pedido = {'tipo': 'url', 'url': url, 'fuente': str(d.get('fuente') or 'en línea')[:40]}
    elif tipo == 'subida':
        nombre = os.path.basename(str(d.get('subida') or ''))
        if not nombre or not os.path.isfile(os.path.join(SUBIDAS, nombre)):
            return jsonify(ok=False, error='No se encontró la imagen subida (vuelve a subirla)'), 400
        pedido = {'tipo': 'subida', 'subida': nombre}
    else:
        return jsonify(ok=False, error='No se indicó qué imagen usar'), 400
    if k == 'b' and not fila:
        fila = almacen.album(almacen.de_biblioteca(ruta, o['artista'], o['album'], o['anio']))
    if not fila or fila['estado'] not in EN_BIBLIOTECA:
        return jsonify(ok=False, error='El álbum se está procesando: espera a que termine'), 409
    cam = dict(fila.get('cambios') or {}, biblioteca={'caratula': pedido, 'volver': fila['estado'], 'lote': False})
    if not almacen.cambiar_estado(fila['id'], fila['estado'], 'por_ajustar', progreso='En cola para cambiar la carátula',
                                  cambios=json.dumps(cam, ensure_ascii=False)):
        return jsonify(ok=False, error='El álbum cambió de estado: recarga la página'), 409
    procesador.ya()
    return jsonify(ok=True)


@app.route('/biblioteca/album/<int:oid>/caratula')
def biblioteca_caratula(oid):
    o = _album_orbyte(oid)
    raiz = os.path.realpath(almacen.ajustes().get('biblioteca') or '/mnt/musica')
    cp = (o or {}).get('cover_path')
    if not cp or not os.path.realpath(cp).startswith(raiz + os.sep) or not os.path.isfile(cp):
        abort(404)
    return send_file(_miniatura(cp) or cp, max_age=3600)


@app.route('/api/biblioteca/<int:oid>/autocorregir', methods=['POST'])
def api_biblioteca_autocorregir(oid):
    o = _album_orbyte(oid)
    if not o or not o['ruta']:
        return jsonify(ok=False, error='Ese álbum ya no está en Orbyte'), 404
    aid = almacen.fila_de_ruta(o['ruta'])
    try:
        return jsonify(ok=True, **_propuesta_lib(o['ruta'], almacen.album(aid) if aid else None))
    except ajustar_mod.FalloAjuste as e:
        return jsonify(ok=False, error=str(e)), 409


@app.route('/api/biblioteca/<int:oid>/ajustar', methods=['POST'])
def api_biblioteca_ajustar(oid):
    o = _album_orbyte(oid)
    if not o or not o['ruta']:
        return jsonify(ok=False, error='Ese álbum ya no está en Orbyte'), 404
    d = request.get_json(silent=True) or {}
    aid = almacen.de_biblioteca(o['ruta'], o['artista'], o['album'], o['anio'])
    error, codigo = _encolar_ajuste(almacen.album(aid), {'tags': d.get('tags') or {}, 'pistas': d.get('pistas') or {}})
    if error:
        return jsonify(ok=False, error=error), codigo
    procesador.ya()
    return jsonify(ok=True)


@app.route('/api/biblioteca/<int:oid>/ignorar', methods=['POST'])
def api_biblioteca_ignorar(oid):
    o = _album_orbyte(oid)
    if not o:
        return jsonify(ok=False), 404
    almacen.ignorar_titulos(oid, o['ruta'], bool((request.get_json(silent=True) or {}).get('si', True)))
    return jsonify(ok=True)


MAX_LOTE = 40


@app.route('/api/biblioteca/lote/previa', methods=['POST'])
def api_biblioteca_lote_previa():
    """Autocorregir (solo títulos de pista) de varios álbumes: antes → después, sin guardar nada."""
    ids = [int(i) for i in ((request.get_json(silent=True) or {}).get('ids') or []) if str(i).isdigit()][:MAX_LOTE]

    def uno(oid):
        o = _album_orbyte(oid)
        if not o or not o['ruta']:
            return {'id': oid, 'error': 'Ya no está en Orbyte'}
        base = {'id': oid, 'artista': o['artista'], 'album': o['album'], 'anio': o['anio']}
        try:
            aid = almacen.fila_de_ruta(o['ruta'])
            fila = almacen.album(aid) if aid else None
            if fila and fila['estado'] not in EN_BIBLIOTECA:
                return dict(base, error='Se está procesando en este momento')
            prop = _propuesta_lib(o['ruta'], fila)
        except Exception as e:
            return dict(base, error=str(e))
        pistas = [{'clave': k, 'antes': c['titulo']['antes'], 'despues': c['titulo']['despues']}
                  for k, c in prop['pistas'].items() if 'titulo' in c and c['titulo']['grupo'] in ('titulos', 'mayusculas')]
        al_dia = False
        if not pistas:
            # ¿los archivos ya están bien y es Orbyte el que tiene los títulos viejos? (falta releer la metadata)
            lib = ajustar_mod.leer(o['ruta'])
            al_dia = not titulos_mod.revisar_pistas([(x['titulo'], x['artista']) for x in lib['plan']],
                                                    lib['tags_album']['artista'], lib['tags_album']['album'])[0]
        return dict(base, fuente=prop['fuente'], pistas=sorted(pistas, key=lambda p: p['clave']), al_dia=al_dia)

    with ThreadPoolExecutor(max_workers=4) as ex:
        return jsonify(albumes=list(ex.map(uno, ids)))


@app.route('/api/biblioteca/lote/aplicar', methods=['POST'])
def api_biblioteca_lote_aplicar():
    d = request.get_json(silent=True) or {}
    pedido = d.get('albumes') or {}
    encolados, errores = 0, []
    for oid in [int(i) for i in d.get('releer') or [] if str(i).isdigit()][:MAX_LOTE]:
        o = _album_orbyte(oid)
        if not o or not o['ruta']:
            continue
        a = almacen.album(almacen.de_biblioteca(o['ruta'], o['artista'], o['album'], o['anio']))
        if a['estado'] in EN_BIBLIOTECA and almacen.cambiar_estado(a['id'], a['estado'], 'por_metadata',
                                                                    progreso='En cola para la metadata', registro=None):
            encolados += 1
        else:
            errores.append(f'{o["artista"]} — {o["album"]}: se está procesando en este momento')
    for oid, cambios in list(pedido.items())[:MAX_LOTE]:
        o = _album_orbyte(int(oid)) if str(oid).isdigit() else None
        if not o or not o['ruta'] or not isinstance(cambios, dict) or not cambios:
            continue
        aid = almacen.de_biblioteca(o['ruta'], o['artista'], o['album'], o['anio'])
        error, _ = _encolar_ajuste(almacen.album(aid),
                                   {'pistas': {k: {'titulo': str(v).strip()} for k, v in cambios.items()
                                               if str(v).strip()}}, lote=True)
        if error:
            errores.append(f'{o["artista"]} — {o["album"]}: {error}')
        else:
            encolados += 1
    if encolados:
        procesador.ya()
    return jsonify(ok=True, encolados=encolados, errores=errores)


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


# ─────────────────────────── artistas duplicados ───────────────────────────

_unificando = artistas_mod.BLOQUEO


@app.route('/artistas')
def artistas_pagina():
    return render_template('artistas.html', pagina='artistas')


@app.route('/api/artistas')
def api_artistas():
    aj = almacen.ajustes()
    try:
        grupos = artistas_mod.detectar(aj['db_orbyte'], aj.get('biblioteca') or '/mnt/musica', almacen.ignorados())
    except Exception as e:
        return jsonify(grupos=[], error=str(e), historial=almacen.unificaciones())
    return jsonify(grupos=grupos, historial=almacen.unificaciones(), ocupado=_unificando.locked())


@app.route('/api/artistas/nombre')
def api_artistas_nombre():
    aj = almacen.ajustes()
    clave = request.args.get('clave') or ''
    grupo = next((g for g in artistas_mod.detectar(aj['db_orbyte'], aj.get('biblioteca') or '/mnt/musica')
                  if g['clave'] == clave), None)
    if not grupo:
        return jsonify(error='grupo no encontrado'), 404
    return jsonify(artistas_mod.nombre_oficial(vigilante.cliente_mb, grupo['variantes']))


@app.route('/api/artistas/ignorar', methods=['POST'])
def api_artistas_ignorar():
    clave = (request.get_json(silent=True) or {}).get('clave')
    if clave:
        almacen.ignorar(clave)
    return jsonify(ok=True)


@app.route('/api/artistas/unificar', methods=['POST'])
def api_artistas_unificar():
    d = request.get_json(silent=True) or {}
    ids = [int(i) for i in d.get('ids') or [] if str(i).isdigit()]
    final = (d.get('nombre') or '').strip()
    if len(ids) < 2 or not final:
        return jsonify(ok=False, error='Faltan datos'), 400
    if procesador.actual:
        return jsonify(ok=False, error='Hay un álbum procesándose: espera a que termine'), 409
    if not _unificando.acquire(blocking=False):
        return jsonify(ok=False, error='Ya hay una unificación en curso'), 409
    aj = almacen.ajustes()
    uid = almacen.nueva_unificacion(d.get('clave'), ' / '.join(str(i) for i in ids), final)

    def trabajo():
        try:
            res = artistas_mod.unificar(aj['db_orbyte'], aj.get('biblioteca') or '/mnt/musica', ids, final,
                                        lambda m: almacen.unificacion_log(uid, m))
            almacen.cerrar_unificacion(uid, 'ok', res)
            almacen.avisar('ok', f'Artista unificado: {final}',
                           f'{res["albumes"]} álbumes en {res["carpeta"]}' +
                           (f' · {len(res["avisos"])} avisos' if res['avisos'] else ''))
        except Exception as e:
            log.error('Unificar %s:\n%s', final, traceback.format_exc())
            almacen.unificacion_log(uid, f'ERROR: {type(e).__name__}: {e}')
            almacen.cerrar_unificacion(uid, 'error', {'error': str(e)})
            almacen.avisar('error', f'No se pudo unificar {final}', str(e))
        finally:
            _unificando.release()

    threading.Thread(target=trabajo, daemon=True, name='unificar').start()
    return jsonify(ok=True, id=uid)


@app.route('/api/artistas/unificacion/<int:uid>')
def api_artistas_unificacion(uid):
    u = next((x for x in almacen.unificaciones(50) if x['id'] == uid), None)
    return jsonify(u or {})


# ─────────────────────────── países de origen ───────────────────────────

recalculo_paises = nacionalidades_mod.Recalculo(os.path.join(DATOS, 'nacionalidades.json'))


@app.route('/artistas/paises')
def paises_pagina():
    return render_template('paises.html', pagina='artistas', artista=request.args.get('artista', ''))


@app.route('/api/paises')
def api_paises():
    d = recalculo_paises.estado()
    d['corriendo'] = recalculo_paises.corriendo()
    d['paises'] = nacionalidades_mod.lista_paises()
    return jsonify(d)


@app.route('/api/paises/recalcular', methods=['POST'])
def api_paises_recalcular():
    continuar = bool((request.get_json(silent=True) or {}).get('continuar'))
    ok = recalculo_paises.iniciar(almacen.ajustes()['db_orbyte'], vigilante.cliente_mb, continuar=continuar)
    return jsonify(ok=ok, error=None if ok else 'Ya hay una revisión en curso')


@app.route('/api/paises/detener', methods=['POST'])
def api_paises_detener():
    recalculo_paises.parar()
    return jsonify(ok=True)


@app.route('/api/paises/aplicar', methods=['POST'])
def api_paises_aplicar():
    ids = [int(i) for i in (request.get_json(silent=True) or {}).get('ids') or [] if str(i).isdigit()]
    try:
        n = recalculo_paises.aplicar(almacen.ajustes()['db_orbyte'], ids)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True, aplicados=n)


@app.route('/api/paises/buscar')
def api_paises_buscar():
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify(artistas=[])
    return jsonify(artistas=nacionalidades_mod.buscar(almacen.ajustes()['db_orbyte'], q))


@app.route('/api/paises/<int:artist_id>', methods=['POST'])
def api_paises_fijar(artist_id):
    codigo = (request.get_json(silent=True) or {}).get('codigo') or ''
    try:
        nacionalidades_mod.fijar(almacen.ajustes()['db_orbyte'], artist_id, codigo)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    recalculo_paises.marcar_manual(artist_id, codigo.lower() or None)
    return jsonify(ok=True)


@app.route('/api/paises/<int:artist_id>/buscar', methods=['POST'])
def api_paises_uno(artist_id):
    try:
        fila = recalculo_paises.uno(almacen.ajustes()['db_orbyte'], vigilante.cliente_mb, artist_id)
    except RuntimeError as e:
        return jsonify(ok=False, error=str(e)), 502
    if not fila:
        return jsonify(ok=False, error='Artista no encontrado'), 404
    return jsonify(ok=True, fila=fila)


# ─────────────────────────── álbumes fantasma ───────────────────────────

_busqueda_fantasmas = {'estado': None, 'avance': [0, 0], 'resultado': None, 'error': None, 'ts': None}


@app.route('/fantasmas')
def fantasmas_pagina():
    return render_template('fantasmas.html', pagina='fantasmas')


@app.route('/api/fantasmas')
def api_fantasmas():
    b = dict(_busqueda_fantasmas)
    b['historial'] = [u for u in almacen.unificaciones(30) if u.get('clave') == 'fantasmas']
    b['ocupado'] = _unificando.locked()
    return jsonify(b)


@app.route('/api/fantasmas/buscar', methods=['POST'])
def api_fantasmas_buscar():
    if _busqueda_fantasmas['estado'] == 'buscando':
        return jsonify(ok=True)
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    aj = almacen.ajustes()
    _busqueda_fantasmas.update(estado='buscando', avance=[0, 0], resultado=None, error=None)

    def trabajo():
        try:
            r = fantasmas_mod.buscar(aj['db_orbyte'],
                                     lambda i, n: _busqueda_fantasmas.update(avance=[i, n]))
            _busqueda_fantasmas.update(estado='listo', resultado=r, ts=time.time())
        except Exception as e:
            _busqueda_fantasmas.update(estado='error', error=str(e))

    threading.Thread(target=trabajo, daemon=True, name='fantasmas').start()
    return jsonify(ok=True)


@app.route('/api/fantasmas/limpiar', methods=['POST'])
def api_fantasmas_limpiar():
    ids = [int(i) for i in (request.get_json(silent=True) or {}).get('ids') or [] if str(i).isdigit()]
    if not ids:
        return jsonify(ok=False, error='Nada seleccionado'), 400
    if procesador.actual:
        return jsonify(ok=False, error='Hay un álbum procesándose: espera a que termine'), 409
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    if not _unificando.acquire(blocking=False):
        return jsonify(ok=False, error='Hay otra operación sobre la base en curso'), 409
    aj = almacen.ajustes()
    uid = almacen.nueva_unificacion('fantasmas', ' '.join(map(str, ids)), f'{len(ids)} álbumes fantasma')

    def trabajo():
        try:
            res = fantasmas_mod.limpiar(aj['db_orbyte'], ids, lambda m: almacen.unificacion_log(uid, m))
            almacen.cerrar_unificacion(uid, 'ok', res)
            almacen.avisar('ok', f'Álbumes fantasma eliminados: {res["borrados"]}',
                           f'{res["movidas"]} referencias pasadas a su gemelo')
            _busqueda_fantasmas.update(estado=None, resultado=None)
        except Exception as e:
            log.error('Fantasmas:\n%s', traceback.format_exc())
            almacen.unificacion_log(uid, f'ERROR: {type(e).__name__}: {e}')
            almacen.cerrar_unificacion(uid, 'error', {'error': str(e)})
        finally:
            _unificando.release()

    threading.Thread(target=trabajo, daemon=True, name='limpiar-fantasmas').start()
    return jsonify(ok=True, id=uid)


# ─────────────────────────── álbumes repetidos ───────────────────────────

_cache_repetidos = {'clave': None, 'grupos': []}
_midiendo = {'cola': [], 'actual': None, 'avance': [0, 0], 'hilo': None}
_lock_medir = threading.Lock()


def _repetidos(forzar=False):
    """Grupos de repetidos (sin los marcados «conservar todas»); se recalcula si cambia music.db."""
    aj = almacen.ajustes()
    db = aj['db_orbyte']
    revisados = almacen.repetidos_revisados()
    try:
        clave = (os.path.getmtime(db), tuple(sorted(revisados.items())))
    except OSError:
        clave = None
    if forzar or clave is None or _cache_repetidos['clave'] != clave:
        _cache_repetidos.update(clave=clave, grupos=repetidos_mod.detectar(db, revisados))
    return _cache_repetidos['grupos']


def _grupo(clave, firma=None):
    g = next((x for x in _repetidos() if x['clave'] == clave), None)
    if g and firma and g['firma'] != firma:
        return None
    return g


def _con_mediciones(g):
    for e in g['ediciones']:
        e['medicion'] = almacen.medicion(e['ruta'], e['bytes'])
        e['en_cola'] = e['ruta'] in _midiendo['cola'] or _midiendo['actual'] == e['ruta']
    return g


@app.route('/repetidos')
def repetidos_pagina():
    return render_template('repetidos.html', pagina='repetidos')


@app.route('/api/repetidos')
def api_repetidos():
    try:
        grupos = [_con_mediciones(dict(g, ediciones=[dict(e) for e in g['ediciones']]))
                  for g in _repetidos(request.args.get('recalcular') == '1')]
    except Exception as e:
        log.error('Repetidos:\n%s', traceback.format_exc())
        return jsonify(grupos=[], error=str(e))
    for g in grupos:
        sug = almacen.sugerencia(g['clave'], g['firma'])
        g['sugerencia'] = sug if sug and sug.get('v') == repetidos_mod.VERSION_IA else None
    return jsonify(grupos=grupos, liberable=sum(g['liberable'] for g in grupos),
                   raiz=almacen.ajustes().get('biblioteca') or '/mnt/musica',
                   ocupado=_unificando.locked(), midiendo=dict(_midiendo, hilo=None),
                   historial=[u for u in almacen.unificaciones(30) if u.get('clave') == 'repetidos'],
                   ia=bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GROQ_API_KEY')))


@app.route('/api/repetidos/detalle')
def api_repetidos_detalle():
    g = _grupo(request.args.get('clave') or '')
    if not g:
        return jsonify(error='El grupo cambió o ya no existe: recarga la página'), 404
    g = repetidos_mod.detalle(almacen.ajustes()['db_orbyte'], dict(g, ediciones=[dict(e) for e in g['ediciones']]))
    return jsonify(_con_mediciones(g))


@app.route('/api/repetidos/portada')
def api_repetidos_portada():
    ruta = request.args.get('ruta') or ''
    if not any(e.get('portada') == ruta for g in _repetidos() for e in g['ediciones']) or not os.path.isfile(ruta):
        abort(404)
    return send_file(_miniatura(ruta) or ruta, max_age=3600)


def _medidor():
    while True:
        with _lock_medir:
            if not _midiendo['cola']:
                _midiendo.update(actual=None, avance=[0, 0], hilo=None)
                return
            ruta = _midiendo['cola'].pop(0)
            _midiendo.update(actual=ruta, avance=[0, 0])
        e = next((x for g in _repetidos() for x in g['ediciones'] if x['ruta'] == ruta), None)
        if not e or almacen.medicion(ruta, e['bytes']):
            continue
        try:
            datos = repetidos_mod.medir(almacen.ajustes()['db_orbyte'], e,
                                        lambda i, n: _midiendo.update(avance=[i, n]),
                                        recursos.nucleos(almacen.ajustes()))
        except Exception as ex:
            log.error('Medir %s:\n%s', ruta, traceback.format_exc())
            datos = {'error': str(ex)}
        almacen.guardar_medicion(ruta, e['bytes'], datos)


@app.route('/api/repetidos/medir', methods=['POST'])
def api_repetidos_medir():
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    d = request.get_json(silent=True) or {}
    claves = d.get('claves') or ([d['clave']] if d.get('clave') else [])
    rutas = []
    for g in _repetidos():
        if g['clave'] in claves:
            rutas += [e['ruta'] for e in g['ediciones'] if not almacen.medicion(e['ruta'], e['bytes'])]
    with _lock_medir:
        for r in rutas:
            if r not in _midiendo['cola'] and r != _midiendo['actual']:
                _midiendo['cola'].append(r)
        if _midiendo['cola'] and not _midiendo['hilo']:
            _midiendo['hilo'] = threading.Thread(target=_medidor, daemon=True, name='medir-master')
            _midiendo['hilo'].start()
    return jsonify(ok=True, en_cola=len(_midiendo['cola']))


@app.route('/api/repetidos/medir/cancelar', methods=['POST'])
def api_repetidos_medir_cancelar():
    with _lock_medir:
        _midiendo['cola'].clear()
    return jsonify(ok=True)


@app.route('/api/repetidos/sugerir', methods=['POST'])
def api_repetidos_sugerir():
    d = request.get_json(silent=True) or {}
    g = _grupo(d.get('clave') or '')
    if not g:
        return jsonify(ok=False, error='El grupo cambió o ya no existe: recarga la página'), 404
    g = repetidos_mod.detalle(almacen.ajustes()['db_orbyte'], dict(g, ediciones=[dict(e) for e in g['ediciones']]))
    mediciones = {e['ruta']: almacen.medicion(e['ruta'], e['bytes']) for e in g['ediciones']}
    r = repetidos_mod.sugerir(g, mediciones)
    if r.get('error'):
        return jsonify(ok=False, error=r['error'])
    r['con_medicion'] = sum(1 for m in mediciones.values() if m and m.get('lufs') is not None)
    almacen.guardar_sugerencia(g['clave'], g['firma'], r)
    return jsonify(ok=True, sugerencia=r)


@app.route('/api/repetidos/conservar', methods=['POST'])
def api_repetidos_conservar():
    d = request.get_json(silent=True) or {}
    g = _grupo(d.get('clave') or '', d.get('firma'))
    if not g:
        return jsonify(ok=False, error='El grupo cambió: recarga la página'), 409
    almacen.marcar_repetido(g['clave'], g['firma'])
    return jsonify(ok=True)


@app.route('/api/repetidos/eliminar', methods=['POST'])
def api_repetidos_eliminar():
    d = request.get_json(silent=True) or {}
    if d.get('confirmacion') != 'ELIMINAR':
        return jsonify(ok=False, error='Falta la confirmación'), 400
    if procesador.actual:
        return jsonify(ok=False, error='Hay un álbum procesándose: espera a que termine'), 409
    if not procesador.biblioteca_lista():
        return jsonify(ok=False, error='La biblioteca no está montada'), 409
    g = _grupo(d.get('clave') or '', d.get('firma'))
    if not g:
        return jsonify(ok=False, error='El grupo cambió desde que lo miraste: recarga la página'), 409
    rutas = {e['ruta'] for e in g['ediciones']}
    borrar = [r for r in d.get('borrar') or [] if r in rutas]
    quedan = [e['ruta'] for e in g['ediciones'] if e['ruta'] not in borrar]
    destino = d.get('destino') if d.get('destino') in quedan else (quedan[0] if quedan else None)
    if not borrar or not destino:
        return jsonify(ok=False, error='Debe quedar al menos una edición'), 400
    if _midiendo['actual'] in borrar:
        return jsonify(ok=False, error='Se está midiendo una de esas ediciones: espera a que termine'), 409
    if not _unificando.acquire(blocking=False):
        return jsonify(ok=False, error='Hay otra operación sobre la base en curso'), 409
    aj = almacen.ajustes()
    titulo = f'{g["artista"]} — {g["album"]}'
    uid = almacen.nueva_unificacion('repetidos', ' | '.join(borrar), titulo)

    def trabajo():
        try:
            res = repetidos_mod.eliminar(aj['db_orbyte'], aj.get('biblioteca') or '/mnt/musica', g, borrar, destino,
                                         lambda m: almacen.unificacion_log(uid, m))
            almacen.cerrar_unificacion(uid, 'ok', res)
            if len(quedan) > 1:          # elegiste quedarte con varias: el grupo queda revisado así
                almacen.marcar_repetido(g['clave'], '|'.join(sorted(quedan)))
            almacen.avisar('ok', f'Repetido resuelto: {titulo}',
                           f'{res["liberado"] / 1e9:.2f} GB liberados · {res["movidas"]} referencias pasadas '
                           f'a la edición que se queda')
        except Exception as e:
            log.error('Repetidos %s:\n%s', titulo, traceback.format_exc())
            almacen.unificacion_log(uid, f'ERROR: {type(e).__name__}: {e}')
            almacen.cerrar_unificacion(uid, 'error', {'error': str(e)})
            almacen.avisar('error', f'No se pudo eliminar: {titulo}', str(e))
        finally:
            _cache_repetidos['clave'] = None
            _unificando.release()

    threading.Thread(target=trabajo, daemon=True, name='eliminar-repetidos').start()
    return jsonify(ok=True, id=uid)


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
    if r.get('origen_entrada') and r.get('carpeta'):
        # ISO de SACD: las pistas están en la extracción local y lo que vino con la ISO, enlazado desde la entrada
        base = os.path.realpath(r['carpeta'])
        raices = (base, os.path.realpath(r['origen_entrada']))
    else:
        base = os.path.realpath(a['ruta'])
        # «Unir como discos»: la carpeta tiene enlaces a los archivos de los álbumes de la biblioteca
        raices = (base,) + tuple(os.path.realpath(u) for u in (a.get('cambios') or {}).get('union') or [])
    raiz = base
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
        if not any(origen.startswith(x + os.sep) for x in raices) or not os.path.isfile(origen):
            abort(404)
    elif car.get('tipo') == 'incrustada' and not disco and not request.args.get('r'):
        pista = os.path.realpath(os.path.join(raiz, car['pista']))
        if not any(pista.startswith(x + os.sep) for x in raices) or not os.path.isfile(pista):
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
    descargador.start()
    salud.start()
    verificador.start()
    try:
        _token_interno()          # que exista antes de reiniciar Orbyte
    except OSError:
        log.exception('token interno')
    threading.Thread(target=verificador.actualizar_criterio, name='calidad-criterio', daemon=True).start()
    reparador.start()
    compat.start()
    log.info('Orbyte Ingesta en el puerto %s · datos en %s', PUERTO, DATOS)
    app.run(host='0.0.0.0', port=PUERTO, threaded=True, debug=False, use_reloader=False)
