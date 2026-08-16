"""
Distancia/similitud entre generos, basada en GENRE_TAXONOMY (genre_taxonomy.py).

similarity(g1, g2) -> float en [0, 1]:
  - 0.8  si comparten subfamilia (ej. Power Metal / Speed Metal)
  - 0.4  si comparten familia pero no subfamilia (ej. Power Metal / Death Metal)
  - 0.0  si no comparten familia (ej. Power Metal / Bachata)

No se usa para genero identico (eso ya es el nivel 5 del score, con su
propio peso +25/+10 -- este mapa es especificamente para "genero
DISTINTO pero cercano").
"""
from .genre_taxonomy import GENRE_TAXONOMY

_GENRE_TO_PATH = {}
for _family, _subfams in GENRE_TAXONOMY.items():
    for _subfam, _genres in _subfams.items():
        for _g in _genres:
            _GENRE_TO_PATH[_g.lower()] = (_family, _subfam)


def genre_path(genre: str):
    """(familia, subfamilia) para un genero, o None si no esta en la taxonomia."""
    return _GENRE_TO_PATH.get(genre.strip().lower())


# ─────────────────────────────────────────────────────────────────────────
# Excepciones puntuales dentro de "Regional/Escena Nacional" -- el PO pidio
# relaciones especificas que rompen el modelo generico de arbol (ej.
# Sudamerica-España tiene un valor propio, distinto al resto de Europa).
# Se evaluan ANTES que el calculo generico por arbol; si no hay excepcion
# que aplique, se cae al modelo generico (0.8 misma subfamilia / 0.4
# misma familia / 0.0 distinta familia).
#
# Grupos definidos por el PO -- OJO, hay supuestos mios marcados con
# # DEFAULT (asumido por mi, no dicho explicitamente) donde el PO no fue
# explicito; revisar antes de confiar en produccion:
_SOUTH_AMERICA = {"argentina", "chile", "colombia", "venezuela", "brasil", "andina"}
_EUROPE = {"european", "french", "irish", "italian", "polish", "spain"}
_ASIA = {"japanese", "kazakh", "korean"}
_OCEANIA = {"australian"}
_ENGLISH_SPEAKING_EUROPE = {"irish"}  # subconjunto de _EUROPE, de habla inglesa
_MEXICO = {"mexico"}
# DEFAULT: Cuba/Dominicana no son geograficamente Sudamerica, las dejo
# fuera de _SOUTH_AMERICA (dentro de su propia subfamilia "Caribe" en la
# taxonomia, que ya les da 0.8 entre si por el arbol generico -- no hace
# falta una excepcion puntual para eso).
# DEFAULT: Canadian sin puentes especiales, no pedido explicitamente.

_REGIONAL_OVERRIDES = [
    (_SOUTH_AMERICA, _SOUTH_AMERICA, 0.8),
    (_EUROPE, _EUROPE, 0.6),
    (_ASIA, _ASIA, 0.6),
    (_SOUTH_AMERICA, {"spain"}, 0.5),           # incluye Brasil-España (mismo valor que el resto de Sudamerica)
    (_SOUTH_AMERICA, _EUROPE - {"spain"}, 0.2),
    (_MEXICO, {"spain"}, 0.5),                  # mismo score que Sudamerica-España
    (_MEXICO, _SOUTH_AMERICA, 0.5),             # DEFAULT: mismo valor que el puente con España, por consistencia -- ajustar si el PO quiere otro numero
    (_OCEANIA, _ENGLISH_SPEAKING_EUROPE, 0.7),  # ir ANTES que la regla general de Europa (mas especifica)
    (_OCEANIA, _EUROPE, 0.5),
]


def _regional_override(g1_lower: str, g2_lower: str):
    for group_a, group_b, value in _REGIONAL_OVERRIDES:
        if (g1_lower in group_a and g2_lower in group_b) or \
           (g1_lower in group_b and g2_lower in group_a):
            return value
    return None


def genre_similarity(g1: str, g2: str) -> float:
    g1_lower, g2_lower = g1.strip().lower(), g2.strip().lower()
    if g1_lower == g2_lower:
        return 1.0  # no se usa en la practica (ver docstring), pero coherente

    override = _regional_override(g1_lower, g2_lower)
    if override is not None:
        return override

    path1 = genre_path(g1)
    path2 = genre_path(g2)
    if path1 is None or path2 is None:
        return 0.0  # genero no encontrado en la taxonomia -- conservador, no asumir cercania
    if path1 == path2:
        return 0.8  # misma subfamilia
    if path1[0] == path2[0]:
        return 0.4  # misma familia, distinta subfamilia
    return 0.0


if __name__ == "__main__":
    # Smoke test con pares que deberian salir bien Y mal, para confirmar
    # que el mapa realmente discrimina (no todo 0.4 parejo).
    test_pairs = [
        ("Power Metal", "Speed Metal", 0.8),       # misma subfamilia (Tradicional/Power/Speed)
        ("Heavy Metal", "Power Metal", 0.8),        # misma subfamilia
        ("Power Metal", "Death Metal", 0.4),        # misma familia (Metal), distinta subfamilia
        ("Death Metal", "Melodic Death Metal", 0.8),# misma subfamilia
        ("Power Metal", "Bachata", 0.0),            # familias totalmente distintas
        ("Salsa", "Cumbia", 0.8),                   # misma subfamilia (Tropical/Baile)
        ("Salsa", "Bolero", 0.4),                   # misma familia (Latin), distinta subfamilia
        ("Rock", "Jazz", 0.0),                      # familias distintas
        ("Folk-Metal", "Viking Metal", 0.8),        # misma subfamilia
        ("Alternative-Rock", "Death Metal", 0.0),   # Rock vs Metal, familias distintas
        # Excepciones regionales (Sudamerica/Europa/Asia, pedido explicito del PO)
        ("Argentina", "Chile", 0.8),                 # Sudamerica entre si
        ("Argentina", "Andina", 0.8),                # Sudamerica entre si
        ("French", "Italian", 0.6),                  # Europa entre si
        ("Japanese", "Korean", 0.6),                 # Asia entre si
        ("Argentina", "Spain", 0.5),                 # caso especial Sudamerica-España
        ("Argentina", "French", 0.2),                # Sudamerica vs resto de Europa
        ("Argentina", "Japanese", 0.4),               # sin excepcion -> cae al generico (misma familia)
        ("Boys-Band", "Glam-Rock", 0.0),              # Boys-Band ahora en Pop, Glam-Rock en Rock -> familias distintas
        ("Cuba", "Mexico", 0.4),                      # ninguno de los dos entra en Sudamerica -> generico, NO 0.8
        ("African", "Australian", 0.4),               # sin relacion real (African no es Oceania) -> generico
        ("Cuba", "Dominicana", 0.8),                   # Caribe entre si (grupo real, no cajon de sobrantes)
        ("Australian", "Irish", 0.7),                  # Oceania <-> Europa de habla inglesa
        ("Australian", "French", 0.5),                 # Oceania <-> resto de Europa (general)
        ("Australian", "Spain", 0.5),                  # Oceania <-> Europa (Spain no es habla inglesa, cae al general)
        ("Brasil", "Spain", 0.5),                      # Brasil recibe el mismo puente que el resto de Sudamerica
        ("Mexico", "Spain", 0.5),                      # mismo score que Sudamerica-España
        ("Mexico", "Argentina", 0.5),                  # puente Mexico-Sudamerica por idioma
        ("Mexico", "Canadian", 0.4),                   # sin relacion especial -> generico
    ]
    failed = 0
    for g1, g2, expected in test_pairs:
        got = genre_similarity(g1, g2)
        status = "OK" if got == expected else "FALLA"
        if got != expected:
            failed += 1
        print(f"[{status}] similarity({g1!r}, {g2!r}) = {got}  (esperado: {expected})")
    print(f"\n{'TODO OK' if failed == 0 else f'{failed} FALLAS'}")
