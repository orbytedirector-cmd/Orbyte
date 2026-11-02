"""
Orbyte Ingesta — carátula y formato de los álbumes que ya están en la biblioteca.

- estado(ruta, cover_path): la Cover.jpg actual, las imágenes de la carpeta y si hay una incrustada.
- aplicar(ruta, pedido, ...): reemplaza Cover.jpg y la incrusta en todas las pistas (FLAC, DSF, DFF, MP3,
  AIFF, M4A). El audio no se toca: solo el bloque de tags de cada archivo.
- formatos(ruta, plan): etiqueta y clase de calidad de cada archivo («DSD256», «FLAC 24/96»…), sin decodificar.
"""
import io
import os
import sqlite3
from pathlib import Path

from . import analisis, dsd, procesar

LIMITE_CANDIDATAS = 30
AUDIO_EMBEBIBLE = ('.flac', '.dsf', '.dff', '.mp3', '.aif', '.aiff', '.m4a', '.mp4')


class FalloPortada(Exception):
    pass


# ─────────────────────────── qué hay ───────────────────────────

def _info(ruta):
    d = {'tamano': os.path.getsize(ruta)}
    if analisis.Image is not None:
        try:
            with analisis.Image.open(ruta) as im:          # solo lee la cabecera
                d['ancho'], d['alto'] = im.size
                d['formato'] = (im.format or '').upper()
        except Exception:
            d['ilegible'] = True
    return d


def _audios(ruta):
    from .ajustar import _audio                 # mismo criterio que el editor de tags (sin «Extras»)
    return [os.path.join(ruta, rel) for rel in _audio(ruta)]


def estado(ruta, cover_path=None):
    """{'actual': {...}|None, 'candidatas': [...], 'incrustada': rel|None}"""
    ruta = ruta.rstrip('/')
    actual = None
    cover = os.path.join(ruta, 'Cover.jpg')
    if not os.path.isfile(cover) and cover_path and os.path.isfile(cover_path) \
            and os.path.realpath(cover_path).startswith(os.path.realpath(ruta) + os.sep):
        cover = cover_path
    if os.path.isfile(cover):
        actual = dict(_info(cover), archivo=os.path.relpath(cover, ruta), mtime=int(os.path.getmtime(cover)))
    lista = []
    for d, dirs, archivos in os.walk(ruta):
        dirs[:] = sorted(x for x in dirs if not x.startswith('.'))
        for a in archivos:
            if a.startswith('.') or Path(a).suffix.lower() not in analisis.IMAGEN_EXT:
                continue
            full = os.path.join(d, a)
            rel = os.path.relpath(full, ruta)
            if actual and rel == actual['archivo']:
                continue
            lista.append((analisis._puntaje_imagen(a), rel.count(os.sep), rel.lower(), rel))
    lista.sort()
    candidatas = [dict(_info(os.path.join(ruta, rel)), archivo=rel) for *_, rel in lista[:LIMITE_CANDIDATAS]]
    incrustada = None
    for full in _audios(ruta)[:3]:
        try:
            if _tiene_incrustada(full):
                incrustada = os.path.relpath(full, ruta)
                break
        except Exception:
            continue
    return {'actual': actual, 'candidatas': candidatas, 'incrustada': incrustada}


def _tiene_incrustada(full):
    return bool(imagen_de_pista(full, solo_mirar=True))


def imagen_de_pista(full, solo_mirar=False):
    """Bytes de la carátula incrustada en una pista (o None). Con solo_mirar devuelve True/None."""
    ext = os.path.splitext(full)[1].lower()
    datos = None
    if ext == '.flac':
        from mutagen.flac import FLAC
        pics = FLAC(full).pictures
        pic = next((p for p in pics if p.type == 3), pics[0] if pics else None)
        datos = pic.data if pic else None
    elif ext == '.dff':
        from mutagen.id3 import ID3
        b = dsd.id3_dff(full)
        if b:
            apics = ID3(io.BytesIO(b)).getall('APIC')
            datos = apics[0].data if apics else None
    elif ext in ('.dsf', '.mp3', '.aif', '.aiff'):
        import mutagen
        f = mutagen.File(full)
        apics = f.tags.getall('APIC') if f is not None and f.tags is not None and hasattr(f.tags, 'getall') else []
        datos = apics[0].data if apics else None
    elif ext in ('.m4a', '.mp4'):
        from mutagen.mp4 import MP4
        covr = (MP4(full).tags or {}).get('covr') or []
        datos = bytes(covr[0]) if covr else None
    else:
        if solo_mirar:
            return None
        try:
            datos = procesar.imagen_incrustada(full)
        except Exception:
            datos = None
    if solo_mirar:
        return True if datos else None
    return datos


# ─────────────────────────── formato de cada pista (sin decodificar) ───────────────────────────

LOSSY = {'.mp3': 'MP3', '.ogg': 'OGG', '.opus': 'Opus', '.aac': 'AAC'}


def formato(full):
    """(etiqueta, clase) como en la revisión: ('DSD256', 'DSD'), ('FLAC 24/96', 'HR'), ('MP3 · 320 kbps', 'Lossy')."""
    ext = os.path.splitext(full)[1].lower()
    try:
        if ext in ('.dsf', '.dff'):
            fs = dsd.info(full).get('fs')
            return analisis.DSD_RATES.get(fs) or 'DSD', 'DSD'
        import mutagen
        f = mutagen.File(full)
        info = f.info if f is not None else None
        if info is None:
            return ext.lstrip('.').upper(), None
        sr = getattr(info, 'sample_rate', 0) or 0
        if ext in LOSSY or (ext in ('.m4a', '.mp4') and getattr(info, 'codec', '') != 'alac'):
            nombre = LOSSY.get(ext) or 'AAC'
            br = getattr(info, 'bitrate', 0) or 0
            k = analisis.kbps_nominal(br, nombre.lower())
            return (f'{nombre} · {k} kbps' if k else nombre), 'Lossy'
        nombre = {'.flac': 'FLAC', '.m4a': 'ALAC', '.mp4': 'ALAC', '.wv': 'WavPack', '.ape': "Monkey's Audio",
                  '.wav': 'WAV', '.aif': 'AIFF', '.aiff': 'AIFF'}.get(ext, ext.lstrip('.').upper())
        bits = getattr(info, 'bits_per_sample', None)
        etiqueta = f'{nombre} {bits or "?"}/{sr / 1000:g}' if sr else nombre
        return etiqueta, ('HR' if sr > 48000 else 'CD')
    except Exception:
        return ext.lstrip('.').upper(), None


def formatos(ruta, plan):
    """{clave: (etiqueta, clase)} de las pistas de ajustar.leer()."""
    return {x['clave']: formato(os.path.join(ruta, x['clave'])) for x in plan}


# ─────────────────────────── aplicar ───────────────────────────

def _bytes_de(ruta, pedido, subidas):
    tipo = pedido.get('tipo')
    if tipo == 'archivo':
        full = os.path.realpath(os.path.join(ruta, pedido.get('archivo') or ''))
        if not full.startswith(os.path.realpath(ruta) + os.sep) or not os.path.isfile(full):
            raise FalloPortada('Esa imagen ya no está en la carpeta del álbum')
        with open(full, 'rb') as f:
            return f.read(), os.path.relpath(full, os.path.realpath(ruta))
    if tipo == 'incrustada':
        full = os.path.realpath(os.path.join(ruta, pedido.get('pista') or ''))
        if not full.startswith(os.path.realpath(ruta) + os.sep) or not os.path.isfile(full):
            raise FalloPortada('Esa pista ya no está en la carpeta del álbum')
        datos = imagen_de_pista(full)
        if not datos:
            raise FalloPortada('La pista no trae carátula incrustada')
        return datos, f'incrustada en {os.path.basename(full)}'
    if tipo == 'url':
        return procesar.descargar(pedido.get('url') or ''), pedido.get('fuente') or 'en línea'
    if tipo == 'subida':
        nombre = os.path.basename(pedido.get('subida') or '')
        full = os.path.join(subidas, nombre)
        if not nombre or not os.path.isfile(full):
            raise FalloPortada('No se encontró la imagen subida (vuelve a subirla)')
        with open(full, 'rb') as f:
            return f.read(), 'subida desde tu equipo'
    raise FalloPortada('No se indicó qué imagen usar')


def a_jpeg(datos):
    if analisis.Image is None:
        raise FalloPortada('Falta Pillow para convertir la imagen')
    try:
        with analisis.Image.open(io.BytesIO(datos)) as im:
            im.verify()
    except Exception:
        raise FalloPortada('El archivo no es una imagen válida')
    jpg = procesar._a_jpeg(datos, procesar.LADO_MAX_DESCARGA)
    with analisis.Image.open(io.BytesIO(jpg)) as im:
        return jpg, im.size


def incrustar(full, jpg, tam):
    """Reemplaza la carátula incrustada de una pista (solo el bloque de tags). False si el formato no admite."""
    ext = os.path.splitext(full)[1].lower()
    if ext == '.flac':
        from mutagen.flac import FLAC, Picture
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, 'image/jpeg', jpg
        pic.width, pic.height = tam
        pic.depth = 24
        f = FLAC(full)
        f.clear_pictures()
        f.add_picture(pic)
        f.save()
        return True
    if ext in ('.dsf', '.dff', '.mp3', '.aif', '.aiff'):
        from mutagen.id3 import APIC, ID3, ID3NoHeaderError

        def poner(t):
            t.delall('APIC')
            t.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=jpg))
        if ext == '.dff':                                  # bloque «ID3 » al final, sin recorrer el DST
            b = dsd.id3_dff(full)
            t = ID3(io.BytesIO(b)) if b else ID3()
            poner(t)
            out = io.BytesIO()
            t.save(out, v2_version=3)
            dsd.escribir_id3_dff(full, out.getvalue())
            return True
        if ext == '.dsf':
            from mutagen.dsf import DSF
            f = DSF(full)
        elif ext in ('.aif', '.aiff'):
            from mutagen.aiff import AIFF
            f = AIFF(full)
        else:
            try:
                t = ID3(full)
            except ID3NoHeaderError:
                t = ID3()
            poner(t)
            t.save(full, v2_version=3)
            return True
        if f.tags is None:
            f.add_tags()
        poner(f.tags)
        f.save()
        return True
    if ext in ('.m4a', '.mp4'):
        from mutagen.mp4 import MP4, MP4Cover
        f = MP4(full)
        if f.tags is None:
            f.add_tags()
        f.tags['covr'] = [MP4Cover(jpg, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
        return True
    return False


def aplicar(ruta, pedido, aj, reg, subidas):
    """Cover.jpg nueva + incrustada en cada pista + cover_path en music.db. Devuelve el resumen."""
    ruta = ruta.rstrip('/')
    datos, origen = _bytes_de(ruta, pedido, subidas)
    jpg, tam = a_jpeg(datos)
    reg(f'Carátula nueva ({origen}): {tam[0]}×{tam[1]} px · {len(jpg) // 1024} KB')
    cover = os.path.join(ruta, 'Cover.jpg')
    tmp = cover + '.nueva'
    with open(tmp, 'wb') as f:
        f.write(jpg)
    os.replace(tmp, cover)
    reg('  Cover.jpg reemplazada')
    hechas, saltadas, errores = 0, [], []
    for full in _audios(ruta):
        rel = os.path.relpath(full, ruta)
        try:
            if incrustar(full, jpg, tam):
                hechas += 1
            else:
                saltadas.append(rel)
        except Exception as e:
            errores.append(f'{rel}: {type(e).__name__}: {e}')
    reg(f'  Incrustada en {hechas} pista(s)')
    if saltadas:
        reg(f'  Sin carátula incrustada (formato sin soporte): {", ".join(saltadas[:5])}')
    for e in errores[:10]:
        reg(f'  ⚠ {e}')
    db = aj.get('db_orbyte')
    if db and os.path.isfile(db):
        con = sqlite3.connect(db, timeout=60)
        try:
            with con:
                n = con.execute('UPDATE albums SET cover_path=? WHERE (path=? OR path=?) AND '
                                "COALESCE(cover_path, '') != ?", (cover, ruta, ruta + '/', cover)).rowcount
            if n:
                reg('  Orbyte apunta ahora a Cover.jpg')
        finally:
            con.close()
    if pedido.get('tipo') == 'subida':
        try:
            os.remove(os.path.join(subidas, os.path.basename(pedido['subida'])))
        except OSError:
            pass
    if errores:
        raise FalloPortada(f'Cover.jpg quedó, pero {len(errores)} pista(s) no aceptaron la carátula incrustada')
    return {'incrustadas': hechas, 'saltadas': len(saltadas)}
