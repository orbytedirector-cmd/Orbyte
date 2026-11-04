"""
Prueba la descarga de UNA pista tal como la hace el rescate de Descargas (reintento en Tidal / pista del otro
servicio), en una carpeta temporal que se borra al terminar. No toca la entrada ni tu historial de tidal-dl-ng.

  python3 -m ingesta.probar_pista URL_DEL_ALBUM NUMERO_DE_PISTA [DISCO]
  python3 -m ingesta.probar_pista buscar 'ARTISTA ÁLBUM'        URLs del álbum en Tidal y Qobuz, con su resolución

Muestra qué calidad dice Tidal que entrega para esa pista, el código de salida y las últimas líneas de la
herramienta, y el formato del archivo que llegó.
"""
import json
import os
import re
import shutil
import sys
import tempfile

from . import tienda


def _resolucion(ruta):
    import subprocess
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'a:0', '-show_entries',
                        'stream=sample_rate,bits_per_raw_sample,bits_per_sample', '-of', 'json', ruta],
                       capture_output=True, text=True)
    try:
        st = json.loads(r.stdout)['streams'][0]
        bits = st.get('bits_per_raw_sample') or st.get('bits_per_sample') or '?'
        return f'{bits} bits · {st.get("sample_rate")} Hz'
    except Exception:  # noqa: BLE001
        return '?'


def buscar(texto):
    for s in ('tidal', 'qobuz'):
        try:
            if s == 'tidal':
                tienda.preparar_tidal()
            vs = tienda.cliente(s).buscar_albumes(texto, limite=8)
        except Exception as e:  # noqa: BLE001
            print(f'{s.capitalize()}: error {type(e).__name__}: {e}')
            continue
        print(f'{s.capitalize()}:')
        for v in vs:
            res = f'{v.get("bits") or "?"}/{v.get("sr") or "?"}' if s == 'qobuz' else \
                ('Hi-Res' if v.get('hires') else ('16/44100' if v.get('lossless') else 'con pérdida'))
            print(f'  {v["artista"]} — {v["titulo"]} ({v.get("anio") or "?"}) · {v.get("n_pistas")} pistas · {res}')
            print(f'      {v["url"]}')
    return 0


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == 'buscar':
        return buscar(' '.join(sys.argv[2:]))
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    url, numero = sys.argv[1], int(sys.argv[2])
    disco = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    servicio = 'qobuz' if 'qobuz' in url else 'tidal'
    m = re.search(r'album/(?:[^/]+/)?([0-9a-z]+)\s*/?$', url)
    if not m:
        print('No reconozco la URL del álbum')
        return 2
    album = m.group(1)
    if servicio == 'tidal':
        tienda.preparar_tidal()
    c = tienda.cliente(servicio)
    pista = next((p for p in c.pistas(album) if p.get('numero') == numero and (p.get('disco') or 1) == disco), None)
    if not pista:
        print(f'El álbum no tiene la pista {disco}-{numero}')
        return 1
    print(f'{servicio.capitalize()} · pista {disco}-{numero}: {pista["titulo"]} (id {pista["id"]})')
    if servicio == 'tidal':
        for q in ('HI_RES_LOSSLESS', 'LOSSLESS'):
            try:
                p = c._get(f'tracks/{pista["id"]}/playbackinfopostpaywall', audioquality=q, playbackmode='STREAM',
                           assetpresentation='FULL')
                print(f'  pedida {q:15} → Tidal entrega {p.get("audioQuality")} · {p.get("bitDepth") or "-"} bits · '
                      f'{p.get("sampleRate") or "-"} Hz · {p.get("manifestMimeType")}')
            except Exception as e:  # noqa: BLE001
                print(f'  pedida {q:9} → error {type(e).__name__}: {e}')
    tmp = tempfile.mkdtemp(prefix='probar-pista-')
    try:
        if servicio == 'tidal':
            rc, sal = tienda._bajar_tidal_a(f'https://tidal.com/browse/track/{pista["id"]}', os.path.join(tmp, 'audio'),
                                            os.path.join(tmp, 'casa'), letras=True)
        else:
            rc, sal = tienda._bajar_qobuz_a(f'https://open.qobuz.com/track/{pista["id"]}', os.path.join(tmp, 'audio'))
        print(f'Herramienta: código {rc}')
        for linea in [l for l in sal.splitlines() if l.strip() and '━' not in l][-12:]:
            print('  │ ' + linea.strip()[:200])
        audios = tienda._audios(os.path.join(tmp, 'audio'))
        if not audios:
            print('No llegó ningún archivo de audio')
            todo = [os.path.relpath(os.path.join(r, f), tmp) for r, _d, fs in os.walk(tmp) for f in fs]
            print('Archivos en la carpeta temporal: ' + (json.dumps(todo[:15], ensure_ascii=False) if todo else 'ninguno'))
        for a in audios:
            print(f'Llegó: {os.path.basename(a)} · {(tienda.codec(a) or "?").upper()} · {_resolucion(a)}')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
