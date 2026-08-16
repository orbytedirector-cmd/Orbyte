#!/usr/bin/env python3
"""
Normalizacion interactiva de generos/subgeneros en la base de Orbyte.

Por que existe: track_meta.genre_primary/genre_secondary y
album_meta.genre_primary/genre_secondary son TEXT libre, sin ningun
constraint ni tabla de lookup (ver schema.sql) -- el pipeline que tagea
la biblioteca escribe lo que sea que haya encontrado, asi que "rock",
"Rock" y " rock " conviven como si fueran generos distintos. Esto rompe
cualquier mapa de cercania entre generos que se arme despues (Ticket
14): "rock" y "Rock" quedarian como dos nodos separados en el grafo en
vez de uno solo.

Que NO resuelve este script (a proposito, alcance acotado a lo que se
pidio -- mayusculas/espacios/guiones): sinonimos con palabras
DISTINTAS, como "alternative rock" vs "Rock Alternativo" vs
"Alternativa & Indie". Esos no se detectan aca porque no son el mismo
texto normalizado, son conceptos escritos distinto. Esa decision queda
para una segunda pasada, a mano, una vez que la lista ya este mucho mas
corta despues de correr esto.

Dos fases, separadas a proposito (mismo criterio que se usa en todo el
proyecto: nunca aplicar un cambio de una sola pasada sin poder revisarlo
antes):

  FASE 1 (default, sin --apply):
    Lee la base (solo SELECT, no toca nada), agrupa los valores que
    normalizan igual (minusculas + trim + espacios/guiones colapsados),
    y para cada grupo con MAS DE UNA variante te pregunta interactivo
    cual dejar como forma canonica. Los grupos que ya tienen una sola
    variante se aceptan solos, sin preguntar (no tiene sentido
    interrumpirte por algo que ya esta bien). Guarda cada respuesta al
    toque en un .json -- si cortas con Ctrl+C a mitad de camino, la
    proxima corrida retoma donde quedaste, no se pierde nada.

  FASE 2 (--apply):
    Lee ese .json de decisiones, hace un backup del .db ANTES de tocar
    nada (obligatorio, no se puede saltear), y aplica los UPDATE
    correspondientes en una sola transaccion -- si algo falla a mitad de
    camino, se hace rollback completo, no queda a medio migrar.

Uso:
    python3 normalize_genres.py --db /ruta/a/orbyte.db
    ... contesta las preguntas interactivas ...
    python3 normalize_genres.py --db /ruta/a/orbyte.db --apply
"""
import argparse
import json
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Las 6 columnas donde puede vivir un valor de genero -- se tratan como
# una sola bolsa de texto para contar variantes y clusters, pero al
# aplicar se actualiza cada tabla.columna por separado.
#
# tracks.genre es la columna "clasica": tag crudo de ID3/FLAC tal cual
# vino del archivo, sin pasar por RichMetaPro -- la mas propensa a
# tener inconsistencias de mayusculas/formato, ya que la escribio
# quien sea que haya tageado cada album originalmente, con
# convenciones distintas a lo largo del tiempo.
#
# users.favorite_genres_json vive en una tabla completamente aparte de
# la biblioteca (la crea app.py, no el pipeline de escaneo) -- generos
# que el usuario elige a mano en su perfil, sin relacion directa con
# los tags de ninguna pista. Mismo formato de lista JSON que
# genre_secondary, asi que el mismo extract_tags()/rebuild_value() de
# abajo ya lo cubre sin cambios.
#
# Confirmado exhaustivo (no por grep de palabras clave, sino revisando
# el CREATE TABLE completo de cada una de las tablas que existen en la
# base -- las del schema.sql del pipeline de escaneo, mas las 9 que
# crea app.py por migracion perezosa): estas 6 son TODAS las columnas
# de toda la base donde puede vivir un genero musical como texto. No
# hay una septima escondida.
GENRE_SOURCES = [
    ("tracks", "genre"),
    ("track_meta", "genre_primary"),
    ("track_meta", "genre_secondary"),
    ("album_meta", "genre_primary"),
    ("album_meta", "genre_secondary"),
    ("users", "favorite_genres_json"),
]


def split_multi_genre(text: str) -> list:
    """Separa un texto plano que en realidad combina varios generos con
    distintos delimitadores, todos vistos en datos reales:
      - coma:            "Progressive Metal, Symphonic Metal"
      - punto y coma:     "A; B"  (delimitador estandar de multi-genero
                           en ID3, no confirmado en los datos pero es
                           tan comun que vale la pena cubrirlo igual)
      - barra:            "World Fusion / Latin / Prog Folk"
      - guion CON espacio a ambos lados: "electronic - experimental"

    A proposito NO separa por guion pegado sin espacios (Nu-Metal,
    Trip-Hop, Post-Rock, etc.) -- ahi el guion es parte del nombre del
    genero, no un separador entre generos distintos."""
    parts = re.split(r"[,;/]+", text)
    result = []
    for part in parts:
        result.extend(re.split(r"\s+-\s+", part))
    return [p.strip() for p in result if p.strip()]


def extract_tags(raw: str) -> list:
    """Si `raw` es una lista JSON valida de strings (ej. genre_secondary
    suele venir asi: '["Hip-Hop", "rap"]'), desarma esa lista Y ADEMAS
    aplica split_multi_genre a cada elemento (por si un elemento de la
    lista es a su vez una combinacion, caso raro pero visto en datos
    reales). Si `raw` no es una lista JSON, aplica split_multi_genre
    directo sobre el valor plano."""
    stripped = raw.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                tags = []
                for item in parsed:
                    tags.extend(split_multi_genre(item))
                return tags
        except (json.JSONDecodeError, ValueError):
            pass
    return split_multi_genre(raw)


def normalize_key(raw: str) -> str:
    """minusculas + trim + espacios/guiones/underscores colapsados a un
    solo espacio. A proposito NO toca '&' ni '/' -- eso ya es una
    decision de sinonimos (fuera de alcance de este script, ver
    docstring del modulo)."""
    key = raw.strip().lower()
    key = re.sub(r"[\s\-_]+", " ", key)
    return key.strip()


def fetch_tag_counts(conn: sqlite3.Connection) -> dict:
    """Devuelve {tag_individual: cantidad_de_apariciones} -- si una fila
    tiene genre_secondary = '["Hip-Hop","rap"]', eso suma +1 a "Hip-Hop"
    y +1 a "rap" por separado, no +1 a la combinación completa."""
    counts: dict[str, int] = {}
    for table, column in GENRE_SOURCES:
        rows = conn.execute(
            f"SELECT {column} AS v FROM {table} "
            f"WHERE {column} IS NOT NULL AND TRIM({column}) != ''"
        ).fetchall()
        for row in rows:
            for tag in extract_tags(row["v"]):
                tag = tag.strip()
                if tag:
                    counts[tag] = counts.get(tag, 0) + 1
    return counts


def build_clusters(raw_counts: dict) -> dict:
    """Agrupa valores crudos por su normalize_key. Devuelve
    {clave_normalizada: [(valor_crudo, cantidad), ...]} ordenado por
    cantidad descendente dentro de cada cluster."""
    clusters: dict[str, list] = {}
    for raw, count in raw_counts.items():
        key = normalize_key(raw)
        clusters.setdefault(key, []).append((raw, count))
    for key in clusters:
        clusters[key].sort(key=lambda pair: pair[1], reverse=True)
    return clusters


def load_decisions(path: Path) -> dict:
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_decision(path: Path, decisions: dict, key: str, entry: dict) -> None:
    decisions[key] = entry
    with path.open("w", encoding="utf-8") as f:
        json.dump(decisions, f, ensure_ascii=False, indent=2, sort_keys=True)


def phase_decide(conn: sqlite3.Connection, decisions_path: Path) -> None:
    tag_counts = fetch_tag_counts(conn)
    clusters = build_clusters(tag_counts)
    decisions = load_decisions(decisions_path)

    # Solo interesan los clusters con MAS DE UNA variante -- el resto ya
    # esta bien tal cual, se auto-acepta sin preguntar.
    multi = {k: v for k, v in clusters.items() if len(v) > 1}
    single = {k: v for k, v in clusters.items() if len(v) == 1}

    for key, variants in single.items():
        if key not in decisions:
            save_decision(decisions_path, decisions, key, {
                "canonical": variants[0][0],
                "variants": [v[0] for v in variants],
                "auto": True,
            })

    pending = {k: v for k, v in multi.items() if k not in decisions}
    already_done = len(multi) - len(pending)

    print(f"\n{len(tag_counts)} tags individuales distintos -> "
          f"{len(clusters)} generos reales despues de normalizar "
          f"(minusculas/espacios/guiones).")
    print(f"{len(multi)} de esos tienen mas de una variante en la base "
          f"({already_done} ya decididos, {len(pending)} pendientes).\n")

    if not pending:
        print("No queda nada pendiente por decidir. Corre con --apply "
              "para aplicar los cambios a la base.")
        return

    sorted_pending = sorted(
        pending.items(),
        key=lambda item: sum(c for _, c in item[1]),
        reverse=True,
    )

    for i, (key, variants) in enumerate(sorted_pending, 1):
        total = sum(c for _, c in variants)
        print(f"\n[{i}/{len(pending)}]  ({total} filas en total)")
        for idx, (raw, count) in enumerate(variants, 1):
            print(f"    {idx}. \"{raw}\"  ({count} filas)")
        print(f"    0. escribir un texto distinto")
        print(f"    s. saltar (dejar estas variantes como estan, no son "
              f"el mismo genero)")
        print(f"    q. guardar y salir (podes retomar despues corriendo "
              f"de nuevo el script)")

        default_choice = "1"  # la variante mas frecuente, ya viene primera
        try:
            answer = input(f"  Elegi forma canonica [{default_choice}]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n\nInterrumpido. Lo ya decidido quedó guardado en "
                  f"{decisions_path} -- corre el script de nuevo cuando "
                  f"quieras seguir con el resto.")
            return
        if answer == "":
            answer = default_choice

        if answer.lower() == "q":
            print(f"\nGuardado. {len(pending) - i + 1} clusters quedaron "
                  f"pendientes -- corre el script de nuevo cuando quieras "
                  f"seguir.")
            return
        elif answer.lower() == "s":
            save_decision(decisions_path, decisions, key, {
                "canonical": None,
                "variants": [v[0] for v in variants],
                "skipped": True,
            })
            continue
        elif answer == "0":
            try:
                custom = input("  Texto canonico: ").strip()
            except (EOFError, KeyboardInterrupt):
                print(f"\n\nInterrumpido antes de terminar este cluster -- "
                      f"no se guardó (no quedó a medio definir). Lo demás "
                      f"ya decidido sigue en {decisions_path}.")
                return
            if not custom:
                print("  Vacio, salteando este cluster.")
                continue
            canonical = custom
        else:
            try:
                choice_idx = int(answer)
                canonical = variants[choice_idx - 1][0]
            except (ValueError, IndexError):
                print("  Opcion invalida, salteando este cluster (podes "
                      "decidirlo en la proxima corrida).")
                continue

        save_decision(decisions_path, decisions, key, {
            "canonical": canonical,
            "variants": [v[0] for v in variants],
            "skipped": False,
        })

    print(f"\nListo -- {len(pending)} clusters decididos. Revisa "
          f"{decisions_path} si queres, y despues corre con --apply.")


def canonical_lookup(decisions: dict) -> dict:
    """Arma {tag_original: tag_canonico} para TODOS los tags que
    aparecieron en alguna decision no salteada (incluye los clusters
    de una sola variante, que ya son su propio canonico)."""
    lookup: dict = {}
    for entry in decisions.values():
        if entry.get("skipped"):
            continue
        canonical = entry.get("canonical")
        if not canonical:
            continue
        for variant in entry.get("variants", []):
            lookup[variant] = canonical
    return lookup


def rebuild_value(raw: str, lookup: dict):
    """Reconstruye el valor de una fila aplicando el lookup tag por tag.
    Devuelve (nuevo_valor, es_combo_no_aplicable):
      - Si `raw` ya era una lista JSON: devuelve una lista JSON nueva
        (tags ya normalizados, sin duplicados, orden preservado).
      - Si `raw` era plano y se separa en UN SOLO tag: devuelve ese tag
        canonico directo (mismo shape de siempre, sin riesgo).
      - Si `raw` era plano pero se separa en VARIOS tags (ej. "World
        Fusion / Latin / Prog Folk"): NO se reescribe -- devolver eso
        como lista JSON cambiaria la forma de un valor que hoy es texto
        plano, y el resto de la app consulta esa columna como valor
        unico (t.genre=?, GROUP BY t.genre). Se marca como
        es_combo_no_aplicable=True para reportarlo aparte, no se aplica
        solo."""
    stripped = raw.strip()
    is_list = stripped.startswith("[") and stripped.endswith("]")
    if is_list:
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
            new_tags = []
            for tag in parsed:
                for sub_tag in split_multi_genre(tag):
                    canonical_tag = lookup.get(sub_tag, sub_tag)
                    if canonical_tag not in new_tags:
                        new_tags.append(canonical_tag)
            return json.dumps(new_tags, ensure_ascii=False), False

    tags = split_multi_genre(raw)
    if len(tags) <= 1:
        single = tags[0] if tags else raw
        return lookup.get(single, raw), False
    return raw, True


def phase_apply(conn: sqlite3.Connection, db_path: Path, decisions_path: Path) -> None:
    if not decisions_path.exists():
        print(f"No existe {decisions_path} -- corre primero sin --apply "
              f"para decidir los generos.")
        sys.exit(1)

    decisions = load_decisions(decisions_path)
    lookup = canonical_lookup(decisions)

    # Para cada tabla.columna, juntamos todos los valores crudos
    # distintos que existen hoy, calculamos su valor reconstruido (tag
    # por tag) y nos quedamos solo con los que realmente cambian.
    changes_per_source = {}
    combos_per_source = {}
    total_rows_to_change = 0
    for table, column in GENRE_SOURCES:
        rows = conn.execute(
            f"SELECT {column} AS v, COUNT(*) AS c FROM {table} "
            f"WHERE {column} IS NOT NULL AND TRIM({column}) != '' "
            f"GROUP BY {column}"
        ).fetchall()
        source_changes = []
        source_combos = []
        for row in rows:
            old_value = row["v"]
            new_value, is_combo = rebuild_value(old_value, lookup)
            if is_combo:
                source_combos.append((old_value, split_multi_genre(old_value), row["c"]))
            elif new_value != old_value:
                source_changes.append((old_value, new_value, row["c"]))
                total_rows_to_change += row["c"]
        if source_changes:
            changes_per_source[(table, column)] = source_changes
        if source_combos:
            combos_per_source[(table, column)] = source_combos

    if combos_per_source:
        total_combo_rows = sum(c for v in combos_per_source.values() for _, _, c in v)
        print(f"AVISO: {total_combo_rows} filas tienen un valor plano que en "
              f"realidad combina varios generos (ej. \"World Fusion / Latin / "
              f"Prog Folk\") -- estas NO se tocan automaticamente, porque "
              f"convertirlas a lista cambiaria la forma de la columna y el "
              f"resto de la app las consulta hoy como valor unico "
              f"(t.genre=?, GROUP BY t.genre, etc). Se listan para que las "
              f"revises, no se aplican:\n")
        for (table, column), combos in combos_per_source.items():
            print(f"  {table}.{column}: {len(combos)} valores combinados")
            for old_value, split_tags, count in combos[:8]:
                print(f'      "{old_value}"  ({count} filas)  ->  se separaria en {split_tags}')
            if len(combos) > 8:
                print(f"      ... y {len(combos) - 8} mas")
        print()

    if not changes_per_source:
        print("No hay cambios automaticos para aplicar (todo eran clusters de "
              "una sola variante, ya coincide, se saltearon, o son combos "
              "que quedan afuera del apply -- ver aviso arriba).")
        return

    print(f"Se van a actualizar {total_rows_to_change} filas en total, "
          f"repartidas asi:\n")
    for (table, column), source_changes in changes_per_source.items():
        print(f"  {table}.{column}: {len(source_changes)} valores distintos")
        for old_value, new_value, count in source_changes[:5]:
            print(f'      "{old_value}"  ({count} filas)  ->  "{new_value}"')
        if len(source_changes) > 5:
            print(f"      ... y {len(source_changes) - 5} mas")

    try:
        confirm = input(f"\nEsto va a modificar filas reales en {db_path}. "
                         f"Se hace un backup antes. Escribi APLICAR para "
                         f"confirmar: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\nCancelado, no se tocó nada.")
        return
    if confirm != "APLICAR":
        print("Cancelado, no se toco nada.")
        return

    backup_path = db_path.with_name(
        f"{db_path.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{db_path.suffix}"
    )
    shutil.copy2(db_path, backup_path)
    print(f"Backup creado en {backup_path}")

    total_rows_changed = 0
    try:
        for (table, column), source_changes in changes_per_source.items():
            for old_value, new_value, _count in source_changes:
                cur = conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                    (new_value, old_value),
                )
                total_rows_changed += cur.rowcount
        conn.commit()
        print(f"\nOK -- {total_rows_changed} filas actualizadas en total. "
              f"Backup previo en {backup_path} por si hay que revertir "
              f"(basta con reemplazar el .db actual por el backup).")
        print("Reinicia el Flask para que sirva los valores nuevos "
              "(no hay ningun cache de generos de por medio, pero las "
              "conexiones ya abiertas no ven el cambio hasta reconectar).")
    except Exception:
        conn.rollback()
        print("\nFalló algo a mitad de camino -- se hizo ROLLBACK, la base "
              "quedo intacta. El backup en " + str(backup_path) +
              " existe igual por las dudas, pero no deberia hacer falta.")
        raise


def phase_list(conn: sqlite3.Connection) -> None:
    tag_counts = fetch_tag_counts(conn)
    print(f"{len(tag_counts)} generos/tags distintos (ya separados por "
          f"coma/punto y coma/barra/guion-con-espacios, ya desarmadas "
          f"las listas JSON):\n")
    for tag in sorted(tag_counts, key=lambda t: t.lower()):
        print(f"  {tag}  ({tag_counts[tag]})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="ruta al orbyte.db")
    parser.add_argument("--decisions", default="genre_normalization_decisions.json",
                         help="archivo donde se guardan las decisiones "
                              "(default: genre_normalization_decisions.json "
                              "en el directorio actual)")
    parser.add_argument("--apply", action="store_true",
                         help="aplica las decisiones ya tomadas a la base "
                              "(sin esto, el script solo pregunta y guarda "
                              "decisiones, no toca la base)")
    parser.add_argument("--list", action="store_true",
                         help="solo imprime el listado completo de tags "
                              "distintos (ya separados/normalizados por "
                              "forma), sin preguntar nada ni tocar la base")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"No existe el archivo: {db_path}")
        sys.exit(1)
    decisions_path = Path(args.decisions)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        if args.list:
            phase_list(conn)
        elif args.apply:
            phase_apply(conn, db_path, decisions_path)
        else:
            phase_decide(conn, decisions_path)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
