"""
Orbyte — país de origen de los artistas (artists.nationality).

  PAISES          código ISO 3166-1 alfa-2 (minúsculas) → nombre en inglés, el que se guarda en la base
  COUNTRY_ISO     nombre → código (los de PAISES más los nombres viejos: ciudades, «England»…)
  resolver()      busca al artista en MusicBrainz y devuelve su país, verificado con los discos que tienes
  guardar()       lo escribe en music.db (respeta lo corregido a mano)

Por qué así: buscar solo por nombre confunde artistas que se llaman igual (Rhapsody italiano vs. el sueco).
Se identifica al artista por los IDs de MusicBrainz de los tags o por un disco tuyo que figure en su
discografía; recién si nada de eso calza se usa el nombre, y solo cuando hay un único artista con ese nombre.
Se guarda siempre el país (nunca la ciudad): si MusicBrainz trae una ciudad, se sube por sus áreas hasta el país.
"""
import os
import re
import unicodedata
from difflib import SequenceMatcher

PAISES = {
    'ad': 'Andorra', 'ae': 'United Arab Emirates', 'af': 'Afghanistan', 'ag': 'Antigua and Barbuda',
    'ai': 'Anguilla', 'al': 'Albania', 'am': 'Armenia', 'ao': 'Angola',
    'aq': 'Antarctica', 'ar': 'Argentina', 'as': 'American Samoa', 'at': 'Austria',
    'au': 'Australia', 'aw': 'Aruba', 'ax': 'Åland Islands', 'az': 'Azerbaijan',
    'ba': 'Bosnia and Herzegovina', 'bb': 'Barbados', 'bd': 'Bangladesh', 'be': 'Belgium',
    'bf': 'Burkina Faso', 'bg': 'Bulgaria', 'bh': 'Bahrain', 'bi': 'Burundi',
    'bj': 'Benin', 'bl': 'Saint Barthélemy', 'bm': 'Bermuda', 'bn': 'Brunei',
    'bo': 'Bolivia', 'bq': 'Caribbean Netherlands', 'br': 'Brazil', 'bs': 'Bahamas',
    'bt': 'Bhutan', 'bv': 'Bouvet Island', 'bw': 'Botswana', 'by': 'Belarus',
    'bz': 'Belize', 'ca': 'Canada', 'cc': 'Cocos (Keeling) Islands', 'cd': 'Democratic Republic of the Congo',
    'cf': 'Central African Republic', 'cg': 'Republic of the Congo', 'ch': 'Switzerland', 'ci': 'Ivory Coast',
    'ck': 'Cook Islands', 'cl': 'Chile', 'cm': 'Cameroon', 'cn': 'China',
    'co': 'Colombia', 'cr': 'Costa Rica', 'cu': 'Cuba', 'cv': 'Cape Verde',
    'cw': 'Curaçao', 'cx': 'Christmas Island', 'cy': 'Cyprus', 'cz': 'Czechia',
    'de': 'Germany', 'dj': 'Djibouti', 'dk': 'Denmark', 'dm': 'Dominica',
    'do': 'Dominican Republic', 'dz': 'Algeria', 'ec': 'Ecuador', 'ee': 'Estonia',
    'eg': 'Egypt', 'eh': 'Western Sahara', 'er': 'Eritrea', 'es': 'Spain',
    'et': 'Ethiopia', 'fi': 'Finland', 'fj': 'Fiji', 'fk': 'Falkland Islands',
    'fm': 'Micronesia', 'fo': 'Faroe Islands', 'fr': 'France', 'ga': 'Gabon',
    'gb': 'United Kingdom', 'gd': 'Grenada', 'ge': 'Georgia', 'gf': 'French Guiana',
    'gg': 'Guernsey', 'gh': 'Ghana', 'gi': 'Gibraltar', 'gl': 'Greenland',
    'gm': 'Gambia', 'gn': 'Guinea', 'gp': 'Guadeloupe', 'gq': 'Equatorial Guinea',
    'gr': 'Greece', 'gs': 'South Georgia and the South Sandwich Islands', 'gt': 'Guatemala', 'gu': 'Guam',
    'gw': 'Guinea-Bissau', 'gy': 'Guyana', 'hk': 'Hong Kong', 'hm': 'Heard Island and McDonald Islands',
    'hn': 'Honduras', 'hr': 'Croatia', 'ht': 'Haiti', 'hu': 'Hungary',
    'id': 'Indonesia', 'ie': 'Ireland', 'il': 'Israel', 'im': 'Isle of Man',
    'in': 'India', 'io': 'British Indian Ocean Territory', 'iq': 'Iraq', 'ir': 'Iran',
    'is': 'Iceland', 'it': 'Italy', 'je': 'Jersey', 'jm': 'Jamaica',
    'jo': 'Jordan', 'jp': 'Japan', 'ke': 'Kenya', 'kg': 'Kyrgyzstan',
    'kh': 'Cambodia', 'ki': 'Kiribati', 'km': 'Comoros', 'kn': 'Saint Kitts and Nevis',
    'kp': 'North Korea', 'kr': 'South Korea', 'kw': 'Kuwait', 'ky': 'Cayman Islands',
    'kz': 'Kazakhstan', 'la': 'Laos', 'lb': 'Lebanon', 'lc': 'Saint Lucia',
    'li': 'Liechtenstein', 'lk': 'Sri Lanka', 'lr': 'Liberia', 'ls': 'Lesotho',
    'lt': 'Lithuania', 'lu': 'Luxembourg', 'lv': 'Latvia', 'ly': 'Libya',
    'ma': 'Morocco', 'mc': 'Monaco', 'md': 'Moldova', 'me': 'Montenegro',
    'mf': 'Saint Martin', 'mg': 'Madagascar', 'mh': 'Marshall Islands', 'mk': 'North Macedonia',
    'ml': 'Mali', 'mm': 'Myanmar', 'mn': 'Mongolia', 'mo': 'Macao',
    'mp': 'Northern Mariana Islands', 'mq': 'Martinique', 'mr': 'Mauritania', 'ms': 'Montserrat',
    'mt': 'Malta', 'mu': 'Mauritius', 'mv': 'Maldives', 'mw': 'Malawi',
    'mx': 'Mexico', 'my': 'Malaysia', 'mz': 'Mozambique', 'na': 'Namibia',
    'nc': 'New Caledonia', 'ne': 'Niger', 'nf': 'Norfolk Island', 'ng': 'Nigeria',
    'ni': 'Nicaragua', 'nl': 'Netherlands', 'no': 'Norway', 'np': 'Nepal',
    'nr': 'Nauru', 'nu': 'Niue', 'nz': 'New Zealand', 'om': 'Oman',
    'pa': 'Panama', 'pe': 'Peru', 'pf': 'French Polynesia', 'pg': 'Papua New Guinea',
    'ph': 'Philippines', 'pk': 'Pakistan', 'pl': 'Poland', 'pm': 'Saint Pierre and Miquelon',
    'pn': 'Pitcairn Islands', 'pr': 'Puerto Rico', 'ps': 'Palestine', 'pt': 'Portugal',
    'pw': 'Palau', 'py': 'Paraguay', 'qa': 'Qatar', 're': 'Réunion',
    'ro': 'Romania', 'rs': 'Serbia', 'ru': 'Russia', 'rw': 'Rwanda',
    'sa': 'Saudi Arabia', 'sb': 'Solomon Islands', 'sc': 'Seychelles', 'sd': 'Sudan',
    'se': 'Sweden', 'sg': 'Singapore', 'sh': 'Saint Helena', 'si': 'Slovenia',
    'sj': 'Svalbard and Jan Mayen', 'sk': 'Slovakia', 'sl': 'Sierra Leone', 'sm': 'San Marino',
    'sn': 'Senegal', 'so': 'Somalia', 'sr': 'Suriname', 'ss': 'South Sudan',
    'st': 'Sao Tome and Principe', 'sv': 'El Salvador', 'sx': 'Sint Maarten', 'sy': 'Syria',
    'sz': 'Eswatini', 'tc': 'Turks and Caicos Islands', 'td': 'Chad', 'tf': 'French Southern Territories',
    'tg': 'Togo', 'th': 'Thailand', 'tj': 'Tajikistan', 'tk': 'Tokelau',
    'tl': 'Timor-Leste', 'tm': 'Turkmenistan', 'tn': 'Tunisia', 'to': 'Tonga',
    'tr': 'Turkey', 'tt': 'Trinidad and Tobago', 'tv': 'Tuvalu', 'tw': 'Taiwan',
    'tz': 'Tanzania', 'ua': 'Ukraine', 'ug': 'Uganda', 'um': 'United States Minor Outlying Islands',
    'us': 'United States', 'uy': 'Uruguay', 'uz': 'Uzbekistan', 'va': 'Vatican City',
    'vc': 'Saint Vincent and the Grenadines', 've': 'Venezuela', 'vg': 'British Virgin Islands', 'vi': 'U.S. Virgin Islands',
    'vn': 'Vietnam', 'vu': 'Vanuatu', 'wf': 'Wallis and Futuna', 'ws': 'Samoa',
    'xk': 'Kosovo', 'ye': 'Yemen', 'yt': 'Mayotte', 'za': 'South Africa',
    'zm': 'Zambia', 'zw': 'Zimbabwe',}

# Nombres que ya hay en music.db de la versión anterior (guardaba la ciudad o la región). Solo para la bandera
# mientras no se recalculen; lo nuevo se guarda siempre con el nombre de PAISES.
ALIAS = {
    'England': 'gb', 'Scotland': 'gb', 'Wales': 'gb', 'Northern Ireland': 'gb',
    'Pontypridd': 'gb', 'Brighton': 'gb', 'London': 'gb',
    'Saarbrücken': 'de', 'Dortmund': 'de', 'Savona': 'it', 'Madrid': 'es', 'Asturias': 'es',
    'Arvika Municipality': 'se', 'Copenhagen': 'dk', 'Montréal': 'ca', 'Bali': 'id',
    'Boston': 'us', 'Miami': 'us', 'New York': 'us', 'San Francisco': 'us', 'Seattle': 'us',
    'Texas': 'us', 'Washington': 'us', 'Raleigh': 'us', 'Arlington': 'us', 'Volcano': 'us',
    'Russian Federation': 'ru', 'Korea, Republic of': 'kr', 'Czech Republic': 'cz', 'Macedonia': 'mk',
    'Turkey': 'tr', 'Türkiye': 'tr', 'Ivory Coast': 'ci', 'Côte d\'Ivoire': 'ci',
}

COUNTRY_ISO = {**ALIAS, **{v: k for k, v in PAISES.items()}}

SIN_PAIS = 'Unknown'
VARIOS_ARTISTAS = '89ad4ac3-39f7-470e-963a-56509c546377'
GENERICOS = re.compile(r'\b(greatest hits|best of|hits|collection|anthology|live|en vivo|unplugged|'
                       r'essential|singles|b-sides|rarities|demos?|box set|grandes [eé]xitos|lo mejor)\b', re.I)


def iso_de(nombre):
    return COUNTRY_ISO.get(nombre or '')


def nombre_de(codigo):
    return PAISES.get((codigo or '').lower())


# ─────────────────────────── normalización ───────────────────────────

def _norm(s):
    s = unicodedata.normalize('NFKD', s or '').encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'^(the|los|las|les|die|el|la)\s+', '', s)
    s = s.replace('&', ' and ')
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _parecido(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    return 1.0 if a == b else SequenceMatcher(None, a, b).ratio()


def _titulo_limpio(t):
    """'Dawn Of Victory [HR]' → 'Dawn Of Victory'; 'Power of the Dragonflame (Remastered 2011)' → '… Dragonflame'."""
    t = re.sub(r'\s*[\(\[\{][^\)\]\}]*[\)\]\}]', ' ', t or '')
    t = re.sub(r'\s+-\s+(remaster|deluxe|expanded|anniversary|special|bonus).*$', '', t, flags=re.I)
    return re.sub(r'\s+', ' ', t).strip()


def _esc(s):
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', s or '')


# ─────────────────────────── tags de MusicBrainz en los archivos ───────────────────────────

def _ids_de_archivo(ruta):
    """{'albumartist': [...], 'album': '...'} con los IDs de MusicBrainz del archivo (vacío si no hay o no se puede leer)."""
    try:
        import mutagen
    except ImportError:
        return {}
    try:
        f = mutagen.File(ruta)
    except Exception:
        return {}
    if f is None or f.tags is None:
        return {}
    buscados = {'albumartist': ('musicbrainz_albumartistid', 'MusicBrainz Album Artist Id'),
                'album': ('musicbrainz_albumid', 'MusicBrainz Album Id')}
    salida = {}
    tags = f.tags
    for clave, (vorbis, txxx) in buscados.items():
        valores = []
        try:
            if hasattr(tags, 'getall'):                                   # ID3 (MP3, DSF, AIFF, WAV)
                for fr in tags.getall('TXXX'):
                    if (fr.desc or '').lower() == txxx.lower():
                        valores += [str(x) for x in fr.text]
            elif hasattr(tags, 'get') and type(tags).__name__ == 'MP4Tags':
                for x in tags.get(f'----:com.apple.iTunes:{txxx}', []):
                    valores.append(bytes(x).decode('utf-8', 'ignore'))
            else:                                                         # Vorbis (FLAC, OGG) y APE
                for k in list(tags.keys()):
                    if str(k).lower() == vorbis:
                        v = tags[k]
                        valores += [str(x) for x in (v if isinstance(v, list) else [v])]
        except Exception:
            pass
        ids = []
        for v in valores:
            ids += [x.strip() for x in re.split(r'[;/,]', v) if re.fullmatch(r'[0-9a-f-]{36}', x.strip())]
        if ids:
            salida[clave] = ids if clave == 'albumartist' else ids[0]
    return salida


# ─────────────────────────── país de un artista de MusicBrainz ───────────────────────────

def _pais_de_area(mb, area, pasos=6):
    """Sube por las áreas («Seattle» → «Washington» → «United States») hasta una con código de país."""
    vistos = set()
    while area and pasos > 0:
        codigos = area.get('iso-3166-1-codes') or []
        if codigos:
            return codigos[0].lower()
        aid = area.get('id')
        if not aid or aid in vistos:
            return None
        vistos.add(aid)
        d = mb(f'area/{aid}', inc='area-rels') or {}
        codigos = d.get('iso-3166-1-codes') or []
        if codigos:
            return codigos[0].lower()
        padre = None
        for rel in d.get('relations') or []:
            if rel.get('type') == 'part of' and rel.get('direction') == 'backward' and rel.get('area'):
                padre = rel['area']
                break
        area = padre
        pasos -= 1
    return None


def pais_de_artista(mb, mbid):
    """(codigo|None, nombre del artista en MusicBrainz, desambiguación)."""
    d = mb(f'artist/{mbid}') or {}
    codigo = (d.get('country') or '').lower() or None
    if not codigo or codigo not in PAISES:
        codigo = _pais_de_area(mb, d.get('area')) or _pais_de_area(mb, d.get('begin-area'))
    if codigo and codigo not in PAISES:                     # XW (mundial), XE (Europa)…
        codigo = None
    return codigo, d.get('name'), d.get('disambiguation') or ''


# ─────────────────────────── identificar al artista ───────────────────────────

def _credito_calza(credito, nombre):
    """El crédito de MusicBrainz corresponde al artista de la biblioteca (por el nombre acreditado o el del artista)."""
    art = credito.get('artist') or {}
    candidatos = [credito.get('name'), art.get('name'), art.get('sort-name')]
    candidatos += [a.get('name') for a in art.get('aliases') or []]
    return max((_parecido(c, nombre) for c in candidatos if c), default=0) >= 0.9


def _por_tags(mb, nombre, albumes, registro):
    votos = {}
    consultas = 0
    for al in albumes:
        if not al.get('archivo'):
            continue
        ids = _ids_de_archivo(al['archivo'])
        artistas = [x for x in ids.get('albumartist', []) if x != VARIOS_ARTISTAS]
        if len(artistas) == 1:
            votos[artistas[0]] = votos.get(artistas[0], 0) + 1
            continue
        if ids.get('album') and consultas < 3:
            consultas += 1
            rel = mb(f'release/{ids["album"]}', inc='artist-credits') or {}
            for c in rel.get('artist-credit') or []:
                if isinstance(c, dict) and (c.get('artist') or {}).get('id') != VARIOS_ARTISTAS and _credito_calza(c, nombre):
                    votos[c['artist']['id']] = votos.get(c['artist']['id'], 0) + 1
                    break
        if votos and max(votos.values()) >= 2:
            break
    if votos:
        mbid = max(votos, key=votos.get)
        registro.append(f'tags: {votos[mbid]} disco(s) con el ID de MusicBrainz del artista')
        return mbid
    return None


def _por_discos(mb, nombre, albumes, registro):
    titulos = []
    for al in albumes:
        t = _titulo_limpio(al.get('titulo'))
        if t and _norm(t) not in {_norm(x) for x in titulos}:
            titulos.append(t)
    titulos.sort(key=lambda t: (bool(GENERICOS.search(t)), -len(t)))     # primero los títulos propios y largos
    votos, calzados = {}, {}
    for t in titulos[:4]:
        palabras = ' '.join(_esc(w) for w in t.split())
        q = f'releasegroup:({palabras}) AND artist:"{_esc(nombre)}"'
        res = mb('release-group/', query=q, limit=15) or {}
        en_este = set()
        for rg in res.get('release-groups') or []:
            if _parecido(rg.get('title'), t) < 0.85:
                continue
            for c in rg.get('artist-credit') or []:
                if isinstance(c, dict) and (c.get('artist') or {}).get('id') not in (None, VARIOS_ARTISTAS) \
                        and _credito_calza(c, nombre):
                    en_este.add(c['artist']['id'])
                    calzados.setdefault(c['artist']['id'], []).append(rg.get('title'))
        for a in en_este:
            votos[a] = votos.get(a, 0) + 1
        if len(en_este) == 1 and len(votos) == 1:
            break                                                       # un disco propio con un solo dueño: listo
    if not votos:
        return None
    orden = sorted(votos, key=votos.get, reverse=True)
    if len(orden) > 1 and votos[orden[0]] == votos[orden[1]]:
        registro.append('discos: más de un artista con ese nombre tiene tus discos')
        return None
    mbid = orden[0]
    registro.append('discos: ' + ', '.join(dict.fromkeys(calzados.get(mbid, [])))[:200])
    return mbid


def _por_nombre(mb, nombre, registro):
    res = mb('artist/', query=f'artist:"{_esc(nombre)}"', limit=10) or {}
    exactos = []
    for a in res.get('artists') or []:
        nombres = [a.get('name'), a.get('sort-name')] + [x.get('name') for x in a.get('aliases') or []]
        if any(_norm(n) == _norm(nombre) for n in nombres if n):
            exactos.append(a)
    if len(exactos) == 1:
        registro.append('nombre: único artista con ese nombre en MusicBrainz')
        return exactos[0]['id']
    if exactos:
        registro.append(f'nombre: {len(exactos)} artistas se llaman así en MusicBrainz y ninguno tiene tus discos')
    else:
        registro.append('nombre: no aparece en MusicBrainz')
    return None


def resolver(mb, nombre, albumes):
    """
    mb(ruta, **params) → dict  (cliente de la API de MusicBrainz, con su propia pausa entre consultas)
    albumes: [{'titulo': str, 'archivo': ruta de una pista o None}]
    → {'codigo', 'pais', 'mbid', 'mb_nombre', 'metodo': 'tags'|'discos'|'nombre'|None, 'detalle'}
    """
    registro = []
    mbid, metodo = None, None
    for paso, fn in (('tags', lambda: _por_tags(mb, nombre, albumes, registro)),
                     ('discos', lambda: _por_discos(mb, nombre, albumes, registro)),
                     ('nombre', lambda: _por_nombre(mb, nombre, registro))):
        mbid = fn()
        if mbid:
            metodo = paso
            break
    if not mbid:
        return {'codigo': None, 'pais': None, 'mbid': None, 'mb_nombre': None, 'metodo': None,
                'detalle': ' · '.join(registro)}
    codigo, mb_nombre, desamb = pais_de_artista(mb, mbid)
    if not codigo:
        registro.append('MusicBrainz no tiene el país de este artista')
    return {'codigo': codigo, 'pais': nombre_de(codigo), 'mbid': mbid,
            'mb_nombre': mb_nombre + (f' ({desamb})' if desamb else '') if mb_nombre else None,
            'metodo': metodo if codigo else None, 'detalle': ' · '.join(registro)}


# ─────────────────────────── music.db ───────────────────────────

COLUMNAS = (('nationality', 'TEXT'), ('country_code', 'TEXT'), ('mb_artist_id', 'TEXT'), ('nationality_source', 'TEXT'))


def asegurar_columnas(con):
    existentes = {r[1] for r in con.execute('PRAGMA table_info(artists)')}
    for col, tipo in COLUMNAS:
        if col not in existentes:
            con.execute(f'ALTER TABLE artists ADD COLUMN {col} {tipo} DEFAULT NULL')
    con.commit()


def albumes_de(con, artist_id, maximo=8):
    """Álbumes del artista en music.db con una pista de cada uno (para leer sus tags)."""
    filas = con.execute('SELECT id, name, year FROM albums WHERE artist_id=? ORDER BY year, name',
                        (artist_id,)).fetchall()
    salida = []
    for f in filas[:maximo]:
        pista = con.execute('SELECT file_path FROM tracks WHERE album_id=? ORDER BY disc_number, track_number LIMIT 1',
                            (f[0],)).fetchone()
        archivo = pista[0] if pista and pista[0] and os.path.isfile(pista[0]) else None
        salida.append({'titulo': f[1], 'anio': f[2], 'archivo': archivo})
    return salida


def guardar(con, artist_id, codigo, fuente, mbid=None):
    """Escribe el país. codigo None → «Unknown» (no se vuelve a buscar solo)."""
    pais = nombre_de(codigo) if codigo else None
    con.execute('UPDATE artists SET nationality=?, country_code=?, mb_artist_id=COALESCE(?, mb_artist_id), '
                'nationality_source=? WHERE id=?',
                (pais or SIN_PAIS, (codigo or '').lower() or None, mbid, fuente, artist_id))
