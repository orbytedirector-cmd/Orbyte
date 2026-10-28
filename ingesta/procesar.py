"""
Orbyte Ingesta — etapa 2: corregir y ubicar un álbum en la biblioteca.

Ejecuta el «plan» que dejó la revisión:
  1. arma el álbum en una carpeta temporal dentro de la propia biblioteca (mismo disco);
  2. copia o convierte cada pista (corte por .cue exacto por muestra, coma flotante → 24 bits
     con la ganancia del álbum, WavPack/APE/ALAC/WAV → FLAC), nunca recodifica MQA, DSD ni lossy;
  3. escribe los tags (los del álbum + edición de MusicBrainz) y quita ID3 pegados a FLAC;
  4. guarda Cover.jpg (y «Cover CD2.jpg»… si cada disco trae portada) y los extras (escaneos, .log);
  5. verifica cada archivo (audio idéntico al original cuando no hubo conversión, duración y decodificación);
  6. recién entonces mueve la carpeta a su lugar definitivo y borra el original de la entrada.
Si algo falla, borra la carpeta temporal y deja el original intacto.
"""
import io
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import analisis
from . import dsd

try:
    import mutagen
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB, TDRC, TDOR, TRCK, TPOS, TCON, TPUB, TXXX
except ImportError:          # el venv de Orbyte ya trae mutagen
    mutagen = None

EXTRAS_EXT = {'.log', '.txt', '.nfo', '.pdf', '.rtf', '.html', '.htm'}
HOSTS_PORTADA = ('coverartarchive.org', 'archive.org', 'mzstatic.com', 'dzcdn.net', 'deezer.com')
LADO_MAX_DESCARGA = 2000


class FalloProceso(Exception):
    pass


def ubicar(rep, ajustes, registro, borrar_origen=True, trabajadores=2):
    """Ejecuta la etapa 2. Devuelve la ruta final. Lanza FalloProceso con el motivo si algo sale mal."""
    if mutagen is None:
        raise FalloProceso('Falta mutagen en el entorno de Python')
    if (rep.get('version') or 0) < analisis.VERSION_REPORTE or 'plan' not in rep:
        raise FalloProceso('La revisión es de una versión anterior: vuelve a revisar el álbum')
    origen = rep['carpeta']
    destino = rep['destino']['ruta']
    biblioteca = os.path.abspath(ajustes.get('biblioteca') or '/mnt/musica')
    if not os.path.isdir(biblioteca):
        raise FalloProceso(f'No existe la biblioteca {biblioteca} (¿disco montado?)')
    if not os.access(biblioteca, os.W_OK):
        raise FalloProceso(f'El servicio no tiene permiso de escritura en {biblioteca}')
    if os.path.exists(destino):
        raise FalloProceso(f'Ya existe {destino}')
    if not os.path.abspath(destino).startswith(biblioteca + os.sep):
        raise FalloProceso('La ruta de destino queda fuera de la biblioteca')
    libre = shutil.disk_usage(biblioteca).free
    necesario = int(rep.get('tamano', 0) * 1.3) + 200 * 1024 * 1024
    if libre < necesario:
        raise FalloProceso(f'Espacio insuficiente en la biblioteca: hay {libre >> 20} MB, se necesitan {necesario >> 20} MB')

    tmp = os.path.join(biblioteca, '.ingesta_tmp', f'{int(time.time())}-{os.getpid()}-{abs(hash(origen)) % 10**6}')
    os.makedirs(tmp)
    try:
        plan = rep['plan']
        registro(f'Armando {len(plan)} pistas en una carpeta temporal')
        hechas = [0]

        def una(x, de_nuevo=False):
            salida = os.path.join(tmp, x['destino'])
            fuente = os.path.join(origen, x['origen'])
            if x['accion'] == 'copiar':
                shutil.copy2(fuente, salida)
            elif x['accion'] in ('a_dsf', 'cortar_dsd'):
                b0, b1 = _bytes_dsd(fuente, x)
                dsd.a_dsf(fuente, salida, b0, b1)
            else:
                convertir(fuente, salida, x, rep.get('ganancia_db') or 0.0)
            escribir_tags(salida, x, rep['tags_album'], rep)
            if x.get('portada_disco'):
                incrustar_portada(salida, os.path.join(origen, x['portada_disco']))
            if de_nuevo:
                return salida
            hechas[0] += 1
            nota = {'convertir': '  (convertida)',
                    'a_dsf': '  (DFF → DSF, mismo DSD)' if x['origen'].lower().endswith('.dff')
                    else '  (DSF reescrito, mismo DSD)',
                    'cortar_dsd': '  (cortada en DSD)'}.get(x['accion'], '')
            registro(f'  {hechas[0]}/{len(plan)} {x["destino"]}{nota}')
            return salida

        with ThreadPoolExecutor(max_workers=max(1, trabajadores)) as ex:
            list(ex.map(una, plan))

        registro('Carátula')
        guardar_caratulas(rep, origen, tmp, registro)
        n_extras = copiar_extras(rep, origen, tmp)
        if n_extras:
            registro(f'Extras (escaneos, .log…): {n_extras} archivos en «Extras/»')

        registro('Verificando cada archivo')

        def revisar(x):
            return verificar(os.path.join(origen, x['origen']), os.path.join(tmp, x['destino']), x,
                             rep.get('ganancia_db') or 0.0)

        fallidas = [(x, p) for x in plan for p in [revisar(x)] if p]
        # una copia que no calza se rehace UNA vez (fallos esporádicos de escritura/CPU bajo carga);
        # si vuelve a fallar, el álbum no se ubica
        rehechas = []
        for x, p in fallidas:
            registro(f'  ⟳ {x["destino"]}: {p}; se copia de nuevo')
            if os.path.exists(os.path.join(tmp, x['destino'])):
                os.remove(os.path.join(tmp, x['destino']))
            una(x, de_nuevo=True)
            p2 = revisar(x)
            if p2:
                raise FalloProceso(f'La verificación falló dos veces:\n{x["destino"]}: {p2}')
            registro(f'  ✓ {x["destino"]}: la segunda copia sí es idéntica')
            rehechas.append(x['destino'])
        rep['_rehechas'] = rehechas
        registro(f'Verificación OK ({len(plan)} pistas)' + (f' · {len(rehechas)} copiada(s) de nuevo' if rehechas else ''))

        # el origen no debe haber cambiado mientras se trabajaba
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        if os.path.exists(destino):
            raise FalloProceso(f'Apareció {destino} mientras se trabajaba')
        os.rename(tmp, destino)
        registro(f'Ubicado en {destino}')
    except FalloProceso:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise FalloProceso(f'{type(e).__name__}: {e}')
    finally:
        _limpiar_tmp_vacio(biblioteca)

    if borrar_origen and not rep.get('origen_entrada'):     # una ISO la cierra el procesador (ver _cerrar_iso)
        borrar_de_entrada(origen, ajustes.get('carpeta_entrada'))
        registro('Original borrado de la carpeta de entrada')
    return destino


def _bytes_dsd(fuente, x):
    """Tramo (bytes por canal) que va a la pista: todo el archivo, o del cue con precisión de muestra."""
    if not x.get('corte'):
        return 0, None
    fs = dsd.info(fuente)['fs']
    ini, fin = x['corte']
    return dsd.byte_de(ini, fs), (dsd.byte_de(fin, fs) if fin is not None else None)


def _limpiar_tmp_vacio(biblioteca):
    t = os.path.join(biblioteca, '.ingesta_tmp')
    try:
        if os.path.isdir(t) and not os.listdir(t):
            os.rmdir(t)
    except OSError:
        pass


def borrar_de_entrada(carpeta, entrada):
    """Borra la carpeta del álbum y las carpetas padre que queden vacías (sin tocar la raíz de la entrada)."""
    entrada = os.path.realpath(entrada or '')
    real = os.path.realpath(carpeta)
    if not entrada or not real.startswith(entrada + os.sep):
        raise FalloProceso('El original no está dentro de la carpeta de entrada: no se borra')
    shutil.rmtree(real)
    padre = os.path.dirname(real)
    while padre.startswith(entrada + os.sep):
        try:
            if os.listdir(padre):
                break
            os.rmdir(padre)
        except OSError:
            break
        padre = os.path.dirname(padre)


# ─────────────────────────── conversión ───────────────────────────

def _filtros(x, ganancia):
    f = []
    if x.get('corte'):
        ini, fin = x['corte']
        sr = x.get('sr') or 44100
        t = f'atrim=start_sample={round(ini * sr)}'
        if fin is not None:
            t += f':end_sample={round(fin * sr)}'
        f += [t, 'asetpts=PTS-STARTPTS']
    if x.get('float') and ganancia:
        f.append(f'volume={ganancia}dB')
    return f


def _bits_salida(x):
    if x.get('float'):
        return 24
    b = x.get('bits') or 16
    return 16 if b <= 16 else 24


def convertir(fuente, salida, x, ganancia):
    if x.get('mqa') and (x.get('float') or ganancia):
        raise FalloProceso(f'{x["origen"]}: es MQA y no se puede modificar el audio')
    bits = _bits_salida(x)
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', fuente, '-map', '0:a:0', '-map_metadata', '-1']
    f = _filtros(x, ganancia)
    if f:
        cmd += ['-af', ','.join(f)]
    if bits == 16:
        cmd += ['-c:a', 'flac', '-sample_fmt', 's16']
    else:
        cmd += ['-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', '24']
    cmd += ['-compression_level', '8', salida]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(salida):
        raise FalloProceso(f'ffmpeg no pudo convertir {x["origen"]}: {r.stderr.strip()[-300:]}')


# ─────────────────────────── tags ───────────────────────────

def _valores(x, ta):
    d = {
        'titulo': x.get('titulo'), 'artista': x.get('artista') or ta.get('artista'),
        'artista_album': ta.get('artista'), 'album': ta.get('album'),
        'fecha': ta.get('fecha') or ta.get('anio'), 'anio_original': ta.get('anio_original'),
        'pista': x.get('numero'), 'total_pistas': x.get('total_pistas'),
        'disco': x.get('disco') or 1, 'total_discos': ta.get('total_discos') or 1,
        'genero': ta.get('genero'), 'mbid': ta.get('mbid'), 'mb_grupo': ta.get('mb_grupo'),
        'sello': ta.get('sello'), 'catalogo': ta.get('catalogo'), 'pais': ta.get('pais'),
        'barcode': ta.get('barcode'), 'genero_secundario': ta.get('genero_secundario') or None,
    }
    return {k: v for k, v in d.items() if v not in (None, '', [])}


VORBIS = {'titulo': 'TITLE', 'artista': 'ARTIST', 'artista_album': 'ALBUMARTIST', 'album': 'ALBUM',
          'fecha': 'DATE', 'anio_original': 'ORIGINALDATE', 'pista': 'TRACKNUMBER', 'total_pistas': 'TRACKTOTAL',
          'disco': 'DISCNUMBER', 'total_discos': 'DISCTOTAL', 'genero': 'GENRE',
          'mbid': 'MUSICBRAINZ_ALBUMID', 'mb_grupo': 'MUSICBRAINZ_RELEASEGROUPID', 'sello': 'LABEL',
          'catalogo': 'CATALOGNUMBER', 'pais': 'RELEASECOUNTRY', 'barcode': 'BARCODE'}
TXXX_DESC = {'mbid': 'MusicBrainz Album Id', 'mb_grupo': 'MusicBrainz Release Group Id',
        'catalogo': 'CATALOGNUMBER', 'pais': 'MusicBrainz Album Release Country', 'barcode': 'BARCODE'}


def escribir_tags(ruta, x, ta, rep):
    v = _valores(x, ta)
    ext = os.path.splitext(ruta)[1].lower()
    if ext == '.flac':
        f = FLAC(ruta)
        if f.tags is None:
            f.add_tags()
        for k, clave in VORBIS.items():
            if k in v:
                f.tags[clave] = str(v[k])
        if v.get('genero_secundario'):          # como lo escribe RichMeta: un valor por género
            f.tags['GENRE_SECONDARY'] = list(v['genero_secundario'])
        for viejo in ('TOTALTRACKS', 'TOTALDISCS'):
            if viejo in f.tags:
                del f.tags[viejo]
        f.save(deleteid3=bool(x.get('quitar_id3')))
    elif ext in ('.mp3', '.dsf', '.aif', '.aiff', '.dff'):
        _tags_id3(ruta, v, ext)
    elif ext in ('.m4a', '.mp4'):
        from mutagen.easymp4 import EasyMP4
        f = EasyMP4(ruta)
        mapa = {'titulo': 'title', 'artista': 'artist', 'artista_album': 'albumartist', 'album': 'album',
                'fecha': 'date', 'genero': 'genre', 'mbid': 'musicbrainz_albumid',
                'mb_grupo': 'musicbrainz_releasegroupid', 'pais': 'releasecountry', 'barcode': 'barcode'}
        for k, clave in mapa.items():
            if k in v:
                f[clave] = str(v[k])
        if 'pista' in v:
            f['tracknumber'] = f'{v["pista"]}/{v.get("total_pistas", "")}'.rstrip('/')
        f['discnumber'] = f'{v["disco"]}/{v["total_discos"]}'
        f.save()
    elif ext in ('.ogg', '.oga', '.opus'):
        f = mutagen.File(ruta)
        for k, clave in VORBIS.items():
            if k in v:
                f[clave] = str(v[k])
        f.save()
    # otros formatos (WMA…): se dejan los tags que traen


def _tags_id3(ruta, v, ext):
    if ext == '.dff':
        # .dff (solo queda si es DST): el ID3 va como bloque «ID3 » al final, nunca pegado al inicio
        datos = dsd.id3_dff(ruta)
        t = ID3(io.BytesIO(datos)) if datos else ID3()
        _frames_id3(t, v)
        b = io.BytesIO()
        t.save(b, v2_version=3)
        dsd.escribir_id3_dff(ruta, b.getvalue())
        return
    if ext == '.dsf':
        from mutagen.dsf import DSF
        f = DSF(ruta)
        if f.tags is None:
            f.add_tags()
        t = f.tags
    else:
        try:
            t = ID3(ruta)
        except ID3NoHeaderError:
            t = ID3()
        f = None
    _frames_id3(t, v)
    if f is not None:
        f.save()
    else:
        t.save(ruta, v2_version=3)


def _frames_id3(t, v):
    simples = {'titulo': TIT2, 'artista': TPE1, 'artista_album': TPE2, 'album': TALB, 'fecha': TDRC,
               'anio_original': TDOR, 'genero': TCON, 'sello': TPUB}
    for k, frame in simples.items():
        if k in v:
            t.setall(frame.__name__, [frame(encoding=3, text=str(v[k]))])
    if 'pista' in v:
        t.setall('TRCK', [TRCK(encoding=3, text=f'{v["pista"]}/{v.get("total_pistas", "")}'.rstrip('/'))])
    t.setall('TPOS', [TPOS(encoding=3, text=f'{v["disco"]}/{v["total_discos"]}')])
    for k, desc in TXXX_DESC.items():
        if k in v:
            t.setall(f'TXXX:{desc}', [TXXX(encoding=3, desc=desc, text=str(v[k]))])
    if v.get('genero_secundario'):               # como lo escribe RichMeta: «a;b;c»
        t.setall('TXXX:genre_secondary', [TXXX(encoding=3, desc='genre_secondary',
                                                text=';'.join(v['genero_secundario']))])


def incrustar_portada(ruta, imagen):
    if not ruta.lower().endswith('.flac') or not os.path.isfile(imagen):
        return
    datos = _a_jpeg(open(imagen, 'rb').read())
    pic = Picture()
    pic.type = 3
    pic.mime = 'image/jpeg'
    pic.data = datos
    if analisis.Image is not None:
        with analisis.Image.open(io.BytesIO(datos)) as im:
            pic.width, pic.height = im.size
            pic.depth = 24
    f = FLAC(ruta)
    f.clear_pictures()
    f.add_picture(pic)
    f.save()


# ─────────────────────────── carátulas y extras ───────────────────────────

def _a_jpeg(datos, lado_max=None):
    """Bytes de imagen → JPEG (si ya es JPEG y no hay que achicar, se deja tal cual)."""
    if analisis.Image is None:
        return datos
    with analisis.Image.open(io.BytesIO(datos)) as im:
        grande = lado_max and max(im.size) > lado_max
        if im.format == 'JPEG' and not grande:
            return datos
        im = im.convert('RGB')
        if grande:
            im.thumbnail((lado_max, lado_max), analisis.Image.LANCZOS)
        b = io.BytesIO()
        im.save(b, 'JPEG', quality=92)
        return b.getvalue()


def descargar(url):
    host = urllib.parse.urlparse(url).hostname or ''
    if not any(host == h or host.endswith('.' + h) for h in HOSTS_PORTADA):
        raise FalloProceso(f'No se descargan imágenes desde {host}')
    req = urllib.request.Request(url, headers={'User-Agent': 'OrbyteIngesta/1.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        datos = r.read(40 * 1024 * 1024)
    return datos


def imagen_incrustada(pista):
    r = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', pista, '-map', '0:v:0', '-frames:v', '1',
                        '-c:v', 'copy', '-f', 'image2pipe', '-'], capture_output=True, timeout=60)
    if r.returncode != 0 or not r.stdout:
        r = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', pista, '-map', '0:v:0', '-frames:v', '1',
                            '-f', 'image2pipe', '-c:v', 'mjpeg', '-q:v', '2', '-'], capture_output=True, timeout=60)
    if not r.stdout:
        raise FalloProceso(f'No se pudo extraer la carátula incrustada de {os.path.basename(pista)}')
    return r.stdout


def guardar_caratulas(rep, origen, tmp, registro):
    car = rep.get('caratula') or {}
    tipo = car.get('tipo')
    datos = None
    if tipo == 'archivo':
        datos = _a_jpeg(open(os.path.join(origen, car['archivo']), 'rb').read())
    elif tipo == 'incrustada':
        datos = _a_jpeg(imagen_incrustada(os.path.join(origen, car.get('pista') or car.get('incrustada_en'))))
    elif tipo == 'caa' and car.get('mb'):
        datos = _a_jpeg(descargar(f'https://coverartarchive.org/release/{car["mb"]}/front'), LADO_MAX_DESCARGA)
    elif tipo == 'url' and car.get('url'):
        datos = _a_jpeg(descargar(car['url']), LADO_MAX_DESCARGA)
    if datos:
        with open(os.path.join(tmp, 'Cover.jpg'), 'wb') as f:
            f.write(datos)
    else:
        registro('  (sin carátula)')
    for d in car.get('por_disco') or []:
        if (d.get('disco') or 0) >= 2:
            with open(os.path.join(origen, d['archivo']), 'rb') as f:
                dd = _a_jpeg(f.read())
            with open(os.path.join(tmp, f'Cover CD{d["disco"]}.jpg'), 'wb') as f:
                f.write(dd)


def copiar_extras(rep, origen, tmp):
    """Escaneos y .log/.txt/.pdf van a «Extras/» (no se llevan .cue, .m3u, .sfv… que apuntan a los nombres viejos)."""
    usados = set()
    car = rep.get('caratula') or {}
    if car.get('tipo') == 'archivo' and car.get('archivo'):
        usados.add(car['archivo'])
    for d in car.get('por_disco') or []:
        usados.add(d['archivo'])
    n = 0
    for raiz, dirs, archivos in os.walk(origen):
        for a in archivos:
            ext = os.path.splitext(a)[1].lower()
            rel = os.path.relpath(os.path.join(raiz, a), origen)
            if rel in usados or a.startswith('.'):
                continue
            if ext in EXTRAS_EXT or ext in analisis.IMAGEN_EXT:
                dst = os.path.join(tmp, 'Extras', rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(os.path.join(raiz, a), dst)
                n += 1
    return n


# ─────────────────────────── verificación ───────────────────────────

def md5_audio(ruta, filtros=None, bits=None, paquetes=False):
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-i', ruta, '-map', '0:a:0']
    if paquetes:
        cmd += ['-c', 'copy']
    f = list(filtros or [])
    if bits:
        f.append(f'aformat=sample_fmts={"s16" if bits == 16 else "s32"}')
    if f:
        cmd += ['-af', ','.join(f)]
    cmd += ['-f', 'md5', '-']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    return r.stdout.strip() if r.returncode == 0 else None


def _md5_flac(ruta):
    try:
        m = FLAC(ruta).info.md5_signature
        return m or None
    except Exception:
        return None


def verificar(fuente, salida, x, ganancia):
    """Devuelve None si está bien, o el motivo del problema."""
    if not os.path.isfile(salida):
        return 'no se creó'
    ok, errores, _, segundos = analisis.decodificar(salida)
    if not ok:
        # el original ya traía esos errores (se leyó completo y lo aceptaste): la copia debe leerse igual de
        # completa, y más abajo se compara que el audio sea idéntico
        largo = x.get('duracion_origen') or 0
        if not (x.get('errores_aceptados') and segundos is not None and largo and segundos >= largo - 0.5):
            return 'no decodifica: ' + '; '.join(errores[:2])
    if x['accion'] in ('a_dsf', 'cortar_dsd'):           # DSD: lo exacto sale del conteo de muestras
        d = dsd.info(fuente)
        b0, b1 = _bytes_dsd(fuente, x)
        esperado = ((b1 if b1 is not None else d['bytes_canal']) - b0) * 8 / d['fs']
    elif x.get('corte'):
        ini, fin = x['corte']
        esperado = (fin if fin is not None else (x.get('duracion_origen') or 0)) - ini
    else:
        esperado = x.get('duracion_origen') or 0
    if x['accion'] != 'copiar' and esperado and segundos is not None and abs(segundos - esperado) > 0.1:
        return f'duración {segundos:.2f} s, se esperaba {esperado:.2f} s'
    # audio idéntico al original (salvo coma flotante con ganancia, que cambia a propósito)
    if x.get('float'):
        return None
    if x['accion'] == 'copiar' and salida.lower().endswith('.flac'):
        a, b = _md5_flac(fuente), _md5_flac(salida)
        if a and b:
            return None if a == b else 'el audio no es idéntico al original'
    if x.get('dsd') and x['accion'] == 'copiar' and salida.lower().endswith('.dsf'):
        try:                                               # DSF copiado: el DSD intacto tras escribir los tags
            return None if dsd.md5(fuente) == dsd.md5(salida) else 'el DSD no es idéntico al original'
        except dsd.ErrorDSD as e:
            return str(e)
    if x['accion'] in ('a_dsf', 'cortar_dsd'):           # DSD: muestra por muestra igual al original
        b0, b1 = _bytes_dsd(fuente, x)
        try:
            return None if dsd.md5(fuente, b0, b1) == dsd.md5(salida) else 'el DSD no es idéntico al original'
        except dsd.ErrorDSD as e:
            return str(e)
    if x.get('dsd') and salida.lower().endswith('.dff'):  # DST copiado: el bloque de audio intacto tras los tags
        try:
            return None if dsd.md5_bloque(fuente) == dsd.md5_bloque(salida) else 'el DST no es idéntico al original'
        except dsd.ErrorDSD as e:
            return str(e)
    if x.get('dsd'):
        return None
    if x['accion'] == 'copiar' and x.get('errores_aceptados'):
        # con errores de decodificación se comparan los paquetes tal cual (sin decodificar): la copia solo
        # cambió los tags, los cuadros de audio deben ser los mismos bit a bit
        a, b = md5_audio(fuente, paquetes=True), md5_audio(salida, paquetes=True)
        if not a or not b:
            return 'no se pudo comparar el audio'
        return None if a == b else 'el audio no es idéntico al original'
    bits = None if x['accion'] == 'copiar' else _bits_salida(x)
    a = md5_audio(fuente, _filtros(x, 0) if x.get('corte') else None, bits)
    b = md5_audio(salida, None, bits)
    if not a or not b:
        return 'no se pudo comparar el audio'
    return None if a == b else 'el audio no es idéntico al original'


# ─────────────────────────── búsqueda de carátulas en línea ───────────────────────────

def buscar_caratulas(artista, album, ediciones=(), limite=12):
    """iTunes, Deezer y Cover Art Archive (de las ediciones candidatas). Sin claves."""
    import json
    res = []
    for e in ediciones[:4]:
        if e.get('portada') is not False:
            res.append({'fuente': 'Cover Art Archive', 'titulo': f'{e.get("titulo")} ({e.get("fecha") or "?"})',
                        'url': f'https://coverartarchive.org/release/{e["id"]}/front',
                        'miniatura': f'https://coverartarchive.org/release/{e["id"]}/front-250', 'mb': e['id']})
    termino = f'{artista} {album}'.strip()
    try:
        u = 'https://itunes.apple.com/search?' + urllib.parse.urlencode({'term': termino, 'entity': 'album', 'limit': 6})
        with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'OrbyteIngesta/1.0'}), timeout=15) as r:
            for it in json.loads(r.read()).get('results', []):
                art = it.get('artworkUrl100')
                if art:
                    res.append({'fuente': 'iTunes', 'titulo': f'{it.get("collectionName")} — {it.get("artistName")}',
                                'url': art.replace('100x100bb', '1400x1400bb'),
                                'miniatura': art.replace('100x100bb', '300x300bb')})
    except Exception:
        pass
    try:
        u = 'https://api.deezer.com/search/album?' + urllib.parse.urlencode({'q': f'artist:"{artista}" album:"{album}"'})
        with urllib.request.urlopen(urllib.request.Request(u, headers={'User-Agent': 'OrbyteIngesta/1.0'}), timeout=15) as r:
            for it in json.loads(r.read()).get('data', [])[:6]:
                if it.get('cover_xl'):
                    res.append({'fuente': 'Deezer', 'titulo': f'{it.get("title")} — {(it.get("artist") or {}).get("name")}',
                                'url': it['cover_xl'], 'miniatura': it.get('cover_medium') or it['cover_xl']})
    except Exception:
        pass
    return res[:limite]

