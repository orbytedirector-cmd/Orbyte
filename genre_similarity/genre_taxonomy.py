"""
Taxonomia de generos para el mapa de cercania de Infinite (Ticket 14).

Estructura: familia -> subfamilia -> [generos canonicos].
Cada genero de canonical_genres.txt aparece EXACTAMENTE una vez.

Esto es criterio editorial musical, no ciencia exacta -- el PO debe
revisarlo y corregir cualquier ubicacion que no le cierre antes de usarlo
en produccion. Casos ambiguos marcados con # AMBIGUO: explican la duda.

Distancia entre dos generos (ver genre_distance.py):
  - misma subfamilia         -> 0.8
  - misma familia, distinta subfamilia -> 0.4
  - familias distintas       -> 0.0
"""

GENRE_TAXONOMY = {
    "Rock": {
        "Clasico/Mainstream": [
            "Rock", "Classic Rock", "Hard Rock", "Rock & Roll", "Soft-Rock",
            "AOR", "Melodic Rock", "Rockabilly",
        ],
        "Alternative/Indie": [
            "Alternative", "Alternative-Rock", "Alternative-Indie", "Indie",
            "Indie-Rock", "Indie-Pop", "Indie-Folk",
        ],
        "Punk": [
            "Punk", "Punk-Rock", "Pop-Punk", "Post-Punk", "Proto-Punk",
            "Street Punk", "Hardcore", "Harcore-Punk", "D-Beat", "Screamo",
            "Post-Hardcore", "Emo", "Alternative-Punk",
        ],
        "Progressive/Art": [
            "Progressive Rock", "Art-Rock", "Symphonic",
        ],
        "Psychedelic": [
            "Psychedelic", "Psychedelic-Rock",
        ],
        "Grunge": [
            "Grunge", "Post-Grunge",
        ],
        "Glam": [
            "Glam", "Glam-Rock",
        ],
        "Gothic/Dark": [
            "Gothic", "Gothic-Rock", "Dark Wave", "New Wave", "New Romantic",
            "Depressive-Rock",
        ],
        "Blues/Country Rock": [
            "Blues-Rock", "Country-Rock", "Surf-Rock",
        ],
        "Regional Rock": [
            "British-Rock", "BritPop", "Rock Argentino", "Rock Chileno",
            "Rock-Latino", "Spanish-Rock", "Latin-Rock", "Celtic-Rock",
            "J-Rock", "Visual Kei",
        ],
        "Fusion Rock": [
            "Funk-Rock", "Folk-Rock", "Rock-Industrial",
        ],
        "Piano/Guitar Rock": [
            "Piano-Rock",
        ],
    },

    "Metal": {
        "Tradicional/Power/Speed": [
            "Heavy Metal", "Power Metal", "Speed Metal", "NWOBHM",
        ],
        "Death": [
            "Death Metal", "Melodic Death Metal", "Brutal Death Metal",
            "Technical Death Metal", "Progressive Death Metal",
            "Old School Death Metal", "DeathCore",
        ],
        "Black": [
            "Black Metal", "Melodic Black Metal",
        ],
        "Thrash": [
            "Thrash Metal", "Technical Thrash Metal", "Blackened Thrash Metal",
        ],
        "Doom/Sludge": [
            "Doom Metal", "Sludge", "Stoner-Rock", "Drone",
        ],
        "Groove/Industrial/Nu": [
            "Groove-Metal", "Nu-Metal", "Metal-Industrial", "Funk-Metal",
            "Industrial", "Alternative-Metal",
        ],
        "Progressive/Symphonic/Gothic": [
            "Progressive Metal", "Symphonic-Metal", "Gothic-Metal",
            "Avant-Garde Metal", "NeoClasical-Metal", "MetalCore",
            "Melodic Metal",
        ],
        "Glam/Hair": [
            "Glam-Metal", "Hair-Metal",
        ],
        "Folk/Viking/Pirate": [
            "Folk-Metal", "Viking Metal", "Pirate-Metal", "Metal Pajaro",
        ],
        "Regional Metal": [
            "Spanish-Metal",
        ],
        "Extremo/Otros": [
            "GrindCore", "Power-Violence", "RapCore",
        ],
        "General": [
            "Metal",
        ],
    },

    "Pop": {
        "Mainstream": [
            "Pop", "Pop-Rock", "Adult Contemporary", "Crooners", "Oldies",
            "Easy Listening",
        ],
        "Dance/Electro Pop": [
            "Dance-Pop", "Electro-Pop", "Synth-Pop", "New Age",
        ],
        "Alt/Indie Pop": [
            "Power Pop", "Opera-Pop", "Boys-Band",
        ],
        "Regional Pop": [
            "J-Pop", "K-Pop", "Latin-Pop", "Spanish-Pop",
        ],
        "Balada/Romantico": [
            "Balada", "Romantic",
        ],
    },

    "Electronica/Baile": {
        "Club/Dance": [
            "Dance", "Club", "Euro-Dance", "House", "Techno", "Trance",
            "Dubstep", "DanceHall", "Drum & Bass",
        ],
        "Ambient/Experimental": [
            "Ambient", "Dark Ambient", "Chill", "Lo-Fi", "Experimental",
        ],
        "Urban Electronic": [
            "Trip-Hop",
        ],
        "Disco": [
            "Disco",
        ],
        "General": [
            "Electronic",
        ],
    },

    "Hip-Hop/Rap": {
        "General": [
            "Hip-Hop", "Rap",
        ],
        "Subgeneros": [
            "Gangsta-Rap", "West Coast Rap", "G-Funk", "FreeStyle",
        ],
    },

    "Jazz": {
        "General": [
            "Jazz", "Acid-Jazz", "Smooth Jazz", "Vocal-Jazz", "Soul-Jazz",
            "Gypsy-Jazz", "Fusion", "Swing", "Lounge",
        ],
    },

    "Blues/Soul/Funk": {
        "Blues": [
            "Blues", "Blue-Eyed Soul",
        ],
        "Soul/Motown": [
            "Soul", "Motown", "Neo-Soul", "Gospel",
        ],
        "Funk": [
            "Funk", "Groove",
        ],
        "R&B": [
            "R&B",
        ],
    },

    "Country/Folk/Americana": {
        "Country": [
            "Country",
        ],
        "Folk": [
            "Folk", "Canta-Autor", "SongWriter", "Celtic",
        ],
        "Cantautor Latino": [
            "Nueva Trova", "Trova",
        ],
        "Otros": [
            "Guitar",
        ],
    },

    "Latin": {
        "Tropical/Baile": [
            "Salsa", "Cumbia", "Merengue", "Bachata", "Vallenato",
            "Reggaeton", "Latino-America",
        ],
        "Tradicional": [
            "Bolero", "Ranchera", "Mariachi", "Tango", "Flamenco",
            "Nueva Ola",
        ],
        "General": [
            "Latin",
        ],
    },

    "Reggae/Ska": {
        "General": [
            "Reggae", "Roots-Reggae", "Latin-Reggae", "SKA",
        ],
    },

    "Clasica/Soundtrack": {
        "Clasica": [
            "Classical", "Opera", "Medieval", "Instrumental",
        ],
        "Soundtrack/Score": [
            "Soundtrack", "OST", "Film-Score", "Bandas Sonoras", "Composer",
            "Anime",
        ],
    },

    "Mundo/Otras Tradiciones": {
        "General": [
            "World", "Bollywood",
        ],
    },

    "Regional/Escena Nacional": {
        # Tags de nacionalidad/region usados como genero -- funcionan mas
        # como "escena regional" que genero musical en si (distinto del
        # dato de nacionalidad del artista, que ya es su propia dimension
        # de score -- nivel 8).
        #
        # Subdividido en subfamilias reales (no una sola "General" plana)
        # para que el fallback generico de genre_distance.py (misma
        # subfamilia -> 0.8) de resultados sensatos en los pares que NO
        # tienen una excepcion explicita del PO -- ver
        # _REGIONAL_OVERRIDES en genre_distance.py para los casos
        # puntuales (ej. Sudamerica-España) que pisan este agrupamiento.
        "Sudamerica": [
            "Argentina", "Chile", "Colombia", "Venezuela", "Brasil", "Andina",
        ],
        "Europa": [
            "European", "French", "Irish", "Italian", "Polish", "Spain",
        ],
        "Asia": [
            "Japanese", "Kazakh", "Korean",
        ],
        "Caribe": [
            # A diferencia del viejo "Otros" (cajon sin relacion real que
            # se elimino), Cuba y Dominicana SI comparten herencia
            # caribeña/hispanohablante real -- agrupar tiene sentido.
            "Cuba", "Dominicana",
        ],
        "Oceania": [
            "Australian",
        ],
        # DEFAULT mio: Mexico y Canadian quedan cada uno en su propia
        # subfamilia (no un "Norteamerica" compartido) -- geograficamente
        # son Norteamerica los dos, pero no hay relacion musical/cultural
        # real entre ellos que justifique agruparlos (mismo error que ya
        # se corrigio con el viejo "Otros"). Mexico SI tiene puentes
        # explicitos por idioma (ver _MEXICO en genre_distance.py), pero
        # eso se maneja como excepcion puntual, no como agrupamiento con
        # Canadian.
        "Mexico": ["Mexico"],
        "Canadian": ["Canadian"],
        "African": ["African"],
    },

    "Otros/Formato": {
        "Vocal/Formato": [
            "Vocal", "Female Vocalist", "Piano", "Acoustic",
        ],
        "Comedia/Novedad": [
            "Comedy", "RickRoll", "Political",
        ],
        "Spoken/Audio": [
            "Audiolibros",
        ],
        "Tematica/Ocasion": [
            "Christmas",
        ],
        "Sin categoria clara": [
            "Varios", "Remix", "Beat", "CrossOver",
        ],
    },
}

