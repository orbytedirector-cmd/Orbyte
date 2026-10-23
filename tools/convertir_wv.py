#!/usr/bin/env python3
"""
convertir_wv.py — WavPack (.wv) → FLAC 24-bit, sin pérdida audible
====================================================================
Pensado para rips de vinilo en WavPack 32-bit float / 192 kHz que Orbyte
no indexa ni reproduce. FLAC no guarda coma flotante: se pasa a 24-bit
entero (144 dB de rango dinámico, más que cualquier DAC) a la misma
frecuencia. El DAC iFi lo muestra en blanco (Hi-Res).

Por carpeta:
  - Si hay un .cue que apunta a los .wv (ej. una pista por cara del vinilo),
    se divide en pistas según el .cue, con título/artista/álbum/año/número.
  - Si no hay .cue, convierte cada .wv a un .flac con el mismo nombre y tags.
  - Revisa el pico de cada archivo: si el float pasa de 0 dBFS (pasaría a
    saturar al convertir a entero), baja el volumen justo lo necesario.
  - La carátula (Folder.jpg/cover.jpg) se incrusta en cada FLAC.

No borra ni mueve los .wv originales. Por defecto solo muestra el plan;
con --aplicar convierte.

USO:
  python3 tools/convertir_wv.py "/mnt/musica/O/Ozzy Osbourne/DSD/Black Rain"
  python3 tools/convertir_wv.py "/mnt/musica/O/Ozzy Osbourne/DSD/Black Rain" --aplicar
  python3 tools/convertir_wv.py --buscar /mnt/musica          # lista carpetas con .wv
"""
import argparse, json, os, re, shlex, subprocess, sys
from pathlib import Path

PORTADAS = ('folder.jpg', 'cover.jpg', 'front.jpg', 'folder.png', 'cover.png')


def ffprobe(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format',
                        '-show_streams', '-select_streams', 'a:0', str(path)],
                       capture_output=True, text=True)
    d = json.loads(r.stdout or '{}')
    st = (d.get('streams') or [{}])[0]
    fmt = d.get('format') or {}
    tags = {k.lower(): v for k, v in (fmt.get('tags') or {}).items()}
    return {'sr': int(st.get('sample_rate') or 0), 'fmt': st.get('sample_fmt'),
            'dur': float(fmt.get('duration') or 0), 'tags': tags}


def pico_db(path, inicio=None, duracion=None):
    """Pico máximo en dBFS (0 = tope de un archivo entero)."""
    cmd = ['ffmpeg', '-hide_banner', '-nostdin']
    if inicio is not None:
        cmd += ['-ss', f'{inicio:.6f}']
    cmd += ['-i', str(path)]
    if duracion is not None:
        cmd += ['-t', f'{duracion:.6f}']
    cmd += ['-map', '0:a', '-af', 'astats=measure_perchannel=none', '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = re.findall(r'Peak level dB:\s*(-?[\d.]+|-inf)', r.stderr)
    return float(m[-1]) if m and m[-1] != '-inf' else -120.0


def leer_cue(cue):
    raw = cue.read_bytes()
    for enc in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            texto = raw.decode(enc); break
        except UnicodeDecodeError:
            continue
    album = {'title': '', 'performer': '', 'date': '', 'genre': ''}
    pistas, archivo, actual = [], None, None

    def valor(linea):
        m = re.match(r'\s*\S+\s+(?:\S+\s+)?"(.*)"\s*$', linea)
        if m: return m.group(1)
        return linea.split(None, 2)[-1].strip().strip('"')

    for linea in texto.splitlines():
        l = linea.strip()
        if not l: continue
        up = l.upper()
        if up.startswith('FILE '):
            m = re.match(r'FILE\s+"(.*)"\s+\S+$', l, re.I) or re.match(r'FILE\s+(\S+)\s+\S+$', l, re.I)
            archivo = m.group(1) if m else None
        elif up.startswith('TRACK '):
            actual = {'num': int(l.split()[1]), 'file': archivo, 'title': '', 'performer': '', 'start': None}
            pistas.append(actual)
        elif up.startswith('INDEX 01') and actual is not None:
            mm, ss, ff = (int(x) for x in l.split()[2].split(':'))
            actual['start'] = mm * 60 + ss + ff / 75.0
        elif up.startswith('TITLE '):
            (actual if actual is not None else album)['title'] = valor(l)
        elif up.startswith('PERFORMER '):
            (actual if actual is not None else album)['performer'] = valor(l)
        elif up.startswith('REM DATE '):
            album['date'] = valor(l)
        elif up.startswith('REM GENRE '):
            album['genre'] = valor(l)
    return album, [p for p in pistas if p['start'] is not None and p['file']]


def portada(carpeta):
    for f in carpeta.iterdir():
        if f.name.lower() in PORTADAS:
            return f
    return None


def comando_flac(origen, destino, tags, portada_img, ganancia_db, inicio=None, duracion=None):
    cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-n']
    if inicio is not None:
        cmd += ['-ss', f'{inicio:.6f}']
    cmd += ['-i', str(origen)]
    if duracion is not None:
        cmd += ['-t', f'{duracion:.6f}']
    if portada_img:
        cmd += ['-i', str(portada_img), '-map', '0:a', '-map', '1:v',
                '-c:v', 'copy', '-disposition:v', 'attached_pic',
                '-metadata:s:v', 'title=Cover', '-metadata:s:v', 'comment=Cover (front)']
    else:
        cmd += ['-map', '0:a']
    filtros = []
    if ganancia_db < 0:
        filtros.append(f'volume={ganancia_db:.2f}dB')
    if filtros:
        cmd += ['-af', ','.join(filtros)]
    cmd += ['-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', '24',
            '-compression_level', '8', '-map_metadata', '-1']
    for k, v in tags.items():
        if v:
            cmd += ['-metadata', f'{k}={v}']
    cmd += [str(destino)]
    return cmd


def nombre_seguro(s):
    return re.sub(r'[\\/:*?"<>|]', '-', s).strip() or 'Pista'


def plan_carpeta(carpeta):
    wvs = sorted(f for f in carpeta.iterdir() if f.suffix.lower() == '.wv')
    if not wvs:
        return []
    img = portada(carpeta)
    trabajos = []
    cues = sorted(f for f in carpeta.iterdir() if f.suffix.lower() == '.cue')
    usados = set()
    for cue in cues:
        album, pistas = leer_cue(cue)
        if not pistas:
            continue
        total = len(pistas)
        for i, p in enumerate(pistas):
            origen = carpeta / p['file']
            if not origen.exists():
                # el .cue a veces dice .wav/.flac aunque el archivo sea .wv
                alt = origen.with_suffix('.wv')
                origen = alt if alt.exists() else origen
            if origen.suffix.lower() != '.wv' or not origen.exists():
                continue
            usados.add(origen)
            sig = pistas[i + 1] if i + 1 < total else None
            dur = (sig['start'] - p['start']) if sig and sig['file'] == p['file'] else None
            artista = p['performer'] or album['performer']
            tags = {'title': p['title'], 'artist': artista, 'album_artist': album['performer'] or artista,
                    'album': album['title'], 'date': album['date'], 'genre': album['genre'],
                    'track': f"{p['num']}/{total}"}
            destino = carpeta / f"{p['num']:02d} - {nombre_seguro(p['title'])}.flac"
            trabajos.append({'origen': origen, 'destino': destino, 'inicio': p['start'],
                             'duracion': dur, 'tags': tags, 'portada': img, 'cue': cue.name})
    for wv in wvs:
        if wv in usados:
            continue
        info = ffprobe(wv)
        t = info['tags']
        tags = {k: t.get(k, '') for k in ('title', 'artist', 'album_artist', 'album', 'date', 'genre', 'track', 'disc')}
        if not tags['album_artist']:
            tags['album_artist'] = t.get('album artist', '')
        trabajos.append({'origen': wv, 'destino': wv.with_suffix('.flac'), 'inicio': None,
                         'duracion': None, 'tags': tags, 'portada': img, 'cue': None})
    return trabajos


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('carpeta', nargs='?')
    ap.add_argument('--buscar', metavar='RAIZ', help='lista carpetas con .wv bajo RAIZ')
    ap.add_argument('--aplicar', action='store_true', help='convertir (sin esto, solo muestra el plan)')
    a = ap.parse_args()

    if a.buscar:
        for raiz, _, files in os.walk(a.buscar):
            n = sum(1 for f in files if f.lower().endswith('.wv'))
            if n:
                print(f'{n:4d}  {raiz}')
        return
    if not a.carpeta:
        ap.error('indica una carpeta o usa --buscar')
    carpeta = Path(a.carpeta)
    trabajos = plan_carpeta(carpeta)
    if not trabajos:
        sys.exit(f'No hay .wv en {carpeta}')

    print(f'Carpeta: {carpeta}')
    print(f'Portada: {trabajos[0]["portada"].name if trabajos[0]["portada"] else "(ninguna)"}')
    if trabajos[0]['cue']:
        print(f'Se divide según: {trabajos[0]["cue"]}')
    print(f'{len(trabajos)} pistas FLAC 24-bit:\n')

    picos = {}
    for t in trabajos:
        clave = (t['origen'], t['inicio'], t['duracion'])
        picos[clave] = pico_db(t['origen'], t['inicio'], t['duracion'])
    pico_max = max(picos.values())
    # Misma ganancia para todo el álbum (no cambia el balance entre pistas)
    ganancia = -(pico_max + 0.1) if pico_max > -0.1 else 0.0

    for t in trabajos:
        dur = f"{t['duracion'] / 60:.1f} min" if t['duracion'] else 'hasta el final'
        desde = f" desde {int(t['inicio'] // 60)}:{t['inicio'] % 60:05.2f}" if t['inicio'] is not None else ''
        print(f"  {t['destino'].name}")
        print(f"      ← {t['origen'].name}{desde} ({dur}) · {t['tags'].get('artist') or '?'} / {t['tags'].get('album') or '?'}")
    print(f'\nPico máximo: {pico_max:+.2f} dBFS → ' +
          (f'se baja {-ganancia:.2f} dB para no saturar al pasar a entero' if ganancia else 'sin ajuste de volumen'))

    if not a.aplicar:
        print('\nSimulación: no se convirtió nada. Repite con --aplicar.')
        return
    ok = 0
    for t in trabajos:
        if t['destino'].exists():
            print(f'  ya existe, se omite: {t["destino"].name}')
            continue
        cmd = comando_flac(t['origen'], t['destino'], t['tags'], t['portada'], ganancia, t['inicio'], t['duracion'])
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            ok += 1
            print(f'  OK  {t["destino"].name}')
        else:
            print(f'  ERROR  {t["destino"].name}: {r.stderr.strip()[:300]}')
            print('         comando: ' + ' '.join(shlex.quote(c) for c in cmd))
    print(f'\nListo: {ok}/{len(trabajos)} convertidas. Los .wv originales no se tocaron.')


if __name__ == '__main__':
    main()
