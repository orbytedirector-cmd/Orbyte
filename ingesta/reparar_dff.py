"""
Orbyte Ingesta — repara los .dff de la biblioteca que quedaron con un ID3 pegado al inicio
(lo hacía Ingesta antes del PR #84): cada uno pasa a .dsf con el mismo DSD bit a bit y sus mismos tags,
y en music.db la pista conserva su ID (escuchas, favoritos y playlists siguen apuntando a ella).

  python3 -m ingesta.reparar_dff CARPETA [--db music.db]            muestra qué haría
  python3 -m ingesta.reparar_dff CARPETA [--db music.db] --aplicar  lo hace (antes respalda music.db)
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

from . import dsd
from .artistas import respaldo


def candidatos(carpeta):
    for raiz, _, archivos in os.walk(carpeta):
        for a in sorted(archivos):
            if a.lower().endswith('.dff'):
                ruta = os.path.join(raiz, a)
                try:
                    d = dsd.info(ruta)
                except (dsd.ErrorDSD, OSError) as e:
                    print(f'  ? {ruta}: {e}')
                    continue
                if d.get('id3_al_inicio'):
                    yield ruta, d


def reparar(ruta, d, con):
    from mutagen.dsf import DSF
    from mutagen.id3 import ID3
    if d['dst']:
        return 'DFF comprimido (DST): no se puede pasar a DSF, se deja como está'
    nuevo = os.path.splitext(ruta)[0] + '.dsf'
    if os.path.exists(nuevo):
        return f'ya existe {os.path.basename(nuevo)}: no se toca'
    parte = nuevo + '.parte'
    try:
        dsd.a_dsf(ruta, parte)
        if dsd.md5(ruta) != dsd.md5(parte):
            raise dsd.ErrorDSD('el DSD de la copia no es idéntico')
        viejos = ID3(ruta)                     # el ID3 que estaba pegado al inicio del .dff
        f = DSF(parte)
        if f.tags is None:
            f.add_tags()
        for frame in viejos.values():
            f.tags.add(frame)
        f.save()
        os.replace(parte, nuevo)
    except Exception:
        if os.path.exists(parte):
            os.remove(parte)
        raise
    st = os.stat(nuevo)
    fecha = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    with con:
        n = con.execute("UPDATE tracks SET file_path=?, file_size=?, codec='dsd_lsbf_planar', file_modified_date=? "
                        "WHERE file_path=?", (nuevo, st.st_size, fecha, ruta)).rowcount
    os.remove(ruta)
    return None if n else 'listo, pero no estaba en music.db (se indexará la próxima vez)'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('carpeta')
    ap.add_argument('--db', default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                 'music.db'))
    ap.add_argument('--aplicar', action='store_true')
    a = ap.parse_args()
    lista = list(candidatos(a.carpeta))
    if not lista:
        print('No hay .dff con un ID3 pegado al inicio en esa carpeta.')
        return
    print(f'{len(lista)} archivo(s) .dff con ID3 al inicio:')
    for ruta, d in lista:
        print(f'  {os.path.relpath(ruta, a.carpeta)}  ({"DST" if d["dst"] else "DSD" + str(d["fs"] // 44100)})')
    if not a.aplicar:
        print('\nNada se cambió. Para repararlos agrega --aplicar')
        return
    respaldo(a.db, print)
    con = sqlite3.connect(a.db, timeout=60)
    malos = 0
    for ruta, d in lista:
        try:
            nota = reparar(ruta, d, con)
            print(f'  ✓ {os.path.basename(ruta)} → .dsf' + (f'  ({nota})' if nota else ''))
        except Exception as e:
            malos += 1
            print(f'  ✗ {os.path.basename(ruta)}: {type(e).__name__}: {e}  (el .dff queda intacto)')
    con.close()
    print(f'\nListo: {len(lista) - malos} reparados, {malos} con error.')
    sys.exit(1 if malos else 0)


if __name__ == '__main__':
    main()
