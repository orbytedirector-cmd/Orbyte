"""
Orbyte Ingesta — «Autocorregir» del editor de tags: propone (no aplica) una versión ordenada de los tags.

Fuentes, en orden:
  1. la edición de MusicBrainz identificada, si su listado calza con las pistas (cantidad y duraciones)
  2. reglas de limpieza:
     - títulos sin prefijo de numeración («1.1.», «01 -», «A1_», «B2.»), solo si la mayoría del álbum lo trae
     - álbum sin catálogo, código de barras ni etiquetas de formato/calidad («(MOVLP865, 887…)», «[DSD]», «_Stig_DSD»)
     - artista «Clash, The» → «The Clash», escrito como ya está en la biblioteca
     - mayúsculas solo si todo viene en MAYÚSCULAS o todo en minúsculas
     - género: uno principal (mismo criterio canónico que RichMeta) + hasta 3 secundarios
     - números de pista: correlativos si faltan o se repiten (un solo disco, numeración corrida)

Devuelve los cambios agrupados para que la pantalla los muestre (antes → después) y se puedan desmarcar por grupo.
"""
import re

from . import analisis

GRUPOS = {
    'titulos': 'Títulos de pista',
    'album': 'Nombre del álbum',
    'artista': 'Artista',
    'mayusculas': 'Mayúsculas',
    'genero': 'Género principal y secundarios',
    'numeros': 'Números de pista',
}

# mismo listado y orden que _CANONICAL_GENRES de tools/metadata/RichMetaDSD3.py
CANONICOS = [
    "Black Metal", "Death Metal", "Thrash Metal", "Power Metal", "Doom Metal",
    "Progressive Metal", "Symphonic Metal", "Folk Metal", "Groove Metal",
    "Speed Metal", "German Metal", "Heavy Metal", "Hair Metal", "Glam Metal",
    "Nu Metal", "Alternative Metal", "Metalcore", "Melodic Death Metal",
    "Punk Rock", "Hard Rock", "Classic Rock", "Progressive Rock", "Psychedelic Rock",
    "Alternative Rock", "Indie Rock", "Garage Rock", "Blues Rock", "Southern Rock",
    "Gothic Rock", "Post Rock", "Grunge", "New Wave", "Post-Punk",
    "Rock", "Blues", "Jazz", "Soul", "Funk", "Reggae", "Country", "Folk",
    "Classical", "Electronic", "Ambient", "Techno", "House", "Trance", "Drum and Bass",
    "Hip Hop", "Rap", "R&B", "Pop", "Disco",
]

# «01 - Título», «01. Título», «01.Título», «01_Título», «1-03. Título», «A1. Título», «A1 Título»; NO «4:30AM»,
# «0-GRAVITY», «7 -seven-», «7.5 Seconds» ni «99 Luftballons» (sin separador claro no se toca)
PREFIJO_RE = re.compile(r'^\s*(?:(?:[A-Ha-h]\d{1,2}|\d{1,2}[.\-]\d{1,3}|\d{1,3})'
                        r'(?:\s*[.)](?!\d)\s*|\s+[-–—]+\s+|\s*[-–—_]+\s+|_+)'
                        r'|[A-H]\d{1,2}\s+)(?=\S)')
FORMATO = r'(?:dsd\d*|dsf|dff|sacd|flac|wav|alac|mp3|aac|mqa|hi-?res|hr|cd|lp\d*|\d?lp|vinyl|vinilo|web|' \
          r'\d{2}\s*-?\s*bit|\d{2,3}(?:[.,]\d)?\s*khz|24-?\d+|16-?44|\d{4})'
GRUPO_FORMATO_RE = re.compile(r'\s*[\(\[]\s*(?:' + FORMATO + r')(?:[\s,/+_-]+(?:' + FORMATO + r'))*\s*[\)\]]', re.I)
COLA_SUBRAYADO_RE = re.compile(r'(?:_[^_]+)*_(?:dsd\d*|dsf|dff|flac|sacd|hr|mqa|lp|vinyl)(?:_[^_]+)*$', re.I)
CATALOGO_RE = re.compile(r'[A-Z]{2,6}[- ]?[A-Z]{0,2}\d{3,7}[A-Z]?|\d{8,14}')
INVERTIDO_RE = re.compile(r'^(.+?),\s*(The|Los|Las|El|La|Les|Le|Die|Der|Das|Il|I)$', re.I)
MENORES = {'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'in', 'of', 'on', 'or', 'the', 'to', 'vs', 'y', 'de',
           'del', 'la', 'el', 'los', 'las', 'en', 'con'}


# ─────────────────────────── reglas ───────────────────────────

def sin_prefijo(t):
    return PREFIJO_RE.sub('', t or '', count=1).strip()


def quitar_artista(t, artistas):
    """«Battle Beast - Unbroken» → «Unbroken» (si el título empieza con el artista y un guion)."""
    for a in artistas:
        if a and len(a) > 1:
            m = re.match(r'^\s*' + re.escape(a) + r'\s*[-–—:]\s*(?=\S)', t or '', re.I)
            if m:
                return t[m.end():].strip()
    return t


def limpiar_album(a, artista=None):
    s = a or ''
    if artista:                                     # «Sonic Youth - 1992 - Dirty» → «Dirty»
        s = re.sub(r'^\s*' + re.escape(artista) + r'\s*[-–—]\s*', '', s, flags=re.I)
    s = re.sub(r'^\s*(?:1[89]|20)\d\d\s*[-–—.]\s*(?=\S)', '', s)
    # grupos entre paréntesis/corchetes que son catálogos o códigos de barras: «(MOVLP865, 88725462121)»
    def catalogo(m):
        dentro = m.group(1)
        partes = [x.strip() for x in re.split(r'[,;/]', dentro) if x.strip()]
        # un grupo con catálogo o código de barras es información de la edición, no parte del nombre
        return '' if any(CATALOGO_RE.fullmatch(x.replace(' ', '')) or CATALOGO_RE.fullmatch(x)
                         for x in partes) else m.group(0)
    s = re.sub(r'\s*[\(\[]([^\)\]]*)[\)\]]', catalogo, s)
    s = GRUPO_FORMATO_RE.sub('', s)                 # «[DSD]», «(24bit-176.4kHz)», «(1980)», «(Vinyl)»
    s = COLA_SUBRAYADO_RE.sub('', s)                # «Iron Fist_Stig_DSD»
    s = re.sub(r'\s*[-–]\s*(?:cd|disc|disco)\s*\d+\s*$', '', s, flags=re.I)
    s = re.sub(r'\s{2,}', ' ', s).strip(' -_.,')
    return s or (a or '')


def derecho(nombre):
    """«Clash, The» → «The Clash»."""
    m = INVERTIDO_RE.match((nombre or '').strip())
    return f'{m.group(2).capitalize() if m.group(2).islower() else m.group(2)} {m.group(1)}' if m else nombre


def estilo(s):
    """'upper' / 'lower' si todo el texto viene así (con letras suficientes), o None."""
    s = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', s or '').strip()     # «evermore (Explicit)» → «evermore»
    letras = [c for c in s if c.isalpha()]
    if len(letras) < 3 or not any(c.isascii() for c in letras):
        return None
    return 'upper' if s.isupper() else 'lower' if s.islower() else None


def mayusculas(s, conservar=()):
    """Solo si todo viene en MAYÚSCULAS o todo en minúsculas (con letras suficientes como para decidir)."""
    letras = [c for c in s or '' if c.isalpha()]
    if len(letras) < 4 or not (s.isupper() or s.islower()) or (s.isupper() and len(s.split()) < 2):
        return s                                    # «ABBA», «HELP!»: una palabra en mayúsculas puede ser a propósito
    palabras = s.lower().split(' ')
    fuera = []
    for i, p in enumerate(palabras):
        if p.upper() in conservar:
            fuera.append(p.upper())             # el nombre del artista va como se escribe: «ABBA Gold»
        elif re.fullmatch(r'[ivxlcdm]+', p) and len(p) <= 4 and p not in ('i', 'mi', 'dim', 'mix', 'lid', 'did'):
            fuera.append(p.upper())             # números romanos: «Part II»
        elif i and p in MENORES and not fuera[-1].endswith((':', '-', '(')):
            fuera.append(p)
        else:
            fuera.append(re.sub(r"(^|[\(\[\"'¿¡/-])([a-záéíóúñü])", lambda m: m.group(1) + m.group(2).upper(), p))
    return ' '.join(fuera)


def generos(texto):
    """«rock, funk, reggae, jazz» → ('Rock', ['Funk', 'Reggae', 'Jazz'])."""
    partes, vistos = [], set()
    for g in re.split(r'\s*[,;/|]\s*', texto or ''):
        g = g.strip()
        if g and g.lower() not in vistos:
            vistos.add(g.lower())
            partes.append(g)
    if not partes:
        return None, []
    canon = {analisis._norm(c): c for c in CANONICOS}
    presentes = {analisis._norm(g): g for g in partes}
    primario = next((canon[n] for n in canon if n in presentes), None) or partes[0]
    bonito = lambda g: canon.get(analisis._norm(g)) or (g.title() if g.islower() or g.isupper() else g)
    secundarios = [bonito(g) for g in partes if analisis._norm(g) != analisis._norm(primario)][:3]
    return bonito(primario), secundarios


def como_en_biblioteca(nombre, bib):
    """El nombre del artista tal como ya está escrito en la biblioteca (si existe)."""
    if not bib or not nombre:
        return nombre
    c = (bib.get('nombres') or {}).get(analisis._norm(nombre))
    return c.most_common(1)[0][0] if c else nombre


# ─────────────────────────── MusicBrainz ───────────────────────────

def _duracion(x):
    if x.get('corte'):
        ini, fin = x['corte']
        return (fin - ini) if fin is not None else None
    return x.get('duracion_origen')


def titulos_mb(rep):
    """Títulos de la edición identificada, en el orden del plan, si el listado calza (o None)."""
    el = (rep.get('edicion_mb') or {}).get('elegida') or {}
    titulos = [t for d in el.get('titulos') or [] for t in d]
    durs = [t for d in el.get('duraciones') or [] for t in d]
    plan = rep.get('plan') or []
    if not titulos or len(titulos) != len(plan) or not all(titulos):
        return None, el
    comparables = [(x, d) for x, d in zip(plan, durs) if _duracion(x) and d]
    calzan = sum(1 for x, d in comparables if abs(_duracion(x) - d) <= 4)
    if comparables and calzan < 0.8 * len(comparables):
        return None, el
    return titulos, el


# ─────────────────────────── propuesta ───────────────────────────

def proponer(rep, bib=None):
    ta = dict(rep.get('tags_album') or {})
    ov = (rep.get('cambios') or {}).get('tags') or {}
    ovp = (rep.get('cambios') or {}).get('pistas') or {}
    plan = rep.get('plan') or []
    tags, pistas, usados = {}, {}, set()

    def cambio(destino, campo, antes, despues, grupo):
        if despues and (despues or '') != (antes or ''):
            destino[campo] = {'antes': antes or '', 'despues': despues, 'grupo': grupo}
            usados.add(grupo)

    # artista del álbum
    art = ov.get('artista') or ta.get('artista') or ''
    nuevo_art = como_en_biblioteca(derecho(art), bib)    # el artista no se pasa por mayúsculas: AC/DC, KISS…
    grupo_art = 'artista'
    cambio(tags, 'artista', art, nuevo_art, grupo_art)
    art_final = nuevo_art or art
    conservar = {w.upper() for w in art_final.split() if w.isupper() and len(w) > 1}

    # álbum
    alb = ov.get('album') or ta.get('album') or ''
    limpio = limpiar_album(alb, art_final)
    titulos, el = titulos_mb(rep)
    if el.get('titulo') and analisis._norm(limpiar_album(el['titulo'])) == analisis._norm(limpio):
        limpio = limpiar_album(el['titulo'])            # la grafía oficial de MusicBrainz
    cambio(tags, 'album', alb, limpio, 'album')
    if 'album' not in tags:
        cambio(tags, 'album', alb, mayusculas(alb, conservar), 'mayusculas')

    # género
    gen = ov.get('genero') or ta.get('genero') or ''
    sec_actual = ov.get('genero_secundario') or '; '.join(ta.get('genero_secundario') or [])
    if gen:
        primario, secundarios = generos(gen + ('; ' + sec_actual if sec_actual else ''))
        cambio(tags, 'genero', gen, primario, 'genero')
        cambio(tags, 'genero_secundario', sec_actual, '; '.join(secundarios), 'genero')

    # pistas
    # «folklore», «THE STARS WILL LIGHT THE WAY»: si el álbum se escribe igual, las mayúsculas son a propósito
    estilizado = bool(estilo(alb) or estilo(art))
    con_prefijo = sum(1 for x in plan if PREFIJO_RE.match((ovp.get(x['clave']) or {}).get('titulo') or x.get('titulo') or ''))
    quitar_prefijo = plan and con_prefijo * 2 >= len(plan)

    def _arts(x):
        o = ovp.get(x['clave']) or {}
        return [o.get('artista') or x.get('artista'), art]

    def _base(x):
        t = (ovp.get(x['clave']) or {}).get('titulo') or x.get('titulo') or ''
        return sin_prefijo(t) if quitar_prefijo else t
    con_artista = sum(1 for x in plan if quitar_artista(_base(x), _arts(x)) != _base(x))
    sacar_artista = plan and con_artista * 2 >= len(plan)       # «Artista - Título» en la mayoría de las pistas
    un_disco = len({x.get('disco') or 1 for x in plan}) == 1
    numeros = [(ovp.get(x['clave']) or {}).get('numero') or x.get('numero') for x in plan]
    renumerar = un_disco and (any(not n for n in numeros) or len(set(map(str, numeros))) != len(numeros)
                              or [int(n) for n in numeros if str(n).isdigit()] != list(range(1, len(plan) + 1)))
    for i, x in enumerate(plan):
        o = ovp.get(x['clave']) or {}
        c = {}
        tit = o.get('titulo') or x.get('titulo') or ''
        if titulos:
            cambio(c, 'titulo', tit, titulos[i], 'titulos')
        else:
            nuevo = sin_prefijo(tit) if quitar_prefijo else tit
            if sacar_artista:
                nuevo = quitar_artista(nuevo, _arts(x)) or nuevo
            if nuevo != tit:
                cambio(c, 'titulo', tit, nuevo if estilizado else mayusculas(nuevo, conservar), 'titulos')
            elif not estilizado:
                cambio(c, 'titulo', tit, mayusculas(tit, conservar), 'mayusculas')
        pa = o.get('artista') or x.get('artista') or ''
        if pa and pa == art:
            cambio(c, 'artista', pa, art_final, grupo_art)
        elif pa:
            cambio(c, 'artista', pa, como_en_biblioteca(derecho(pa), bib), 'artista')
        if renumerar and (x.get('accion') != 'cortar_dsd' and not x.get('corte')):
            cambio(c, 'numero', str(numeros[i] or ''), str(i + 1), 'numeros')
        if c:
            pistas[x['clave']] = c
    return {
        'fuente': (f'MusicBrainz: {el.get("titulo")} ({el.get("anio") or "?"}'
                   f'{", " + el["pais"] if el.get("pais") else ""}) + reglas') if titulos else 'reglas de limpieza',
        'tags': tags, 'pistas': pistas,
        'grupos': [{'id': g, 'texto': GRUPOS[g],
                    'n': sum(1 for v in tags.values() if v['grupo'] == g)
                    + sum(1 for c in pistas.values() for v in c.values() if v['grupo'] == g)}
                   for g in GRUPOS if g in usados],
    }
