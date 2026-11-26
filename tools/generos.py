#!/usr/bin/env python3
"""Géneros de la biblioteca con una sola forma (regla de Niko, 25-11-2026; la regla vive en ingesta/generos.py).

Sin opciones solo informa (no toca nada): cómo queda cada forma escrita, qué se quita y cuántas filas cambiarían.
Con --aplicar deja music.db con la regla, en una sola transacción y con un respaldo previo de la base:

  tracks.genre                                 solo el género principal del tag («Pop, Rock, Metal» → «Pop»)
  track_meta.genre_primary / genre_secondary   principal + secundarios; los géneros extra del tag pasan acá
  album_meta.genre_primary / genre_secondary   lo mismo por álbum
  users.favorite_genres_json                   los géneros favoritos del perfil de cada usuario

Los archivos no se tocan: sus tags se corrigen solos cuando el álbum vuelve a pasar por la ingesta (Ajustar,
procesar) o por RichMeta, que ya escriben con la regla.

Uso (desde la carpeta del repo):
  python3 tools/generos.py [--db music.db] [--salida generos.txt]       informe
  python3 tools/generos.py --aplicar [--db music.db]                     aplica (respaldo music.db.antes-generos-…)
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AQUI)
from ingesta import generos as G  # noqa: E402


def columnas(con, tabla):
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info({tabla})')}
    except sqlite3.Error:
        return set()


def _json(lista, original):
    """Lista → JSON; si la columna estaba vacía (NULL) y no hay nada que guardar, sigue NULL."""
    if not lista and original is None:
        return None
    return json.dumps(lista, ensure_ascii=False)


def calcular(con):
    """→ (cambios {tabla: [(id, valores nuevos…)]}, estadística). No escribe."""
    cambios = {'tracks': [], 'track_meta': [], 'album_meta': [], 'users': []}
    formas = defaultdict(Counter)       # forma escrita → {resultado: veces}
    resultado = Counter()               # género final → usos
    otros_tag = {}                      # track_id → géneros extra del tag GENRE
    principal_tag = {}                  # track_id → género principal del tag GENRE

    def anotar(crudo):
        for parte in _crudos(crudo):
            ns = G.separar(parte)
            formas[parte][' + '.join(ns) or '— (se quita)'] += 1

    if 'genre' in columnas(con, 'tracks'):
        for tid, g in con.execute("SELECT id, genre FROM tracks WHERE genre IS NOT NULL AND genre != ''"):
            anotar(g)
            gs = G.separar(g)
            nuevo = gs[0] if gs else ''
            if gs:
                principal_tag[tid] = gs[0]
            if gs[1:]:
                otros_tag[tid] = gs[1:]
            if nuevo:
                resultado[nuevo] += 1
            if nuevo != g:
                cambios['tracks'].append((tid, nuevo))

    tm = columnas(con, 'track_meta')
    if {'genre_primary', 'genre_secondary'} <= tm:
        for tid, gp, gs in con.execute('SELECT track_id, genre_primary, genre_secondary FROM track_meta'):
            anotar(gp)
            anotar(gs)
            pri, sec = G.repartir(gp, gs)
            for o in otros_tag.get(tid, []):
                if o != pri and o not in sec:
                    sec.append(o)
            if not pri and (principal_tag.get(tid) or sec):   # sin principal: el del tag, o el primer secundario
                pri = principal_tag.get(tid) or sec[0]
                sec = [g for g in sec if g != pri]
            nuevo_s = _json(sec, gs)
            if pri:
                resultado[pri] += 1
            resultado.update(sec)
            if pri != (gp if gp else None) or nuevo_s != gs:
                cambios['track_meta'].append((tid, pri, nuevo_s))

    am = columnas(con, 'album_meta')
    if {'genre_primary', 'genre_secondary'} <= am:
        for aid, gp, gs in con.execute('SELECT album_id, genre_primary, genre_secondary FROM album_meta'):
            anotar(gp)
            anotar(gs)
            pri, sec = G.repartir(gp, gs)
            nuevo_s = _json(sec[:3], gs)
            if pri:
                resultado[pri] += 1
            resultado.update(sec[:3])
            if pri != (gp if gp else None) or nuevo_s != gs:
                cambios['album_meta'].append((aid, pri, nuevo_s))
    if 'favorite_genres_json' in columnas(con, 'users'):     # los favoritos que cada usuario eligió en su perfil
        for uid, fav in con.execute('SELECT id, favorite_genres_json FROM users WHERE favorite_genres_json IS NOT NULL'):
            anotar(fav)
            nuevo = json.dumps(G.separar(fav))
            if nuevo != fav:
                cambios['users'].append((uid, nuevo))
    return cambios, formas, resultado


def _crudos(valor):
    """Las partes tal como están escritas (para el informe)."""
    if valor is None:
        return []
    s = str(valor).strip()
    if s.startswith('['):
        try:
            return [str(x).strip() for x in json.loads(s) if str(x).strip()]
        except ValueError:
            pass
    return [p.strip() for p in s.replace('\x00', ';').split(';') if p.strip()]


def informe(cambios, formas, resultado):
    lineas = []
    p = lineas.append
    p(f"Filas que cambian: tracks {len(cambios['tracks'])} · track_meta {len(cambios['track_meta'])} · "
      f"album_meta {len(cambios['album_meta'])} · favoritos de usuarios {len(cambios['users'])}")
    p(f'Formas escritas hoy: {len(formas)} → géneros con la regla: {len(resultado)}')
    p('')
    cambia = {f: c for f, c in formas.items() if list(c) != [f]}
    quita = {f: c for f, c in cambia.items() if '— (se quita)' in c}
    p(f'== Se quitan (no son géneros): {len(quita)} ==')
    p(', '.join(sorted(quita, key=str.lower)))
    p('')
    p(f'== Cambian de forma: {len(cambia) - len(quita)} ==')
    for f in sorted(cambia, key=str.lower):
        if f in quita:
            continue
        p(f'«{f}» → ' + ' + '.join(cambia[f]))
    p('')
    p(f'== Géneros finales: {len(resultado)} (usos) ==')
    for g, n in sorted(resultado.items(), key=lambda x: x[0].lower()):
        p(f'{g}  {n}')
    return '\n'.join(lineas) + '\n'


def aplicar(db, cambios):
    respaldo = f'{db}.antes-generos-{time.strftime("%Y%m%d-%H%M%S")}'
    src = sqlite3.connect(db, timeout=60)
    dst = sqlite3.connect(respaldo)
    with dst:
        src.backup(dst)
    dst.close()
    try:
        src.execute('BEGIN IMMEDIATE')
        src.executemany('UPDATE tracks SET genre=? WHERE id=?', [(g, i) for i, g in cambios['tracks']])
        src.executemany('UPDATE track_meta SET genre_primary=?, genre_secondary=? WHERE track_id=?',
                        [(p, s, i) for i, p, s in cambios['track_meta']])
        src.executemany('UPDATE album_meta SET genre_primary=?, genre_secondary=? WHERE album_id=?',
                        [(p, s, i) for i, p, s in cambios['album_meta']])
        if cambios['users']:
            src.executemany('UPDATE users SET favorite_genres_json=? WHERE id=?',
                            [(f, i) for i, f in cambios['users']])
        src.execute('COMMIT')
    except Exception:
        src.execute('ROLLBACK')
        raise
    finally:
        src.close()
    return respaldo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=os.path.join(AQUI, 'music.db'))
    ap.add_argument('--salida')
    ap.add_argument('--aplicar', action='store_true')
    a = ap.parse_args()
    if not os.path.exists(a.db):
        sys.exit(f'No existe {a.db}')
    con = sqlite3.connect(f'file:{a.db}?mode=ro', uri=True, timeout=60)
    cambios, formas, resultado = calcular(con)
    con.close()
    texto = informe(cambios, formas, resultado)
    if a.salida:
        with open(a.salida, 'w', encoding='utf-8') as fh:
            fh.write(texto)
    resumen = (f"tracks {len(cambios['tracks'])} · track_meta {len(cambios['track_meta'])} · "
               f"album_meta {len(cambios['album_meta'])} · usuarios {len(cambios['users'])} filas · "
               f"{len(formas)} formas → {len(resultado)} géneros")
    if not a.aplicar:
        if not a.salida:
            sys.stdout.write(texto)
        print(f'\nSimulación (no se tocó nada): {resumen}')
        return
    respaldo = aplicar(a.db, cambios)
    print(f'Aplicado: {resumen}')
    print(f'Respaldo: {respaldo}')


if __name__ == '__main__':
    main()
