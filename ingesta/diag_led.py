"""Diagnóstico del LED MQA: una pista de muestra por cada combinación de LED / frecuencia del archivo / frecuencia
original que hay en music.db, con lo que se detecta en su audio (incluidos los códigos crudos de la firma MQA).
Sirve para comparar contra el LED que muestra el DAC al reproducir cada muestra.

Uso (desde la carpeta de Orbyte):
    python3 -m ingesta.diag_led music.db                      # muestras por grupo
    python3 -m ingesta.diag_led music.db 'Blues For Allah'    # además, las pistas cuya ruta contenga ese texto
Solo lee: no escribe tags ni toca la base.
"""
import os
import sqlite3
import sys

from . import mqa

POR_GRUPO = 2          # álbumes de muestra por combinación

_SQL_GRUPOS = """
WITH base AS (
  SELECT t.id, t.album_id, t.file_path, t.title, t.led_color, t.is_mqa, t.mqa_type, t.sample_rate, t.bit_depth,
         COALESCE(t.mqa_original_sample_rate, '') AS orig
  FROM tracks t
  WHERE (t.is_mqa = 1 OR t.led_color IN ('green', 'blue', 'magenta')) AND COALESCE(t.is_dsd, 0) = 0
    AND lower(t.file_path) LIKE '%.flac'
), una AS (                                   -- una pista por álbum y grupo
  SELECT *, ROW_NUMBER() OVER (PARTITION BY led_color, sample_rate, orig, is_mqa, album_id ORDER BY id) AS r
  FROM base
), alb AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY led_color, sample_rate, orig, is_mqa ORDER BY album_id) AS a,
         COUNT(*) OVER (PARTITION BY led_color, sample_rate, orig, is_mqa) AS n_albumes
  FROM una WHERE r = 1
)
SELECT * FROM alb WHERE a <= ? ORDER BY led_color, is_mqa DESC, n_albumes DESC, sample_rate, orig, a
"""

_SQL_BUSCAR = """
SELECT t.id, t.album_id, t.file_path, t.title, t.led_color, t.is_mqa, t.mqa_type, t.sample_rate, t.bit_depth,
       COALESCE(t.mqa_original_sample_rate, '') AS orig, 0 AS n_albumes
FROM tracks t WHERE t.file_path LIKE ? AND lower(t.file_path) LIKE '%.flac'
ORDER BY t.file_path LIMIT 3
"""


def _khz(v):
    try:
        return f'{int(v) / 1000:g}'
    except (TypeError, ValueError):
        return str(v or '—')


def fila(f):
    ruta = f['file_path']
    det = mqa.detectar(ruta) if os.path.isfile(ruta) else {'mqa': False, 'error': 'no está'}
    t = mqa.tags_mqa(ruta) or {}
    if det.get('error'):
        d = 'ERROR ' + det['error'][:40]
        regla = '—'
    elif det.get('mqa'):
        d = (f"{'Studio' if det['studio'] else 'MQA'} orig {_khz(det['sr_original'])} "
             f"proc={det['cod_procedencia']} cod_f={det['cod_frecuencia']} bit{det['bit']}")
        regla = mqa.led(True, det['studio'], f['sample_rate'], det['sr_original'])
    else:
        d = 'no es MQA'
        regla = mqa.led(False, False, f['sample_rate'], None)
    tags = ('MQA' + (' Studio' if t.get('studio') else '') + (f" orig {_khz(t['sr_original'])}" if t.get('sr_original') else '')) \
        if t.get('mqa') else 'sin MQA'
    return ' | '.join([
        f['led_color'] or '—', 'mqa' if f['is_mqa'] else 'no-mqa', f"{f['bit_depth'] or '?'}/{_khz(f['sample_rate'])}",
        f"orig {_khz(f['orig'])}" if f['orig'] else 'orig —', tags, d, f'regla→{regla}',
        os.path.relpath(ruta, '/mnt/musica') if ruta.startswith('/mnt/musica') else ruta,
    ])


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    con = sqlite3.connect(f'file:{argv[1]}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    print('LED Orbyte | marca | archivo | original (Orbyte) | tags | detectado en el audio | LED regla actual | pista')
    grupo = None
    for f in con.execute(_SQL_GRUPOS, (POR_GRUPO,)):
        g = (f['led_color'], f['is_mqa'], f['sample_rate'], f['orig'])
        if g != grupo:
            grupo = g
            print(f"\n# {f['led_color']} · {'mqa' if f['is_mqa'] else 'no-mqa'} · {_khz(f['sample_rate'])} → "
                  f"{_khz(f['orig']) if f['orig'] else '—'} kHz · {f['n_albumes']} álbumes")
        print(fila(f), flush=True)
    for texto in argv[2:]:
        print(f'\n# búsqueda: {texto}')
        for f in con.execute(_SQL_BUSCAR, (f'%{texto}%',)):
            print(fila(f), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
