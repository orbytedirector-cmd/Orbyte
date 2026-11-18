"""
Orbyte Ingesta — historial de cambios de tags (pedido de Niko, 17-11-2026: «para tener una mejor comprensión de lo
que se hizo en cada ítem con la autocorrección debería mostrar qué se ajusta en cada ítem… después en las
revisiones cuesta mucho saber el porqué o qué cambió»).

Lo que se guarda en la Bandeja o en la Biblioteca se compara con lo que había: cada campo que cambió queda con su
antes → después, quién lo propuso (Autocorregir y su motivo, o a mano) y la fuente. Se anota en ingesta.db
(tabla historial, ver almacen.anotar) y se muestra en la ficha.
"""
import os

CAMPOS = {'artista': 'Artista', 'album': 'Álbum', 'anio': 'Año', 'genero': 'Género',
          'genero_secundario': 'Géneros secundarios', 'titulo': 'Título', 'numero': '#', 'disco': 'Disco'}
CAMPOS_ALBUM = ('artista', 'album', 'anio', 'genero', 'genero_secundario')
CAMPOS_PISTA = ('titulo', 'artista', 'disco', 'numero')
MAX_CAMBIOS = 2000


def _txt(v):
    if isinstance(v, (list, tuple)):
        return '; '.join(str(x) for x in v if x)
    return '' if v is None else str(v).strip()


def _disco(v):
    try:
        return str(max(1, int(str(v).split('/')[0])))
    except (TypeError, ValueError):
        return '1'


def etiqueta(clave):
    """«CD1/03 - Song.flac» → «CD1/03 - Song» (lo que se ve en el historial para cada pista)."""
    return os.path.splitext(clave or '')[0]


def motivos_validos(d):
    """{«clave|campo»: {origen, motivo}} que manda la pantalla (de la propuesta de Autocorregir aplicada)."""
    out = {}
    for k, v in (d or {}).items() if isinstance(d, dict) else ():
        if isinstance(k, str) and '|' in k and isinstance(v, dict):
            out[k[:600]] = {'origen': 'autocorregir' if v.get('origen') == 'autocorregir' else 'a mano',
                            'motivo': str(v.get('motivo') or '')[:300]}
    return out


def _fila(pista, campo, antes, despues, motivos, clave_m, origen_defecto):
    m = motivos.get(clave_m) or {}
    return {'pista': pista, 'campo': campo, 'antes': antes, 'despues': despues,
            'origen': m.get('origen') or origen_defecto, 'motivo': m.get('motivo') or ''}


def de_biblioteca(actual, prop, motivos, origen_defecto='a mano'):
    """Diferencias entre ajustar.leer (lo que hay) y ajustar.propuesta (lo que queda)."""
    out = []
    ta0, ta = actual['tags_album'], prop['tags_album']
    for k in CAMPOS_ALBUM:
        if _txt(ta0.get(k)) != _txt(ta.get(k)):
            out.append(_fila(None, k, _txt(ta0.get(k)), _txt(ta.get(k)), motivos, f'|{k}', origen_defecto))
    for x, p in zip(actual['plan'], prop['pistas']):
        for k in CAMPOS_PISTA:
            a, d = (_disco(x.get(k)), _disco(p.get(k))) if k == 'disco' else (_txt(x.get(k)), _txt(p.get(k)))
            if a != d:
                out.append(_fila(etiqueta(x['clave']), k, a, d, motivos, f'{x["clave"]}|{k}', origen_defecto))
    return out[:MAX_CAMBIOS]


def de_bandeja(rep, cam_viejo, cam_nuevo, motivos):
    """Diferencias entre lo que la Bandeja tenía decidido y lo nuevo (los tags se escriben al ubicar)."""
    out = []
    ta = rep.get('tags_album') or {}
    tv, tn = (cam_viejo or {}).get('tags') or {}, (cam_nuevo or {}).get('tags') or {}
    for k in CAMPOS_ALBUM:
        a, d = _txt(tv.get(k) or ta.get(k)), _txt(tn.get(k) or ta.get(k))
        if a != d:
            out.append(_fila(None, k, a, d, motivos, f'|{k}', 'a mano'))
    pv, pn = (cam_viejo or {}).get('pistas') or {}, (cam_nuevo or {}).get('pistas') or {}
    for x in rep.get('plan') or []:
        ov, on = pv.get(x['clave']) or {}, pn.get(x['clave']) or {}
        for k in CAMPOS_PISTA:
            a, d = _txt(ov.get(k) or x.get(k)), _txt(on.get(k) or x.get(k))
            if k == 'disco':
                a, d = _disco(a), _disco(d)
            if a != d:
                out.append(_fila(etiqueta(x['clave']), k, a, d, motivos, f'{x["clave"]}|{k}', 'a mano'))
    return out[:MAX_CAMBIOS]


def resumen(cambios, donde):
    """«Tags (Biblioteca): 14 títulos, 8 discos, 3 números · Autocorregir 24, a mano 1»."""
    n = {}
    for c in cambios:
        n[c['campo']] = n.get(c['campo'], 0) + 1
    nombres = {'titulo': ('título', 'títulos'), 'artista': ('artista', 'artistas'), 'disco': ('disco', 'discos'),
               'numero': ('número', 'números'), 'album': ('álbum', 'álbum'), 'anio': ('año', 'año'),
               'genero': ('género', 'género'), 'genero_secundario': ('géneros secundarios', 'géneros secundarios')}
    partes = [f'{v} {nombres.get(k, (k, k))[0 if v == 1 else 1]}' for k, v in n.items()]
    auto = sum(1 for c in cambios if c['origen'] == 'autocorregir')
    quien = ', '.join(x for x in (f'Autocorregir {auto}' if auto else '',
                                  f'a mano {len(cambios) - auto}' if len(cambios) > auto else '') if x)
    return f'Tags ({donde}): {", ".join(partes)}' + (f' · {quien}' if quien else '')
