"""
Orbyte Ingesta — pone al día la cabecera y Recientes de los álbumes ya ubicados.

Antes, al corregir los tags de un álbum ya ubicado («Editar tags» en la biblioteca), los archivos, la carpeta
y Orbyte quedaban bien, pero la página del álbum y Recientes seguían mostrando el artista/álbum/año de la
revisión original (p. ej. «Various Artists»). Este comando relee los tags de cada carpeta y lo corrige.

  venv/bin/python -m ingesta.refrescar_cabeceras             muestra qué cambiaría (no cambia nada)
  venv/bin/python -m ingesta.refrescar_cabeceras --aplicar   lo guarda
  --horas N   solo los ubicados en las últimas N horas (por defecto, todos)
"""
import argparse
import os
import sys
import time

from . import ajustar
from .almacen import EN_BIBLIOTECA, Almacen

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--aplicar', action='store_true')
    ap.add_argument('--horas', type=float)
    a = ap.parse_args()
    almacen = Almacen(os.path.join(os.environ.get('INGESTA_DATOS') or os.path.join(BASE, 'ingesta_datos'), 'ingesta.db'))
    desde = time.time() - a.horas * 3600 if a.horas else 0
    with almacen._con() as c:
        ids = [r[0] for r in c.execute(
            f"SELECT id FROM albumes WHERE estado IN ({','.join('?' * len(EN_BIBLIOTECA))}) "
            "AND COALESCE(ubicado_en, '') != '' AND COALESCE(ubicado, terminado, 0) >= ?", (*EN_BIBLIOTECA, desde))]
    n = 0
    for aid in ids:
        al = almacen.album(aid)
        ruta = al.get('ubicado_en')
        if not ruta or not os.path.isdir(ruta):
            continue
        try:
            campos = ajustar.cabecera(al.get('reporte'), ruta)
        except Exception as e:  # noqa: BLE001
            print(f'  ! {ruta}: {e}')
            continue
        antes = tuple(str(al.get(k) or '') for k in ('artista', 'album', 'anio'))
        despues = tuple(str(campos.get(k) or al.get(k) or '') for k in ('artista', 'album', 'anio'))
        if not campos or antes == despues:
            continue
        n += 1
        print(f'■ {antes[0]} — {antes[1]} ({antes[2] or "?"})\n  → {despues[0]} — {despues[1]} ({despues[2] or "?"})')
        if a.aplicar:
            almacen.actualizar(aid, **campos)
    if not n:
        print('Todo al día.')
    elif not a.aplicar:
        print(f'\n{n} álbum(es) por corregir. Repite con --aplicar para guardarlo.')
    else:
        print(f'\n{n} álbum(es) corregidos.')


if __name__ == '__main__':
    sys.exit(main())
