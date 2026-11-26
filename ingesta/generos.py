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


def separar(valor):
    """Un valor que puede traer varios géneros («Pop, Rock», «Hip-Hop/Rap», lista JSON) → [géneros] en orden, sin
    repetir y sin lo que no es género."""
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
        trozos = [c] if (k in ALIAS or k in QUITAR) else _SEPARA.split(c)
        for t in trozos:
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
