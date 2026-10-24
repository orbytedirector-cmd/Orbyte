"""
Orbyte Ingesta — cola de proceso (etapas 2 y 3), un álbum a la vez.

  por_ubicar → ubicando → ubicado → por_metadata → metadata → completado
                     ↘ error_ubicar               ↘ error_metadata

Va aparte del vigilante: mientras un álbum se ubica o corre su metadata, la revisión
de los demás sigue.
"""
import logging
import os
import shutil
import threading
import time
import traceback

from . import analisis, metadata, procesar, recursos

log = logging.getLogger('ingesta')


class Procesador(threading.Thread):
    def __init__(self, almacen):
        super().__init__(daemon=True, name='procesador')
        self.almacen = almacen
        self.despertar = threading.Event()
        self.actual = None

    def ya(self):
        self.despertar.set()

    def biblioteca_lista(self):
        """La biblioteca existe y se puede escribir (tras un reinicio el disco de red puede tardar en montarse)."""
        bib = self.almacen.ajustes().get('biblioteca') or '/mnt/musica'
        try:
            return os.path.isdir(bib) and os.access(bib, os.W_OK) and bool(os.listdir(bib))
        except OSError:
            return False

    def run(self):
        recuperado = False
        avisado = False
        while True:
            try:
                if not self.biblioteca_lista():
                    # sin biblioteca no se toca la cola: los álbumes esperan, no se marcan con error
                    if not avisado and (self.almacen.siguiente('por_ubicar') or self.almacen.siguiente('por_metadata')):
                        log.warning('La biblioteca no está disponible: la cola de proceso espera')
                        avisado = True
                    self.despertar.wait(20)
                    self.despertar.clear()
                    continue
                avisado = False
                if not recuperado:
                    self._recuperar()
                    recuperado = True
                trabajo = False
                aid = self.almacen.siguiente('por_metadata')     # primero terminar lo ya ubicado
                if aid:
                    self.metadata(aid)
                    trabajo = True
                else:
                    aid = self.almacen.siguiente('por_ubicar')
                    if aid:
                        self.ubicar(aid)
                        trabajo = True
                if trabajo:
                    descanso = recursos.pausa(self.almacen.ajustes())
                    if descanso and not self.almacen.siguiente('por_metadata'):
                        # entre un álbum y el siguiente el procesador descansa (Ajustes → uso del procesador)
                        self.despertar.wait(descanso)
                        self.despertar.clear()
                    continue
            except Exception:
                log.error('Error en el procesador:\n%s', traceback.format_exc())
            self.despertar.wait(15)
            self.despertar.clear()

    def _recuperar(self):
        """Tras un reinicio del servicio: limpia temporales y deja cada álbum en un estado consistente."""
        aj = self.almacen.ajustes()
        tmp = os.path.join(aj.get('biblioteca') or '/mnt/musica', '.ingesta_tmp')
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        for a in self.almacen.con_estado('ubicando'):
            completo = self.almacen.album(a['id'])
            destino = (completo.get('reporte') or {}).get('destino', {}).get('ruta')
            if destino and os.path.isdir(destino) and not os.path.isdir(completo['ruta']):
                self.almacen.actualizar(a['id'], estado='por_metadata', ruta=destino, ubicado_en=destino)
            else:
                self.almacen.actualizar(a['id'], estado='error_ubicar', progreso=None)
                self.almacen.registrar(a['id'], 'El servicio se reinició mientras se ubicaba: no se movió nada; '
                                                'vuelve a presionar «Ubicar»')
        for a in self.almacen.con_estado('metadata'):
            self.almacen.actualizar(a['id'], estado='por_metadata', progreso=None)

    # ── etapa 2 ──
    def ubicar(self, aid):
        a = self.almacen.album(aid)
        rep = a.get('reporte') or {}
        aj = self.almacen.ajustes()
        self.actual = aid
        self.almacen.actualizar(aid, estado='ubicando', progreso='Ubicando…', registro=None)

        def reg(m):
            self.almacen.registrar(aid, m)
            self.almacen.actualizar(aid, progreso=m.strip()[:120])

        try:
            if analisis.firma(a['ruta']) != _firma(a):
                raise procesar.FalloProceso('La carpeta cambió desde la revisión: se vuelve a revisar')
            reg(f'Etapa 2: {rep.get("artista")} — {rep.get("album")}')
            trab = recursos.nucleos(aj)
            destino = procesar.ubicar(rep, aj, reg, borrar_origen=aj.get('borrar_origen', '1') == '1',
                                      trabajadores=trab)
        except procesar.FalloProceso as e:
            cambio = 'cambió desde la revisión' in str(e)
            reg(f'ERROR: {e}')
            self.almacen.actualizar(aid, estado='en_cola' if cambio else 'error_ubicar', progreso=None)
            if not cambio:
                self.almacen.avisar('error', f'No se pudo ubicar {rep.get("artista")} — {rep.get("album")}',
                                    str(e).splitlines()[0], aid)
            self.actual = None
            return
        self.almacen.actualizar(aid, estado='por_metadata' if aj.get('metadata', '1') == '1' else 'completado',
                                ruta=destino, ubicado_en=destino, progreso=None,
                                terminado=None if aj.get('metadata', '1') == '1' else time.time())
        self.almacen.avisar('ok', f'{rep.get("artista")} — {rep.get("album")}: ubicado en la biblioteca',
                            os.path.relpath(destino, aj.get('biblioteca') or '/'), aid)
        self.actual = None

    # ── etapa 3 ──
    def metadata(self, aid):
        a = self.almacen.album(aid)
        rep = a.get('reporte') or {}
        aj = self.almacen.ajustes()
        self.actual = aid
        self.almacen.actualizar(aid, estado='metadata', progreso='Metadata…')

        def reg(m):
            self.almacen.registrar(aid, m)
            self.almacen.actualizar(aid, progreso=m.strip()[:120])

        try:
            resultado = metadata.correr(a['ubicado_en'] or a['ruta'], aj, reg)
        except Exception as e:
            log.error('Metadata %s:\n%s', aid, traceback.format_exc())
            reg(f'ERROR: {type(e).__name__}: {e}')
            self.almacen.actualizar(aid, estado='error_metadata', progreso=None)
            self.almacen.avisar('error', f'Falló la metadata de {rep.get("artista")} — {rep.get("album")}', str(e), aid)
            self.actual = None
            return
        ok = resultado.get('ok')
        self.almacen.actualizar(aid, estado='completado' if ok else 'error_metadata', progreso=None,
                                resultado=resultado, terminado=time.time())
        if ok:
            txt = 'Listo en Orbyte' + (f' · {len(resultado.get("advertencias", []))} advertencias'
                                       if resultado.get('advertencias') else '')
            self.almacen.avisar('ok', f'{rep.get("artista")} — {rep.get("album")}: completo', txt, aid)
        else:
            self.almacen.avisar('error', f'{rep.get("artista")} — {rep.get("album")}: la metadata quedó incompleta',
                                '; '.join(resultado.get('errores', [])[:2]), aid)
        self.actual = None


def _firma(a):
    import json
    try:
        return json.loads(a.get('firma') or '[]')
    except ValueError:
        return None
