"""
Orbyte Ingesta — ajustar los tags de un álbum que YA está en la biblioteca.

  leer(ruta)                         lo que dicen hoy los archivos de la carpeta (para el editor y Autocorregir)
  aplicar(ruta, cambios, aj, reg)    escribe los tags, renombra pistas (y la carpeta si cambian álbum, año o
                                     artista) y actualiza music.db conservando el ID de cada pista y del álbum:
                                     escuchas, favoritos y playlists siguen apuntando a lo mismo.

Cada renombre se anota en music.db apenas se hace, así un corte a mitad de camino no deja la base
apuntando a archivos que ya no existen. Después corre la metadata (el indexador relee con --update).
"""
import os
import re
import sqlite3

from . import analisis, procesar

CALIDAD_RE = re.compile(r'\s*\[([^\]]+)\]\s*$')


class FalloAjuste(Exception):
    pass


def _audio(ruta):
    out = []
    for raiz, dirs, archivos in os.walk(ruta):
        dirs[:] = sorted(d for d in dirs if d != 'Extras' and not d.startswith('.'))
        for a in sorted(archivos):
            if os.path.splitext(a)[1].lower() in analisis.AUDIO_EXT and not a.startswith('.'):
                out.append(os.path.relpath(os.path.join(raiz, a), ruta))
    return out


def _secundarios(ruta):
    try:
        import mutagen
        f = mutagen.File(ruta)
        t = f.tags if f is not None else None
        if t is None:
            return []
        if hasattr(t, 'getall'):                                    # ID3 (DSF, MP3, AIFF)
            fr = t.getall('TXXX:genre_secondary')
            txt = ';'.join(str(x) for x in fr[0].text) if fr else ''
        else:
            v = t.get('GENRE_SECONDARY') or t.get('genre_secondary') or []
            txt = ';'.join(v) if isinstance(v, list) else str(v)
        return [g.strip() for g in txt.split(';') if g.strip()][:3]
    except Exception:
        return []


def _quitar_secundarios(ruta):
    import mutagen
    f = mutagen.File(ruta)
    if f is None or f.tags is None:
        return
    if hasattr(f.tags, 'delall'):
        f.tags.delall('TXXX:genre_secondary')
    elif 'GENRE_SECONDARY' in f.tags:
        del f.tags['GENRE_SECONDARY']
    f.save()


def _duracion(ruta):
    try:
        import mutagen
        f = mutagen.File(ruta)
        return round(f.info.length, 2) if f is not None and f.info else None
    except Exception:
        return None


def _mayoria(valores):
    vs = [v for v in valores if v]
    return max(set(vs), key=vs.count) if vs else None


def leer(ruta):
    """Tags actuales de la carpeta del álbum, con la misma forma que la revisión (tags_album + plan)."""
    if not os.path.isdir(ruta):
        raise FalloAjuste(f'No existe la carpeta {ruta}')
    plan = []
    for rel in _audio(ruta):
        full = os.path.join(ruta, rel)
        t = analisis.tags_mutagen(full)[0]
        plan.append({'clave': rel, 'destino': rel, 'ext': os.path.splitext(rel)[1].lower(),
                     'titulo': t.get('titulo') or '', 'artista': t.get('artista') or '',
                     'artista_album': t.get('artista_album') or '', 'album': t.get('album') or '',
                     'anio': t.get('anio'), 'genero': t.get('genero') or '',
                     'numero': t.get('pista'), 'disco': t.get('disco') or 1,
                     'total_discos': t.get('total_discos'), 'duracion_origen': _duracion(full)})
    if not plan:
        raise FalloAjuste('La carpeta no tiene archivos de audio')
    discos = {x['disco'] for x in plan}
    ta = {'artista': _mayoria([x['artista_album'] for x in plan]) or _mayoria([x['artista'] for x in plan]) or '',
          'album': _mayoria([x['album'] for x in plan]) or '',
          'anio': str(_mayoria([x['anio'] for x in plan]) or '') or None,
          'genero': _mayoria([x['genero'] for x in plan]) or '',
          'genero_secundario': _secundarios(os.path.join(ruta, plan[0]['clave'])),
          'total_discos': max([x['total_discos'] or 0 for x in plan] + [len(discos)])}
    return {'ruta': ruta, 'tags_album': ta, 'plan': plan, 'n_discos': len(discos)}


def nombre_pista(disco, numero, titulo, ext, n_discos):
    """Mismo criterio que al ubicar: «07 - Título.flac» o «2-07 - Título.flac» si hay varios discos."""
    try:
        n = int(str(numero).split('/')[0])
    except (TypeError, ValueError):
        n = 0
    num = f'{n:02d}' if n else '00'
    pref = f'{disco}-{num}' if n_discos > 1 else num
    return analisis.nombre_seguro(f'{pref} - {titulo or "Pista"}') + ext


def _renombrar(o, d):
    """Renombra aunque solo cambien mayúsculas (el disco compartido no las distingue)."""
    if o == d:
        return
    if os.path.exists(d) and o.lower() != d.lower():
        raise FalloAjuste(f'Ya existe {os.path.basename(d)}')
    tmp = f'{o}.ajustando'
    os.rename(o, tmp)
    os.rename(tmp, d)


def propuesta(actual, cambios):
    """Qué quedaría: tags del álbum, valores por pista y nombres nuevos (sin tocar nada)."""
    ta0 = actual['tags_album']
    ov = cambios.get('tags') or {}
    ovp = cambios.get('pistas') or {}
    ta = dict(ta0)
    for k in ('artista', 'album', 'genero'):
        if (ov.get(k) or '').strip():
            ta[k] = ov[k].strip()
    if ov.get('anio'):
        if not re.fullmatch(r'(1[89]|20)\d\d', str(ov['anio']).strip()):
            raise FalloAjuste(f'Año inválido: {ov["anio"]}')
        ta['anio'] = str(ov['anio']).strip()
    if 'genero_secundario' in ov:
        ta['genero_secundario'] = [g.strip() for g in re.split(r'\s*;\s*', ov['genero_secundario'] or '')
                                   if g.strip()][:3]
    pistas = []
    for x in actual['plan']:
        o = ovp.get(x['clave']) or {}
        p = dict(x)
        if (o.get('titulo') or '').strip():
            p['titulo'] = o['titulo'].strip()
        if (o.get('artista') or '').strip():
            p['artista'] = o['artista'].strip()
        elif p['artista'] == ta0.get('artista') and ta['artista'] != ta0.get('artista'):
            p['artista'] = ta['artista']               # la pista seguía al artista del álbum
        if str(o.get('numero') or '').strip():
            if not str(o['numero']).strip().isdigit():
                raise FalloAjuste(f'Número de pista inválido: {o["numero"]}')
            p['numero'] = int(str(o['numero']).strip())
        p['cambio'] = any(p.get(k) != x.get(k) for k in ('titulo', 'artista', 'numero'))
        nuevo = nombre_pista(p['disco'], p['numero'], p['titulo'], x['ext'], actual['n_discos'])
        p['nuevo'] = os.path.join(os.path.dirname(x['clave']), nuevo) if p['titulo'] != x['titulo'] \
            or p['numero'] != x['numero'] else x['clave']
        pistas.append(p)
    nums = [(p['disco'], p['numero']) for p in pistas if p['numero']]
    if len(set(nums)) != len(nums):
        raise FalloAjuste('Hay números de pista repetidos en el mismo disco')
    nuevos = [p['nuevo'].lower() for p in pistas]
    if len(set(nuevos)) != len(nuevos):
        raise FalloAjuste('Dos pistas quedarían con el mismo nombre de archivo (¿número repetido?)')
    album_cambia = any(ta.get(k) != ta0.get(k) for k in ('artista', 'album', 'genero', 'anio', 'genero_secundario'))
    return {'tags_album': ta, 'pistas': pistas, 'album_cambia': album_cambia,
            'carpeta_cambia': any(ta.get(k) != ta0.get(k) for k in ('artista', 'album', 'anio'))}


def _carpeta_nueva(ruta, ta, ta0, aj):
    """Nueva ruta de la carpeta del álbum (misma etiqueta de calidad «[DSD]», «[CD]»…)."""
    actual = os.path.basename(ruta.rstrip('/'))
    m = CALIDAD_RE.search(actual)
    calidad = m.group(1) if m else None
    base = f'{ta["album"]} ({ta["anio"]})' if ta.get('anio') else ta['album']
    nombre = analisis.nombre_seguro(f'{base} [{calidad}]' if calidad else base)
    padre = os.path.dirname(ruta.rstrip('/'))
    if analisis._norm(ta.get('artista')) != analisis._norm(ta0.get('artista')):
        raiz = aj.get('biblioteca') or '/mnt/musica'
        bib = None
        try:
            bib = analisis.biblioteca(aj.get('db_orbyte'), raiz)
        except Exception:
            pass
        existente = analisis.carpeta_artista_existente(bib, ta['artista'])
        letra, carpeta = existente or (analisis.letra_de(ta['artista']), analisis.nombre_seguro(ta['artista']))
        padre = os.path.join(raiz, letra, carpeta)
    return os.path.join(padre, nombre)


def aplicar(ruta, cambios, aj, reg):
    """Aplica los cambios. Devuelve la ruta final de la carpeta."""
    db = aj.get('db_orbyte')
    if not db or not os.path.isfile(db):
        raise FalloAjuste(f'No se encontró music.db ({db})')
    actual = leer(ruta)
    prop = propuesta(actual, cambios)
    ta, ta0 = prop['tags_album'], actual['tags_album']
    tocadas = [p for p in prop['pistas'] if p['cambio'] or prop['album_cambia']]
    if not tocadas and not prop['carpeta_cambia']:
        reg('No hay cambios que aplicar')
        return ruta

    con = sqlite3.connect(db, timeout=60)
    try:
        aid = con.execute('SELECT id FROM albums WHERE path=? OR path=?', (ruta, ruta.rstrip('/') + '/')).fetchone()
        if not aid:
            raise FalloAjuste('El álbum no está en music.db (corre primero la metadata)')
        aid = aid[0]

        # 1) tags, archivo por archivo (los que no cambian quedan intactos)
        reg(f'Escribiendo tags en {len(tocadas)} archivo(s)')
        total = max([p['numero'] or 0 for p in prop['pistas']] + [len(prop['pistas'])]) \
            if actual['n_discos'] == 1 else None
        base_ta = {'artista': ta['artista'], 'album': ta['album'], 'genero': ta['genero'] or None,
                   'total_discos': ta.get('total_discos') or actual['n_discos'],
                   'genero_secundario': ta.get('genero_secundario') or None}
        if ta.get('anio') != ta0.get('anio'):
            base_ta['fecha'] = str(ta['anio'])                       # si el año no cambia, la fecha queda como está
        for p in tocadas:
            x = {'titulo': p['titulo'], 'artista': p['artista'] or ta['artista'], 'numero': p['numero'],
                 'disco': p['disco'], 'total_pistas': total}
            procesar.escribir_tags(os.path.join(ruta, p['clave']), x, base_ta, {})
            if ta0.get('genero_secundario') and not ta.get('genero_secundario'):
                _quitar_secundarios(os.path.join(ruta, p['clave']))
            reg(f'  ✎ {p["clave"]}')

        # 2) nombres de archivo (cada renombre queda anotado en music.db al tiro)
        renombres = [p for p in prop['pistas'] if p['nuevo'] != p['clave']]
        for p in renombres:                              # primero a un nombre temporal: así dos pistas pueden
            o = os.path.join(ruta, p['clave'])           # intercambiar números sin pisarse
            t = f'{o}.ajustando'
            os.rename(o, t)
            with con:
                con.execute('UPDATE tracks SET file_path=? WHERE file_path=?', (t, o))
        try:
            for p in renombres:
                t, d = os.path.join(ruta, p['clave']) + '.ajustando', os.path.join(ruta, p['nuevo'])
                if os.path.exists(d):
                    raise FalloAjuste(f'Ya existe {p["nuevo"]}')
                os.rename(t, d)
                with con:
                    con.execute('UPDATE tracks SET file_path=? WHERE file_path=?', (d, t))
                reg(f'  ↻ {p["clave"]} → {p["nuevo"]}')
        except Exception:
            for p in renombres:                          # lo que quedó a medias vuelve a su nombre anterior
                o = os.path.join(ruta, p['clave'])
                if os.path.exists(o + '.ajustando') and not os.path.exists(o):
                    os.rename(o + '.ajustando', o)
                    with con:
                        con.execute('UPDATE tracks SET file_path=? WHERE file_path=?', (o, o + '.ajustando'))
            raise

        # 3) carpeta del álbum
        final = ruta
        if prop['carpeta_cambia']:
            nueva = _carpeta_nueva(ruta, ta, ta0, aj)
            if nueva != ruta:
                if os.path.exists(nueva) and nueva.lower() != ruta.lower():
                    raise FalloAjuste(f'Ya existe la carpeta {nueva}: los tags quedaron escritos, la carpeta no se movió')
                os.makedirs(os.path.dirname(nueva), exist_ok=True)
                _renombrar(ruta, nueva)
                n = len(ruta)
                with con:
                    for tid, fp in con.execute('SELECT id, file_path FROM tracks WHERE file_path LIKE ?',
                                               (ruta.replace('%', '_') + '/%',)).fetchall():
                        if fp.startswith(ruta + os.sep):
                            con.execute('UPDATE tracks SET file_path=? WHERE id=?', (nueva + fp[n:], tid))
                    for alid, ap, cp in con.execute('SELECT id, path, cover_path FROM albums WHERE path=? OR path LIKE ?',
                                                    (ruta, ruta.replace('%', '_') + '/%')).fetchall():
                        np_ = nueva + ap[n:] if ap == ruta or ap.startswith(ruta + os.sep) else ap
                        nc = nueva + cp[n:] if cp and cp.startswith(ruta + os.sep) else cp
                        con.execute('UPDATE albums SET path=?, cover_path=? WHERE id=?', (np_, nc, alid))
                reg(f'Carpeta: {os.path.basename(ruta)} → {os.path.relpath(nueva, os.path.dirname(os.path.dirname(ruta)))}')
                viejo_artista = os.path.dirname(ruta)
                try:
                    if not os.listdir(viejo_artista):
                        os.rmdir(viejo_artista)
                        reg(f'Carpeta vacía eliminada: {os.path.basename(viejo_artista)}')
                except OSError:
                    pass
                final = nueva

        # 4) el álbum en music.db (el indexador nunca le cambia el nombre a un álbum que ya existe)
        with con:
            if ta['album'] != ta0['album'] or ta.get('anio') != ta0.get('anio'):
                con.execute('UPDATE albums SET name=?, year=?, letter=? WHERE id=?',
                            (ta['album'], int(ta['anio']) if str(ta.get('anio') or '').isdigit() else None,
                             (ta['album'][:1] or '#').upper(), aid))
            if ta['artista'] != ta0['artista']:
                con.execute('INSERT OR IGNORE INTO artists (name, letter) VALUES (?, ?)',
                            (ta['artista'], analisis.letra_de(ta['artista'])))
                nuevo = con.execute('SELECT id FROM artists WHERE name=?', (ta['artista'],)).fetchone()[0]
                con.execute('UPDATE albums SET artist_id=? WHERE id=?', (nuevo, aid))
                reg(f'Artista del álbum: «{ta0["artista"]}» → «{ta["artista"]}»')
        reg(f'Listo: {len(tocadas)} archivo(s) con tags nuevos, {len(renombres)} renombrado(s)')
        return final
    finally:
        con.close()
