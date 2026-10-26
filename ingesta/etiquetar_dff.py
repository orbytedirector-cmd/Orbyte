"""
Orbyte Ingesta — tags para los .dff (DST) de la biblioteca que quedaron sin ninguno.

Ingesta copiaba los DFF comprimidos con DST sin escribirles tags; Orbyte los muestra como «Unknown Artist».
Este comando les agrega el bloque «ID3 » estándar al final del archivo (el audio DST no se toca) con:
  1. lo que dejó la revisión de Ingesta para esa carpeta (artista, álbum, año, género, títulos), o
  2. si no hay revisión, lo que dicen las carpetas y los nombres: «Artista/Álbum (Año) [DSD]/01 - Título.dff».
Después deja cada álbum en la cola de metadata de Ingesta (RichMeta completo).

  venv/bin/python -m ingesta.etiquetar_dff [CARPETA]            muestra qué escribiría (no cambia nada)
  venv/bin/python -m ingesta.etiquetar_dff [CARPETA] --aplicar  lo escribe
CARPETA por defecto: la biblioteca configurada en Ingesta.
"""
import argparse
import os
import re
import sys

from . import dsd, procesar
from .almacen import EN_BIBLIOTECA, Almacen

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVO_RE = re.compile(r'^(?:(\d{1,2})-)?(\d{1,3})\s*-\s*(.+)$')
CARPETA_RE = re.compile(r'^(.*?)(?:\s*\((\d{4})\))?(?:\s*\[[^\]]*\])*\s*$')


def sin_tags(raiz):
    """{carpeta: [archivos .dff sin bloque ID3]}"""
    out = {}
    for d, dirs, archivos in os.walk(raiz):
        dirs[:] = sorted(x for x in dirs if not x.startswith('.'))
        for a in sorted(archivos):
            if not a.lower().endswith('.dff') or a.startswith('.'):
                continue
            ruta = os.path.join(d, a)
            try:
                i = dsd.info(ruta)
            except (dsd.ErrorDSD, OSError) as e:
                print(f'  ? {ruta}: {e}')
                continue
            if not i.get('id3') and not i.get('id3_al_inicio'):
                out.setdefault(d, []).append(a)
    return out


def propuesta(carpeta, archivos, almacen):
    """(tags del álbum, [(archivo, valores de pista)], fuente)"""
    aid = almacen.fila_de_ruta(carpeta)
    rep = ((almacen.album(aid) or {}).get('reporte') or {}) if aid else {}
    plan = {x.get('destino'): x for x in rep.get('plan') or []}
    padre = os.path.basename(os.path.dirname(carpeta))
    m = CARPETA_RE.match(os.path.basename(carpeta))
    ta = dict(rep.get('tags_album') or {})
    fuente = 'revisión de Ingesta' if ta.get('album') else 'nombres de carpeta y archivo'
    ta['artista'] = ta.get('artista') or padre
    if not ta.get('album'):
        ta['album'] = (m.group(1) if m else os.path.basename(carpeta)).strip()
    if not (ta.get('fecha') or ta.get('anio')) and m and m.group(2):
        ta['fecha'] = m.group(2)
    pistas = []
    for a in archivos:
        x = plan.get(a)
        if x:
            pistas.append((a, {'titulo': x.get('titulo'), 'artista': x.get('artista'), 'numero': x.get('numero'),
                               'disco': x.get('disco') or 1, 'total_pistas': x.get('total_pistas')}))
            continue
        mm = ARCHIVO_RE.match(os.path.splitext(a)[0])
        if mm:
            pistas.append((a, {'titulo': mm.group(3).strip(), 'numero': int(mm.group(2)),
                               'disco': int(mm.group(1) or 1)}))
        else:
            pistas.append((a, {'titulo': os.path.splitext(a)[0], 'disco': 1}))
    ta['total_discos'] = ta.get('total_discos') or max(p['disco'] for _, p in pistas)
    return ta, pistas, fuente


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('carpeta', nargs='?')
    ap.add_argument('--aplicar', action='store_true')
    a = ap.parse_args()
    almacen = Almacen(os.path.join(os.environ.get('INGESTA_DATOS') or os.path.join(BASE, 'ingesta_datos'), 'ingesta.db'))
    aj = almacen.ajustes()
    raiz = os.path.abspath(a.carpeta or aj.get('biblioteca') or '/mnt/musica')
    if not os.path.isdir(raiz):
        sys.exit(f'No existe {raiz} (¿disco montado?)')
    print(f'Buscando .dff sin tags en {raiz}…')
    grupos = sin_tags(raiz)
    if not grupos:
        print('Todos los .dff tienen tags.')
        return
    total = sum(len(v) for v in grupos.values())
    print(f'{total} archivo(s) en {len(grupos)} carpeta(s):\n')
    planes = []
    for carpeta, archivos in sorted(grupos.items()):
        ta, pistas, fuente = propuesta(carpeta, archivos, almacen)
        planes.append((carpeta, ta, pistas))
        anio = ta.get('fecha') or ta.get('anio') or '?'
        print(f'■ {ta["artista"]} — {ta["album"]} ({anio}) · {len(pistas)} pistas · desde {fuente}')
        print(f'    {os.path.relpath(carpeta, raiz)}')
        for arch, p in pistas[:3]:
            print(f'    {p.get("disco", 1)}-{p.get("numero") or "?"}  {p["titulo"]}')
        if len(pistas) > 3:
            print(f'    … y {len(pistas) - 3} más')
    if not a.aplicar:
        print('\nNada se cambió. Si se ve bien, repite con --aplicar')
        return
    print()
    malos = 0
    for carpeta, ta, pistas in planes:
        escritos = 0
        for arch, p in pistas:
            ruta = os.path.join(carpeta, arch)
            try:
                antes = dsd.info(ruta)
                procesar.escribir_tags(ruta, dict(p, artista=p.get('artista') or ta['artista']), ta, {})
                despues = dsd.info(ruta)
                if (antes['inicio'], antes['largo']) != (despues['inicio'], despues['largo']) or not despues.get('id3'):
                    raise dsd.ErrorDSD('la estructura del archivo no quedó como se esperaba')
                escritos += 1
            except Exception as e:
                malos += 1
                print(f'  ✗ {ruta}: {type(e).__name__}: {e}')
        aid = almacen.de_biblioteca(carpeta, ta['artista'], ta['album'], ta.get('anio'))
        fila = almacen.album(aid)
        en_cola = False
        if fila['estado'] in EN_BIBLIOTECA:
            import json
            cam = dict(fila.get('cambios') or {}, richmeta_completo=True)
            en_cola = almacen.cambiar_estado(aid, fila['estado'], 'por_metadata', progreso='En cola para la metadata',
                                             registro=None, cambios=json.dumps(cam, ensure_ascii=False))
        print(f'  ✓ {ta["artista"]} — {ta["album"]}: {escritos}/{len(pistas)} con tags'
              + (' · en cola para la metadata' if en_cola else ' · corre su metadata desde Ingesta cuando termine'))
    print(f'\nListo. {malos} con error.' if malos else '\nListo.')
    sys.exit(1 if malos else 0)


if __name__ == '__main__':
    main()
