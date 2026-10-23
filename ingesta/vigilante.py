"""
Orbyte Ingesta — vigilante de la carpeta de entrada.

Un hilo que cada «intervalo» segundos:
  1. detecta las carpetas-álbum de la entrada;
  2. espera a que dejen de cambiar («espera_min» sin cambios = copia terminada);
  3. revisa de a un álbum (la decodificación de sus pistas va en paralelo);
  4. deja avisos en la campana.
Etapa 1: solo lectura, no mueve ni modifica nada.
"""
import json
import logging
import os
import threading
import time
import traceback

from . import analisis, edicion
from .almacen import PREVIOS

log = logging.getLogger('ingesta')

NOMBRE_ESTADO = {
    'revisado': 'Lista para corregir y ubicar',
    'requiere_revision': 'Requiere revisión',
    'error': 'Con errores',
}


class Vigilante(threading.Thread):
    def __init__(self, almacen):
        super().__init__(daemon=True, name='vigilante')
        self.almacen = almacen
        self.despertar = threading.Event()
        self.estado = {'ultimo_escaneo': None, 'sueltos': [], 'problema_entrada': None,
                       'analizando': None}
        self._tanda = []
        self.cliente_mb = edicion.Cliente(almacen.ruta)
        self.procesador = None

    def ya(self):
        """Fuerza un escaneo inmediato (botón «Revisar ahora» o cambio de ajustes)."""
        self.despertar.set()

    def run(self):
        self.almacen.reiniciar_interrumpidos()
        while True:
            try:
                self.ciclo()
            except Exception:
                log.error('Error en el vigilante:\n%s', traceback.format_exc())
            aj = self.almacen.ajustes()
            try:
                intervalo = max(5, int(aj.get('intervalo_seg') or 30))
            except ValueError:
                intervalo = 30
            self.despertar.wait(intervalo)
            self.despertar.clear()

    # ── un ciclo completo ──
    def ciclo(self):
        aj = self.almacen.ajustes()
        entrada = (aj.get('carpeta_entrada') or '').strip()
        self.estado['ultimo_escaneo'] = time.time()
        if not entrada:
            self.estado['problema_entrada'] = 'Todavía no eliges la carpeta de entrada (Ajustes).'
            return
        if not os.path.isdir(entrada):
            self.estado['problema_entrada'] = f'La carpeta de entrada no existe o no está montada: {entrada}'
            return
        self.estado['problema_entrada'] = None

        try:
            espera = max(0.0, float(aj.get('espera_min') or 2)) * 60
        except ValueError:
            espera = 120.0
        unidades, sueltos = analisis.detectar_albumes(entrada)
        self.estado['sueltos'] = sueltos
        conocidos = self.almacen.por_ruta()
        vistos = set()
        ahora = time.time()
        for u in unidades:
            vistos.add(u['ruta'])
            fila = conocidos.get(u['ruta'])
            if not fila:
                self.almacen.nuevo_album(u['ruta'], u['rel'], u['firma'])
                continue
            firma_vieja = json.loads(fila['firma'] or '[]')
            if fila['estado'] not in PREVIOS + ('por_ubicar',) or fila['estado'] == 'analizando':
                continue
            if firma_vieja != u['firma']:
                # sigue llegando material (o cambió después de revisado): se vuelve a esperar
                self.almacen.actualizar(fila['id'], firma=u['firma'], firma_desde=ahora,
                                        estado='copiando', progreso=None)
            elif fila['estado'] == 'copiando' and ahora - (fila['firma_desde'] or ahora) >= espera:
                self.almacen.actualizar(fila['id'], estado='en_cola')
        # lo que ya no está en la entrada sale de la bandeja
        for ruta, fila in conocidos.items():
            if ruta not in vistos and fila['estado'] in PREVIOS + ('por_ubicar',) and fila['estado'] != 'analizando':
                self.almacen.borrar_album(fila['id'])

        # revisión: de a un álbum, hasta vaciar la cola
        while True:
            sig = self.almacen.siguiente_en_cola()
            if not sig:
                break
            self.revisar(sig, aj)
        self._cerrar_tanda()

    def revisar(self, fila, aj):
        aid = fila['id']
        if not os.path.isdir(fila['ruta']):
            self.almacen.borrar_album(aid)
            return
        firma_antes = analisis.firma(fila['ruta'])
        self.almacen.actualizar(aid, estado='analizando', progreso='Empezando…')
        self.estado['analizando'] = aid
        ultimo = [0.0]

        def progreso(msg):
            ahora = time.time()
            if ahora - ultimo[0] > 0.7:          # no martillar la base
                ultimo[0] = ahora
                self.almacen.actualizar(aid, progreso=msg)

        try:
            trabajadores = max(1, min(8, int(aj.get('trabajadores') or 3)))
        except ValueError:
            trabajadores = 3
        try:
            unidad = {'ruta': fila['ruta'], 'carpetas': []}
            completo = self.almacen.album(aid) or {}
            eleccion = json.loads(completo['eleccion']).get('mbid') if completo.get('eleccion') else None
            previo = completo.get('reporte') if isinstance(completo.get('reporte'), dict) else None
            cliente = self.cliente_mb if aj.get('musicbrainz', '1') == '1' else None
            rep = analisis.analizar_album(unidad, aj, progreso=progreso, trabajadores=trabajadores,
                                          cliente_mb=cliente, eleccion=eleccion, previo=previo,
                                          cambios=completo.get('cambios') or {})
        except Exception as e:
            log.error('Error revisando %s:\n%s', fila['ruta'], traceback.format_exc())
            self.almacen.actualizar(aid, estado='error', progreso=None, analizado=time.time(),
                                    reporte={'fallo': f'{type(e).__name__}: {e}', 'carpeta': fila['ruta'],
                                             'nombre_carpeta': os.path.basename(fila['ruta'])})
            self.almacen.avisar('error', f'No se pudo revisar «{fila["rel"]}»', str(e), aid)
            self._tanda.append('error')
            return
        finally:
            self.estado['analizando'] = None

        if analisis.firma(fila['ruta']) != firma_antes:
            # cambió mientras se revisaba: vuelve a esperar a que termine la copia
            self.almacen.actualizar(aid, estado='copiando', firma=analisis.firma(fila['ruta']),
                                    firma_desde=time.time(), progreso=None)
            return
        self.almacen.actualizar(
            aid, estado=rep['estado'], progreso=None, analizado=rep['analizado'],
            artista=rep['artista'], album=rep['album'], anio=rep['anio'],
            calidad=rep['calidad']['tag'], destino=rep['destino']['ruta'], n_pistas=rep['n_pistas'],
            conteo=rep['conteo_problemas'], reporte=rep, firma=firma_antes, silencioso=0)
        desatendido = aj.get('desatendido') == '1' and rep['estado'] == 'revisado'
        if desatendido:
            self.almacen.actualizar(aid, estado='por_ubicar')
            self.almacen.registrar(aid, 'Modo desatendido: pasa solo a ubicarse')
            if self.procesador:
                self.procesador.ya()
        if completo.get('silencioso') and not desatendido:
            return                      # re-revisión por un cambio tuyo: sin aviso
        titulo = f'{rep["artista"]} — {rep["album"]}'
        if rep['estado'] == 'revisado':
            texto = 'Revisión sin decisiones pendientes.'
        else:
            primeros = [p['texto'] for p in rep['problemas'] if p['nivel'] in ('error', 'decision')][:2]
            texto = ' · '.join(primeros)
        tipo = {'revisado': 'ok', 'requiere_revision': 'revision', 'error': 'error'}[rep['estado']]
        self.almacen.avisar(tipo, f'{titulo}: {NOMBRE_ESTADO[rep["estado"]].lower()}', texto, aid)
        self._tanda.append(rep['estado'])

    def _cerrar_tanda(self):
        if len(self._tanda) >= 2:
            n = len(self._tanda)
            ok = self._tanda.count('revisado')
            rev = self._tanda.count('requiere_revision')
            err = self._tanda.count('error')
            partes = [f'{ok} listos'] + ([f'{rev} requieren revisión'] if rev else []) + \
                     ([f'{err} con errores'] if err else [])
            self.almacen.avisar('resumen', f'Revisión terminada: {n} álbumes', ', '.join(partes))
        self._tanda = []
