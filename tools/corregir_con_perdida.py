#!/usr/bin/env python3
"""
corregir_con_perdida.py — deja al día los MP3/AAC que ya están en music.db
==========================================================================
Corrige dos datos que el indexador v2.0 guardaba mal para archivos con pérdida
(el indexador v2.1 ya los guarda bien para lo nuevo):

  1. tracks.bitrate  → toma el bit_rate real del audio (ffprobe, stream) en vez
                       del del contenedor, que incluye la carátula embebida
                       (un MP3 320 figuraba como 321-343k).
  2. albums.primary_format → 'MP3' / 'AAC' cuando la mayoría de las pistas del
                       álbum son con pérdida (antes quedaba 'FLAC').

No toca led_color (el DAC sigue mostrando amarillo), ni track_meta, ni nada más.
Por defecto solo muestra lo que cambiaría; con --aplicar escribe.

USO:
  python3 tools/corregir_con_perdida.py                 # simulación + resumen
  python3 tools/corregir_con_perdida.py --aplicar       # escribe en music.db
  python3 tools/corregir_con_perdida.py --db /ruta/music.db --aplicar
"""
import argparse, json, os, sqlite3, subprocess, sys
from collections import Counter

LOSSY_CODECS = ('mp3', 'aac', 'vorbis', 'opus', 'wmav1', 'wmav2', 'mp2')
STD_KBPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
ETIQUETA = {'vorbis': 'OGG', 'wmav1': 'WMA', 'wmav2': 'WMA'}
LOSSY_SQL = ("(COALESCE(compression_mode,'')='Lossy' OR LOWER(COALESCE(codec,'')) IN ("
             + ','.join(f"'{c}'" for c in LOSSY_CODECS) + "))")


def bitrate_stream(path):
    try:
        r = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json',
                            '-show_streams', '-select_streams', 'a:0', path],
                           capture_output=True, text=True, timeout=30)
        st = (json.loads(r.stdout or '{}').get('streams') or [{}])[0]
        br = st.get('bit_rate')
        return int(float(br)) if br else None
    except Exception:
        return None


def kbps_nominal(bps, codec):
    if not bps:
        return None
    k = bps / 1000.0
    if codec == 'mp3' and k > 320:
        return 320
    best = min(STD_KBPS, key=lambda v: abs(v - k))
    return best if abs(best - k) / best <= 0.06 else int(round(k))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument('--db', default=os.path.join(base, 'music.db'))
    ap.add_argument('--aplicar', action='store_true', help='escribir los cambios (sin esto, solo simula)')
    a = ap.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f'No existe {a.db}')

    conn = sqlite3.connect(a.db, timeout=30)
    conn.row_factory = sqlite3.Row
    pistas = conn.execute(f'SELECT id, file_path, codec, bitrate, album_id FROM tracks WHERE {LOSSY_SQL}').fetchall()
    print(f'Pistas con pérdida en la BD: {len(pistas)}')

    cambios, sin_archivo, por_kbps = [], 0, Counter()
    for i, t in enumerate(pistas, 1):
        codec = (t['codec'] or '').lower()
        nuevo = None
        if t['file_path'] and os.path.exists(t['file_path']):
            nuevo = bitrate_stream(t['file_path'])
        else:
            sin_archivo += 1
        final = nuevo or t['bitrate']
        por_kbps[f"{ETIQUETA.get(codec, codec.upper())} {kbps_nominal(final, codec) or '?'}"] += 1
        if nuevo and nuevo != t['bitrate']:
            cambios.append((nuevo, t['id']))
        if i % 200 == 0:
            print(f'  {i}/{len(pistas)}...', flush=True)

    print(f'\nBitrate a corregir: {len(cambios)} pistas  (archivos no encontrados: {sin_archivo})')
    print('Distribución por calidad (como se verá en el distintivo):')
    for k, n in sorted(por_kbps.items(), key=lambda x: -x[1]):
        print(f'  {k:>12} kbps : {n}')

    # primary_format de álbumes con alguna pista con pérdida
    albumes = conn.execute(f'SELECT DISTINCT album_id FROM tracks WHERE {LOSSY_SQL}').fetchall()
    fmt_cambios = []
    for (aid,) in albumes:
        filas = conn.execute('SELECT is_mqa, is_dsd, compression_mode, codec FROM tracks WHERE album_id=?', (aid,)).fetchall()
        n = len(filas)
        lossy = [(f['codec'] or '').lower() for f in filas
                 if f['compression_mode'] == 'Lossy' or (f['codec'] or '').lower() in LOSSY_CODECS]
        if len(lossy) * 2 > n:
            c = max(set(lossy), key=lossy.count)
            nuevo = ETIQUETA.get(c, c.upper() or 'MP3')
            actual = conn.execute('SELECT primary_format FROM albums WHERE id=?', (aid,)).fetchone()[0]
            if actual != nuevo:
                fmt_cambios.append((nuevo, aid))
    print(f'\nÁlbumes cuyo formato pasa a MP3/AAC: {len(fmt_cambios)} (de {len(albumes)} con pistas con pérdida)')

    if not a.aplicar:
        print('\nSimulación: no se escribió nada. Repite con --aplicar para guardar.')
        return
    conn.executemany('UPDATE tracks SET bitrate=? WHERE id=?', cambios)
    conn.executemany('UPDATE albums SET primary_format=? WHERE id=?', fmt_cambios)
    conn.commit()
    print('\nListo: cambios guardados.')


if __name__ == '__main__':
    main()
