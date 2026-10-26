"""
Orbyte Ingesta — cola de proceso (etapas 2 y 3), un álbum a la vez.

  por_ubicar → ubicando → ubicado → por_metadata → metadata → completado
                     ↘ error_ubicar               ↘ error_metadata

Va aparte del vigilante: mientras un álbum se ubica o corre su metadata, la revisión
de los demás sigue.
"""
import json
import logging
import os
import shutil
import threading
import time
import traceback

from . import ajustar as ajustar_mod
from . import analisis, artistas, metadata, procesar, recursos, sacd
from .almacen import PREVIOS

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
                if artistas.BLOQUEO.locked():          # se está unificando un artista: esperar
                    self.despertar.wait(10)
                    self.despertar.clear()
                    continue
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
                aid = self.almacen.siguiente('por_ajustar')      # ajustes de tags en la biblioteca: segundos
                if aid:
                    self.ajustar(aid)
                    continue
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
        for a in self.almacen.con_estado('ajustando'):
            # cada renombre ya quedó anotado en music.db: se relee todo con la metadata
            self.almacen.registrar(a['id'], 'El servicio se reinició mientras se ajustaban los tags: '
                                            'se corre la metadata para dejar la base al día; revisa los tags')
            self.almacen.actualizar(a['id'], estado='por_metadata', progreso=None)

    # ── etapa 2 ──
    def ubicar(self, aid):
        a = self.almacen.album(aid)
        rep = a.get('reporte') or {}
        aj = self.almacen.ajustes()
        if not self.almacen.cambiar_estado(aid, 'por_ubicar', 'ubicando', progreso='Ubicando…', registro=None):
            return                                   # lo sacaste de la cola justo ahora
        self.actual = aid

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
        es_iso = bool(rep.get('origen_entrada'))
        if es_iso:
            self._cerrar_iso(aid, rep, aj, reg)
        self.almacen.actualizar(aid, estado='por_metadata' if aj.get('metadata', '1') == '1' else 'completado',
                                ubicado_en=destino, progreso=None, ubicado=time.time(),
                                terminado=None if aj.get('metadata', '1') == '1' else time.time(),
                                # una ISO conserva su ruta de la entrada: así no vuelve a aparecer como nueva
                                **({} if es_iso else {'ruta': destino}))
        self.almacen.avisar('ok', f'{rep.get("artista")} — {rep.get("album")}: ubicado en la biblioteca',
                            os.path.relpath(destino, aj.get('biblioteca') or '/'), aid)
        if rep.get('_rehechas'):
            self.almacen.avisar('revision', f'{rep.get("artista")} — {rep.get("album")}: '
                                            f'{len(rep["_rehechas"])} pista(s) se copiaron de nuevo',
                                'La primera copia no coincidió con el original y la segunda sí: '
                                + ', '.join(rep['_rehechas'][:3]), aid)
        self.actual = None

    def _cerrar_iso(self, aid, rep, aj, reg):
        """Tras ubicar un área de una ISO: la ISO se borra de la entrada (si así está configurado) solo cuando
        no queda otra área de la misma ISO por ubicar; la extracción local se borra siempre."""
        real = rep['origen_entrada']
        datos = os.path.dirname(self.almacen.ruta)
        area_dir = os.path.dirname(rep.get('carpeta') or '')
        if area_dir.startswith(os.path.join(datos, 'sacd') + os.sep) and os.path.isdir(area_dir):
            shutil.rmtree(area_dir, ignore_errors=True)
        pendientes = [x for x in self.almacen.albumes() if x['id'] != aid and sacd.separar(x['ruta'])[0] == real
                      and x['estado'] in PREVIOS + ('por_ubicar', 'ubicando', 'error_ubicar')
                      and x['estado'] != 'descartado']
        if aj.get('borrar_origen', '1') != '1':
            return
        if pendientes:
            reg('La ISO queda en la entrada hasta que la otra área también esté ubicada')
            return
        try:
            procesar.borrar_de_entrada(real, aj.get('carpeta_entrada'))
            sacd.limpiar(datos, entrada=real)
            reg('ISO original borrada de la carpeta de entrada')
        except Exception as e:
            reg(f'No se pudo borrar la ISO de la entrada: {e}')

    # ── ajustar tags de un álbum ya ubicado ──
    def ajustar(self, aid):
        a = self.almacen.album(aid)
        rep = a.get('reporte') or {}
        cam = dict(a.get('cambios') or {})
        pedido = cam.pop('biblioteca', None) or {}
        volver = pedido.get('volver') or 'completado'
        nombre = _nombre(a)
        if pedido.get('lote'):
            cam['lote'] = True                       # la metadata de un lote tampoco avisa cuando sale bien
        aj = self.almacen.ajustes()
        ruta = a['ubicado_en'] or a['ruta']
        if not self.almacen.cambiar_estado(aid, 'por_ajustar', 'ajustando', progreso='Ajustando tags…',
                                           registro=None, cambios=json.dumps(cam, ensure_ascii=False)):
            return
        self.actual = aid

        def reg(m):
            self.almacen.registrar(aid, m)
            self.almacen.actualizar(aid, progreso=m.strip()[:120])

        try:
            reg(f'Ajuste en la biblioteca: {os.path.relpath(ruta, aj.get("biblioteca") or "/")}')
            final = ajustar_mod.aplicar(ruta, pedido, aj, reg)
        except Exception as e:
            log.error('Ajuste %s:\n%s', aid, traceback.format_exc())
            reg(f'ERROR: {e}' if isinstance(e, ajustar_mod.FalloAjuste) else f'ERROR: {type(e).__name__}: {e}')
            # lo hecho hasta el error ya quedó anotado en music.db; la metadata relee el resto
            self.almacen.actualizar(aid, estado='por_metadata' if aj.get('metadata', '1') == '1' else volver,
                                    progreso=None)
            self.almacen.avisar('error', f'No se pudieron ajustar los tags de {nombre}', str(e).splitlines()[0], aid)
            self.actual = None
            return
        campos = {'ubicado_en': final}
        cam_fin = dict((self.almacen.album(aid) or {}).get('cambios') or {}, richmeta_completo=True)
        campos['cambios'] = json.dumps(cam_fin, ensure_ascii=False)
        if not rep.get('origen_entrada'):            # una ISO conserva su ruta de la entrada
            campos['ruta'] = final
        self.almacen.actualizar(aid, estado='por_metadata' if aj.get('metadata', '1') == '1' else volver,
                                progreso=None, **campos)
        if not pedido.get('lote'):
            self.almacen.avisar('ok', f'{nombre}: tags ajustados', os.path.relpath(final, aj.get('biblioteca') or '/'),
                                aid)
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
            resultado = metadata.correr(a['ubicado_en'] or a['ruta'], aj, reg,
                                        richmeta_completo=bool((a.get('cambios') or {}).get('richmeta_completo')))
        except Exception as e:
            log.error('Metadata %s:\n%s', aid, traceback.format_exc())
            reg(f'ERROR: {type(e).__name__}: {e}')
            self.almacen.actualizar(aid, estado='error_metadata', progreso=None)
            self.almacen.avisar('error', f'Falló la metadata de {_nombre(a)}', str(e), aid)
            self.actual = None
            return
        ok = resultado.get('ok')
        cam = a.get('cambios') or {}
        lote = cam.pop('lote', None)
        limpiar = cam.pop('richmeta_completo', None) or lote
        self.almacen.actualizar(aid, estado='completado' if ok else 'error_metadata', progreso=None,
                                resultado=resultado, terminado=time.time(),
                                **({'cambios': json.dumps(cam, ensure_ascii=False)} if limpiar else {}))
        if ok and lote:
            pass
        elif ok:
            txt = 'Listo en Orbyte' + (f' · {len(resultado.get("advertencias", []))} advertencias'
                                       if resultado.get('advertencias') else '')
            self.almacen.avisar('ok', f'{_nombre(a)}: completo', txt, aid)
        else:
            self.almacen.avisar('error', f'{_nombre(a)}: la metadata quedó incompleta',
                                '; '.join(resultado.get('errores', [])[:2]), aid)
        self.actual = None


def _nombre(a):
    rep = a.get('reporte') or {}
    return f'{rep.get("artista") or a.get("artista") or "?"} — {rep.get("album") or a.get("album") or a.get("rel")}'


def _firma(a):
    import json
    try:
        return json.loads(a.get('firma') or '[]')
    except ValueError:
        return None
