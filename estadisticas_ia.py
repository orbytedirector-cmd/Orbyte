"""Resumen IA de las estadísticas de uso (solo administrador).

Toma el mismo JSON que arma estadisticas.py (general o de un usuario), lo reduce a lo que sirve para
interpretar el uso (sin correos ni datos personales) y le pide a la IA un resumen en español con
hallazgos y sugerencias concretas. Mismos proveedores que Orbitron: Gemini y, si falla, Groq.

  resumir(datos, tipo, dias, nuevo=False) -> {resumen, hallazgos[], sugerencias[], datos[], fuente, generado}

El resultado se guarda en memoria 6 horas por (tipo, usuario, período): abrir la pantalla de nuevo no
vuelve a gastar una consulta; «Regenerar» (nuevo=True) sí.
"""
import json
import os
import threading
import time
from datetime import datetime

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-3.6-flash')
GROQ_MODEL = os.environ.get('GROQ_MODEL', 'openai/gpt-oss-120b')
TIMEOUT = 60
VIGENCIA_SEG = 6 * 3600
PERIODOS = {0: 'todo el historial', 7: 'los últimos 7 días', 30: 'los últimos 30 días',
            90: 'los últimos 90 días', 365: 'el último año'}

_cache = {}
_lock = threading.Lock()


def _nombres(xs, n=10, campos=('nombre',)):
    out = []
    for x in (xs or [])[:n]:
        d = {k: x.get(k) for k in campos if x.get(k) not in (None, '')}
        d.update({k: x[k] for k in ('escuchas', 'minutos', 'pct') if k in x})
        out.append(d)
    return out


def _calor_texto(calor):
    """Las 5 franjas (día + hora) con más minutos, en vez de la matriz entera."""
    celdas = []
    for d, fila in enumerate((calor or {}).get('valores') or []):
        for h, v in enumerate(fila):
            if v:
                celdas.append((v, f"{calor['dias'][d]} {h:02d}:00"))
    celdas.sort(reverse=True)
    return [f'{t} ({v} min)' for v, t in celdas[:5]]


def _por_dia(tendencia):
    dias = [d for d in tendencia or [] if d.get('minutos') or d.get('conexiones')]
    return {'dias_con_actividad': len(dias), 'dias_del_periodo': len(tendencia or []),
            'ultimos_14': [{k: d[k] for k in ('dia', 'minutos', 'escuchas', 'usuarios', 'conexiones')}
                           for d in (tendencia or [])[-14:]]}


def _tops(t):
    return {'artistas': _nombres(t.get('artistas')),
            'albumes': _nombres(t.get('albumes'), campos=('nombre', 'artista')),
            'pistas': _nombres(t.get('pistas'), campos=('nombre', 'artista')),
            'generos': _nombres(t.get('generos')), 'calidad': _nombres(t.get('calidad')),
            'decadas': _nombres(t.get('decadas'))}


def _compacto(d, tipo):
    base = {
        'resumen': d.get('resumen'),
        'actividad': _por_dia(d.get('tendencia')),
        'lo_mas_escuchado': _tops(d.get('top') or {}),
        'franjas_con_mas_escucha': _calor_texto((d.get('habitos') or {}).get('calor')),
        'dispositivos': (d.get('habitos') or {}).get('dispositivos'),
        'avisos_de_calidad_de_datos': [a.get('texto') for a in d.get('salud') or []],
    }
    f = d.get('funciones') or {}
    base['busquedas'] = {'total': (f.get('busquedas') or {}).get('total'),
                         'mas_buscado': (f.get('busquedas') or {}).get('top'),
                         'sin_resultado': (f.get('busquedas') or {}).get('sin_resultado')}
    base['orbitron'] = f.get('ia')
    base['favoritos_nuevos'] = f.get('favoritos')
    if tipo == 'general':
        base['usuarios'] = [{k: u.get(k) for k in ('nombre', 'admin', 'dispositivo', 'ultimo_acceso', 'conexiones',
                                                   'escuchas', 'minutos', 'sesiones_escucha', 'sesion_prom_min',
                                                   'completadas_pct', 'top_artista', 'busquedas', 'pedidos_ia')}
                            for u in d.get('usuarios') or []]
        base['colaborativa'] = f.get('colaborativa')
        base['social'] = f.get('social')
        b = d.get('biblioteca') or {}
        base['biblioteca'] = {k: b.get(k) for k in ('albumes_total', 'albumes_escuchados', 'albumes_pct',
                                                    'pistas_total', 'pistas_escuchadas', 'pistas_pct',
                                                    'recientes_dias', 'recientes_total', 'recientes_sin_escuchar')}
        base['biblioteca']['recientes_mas_escuchados'] = _nombres(b.get('recientes'), campos=('nombre', 'artista'))
    else:
        u = d.get('usuario') or {}
        base['usuario'] = {k: u.get(k) for k in ('nombre', 'admin', 'creado', 'ultimo_acceso', 'dispositivo')}
        base['ultimas_escuchas'] = [{k: h.get(k) for k in ('fecha', 'pista', 'artista', 'album', 'segundos',
                                                          'duracion', 'completa')}
                                    for h in (d.get('historial') or [])[:25]]
    return base


_REGLAS = '''Cómo se miden los datos (no lo pongas en duda, úsalo para interpretar):
- «conexiones»: usos reales de la app con la sesión iniciada (volver antes de 10 min es la misma conexión; cuenta si estuvo abierta 2 min o hubo alguna acción). «aperturas»: cada vez que la app pasó a primer plano.
- «sesiones de escucha»: desde que empieza la primera pista hasta 10 min sin reproducir. sesion_prom_min = duración real; sesion_escuchada_min = tiempo efectivamente escuchado.
- «completadas_pct»: pistas escuchadas hasta el final; el resto se saltó.
- Calidades: DSD, MQA, HR (alta resolución), CD, LOSSY (MP3/AAC).
- Si hay avisos de calidad de datos, tenlos en cuenta y menciónalos cuando cambien la lectura (por ejemplo, minutos subestimados).'''

_FORMATO = '''Responde SOLO con un objeto JSON, sin texto alrededor:
{"resumen": "2 a 4 frases con lo más importante, con cifras",
 "hallazgos": ["comportamientos o patrones concretos, cada uno con su cifra", ...],
 "sugerencias": ["acciones concretas para el administrador", ...],
 "datos": ["advertencias sobre la calidad o falta de datos, si las hay", ...]}
Entre 3 y 7 hallazgos y entre 3 y 6 sugerencias. Español neutro (Chile), frases cortas, sin markdown ni emojis.
No inventes nada que no esté en los datos; si algo no se puede saber, dilo en «datos».'''


def _prompt(compacto, tipo, dias):
    periodo = PERIODOS.get(dias, f'los últimos {dias} días')
    if tipo == 'general':
        rol = ('Eres analista de uso de Orbyte, una plataforma privada de música en alta resolución (biblioteca '
               'propia, pocos usuarios: familia y amigos del dueño). El dueño y administrador quiere entender cómo '
               f'se usa la plataforma en {periodo} y qué decidir. Analiza el uso general: quiénes la usan y cuánto, '
               'tendencia, cuándo y desde qué equipos, qué se escucha (artistas, géneros, calidad, décadas), si '
               'se aprovecha la biblioteca y lo recién agregado, búsquedas sin resultado (material que falta), '
               'uso de Orbitron (playlists con IA), colaborativa y social. Las sugerencias deben ser accionables: '
               'qué agregar a la biblioteca, a quién reactivar, qué función mostrar o mejorar, qué revisar.')
    else:
        nombre = (compacto.get('usuario') or {}).get('nombre') or 'este usuario'
        rol = ('Eres analista de uso de Orbyte, una plataforma privada de música en alta resolución (biblioteca '
               f'propia). El administrador quiere entender a {nombre} en {periodo}: cuánto y cuándo escucha, cómo '
               'escucha (sesiones largas o cortas, pistas completas o saltos, álbumes enteros o pistas sueltas), '
               'sus gustos (artistas, géneros, décadas, calidad preferida), desde qué equipos, si busca cosas que '
               'no están y si usa Orbitron y favoritos. Las sugerencias son para el administrador: qué recomendarle '
               'o agregar a la biblioteca para él, qué función mostrarle, si hay que revisar algo de su app.')
    return (f'{rol}\n\n{_REGLAS}\n\n{_FORMATO}\n\nDatos ({periodo}):\n'
            + json.dumps(compacto, ensure_ascii=False, default=str))


def _json(texto):
    t = (texto or '').strip()
    if t.startswith('```'):
        t = t.strip('`')
        if t.lower().startswith('json'):
            t = t[4:]
    i, j = t.find('{'), t.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except ValueError:
        return None


def _gemini(prompt):
    clave = os.environ.get('GEMINI_API_KEY')
    if not (clave and requests):
        return None
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent'
    cuerpo = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
              'generationConfig': {'response_mime_type': 'application/json', 'temperature': 0.3,
                                   'thinkingConfig': {'thinkingLevel': 'low'}}}
    r = requests.post(url, headers={'x-goog-api-key': clave, 'Content-Type': 'application/json'},
                      json=cuerpo, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    return _json(''.join(p.get('text', '') for c in d.get('candidates', [])
                         for p in (c.get('content') or {}).get('parts', [])))


def _groq(prompt):
    clave = os.environ.get('GROQ_API_KEY')
    if not (clave and requests):
        return None
    r = requests.post('https://api.groq.com/openai/v1/chat/completions',
                      headers={'Authorization': f'Bearer {clave}', 'Content-Type': 'application/json'},
                      json={'model': GROQ_MODEL, 'temperature': 0.3, 'response_format': {'type': 'json_object'},
                            'messages': [{'role': 'user', 'content': prompt}]}, timeout=TIMEOUT)
    r.raise_for_status()
    return _json(r.json()['choices'][0]['message']['content'])


def _lista(x, n):
    if isinstance(x, str):
        x = [x]
    return [str(s).strip()[:500] for s in (x or []) if str(s).strip()][:n]


def resumir(datos, tipo, dias, clave_cache, nuevo=False):
    """tipo: 'general' | 'usuario'. clave_cache: p. ej. ('usuario', 5, 30)."""
    with _lock:
        hit = _cache.get(clave_cache)
    if hit and not nuevo and time.time() - hit[0] < VIGENCIA_SEG:
        return dict(hit[1], en_cache=True)
    if not os.environ.get('GEMINI_API_KEY') and not os.environ.get('GROQ_API_KEY'):
        return {'error': 'No hay GEMINI_API_KEY ni GROQ_API_KEY en el .env'}
    prompt = _prompt(_compacto(datos, tipo), tipo, dias)
    errores = []
    for nombre, fn in (('Gemini', _gemini), ('Groq', _groq)):
        try:
            r = fn(prompt)
        except Exception as e:
            errores.append(f'{nombre}: {type(e).__name__}')
            continue
        if r and (r.get('resumen') or r.get('hallazgos')):
            out = {'resumen': str(r.get('resumen') or '').strip()[:1500],
                   'hallazgos': _lista(r.get('hallazgos'), 8), 'sugerencias': _lista(r.get('sugerencias'), 8),
                   'datos': _lista(r.get('datos'), 5), 'fuente': nombre, 'dias': dias,
                   'generado': datetime.now().strftime('%Y-%m-%d %H:%M')}
            with _lock:
                _cache[clave_cache] = (time.time(), out)
            return dict(out, en_cache=False)
        if r is not None:
            errores.append(f'{nombre}: respuesta sin contenido')
    return {'error': 'La IA no respondió (' + ('; '.join(errores) or 'sin proveedor') + '). Intenta de nuevo.'}
