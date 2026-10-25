"""
Orbyte Ingesta — auditoría de cómo está catalogada cada pista en music.db contra el archivo real.

Lee solo la cabecera de cada archivo (rápido, no decodifica) y compara:
  - DSD: la tasa real (DSD64/128/256/512) contra dsd_rate y el color del LED
  - PCM: frecuencia de muestreo, bits y canales contra lo guardado (el LED blanco/amarillo depende de eso)
  - tamaño del archivo (si cambió, el archivo fue reemplazado después de indexarlo)
  - archivos que ya no existen (esos se limpian en «Fantasmas»)
Las pistas MQA no se tocan: su frecuencia «real» viene del decodificador, no de la cabecera.

  venv/bin/python -m ingesta.auditar [--db music.db]            informe, no cambia nada
  venv/bin/python -m ingesta.auditar [--db music.db] --aplicar  corrige lo que el archivo deja claro (respalda antes)
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import dsd
from .artistas import respaldo


def nombre_dsd(fs):
    if not fs:
        return None
    return f'DSD{round(fs / (44100 if fs % 44100 == 0 else 48000))}'


def led(is_dsd, dsd_rate, sample_rate):
    """Mismo criterio que index_libraryV3.get_led_color (sin MQA: esas pistas no se tocan)."""
    if is_dsd:
        return {'DSD64': 'cyan', 'DSD128': 'cyan', 'DSD256': 'red', 'DSD512': 'magenta'}.get(dsd_rate)
    return 'yellow' if (sample_rate or 0) <= 48000 else 'white'


def leer(ruta):
    """Datos técnicos de la cabecera: (dict | None, error)."""
    try:
        tam = os.path.getsize(ruta)
    except OSError:
        return None, 'falta'
    ext = os.path.splitext(ruta)[1].lower()
    try:
        if ext in ('.dsf', '.dff'):
            d = dsd.info(ruta)
            return {'tam': tam, 'dsd': True, 'fs': d['fs'], 'canales': d['canales'], 'dst': d['dst']}, None
        import mutagen
        f = mutagen.File(ruta)
        if f is None or not getattr(f, 'info', None):
            return {'tam': tam}, None
        i = f.info
        return {'tam': tam, 'dsd': False, 'sr': getattr(i, 'sample_rate', None),
                'bits': getattr(i, 'bits_per_sample', None), 'canales': getattr(i, 'channels', None)}, None
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def auditar(db, aplicar=False, salida=print, trabajadores=4):
    con = sqlite3.connect(db, timeout=60)
    con.row_factory = sqlite3.Row
    pistas = con.execute('SELECT id, file_path, file_size, sample_rate, bit_depth, channels, is_dsd, dsd_rate, '
                         'is_mqa, led_color, compression_mode FROM tracks').fetchall()
    salida(f'Revisando {len(pistas)} pistas…')
    hallazgos = defaultdict(list)          # tipo → [(pista, detalle, cambios)]
    errores = Counter()

    def una(p):
        return p, *leer(p['file_path'])

    hechas = 0
    with ThreadPoolExecutor(max_workers=trabajadores) as ex:
        for p, real, err in ex.map(una, pistas):
            hechas += 1
            if hechas % 2000 == 0:
                salida(f'  {hechas}/{len(pistas)}')
            if err == 'falta':
                hallazgos['archivo que ya no existe'].append((p, '', {}))
                continue
            if err:
                errores[err.split(':')[0]] += 1
                hallazgos['no se pudo leer la cabecera'].append((p, err, {}))
                continue
            if real.get('tam') != p['file_size'] and p['file_size'] is not None:
                hallazgos['el archivo cambió desde que se indexó (tamaño distinto)'].append(
                    (p, f'{p["file_size"]} → {real["tam"]} bytes', {'file_size': real['tam']}))
            if real.get('dsd'):
                nombre = nombre_dsd(real['fs'])
                cambios = {}
                if (p['dsd_rate'] or '') != nombre:
                    cambios['dsd_rate'] = nombre
                if not p['is_dsd']:
                    cambios['is_dsd'] = 1
                color = led(True, nombre, None)
                # el LED solo se corrige si era el automático de la tasa mal leída; si es otro, lo curaste tú
                if color and p['led_color'] != color and not p['is_mqa'] \
                        and p['led_color'] in (None, '', led(True, p['dsd_rate'], None)):
                    cambios['led_color'] = color
                if cambios:
                    hallazgos['tasa DSD mal catalogada'].append(
                        (p, f'{p["dsd_rate"] or "—"} → {nombre}', cambios))
            elif real.get('sr') and not p['is_mqa'] and not p['is_dsd']:
                cambios, det = {}, []
                if p['sample_rate'] and real['sr'] != p['sample_rate']:
                    cambios['sample_rate'] = real['sr']
                    det.append(f'{p["sample_rate"]} → {real["sr"]} Hz')
                if real.get('bits') and p['compression_mode'] != 'Lossy' and p['bit_depth'] \
                        and int(p['bit_depth']) != real['bits']:
                    cambios['bit_depth'] = real['bits']
                    det.append(f'{p["bit_depth"]} → {real["bits"]} bits')
                if real.get('canales') and p['channels'] and p['channels'] != real['canales']:
                    cambios['channels'] = real['canales']
                    det.append(f'{p["channels"]} → {real["canales"]} canales')
                if cambios:
                    hallazgos['datos técnicos PCM distintos al archivo'].append((p, ', '.join(det), cambios))
                color = led(False, None, cambios.get('sample_rate') or p['sample_rate'])
                if p['led_color'] in ('yellow', 'white') and p['led_color'] != color:
                    # el indexador no reescribe led_color (campo curado a mano): solo se informa
                    hallazgos['LED distinto al que da la frecuencia (¿curado a mano?) — no se toca'].append(
                        (p, f'{p["led_color"]} con {(cambios.get("sample_rate") or p["sample_rate"]) / 1000:g} kHz '
                            f'{p["bit_depth"] or ""} bits'.strip(), {}))

    salida('')
    faltan = len(hallazgos.get('archivo que ya no existe', []))
    if pistas and faltan > max(50, len(pistas) * 0.2):
        con.close()
        raise SystemExit(f'Faltan {faltan} de {len(pistas)} archivos: ¿está montada la biblioteca? No se cambió nada.')
    if not hallazgos:
        salida('Todo coincide: cada pista está catalogada como dice su archivo.')
        con.close()
        return {}
    for tipo, lista in hallazgos.items():
        salida(f'■ {tipo}: {len(lista)}')
        por_carpeta = Counter(os.path.dirname(p['file_path']) for p, _, _ in lista)
        detalles = Counter(det for _, det, _ in lista if det)
        for d, n in detalles.most_common(4):
            salida(f'    {n} × {d}')
        for c, n in por_carpeta.most_common(6):
            salida(f'    {n:4d}  {c}')
        if len(por_carpeta) > 6:
            salida(f'    … y {len(por_carpeta) - 6} carpetas más')
    informe = os.path.join(os.path.dirname(os.path.abspath(db)), 'ingesta_datos', 'auditoria.tsv')
    try:
        os.makedirs(os.path.dirname(informe), exist_ok=True)
        with open(informe, 'w', encoding='utf-8') as f:
            f.write('tipo\tid\tarchivo\tdetalle\n')
            for tipo, lista in hallazgos.items():
                for p, det, _ in lista:
                    f.write(f'{tipo}\t{p["id"]}\t{p["file_path"]}\t{det}\n')
        salida(f'\nInforme completo: {informe}')
    except OSError:
        pass
    corregibles = [(p, c) for t, l in hallazgos.items() for p, _, c in l if c]
    if not aplicar:
        salida(f'\nNada se cambió. {len(corregibles)} pistas se pueden corregir con --aplicar '
               f'(los archivos que faltan se limpian en «Fantasmas»).')
        con.close()
        return {t: len(l) for t, l in hallazgos.items()}
    respaldo(db, salida)
    juntos = {}
    for p, c in corregibles:
        juntos.setdefault(p['id'], {}).update(c)
    with con:
        for pid, c in juntos.items():
            con.execute(f'UPDATE tracks SET {", ".join(k + "=?" for k in c)} WHERE id=?', (*c.values(), pid))
    salida(f'\nCorregidas {len(juntos)} pistas en music.db.')
    con.close()
    return {t: len(l) for t, l in hallazgos.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                 'music.db'))
    ap.add_argument('--aplicar', action='store_true')
    a = ap.parse_args()
    if not os.path.isfile(a.db):
        sys.exit(f'No existe {a.db}')
    auditar(a.db, a.aplicar)


if __name__ == '__main__':
    main()
