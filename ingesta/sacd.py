"""
Orbyte Ingesta — imágenes ISO de SACD.

Una carpeta de la entrada con una ISO de SACD (y sin otro audio) es un álbum. Antes de revisarlo:
  1. si viene un .md5 o .sfv con la suma de la ISO, se comprueba (una ISO dañada no se extrae);
  2. `sacd_extract -P` lee las áreas del disco (estéreo / multicanal) y sus textos;
  3. se extrae el área que corresponde a .dsf (DST → DSD sin pérdida) en el disco LOCAL
     (ingesta_datos/sacd/…), no en la biblioteca; las pistas quedan sueltas en una carpeta con el nombre
     de la carpeta original, junto a enlaces a lo que venía con la ISO (Artwork/, folder.jpg, logs…);
  4. desde ahí la revisión es la de siempre.

Si el disco trae estéreo y multicanal, se pregunta (decisión «sacd_area»): estéreo / multicanal / ambos.
«Ambos» crea un segundo álbum en la Bandeja (ruta de la entrada + «#multicanal») que se ubica aparte como
«[DSD MC]». Mientras no se elige, se extrae el estéreo para poder revisar el resto.

Herramienta: sacd_extract (github.com/sacd-ripper/sacd-ripper, tools/sacd_extract).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import zlib

SUFIJO_MC = '#multicanal'
AREAS = {'estereo': '2', 'multicanal': 'm'}


# ─────────────────────────── detección ───────────────────────────

def es_iso_sacd(ruta):
    """Una ISO de SACD tiene la tabla maestra «SACDMTOC» en el sector 510."""
    try:
        with open(ruta, 'rb') as f:
            f.seek(510 * 2048)
            return f.read(8) == b'SACDMTOC'
    except OSError:
        return False


def iso_de(carpeta):
    """La ISO de SACD de una carpeta (no recursivo), o None."""
    try:
        for n in sorted(os.listdir(carpeta)):
            p = os.path.join(carpeta, n)
            if n.lower().endswith('.iso') and os.path.isfile(p) and es_iso_sacd(p):
                return p
    except OSError:
        pass
    return None


def separar(ruta):
    """«…/Álbum#multicanal» → («…/Álbum», 'multicanal'); una ruta normal → (ruta, None)."""
    if ruta and ruta.endswith(SUFIJO_MC):
        return ruta[:-len(SUFIJO_MC)], 'multicanal'
    return ruta, None


# ─────────────────────────── herramienta ───────────────────────────

def herramienta(aj):
    p = (aj or {}).get('sacd_extract') or shutil.which('sacd_extract')
    return p if p and os.path.isfile(p) and os.access(p, os.X_OK) else None


def probar(ruta=None):
    exe = ruta or shutil.which('sacd_extract')
    if not exe:
        return {'ok': False, 'error': 'No se encontró sacd_extract (ni en Ajustes ni en el PATH)'}
    try:
        r = subprocess.run([exe, '-v'], capture_output=True, text=True, errors='replace', timeout=20)
        m = re.search(r'sacd_extract[^\n]*?(\d+\.\d+[\d.]*)', r.stdout + r.stderr)
        return {'ok': True, 'ruta': exe, 'version': m.group(1) if m else '?'}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def leer_info(exe, iso):
    """Áreas y textos del disco según `sacd_extract -P`."""
    r = subprocess.run([exe, '-P', '-i', iso], capture_output=True, text=True, errors='replace', timeout=300)
    txt = r.stdout
    info = {'areas': [], 'album': None, 'artista': None, 'catalogo': None}
    disco = txt.split('Album Information:')[0]
    album = txt.split('Album Information:')[1].split('Area count:')[0] if 'Album Information:' in txt else ''
    for bloque, campo, clave in ((album, 'Title', 'album'), (disco, 'Title', 'album'),
                                 (album, 'Artist', 'artista'), (disco, 'Artist', 'artista'),
                                 (disco, 'Disc Catalog Number', 'catalogo')):
        m = re.search(r'^\s*' + campo + r':\s*(.+?)\s*$', bloque, re.M)
        if m and not info[clave]:
            info[clave] = m.group(1)
    for parte in re.split(r'Area Information \[\d+\]:', txt)[1:]:
        canales = re.search(r'Speaker config:\s*(\d+) Channel', parte)
        n = re.search(r'Track Count:\s*(\d+)', parte)
        titulos = re.findall(r'^\s*Title\[\d+\]:\s*(.+?)\s*$', parte, re.M)
        info['areas'].append({'canales': int(canales.group(1)) if canales else None,
                              'pistas': int(n.group(1)) if n else len(titulos), 'titulos': titulos})
    if not info['areas'] and r.returncode != 0:
        raise RuntimeError(f'sacd_extract no pudo leer la ISO: {(r.stderr or txt).strip()[-300:]}')
    return info


def areas_disponibles(info):
    tipos = set()
    for a in info['areas']:
        tipos.add('estereo' if (a['canales'] or 2) <= 2 else 'multicanal')
    return [t for t in ('estereo', 'multicanal') if t in tipos]


# ─────────────────────────── integridad ───────────────────────────

def _suma(iso, algoritmo):
    h = hashlib.md5() if algoritmo == 'md5' else None
    crc = 0
    with open(iso, 'rb') as f:
        for bloque in iter(lambda: f.read(8 << 20), b''):
            if h:
                h.update(bloque)
            else:
                crc = zlib.crc32(bloque, crc)
    return h.hexdigest() if h else f'{crc & 0xffffffff:08x}'


def comprobar(carpeta, iso):
    """(True/False/None, detalle): compara la ISO con su .md5 o .sfv si vienen; None si no hay con qué."""
    nombre = os.path.basename(iso).lower()
    for n in sorted(os.listdir(carpeta)):
        ext = os.path.splitext(n)[1].lower()
        if ext not in ('.md5', '.sfv'):
            continue
        try:
            lineas = open(os.path.join(carpeta, n), encoding='utf-8', errors='replace').read().splitlines()
        except OSError:
            continue
        for l in lineas:
            if l.startswith(';') or nombre not in l.lower():
                continue
            if ext == '.md5':
                m = re.match(r'\s*([0-9a-fA-F]{32})\s+\*?(.+?)\s*$', l)
                if m and os.path.basename(m.group(2).replace('\\', '/')).lower() == nombre:
                    real = _suma(iso, 'md5')
                    return real == m.group(1).lower(), f'MD5 de la ISO {"coincide" if real == m.group(1).lower() else "NO coincide"} con {n}'
            else:
                m = re.match(r'\s*(.+?)\s+([0-9a-fA-F]{8})\s*$', l)
                if m and os.path.basename(m.group(1).replace('\\', '/')).lower() == nombre:
                    real = _suma(iso, 'crc')
                    return real == m.group(2).lower(), f'CRC32 de la ISO {"coincide" if real == m.group(2).lower() else "NO coincide"} con {n}'
    return None, 'No viene un .md5 ni .sfv con la suma de la ISO'


# ─────────────────────────── extracción ───────────────────────────

def _clave(iso, area):
    st = os.stat(iso)
    return hashlib.sha1(f'{iso}:{st.st_size}:{int(st.st_mtime)}:{area}'.encode()).hexdigest()[:16]


def _enlazar_acompanantes(origen, destino, iso):
    """Lo que vino con la ISO (carátulas, Artwork/, logs) queda visible en la carpeta extraída como enlaces."""
    for raiz, dirs, archivos in os.walk(origen):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        rel = os.path.relpath(raiz, origen)
        for a in archivos:
            src = os.path.join(raiz, a)
            if os.path.abspath(src) == os.path.abspath(iso):
                continue
            dst = os.path.join(destino, rel, a)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.lexists(dst):
                os.symlink(src, dst)


def preparar(carpeta, area, aj, datos, progreso=None):
    """
    Deja lista la carpeta extraída del área pedida ('estereo' | 'multicanal' | None = la que corresponda).
    Devuelve dict: carpeta (extraída), iso, info, area, areas, integridad (ok, detalle).
    La extracción queda en caché: revisar de nuevo no vuelve a extraer.
    """
    progreso = progreso or (lambda m: None)
    exe = herramienta(aj)
    if not exe:
        raise RuntimeError('Falta sacd_extract en el servidor (ver Ajustes → «sacd_extract»)')
    iso = iso_de(carpeta)
    if not iso:
        raise RuntimeError('La carpeta ya no tiene la ISO del SACD')
    base = os.path.join(datos, 'sacd', _clave(iso, 'disco'))
    os.makedirs(base, exist_ok=True)
    meta_p = os.path.join(base, 'disco.json')
    meta = json.load(open(meta_p)) if os.path.isfile(meta_p) else {}
    if 'integridad' not in meta:
        progreso('Comprobando la suma de la ISO')
        ok, detalle = comprobar(carpeta, iso)
        meta['integridad'] = [ok, detalle]
    if 'info' not in meta:
        progreso('Leyendo las áreas del SACD')
        meta['info'] = leer_info(exe, iso)
    meta['iso'] = iso
    meta['entrada'] = carpeta
    json.dump(meta, open(meta_p, 'w'), ensure_ascii=False)

    disponibles = areas_disponibles(meta['info'])
    if meta['integridad'][0] is False:
        return {'carpeta': None, 'iso': iso, 'info': meta['info'], 'area': None, 'areas': disponibles,
                'integridad': meta['integridad']}
    if area not in disponibles:
        area = 'estereo' if 'estereo' in disponibles else (disponibles[0] if disponibles else 'estereo')

    destino = os.path.join(base, area, os.path.basename(carpeta.rstrip('/')))
    listo = os.path.join(base, area, '.listo')
    if not os.path.isfile(listo):
        shutil.rmtree(os.path.join(base, area), ignore_errors=True)
        trabajo = os.path.join(base, area, '.extrayendo')
        os.makedirs(trabajo)
        libre = shutil.disk_usage(base).free
        necesario = int(os.path.getsize(iso) * 2.2)
        if libre < necesario:
            raise RuntimeError(f'Espacio insuficiente para extraer el SACD: hay {libre >> 30} GB, '
                               f'se necesitan ~{necesario >> 30} GB en {datos}')
        progreso(f'Extrayendo el SACD ({ "estéreo" if area == "estereo" else "multicanal" }) a DSF…')
        r = subprocess.run([exe, '-' + AREAS[area], '-s', '-c', '-i', iso], cwd=trabajo,
                           capture_output=True, text=True, errors='replace', timeout=4 * 3600)
        pistas = sorted(os.path.join(rz, a) for rz, _, fs in os.walk(trabajo) for a in fs
                        if a.lower().endswith('.dsf'))
        if not pistas:
            raise RuntimeError(f'sacd_extract no generó pistas: {(r.stderr or r.stdout).strip()[-400:]}')
        os.makedirs(destino)
        for p in pistas:                       # sueltas en la carpeta del álbum (sin «Álbum/Stereo/»)
            shutil.move(p, os.path.join(destino, os.path.basename(p)))
        shutil.rmtree(trabajo, ignore_errors=True)
        _enlazar_acompanantes(carpeta, destino, iso)
        open(listo, 'w').write(str(len(pistas)))
    return {'carpeta': destino, 'iso': iso, 'info': meta['info'], 'area': area, 'areas': disponibles,
            'integridad': meta['integridad']}


def limpiar(datos, iso=None, entrada=None):
    """Borra extracciones de ISOs que ya no están (o la de una ISO puntual)."""
    raiz = os.path.join(datos, 'sacd')
    if not os.path.isdir(raiz):
        return
    for n in os.listdir(raiz):
        d = os.path.join(raiz, n)
        try:
            meta = json.load(open(os.path.join(d, 'disco.json')))
        except (OSError, ValueError):
            meta = {}
        if (iso and meta.get('iso') == iso) or (entrada and meta.get('entrada') == entrada) \
                or (not iso and not entrada and not os.path.isfile(meta.get('iso') or '')):
            shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────── después de la revisión ───────────────────────────

NOMBRE_AREA = {'estereo': 'estéreo', 'multicanal': 'multicanal'}
OPCIONES = [{'valor': 'estereo', 'texto': 'Solo estéreo'},
            {'valor': 'multicanal', 'texto': 'Solo multicanal'},
            {'valor': 'ambos', 'texto': 'Ambos (multicanal como álbum aparte)'}]


def completar_reporte(rep, prep, cambios, area_forzada, entrada):
    """Agrega al reporte lo propio de la ISO: origen, áreas, integridad, decisión de área y destino «[DSD MC]»."""
    probs = rep.setdefault('problemas', [])

    def problema(nivel, codigo, texto, opciones=None):
        probs.append({'nivel': nivel, 'codigo': codigo, 'texto': texto, 'pistas': [], 'opciones': opciones or []})

    rep['origen_entrada'] = entrada
    rep['sacd'] = {'iso': os.path.basename(prep['iso']), 'area': prep['area'], 'areas': prep['areas'],
                   'integridad': prep['integridad'][1]}
    detalle = prep['integridad'][1]
    problema('aviso', 'sacd_integridad', f'SACD: {detalle}')
    problema('correccion', 'sacd_extraido',
             f'Extraído de la ISO del SACD (área {NOMBRE_AREA[prep["area"]]}) a DSF; si venía en DST, '
             f'se descomprimió sin pérdida', [])
    if len(prep['areas']) > 1 and not area_forzada:
        eleccion = (cambios or {}).get('sacd_area')
        if eleccion:
            problema('aviso', 'sacd_area_elegida',
                     {'estereo': 'Área elegida por ti: solo estéreo', 'multicanal': 'Área elegida por ti: solo multicanal',
                      'ambos': 'Área elegida por ti: ambas; el multicanal aparece aparte en la Bandeja'}[eleccion])
        else:
            problema('decision', 'sacd_area', 'El SACD trae área estéreo y multicanal: ¿qué quieres ubicar? '
                                              '(mientras, se revisa el estéreo)', OPCIONES)
    if prep['area'] == 'multicanal':
        for p in probs:                       # el multicanal lo pediste tú: no hay nada que decidir
            if p['codigo'] == 'multicanal' and p['nivel'] == 'decision':
                p['nivel'], p['texto'] = 'aviso', p['texto'] + ' — elegido por ti'
        d = rep.get('destino') or {}
        for k in ('ruta', 'album'):
            if d.get(k) and d[k].endswith('[DSD]'):
                d[k] = d[k][:-len('[DSD]')] + '[DSD MC]'
        if d.get('ruta'):
            d['ruta_existe'] = os.path.exists(d['ruta'])
            probs[:] = [p for p in probs if p['codigo'] != 'destino_existe']
            if d['ruta_existe']:
                problema('decision', 'destino_existe', f'Ya existe la carpeta de destino {d["ruta"]}')
    niveles = {}
    for p in probs:
        niveles[p['nivel']] = niveles.get(p['nivel'], 0) + 1
    rep['conteo_problemas'] = niveles
    rep['estado'] = 'error' if niveles.get('error') else 'requiere_revision' if niveles.get('decision') else 'revisado'
    return rep
