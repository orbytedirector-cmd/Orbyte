"""Una sola forma para cada género (regla de Niko, 25-11-2026).

  · Cada palabra con mayúscula inicial; las palabras separadas por un espacio (nunca «-», «_», «.»):
    «heavy metal» y «power-metal» → «Heavy Metal», «Power Metal»; «Post-Punk», «J-pop» → «Post Punk», «J Pop».
  · Las siglas en mayúsculas: AOR, EDM, MPB, NWOBHM, R&B…
  · «and», «'n'» y «n» entre palabras → «&»: «Rock and Roll», «rock n roll» → «Rock & Roll»; «Drum and Bass» →
    «Drum & Bass».
  · Un valor con varios géneros («Pop, Rock, Metal», «Hip-Hop/Rap») se separa: el primero es el principal y el
    resto, secundarios.
  · Sinónimos y traducciones a una forma (ALIAS) y lo que no es género (artistas, décadas, países, etiquetas
    sueltas de Last.fm) fuera (QUITAR). Decidido con Niko sobre el informe de tools/generos.py.

Sin dependencias del resto de la ingesta: lo usan también tools/metadata (indexado y RichMeta) y tools/generos.py.

  normalizar(g)               un género → su forma, o None si no es un género
  separar(valor)              un valor (puede traer varios) → [géneros], sin repetir, en orden
  repartir(principal, secundarios)  → (principal, [secundarios]) con la regla aplicada
"""
import json
import re
import unicodedata

SIGLAS = {'aor': 'AOR', 'edm': 'EDM', 'idm': 'IDM', 'ebm': 'EBM', 'nwobhm': 'NWOBHM', 'nwoahm': 'NWOAHM',
          'mpb': 'MPB', 'ost': 'OST', 'uk': 'UK', 'us': 'US', 'usa': 'USA', 'dj': 'DJ', 'r&b': 'R&B', 'rnb': 'R&B',
          'ccm': 'CCM', 'nrg': 'NRG', 'tv': 'TV', 'asmr': 'ASMR', 'rac': 'RAC', 'nsbm': 'NSBM', 'ii': 'II'}

# sinónimos, traducciones y errores → la forma acordada (claves: ver _clave)
_ALIAS = {
    'Classical': ['Classique'],
    'Ballad': ['Balada', 'Baladas', 'Bal.'],
    'Bolero': ['Boleros'],
    'Romantic': ['Romantico', 'Romantica', 'Romántico', 'Romántica'],
    'Soundtrack': ['Bandas Sonoras', 'Banda Sonora', 'Film', 'Film Score', 'Séries TV', 'Series TV', 'OST',
                   'Original Soundtrack'],
    'World Music': ['World', 'Musiques du monde'],
    'Alternative & Indie': ['Alternativa & Indie', 'Alternativa e Indie', 'Alternatif et Indé',
                            'Alternative/Indie', 'Alternative Indie'],
    'Singer Songwriter': ['Songwriter', 'Cantautor', 'Canta Autor', 'Singer/Songwriter', 'Singer-Songwriter'],
    'Rock En Español': ['Spanish Rock', 'Rock en Espanol'],
    'Pop En Español': ['Spanish Pop', 'Pop en Espanol'],
    'Latin Pop': ['Pop Latino'],
    'Industrial Metal': ['Metal Industrial'],
    'Industrial Rock': ['Rock Industrial'],
    'Eurodance': ['Euro Dance', 'Euro-Dance'],
    'Boy Band': ['Boys Band', 'Boybands', 'Boyband', 'Boy Bands'],
    'Electronic': ['Electronica', 'Electrónica', 'El.'],
    'Progressive Rock': ['Prog Rock', 'Prog-Rock'],
    'Symphonic Power Metal': ['Sympho Power Metal'],
    'Rock': ['Rock General', 'Ro.'],
    'Pop': ['Pop Music'],
    'Latin': ['Latin Music'],
    'Lo Fi': ['Lo Fi Beats', 'Lo-Fi Beats', 'Lofi'],
    'R&B': ['Rhythm & Blues', 'Rhythm and Blues', 'Rhythm&Blues', 'RnB', 'R and B'],
    'Thrash Metal': ['Trash Metal'],
    'Hardcore Punk': ['Harcore Punk'],
    'Neoclassical Metal': ['Neo Classical Metal', 'Neoclasical Metal', 'Neo-Classical Metal'],
    'Funeral Doom Metal': ['Funeral Doom'],
    'Progressive Thrash Metal': ['Progressive Thrash'],
    'Female Voice': ['Female Vocalist', 'Female Vocalists', 'Female Vocals'],
    'Male Voice': ['Male Soul Vocalists', 'Male Vocalist', 'Male Vocalists'],
    'Soft Rock': ['SoftR.'],
    'Synth Pop': ['Synth-P.', 'Synthpop'],
    'Dance Pop': ['Dance-P.'],
    'Contemporary R&B': ['Co.R&B'],
    'Vocal': ['Voc.'],
    'Rock & Roll': ["Rock 'n' Roll", "Rock'n'Roll", 'Rock n Roll', 'Rock and Roll'],
    # 26-11-2026, sobre la lista final
    'Alternative Rock': ['Rock Alternativo'],
    'Andina': ['Musica Andina', 'Música Andina'],
    'Chill': ['Chillout', 'Chill Out'],
    'City Pop': ['Japanese City Pop'],
    'Comedy': ['Humor'],
    'Guitarist': ['Guitar'],
    # 26-11-2026: música de relajación y meditación, un solo género
    'Meditation & Relaxation': ['Meditation', 'Meditación', 'Meditation Music', 'Música para Meditar',
                                'Relaxation', 'Relajación', 'Relax', 'Relaxing', 'Relaxing Music', 'Relajante',
                                'Música Relajante', 'Musica para Relajarse', 'Relaxation & Meditation',
                                'Meditation/Relaxation', 'Relaxation/Meditation', 'Mindfulness', 'Zen', 'Spa',
                                'Sleep', 'Sleep Music', 'Música para Dormir', 'Yoga', 'Healing', 'Healing Music',
                                'Reiki', 'Chakra', 'Chakras'],
    # una palabra pegada ↔ la forma con espacio: gana la de espacio (como «K Pop», «Post Punk»)
    'K Pop': ['Kpop'], 'J Pop': ['Jpop'], 'J Rock': ['Jrock'], 'Electro Pop': ['Electropop'],
    'Hip Hop': ['Hiphop'], 'Trip Hop': ['Triphop'], 'Post Punk': ['Postpunk'],
    'Post Rock': ['Postrock'], 'Nu Metal': ['Numetal'], 'Synth Wave': ['Synthwave'],
    'Dark Wave': ['Darkwave'], 'Cold Wave': ['Coldwave'], 'Power Pop': ['Powerpop'],
}

# no son géneros: artistas, décadas, países y nacionalidades (Orbyte tiene sus propios campos) y etiquetas sueltas
_QUITAR = [
    # artistas
    'AC/DC', 'BTS', 'Devision', 'Javier Solis', 'Jose Jose', 'Jose Luis Perales', 'Juan Gabriel', 'Dyango',
    'Marc Anthony', 'Mariah Carey', 'Miriam Hernandez', 'Mijares', 'Nino Bravo', 'Shamanes del Asis',
    # décadas
    '40s', '50s', '60s', '70s', '80s', '90s', '00s', '2000s', '2010s', '10s', "50's", "60's", "70's", "80's",
    "90's", '1950s', '1960s', '1970s', '1980s', '1990s',
    # países, regiones y nacionalidades
    'Chile', 'Chilean', 'Chileno', 'Chilena', 'Argentina', 'Argentinian', 'Argentine', 'Japanese', 'Japan', 'Japon',
    'Asie', 'Asia', 'Spanish', 'Spain', 'Espana', 'Italian', 'Italy', 'Irish', 'Ireland', 'French', 'France',
    'Mexico', 'Mexican', 'Brazil', 'Brasil', 'Brazilian', 'Russian', 'Russia', 'USA', 'US', 'American', 'Korean',
    'Korea', 'Australian', 'Australia', 'Polish', 'Poland', 'Cuba', 'Cuban', 'Venezuela', 'Venezuelan', 'Kazakh',
    'Kazakhstan', 'Uruguay', 'Uruguayan', 'Danish', 'Denmark', 'Peruvian', 'Peru', 'Puerto Rico', 'England',
    'English', 'British', 'UK', 'Canadian', 'Canada', 'Czech', 'Colombia', 'Colombian', 'Dominicana',
    'Dominican', 'European', 'African', 'Latino America', 'Latinoamerica', 'Latin America', 'Mendoza', 'German',
    'Germany', 'Swedish', 'Sweden', 'Finnish', 'Finland', 'Norwegian', 'Norway', 'Dutch', 'Greek', 'Portuguese',
    'Scottish', 'Belgian', 'Austrian', 'Swiss', 'Icelandic', 'Turkish', 'Israeli', 'Chinese', 'Taiwanese',
    # etiquetas sueltas
    'My Top Songs', 'Rickroll', 'Spotify', 'Sexy', 'Cute', 'Calm', 'Favourite Men', 'Favorites', 'Favourites',
    'Composer', 'Political', 'Other', 'Others', 'Divers', 'Varios', 'Various', 'Remix', 'Hueveo Core',
    'Leiwand Barrio Latino', 'Leiwand Global Village', 'Melancholism', 'Seen Live', 'Albums I Own',
]

MENORES_A_AMP = {'and', "'n'", 'n', "n'", "'n"}
SUELTAS = {'&', 'De', 'Y', 'The', 'Of', 'En', 'El', 'La', 'Los', 'Las', 'A', 'E'}   # solas no son un género
_SEPARA = re.compile(r'\s*(?:[,;|]|/)\s*|\s+[-–—]\s+')     # «electronic - experimental»: guion con espacios
_SEPARA_FUERTE = re.compile(r'\s*[,;|]\s*|\s+[-–—]\s+')
_PALABRAS = re.compile(r"[\s\-_‐‑–—]+")


def _clave(g):
    """Misma clave = mismo género: sin tildes, minúsculas, «&»/«and»/«'n'» iguales, solo letras y números."""
    s = unicodedata.normalize('NFKD', str(g))
    s = ''.join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace('&', ' and ')
    s = re.sub(r"(?:(?<=\s)|^)'?n'?(?=\s|$)", ' and ', s)
    return re.sub(r'[^0-9a-z]+', '', s)


ALIAS = {_clave(v): destino for destino, vs in _ALIAS.items() for v in vs}
assert len(_ALIAS) == len(set(_ALIAS)), 'géneros repetidos en _ALIAS'   # (un dict literal pisa la clave repetida)
QUITAR = {_clave(q) for q in _QUITAR}
# las formas acordadas también se reconocen a sí mismas (p. ej. «R&B» antes de partir en palabras)
ALIAS.update({_clave(d): d for d in _ALIAS})


def _palabra(p):
    b = p.lower()
    if b in SIGLAS:
        return SIGLAS[b]
    if b in MENORES_A_AMP or b == '&':
        return '&'
    if "'" in b and not b.startswith("'"):           # «rock'n'roll» ya se resolvió por ALIAS; «o'connor» no aplica
        return b[:1].upper() + b[1:]
    return b[:1].upper() + b[1:]


def normalizar(g):
    """Un género → su forma según la regla, o None si no es un género (o queda vacío)."""
    if g is None:
        return None
    g = str(g).strip().strip('.').strip()
    if not g:
        return None
    k = _clave(g)
    if not k or k in QUITAR:
        return None
    if k in ALIAS:
        return ALIAS[k]
    # «R&B» y «&» sueltos se conservan como palabra; el resto, separado por espacios
    g = re.sub(r'\s*&\s*', ' & ', g)
    palabras = [p for p in _PALABRAS.split(g.replace('.', ' ')) if p]
    if not palabras:
        return None
    out = ' '.join(_palabra(p) for p in palabras)
    out = re.sub(r'\bR & B\b', 'R&B', out)
    if out in SUELTAS or out.strip('& ') == '':
        return None
    return out


def partes(valor):
    """Los géneros tal como están escritos (sin normalizar), en orden: «Pop, Rock», «Hip-Hop/Rap», lista JSON."""
    if valor is None:
        return []
    if isinstance(valor, (list, tuple)):
        crudos = [str(x) for x in valor]
    else:
        s = str(valor).strip()
        if s.startswith('['):
            try:
                crudos = [str(x) for x in json.loads(s)]
            except ValueError:
                crudos = [s]
        else:
            crudos = [s]
    out = []
    for c in crudos:
        c = c.replace('\x00', ';')
        # el valor completo puede ser un alias con «/» («Singer/Songwriter», «AC/DC»): se mira antes de partir
        k = _clave(c)
        if k in ALIAS or k in QUITAR:
            out.append(c.strip())
            continue
        for t in _SEPARA_FUERTE.split(c):             # «,», «;», «|», « - »; la «/» solo si no es un alias
            if not t or not t.strip():
                continue
            kt = _clave(t)
            trozos = [t] if (kt in ALIAS or kt in QUITAR) else t.split('/')
            out.extend(x.strip() for x in trozos if x and x.strip())
    return out


def separar(valor):
    """Un valor que puede traer varios géneros («Pop, Rock», «Hip-Hop/Rap», lista JSON) → [géneros] en orden, sin
    repetir y sin lo que no es género."""
    out = []
    for t in partes(valor):
        n = normalizar(t)
        if n and n not in out:
            out.append(n)
    return out


def repartir(principal, secundarios=None):
    """(principal, secundarios) crudos → (principal o None, [secundarios]) con la regla: del principal se queda el
    primer género y el resto pasa a los secundarios; sin repetir el principal."""
    p = separar(principal)
    s = p[1:] + separar(secundarios)
    pri = p[0] if p else (s[0] if s else None)
    sec = []
    for g in s:
        if g != pri and g not in sec:
            sec.append(g)
    return pri, sec


# ─────────────────────────── géneros escritos a mano (26-11-2026) ───────────────────────────
# Lo que escribes en «Editar tags» se valida contra los géneros que ya hay: un error de tipeo adopta el existente,
# lo que no existe y cumple la regla se crea. Lo editado a mano queda «fijado» en el archivo (FIJO_TAG) y la metadata
# (RichMeta) lo respeta; lo no fijado lo sigue decidiendo la metadata.

FIJO_TAG = 'ORBYTE_GENERO_FIJO'          # «principal», «secundarios» o «principal;secundarios»
FIJOS = ('principal', 'secundarios')


def fijo_de_texto(texto):
    return {x.strip().lower() for x in re.split(r'[;,\s]+', str(texto or '')) if x.strip().lower() in FIJOS}


def fijo_a_texto(fijo):
    return ';'.join(x for x in FIJOS if x in (fijo or ()))


def leer_fijo(ruta):
    """→ (fijo {'principal', 'secundarios'}, principal del archivo, [secundarios del archivo]). Nunca lanza."""
    try:
        import io
        import mutagen
        if ruta.lower().endswith('.dff'):
            from mutagen.id3 import ID3
            from . import dsd
            datos = dsd.id3_dff(ruta)
            t = ID3(io.BytesIO(datos)) if datos else None
        else:
            f = mutagen.File(ruta)
            t = f.tags if f is not None else None
        if t is None:
            return set(), None, []
        if hasattr(t, 'getall'):                                    # ID3 (MP3, DSF, AIFF, DFF)
            def txxx(desc):
                fr = t.getall(f'TXXX:{desc}')
                return ';'.join(str(x) for x in fr[0].text) if fr else ''
            fijo = fijo_de_texto(txxx(FIJO_TAG))
            tcon = t.getall('TCON')
            pri = str(tcon[0].text[0]) if tcon and tcon[0].text else None
            sec = txxx('genre_secondary')
        else:                                                       # Vorbis (FLAC, OGG)
            def vorbis(k):
                v = t.get(k) or t.get(k.lower()) or []
                return list(v) if isinstance(v, list) else [str(v)]
            fijo = fijo_de_texto(';'.join(vorbis(FIJO_TAG)))
            g = vorbis('GENRE')
            pri = g[0] if g else None
            sec = ';'.join(vorbis('GENRE_SECONDARY'))
        return fijo, pri, [x.strip() for x in sec.split(';') if x.strip()]
    except Exception:
        return set(), None, []


def _distancia(a, b):
    """Damerau-Levenshtein (transposición de dos letras vecinas = 1)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _sin_tildes(s):
    s = unicodedata.normalize('NFKD', s)
    return ''.join(ch for ch in s if not unicodedata.combining(ch)).lower()


def parecido(g, existentes):
    """El género existente del que «g» parece un error de tipeo, o None.
    Mismo género con otro espaciado («Deathmetal» → «Death Metal»), o las mismas palabras salvo una que difiere en
    una letra (palabras de 4 a 6 letras) o dos (7 o más): «Trhash Metal» → «Thrash Metal», «Relaxtion» →
    «Relaxation». No toca palabras cortas («J Rock» ≠ «K Rock») ni cambios de dos letras en palabras cortas
    («Folk Metal» ≠ «Funk Metal»)."""
    k = _clave(g)
    for e in existentes:
        if _clave(e) == k:
            return e
    pg = _sin_tildes(g).split()
    mejor = None
    for e in existentes:
        pe = _sin_tildes(e).split()
        if len(pe) != len(pg):
            continue
        dif = [(a, b) for a, b in zip(pg, pe) if a != b]
        if len(dif) != 1:
            continue
        a, b = dif[0]
        if min(len(a), len(b)) < 4:
            continue
        tope = 1 if max(len(a), len(b)) <= 6 else 2
        dist = _distancia(a, b)
        if dist <= tope and (mejor is None or dist < mejor[0]):
            mejor = (dist, e)
    return mejor[1] if mejor else None


def validar(valor, existentes, mantener=()):
    """Lo escrito a mano → ([géneros], [notas]). Cada nota: {escrito, resultado, tipo} con tipo
    «quitado» (no es un género: país, década, artista…), «corregido» (error de tipeo de uno que ya existe) o «nuevo»
    (no existe y cumple la regla: se crea). «mantener»: lo que pediste dejar como lo escribiste (no se corrige)."""
    existentes = set(existentes or ())
    existentes.update(_ALIAS)                              # las formas acordadas siempre existen
    guardar = {_clave(m) for m in (mantener or ()) if m}
    out, notas = [], []
    for crudo in partes(valor):
        n = normalizar(crudo)
        if not n:
            notas.append({'escrito': crudo, 'resultado': None, 'tipo': 'quitado'})
            continue
        if n not in existentes:
            cerca = None if (_clave(crudo) in guardar or _clave(n) in guardar) else parecido(n, existentes)
            if cerca:
                notas.append({'escrito': crudo, 'resultado': cerca, 'tipo': 'corregido'})
                n = cerca
            else:
                notas.append({'escrito': crudo, 'resultado': n, 'tipo': 'nuevo'})
        if n not in out:
            out.append(n)
    return out, notas
