"""
Orbyte Ingesta — cuánto procesador usa la herramienta (Ajustes → «Uso del procesador»).

Se limita de verdad, no solo la cantidad de tareas: el proceso del servicio y todo lo que lanza
(ffmpeg, los 5 scripts) queda atado a N núcleos con afinidad de CPU, y además:
  - revisión: N pistas decodificándose a la vez;
  - ubicar:   N pistas convirtiéndose a la vez;
  - scripts:  numpy/BLAS con N hilos;
  - desatendido: una pausa entre un álbum y el siguiente para que el procesador descanse.
Orbyte no se ve afectado: es otro servicio.
"""
import os

try:
    ORIGINALES = sorted(os.sched_getaffinity(0))
except (AttributeError, OSError):
    ORIGINALES = list(range(os.cpu_count() or 1))

# nivel: (núcleos, segundos de pausa entre álbumes en modo desatendido)
NIVELES = {'bajo': (1, 60), 'medio': (2, 15), 'alto': (4, 0)}
POR_DEFECTO = 'medio'


def _nivel(aj):
    return NIVELES.get((aj or {}).get('uso_cpu') or POR_DEFECTO, NIVELES[POR_DEFECTO])


def nucleos(aj):
    return max(1, min(_nivel(aj)[0], len(ORIGINALES)))


def pausa(aj):
    return _nivel(aj)[1]


def aplicar(aj):
    """Ata todos los hilos del servicio (y lo que lancen después) a los primeros N núcleos."""
    cpus = set(ORIGINALES[:nucleos(aj)])
    try:
        tids = [int(t) for t in os.listdir('/proc/self/task')]
    except OSError:
        tids = [0]
    for tid in tids:
        try:
            os.sched_setaffinity(tid, cpus)
        except OSError:
            pass
    return sorted(cpus)


def entorno_scripts(aj):
    n = str(nucleos(aj))
    return {k: n for k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS')}
