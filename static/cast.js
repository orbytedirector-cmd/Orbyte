// ── "Reproducir en…" (UPnP/DLNA) — panel del admin ──────────────────────────
// Solo se carga para el admin (ver base.html). window._currentTrack ya lo
// expone player.js (lo usa el sistema de letras) — mismo dato, sin duplicar.
//
// window._castTarget: null cuando el audio sale por el dispositivo local, o
// {id, name} cuando salió elegido como salida. Elegir un dispositivo es una
// decisión de "a partir de ahora el audio sale por acá" — no hace falta que
// haya nada sonando ni en cola para elegirlo; player.js simplemente consulta
// este estado cada vez que hace algo (cambia de pista, pausa, busca, cambia
// el volumen) y actúa en consecuencia vía los hooks de más abajo.
//
// Mientras hay un dispositivo elegido, el <audio> local queda muteado con
// currentAudio.muted = true — es el MISMO mecanismo nativo que ya usa el
// botón de silenciar del reproductor (ver player.js), no un volume=0
// paralelo: así el valor de currentAudio.volume queda intacto (lo sigue
// manejando el slider con normalidad) y no hay forma de que se "filtre"
// audio local sin querer. El volumen real, mientras se transmite, lo maneja
// el dispositivo remoto por RenderingControl.
(function () {
  'use strict';

  let _targetsCache = [];
  let _seekDebounceTimer = null;
  let _volumeDebounceTimer = null;

  function _muteLocal() {
    // El cálculo real (silenciado a mano O transmitiendo) vive en
    // player.js — acá solo le avisamos que recalcule, así el botón de
    // silenciar del usuario y esto nunca se pisan entre sí.
    if (window._applyMuteState) window._applyMuteState();
  }

  function _setActiveButtonState(active) {
    const btn = document.getElementById('cast-btn');
    if (btn) btn.classList.toggle('active', !!active);
    // Mismo botón, replicado en el reproductor en primer plano (np-overlay)
    // — usa la clase que ya usan el resto de los np-action (fav, normalizar,
    // etc.) en vez de "active", que es la del botón chico de la barra.
    const npBtn = document.getElementById('np-cast-btn');
    if (npBtn) npBtn.classList.toggle('np-action-active', !!active);
  }

  // ── Reloj de pared para la posición mientras se transmite ────────────────
  // El <audio> local queda muteado mientras casteás. Un audio MUTEADO en una
  // pestaña de fondo NO recibe la misma excepción de throttling que le da el
  // navegador a un audio realmente audible (esa excepción es justo lo que la
  // pestaña pierde al no haber cast activo). En la práctica esto significa
  // que currentAudio.currentTime puede quedar directamente TRABADO en 2do
  // plano — no es que se refresque poco, el valor deja de avanzar del todo.
  // El dispositivo remoto (el que de verdad está sonando) no tiene ese
  // problema. Por eso, mientras hay cast activo, dejamos de confiar en
  // currentAudio para la posición y calculamos todo por reloj de pared
  // (Date.now()), que nunca se frena.
  let _castClock = null; // {startWallMs, pausedAtSec, isPaused, durationSec}

  function _castClockStart(durationSec, startAtSec) {
    let at = Number(startAtSec) || 0;
    let dur = Number(durationSec) || 0;
    if (startAtSec != null && !Number.isFinite(Number(startAtSec))) {
        console.warn('[cast] _castClockStart recibió startAtSec no numérico:', startAtSec);
    }
    if (durationSec != null && !Number.isFinite(Number(durationSec))) {
        console.warn('[cast] _castClockStart recibió durationSec no numérico (revisar track.duration):', durationSec);
    }
    _castClock = { startWallMs: Date.now() - at * 1000, pausedAtSec: at, isPaused: false, durationSec: dur };
  }
  function _castClockElapsed() {
    if (!_castClock) return 0;
    return _castClock.isPaused ? _castClock.pausedAtSec : (Date.now() - _castClock.startWallMs) / 1000;
  }
  function _castClockSetPaused(paused) {
    if (!_castClock) return;
    if (paused) {
      _castClock.pausedAtSec = _castClockElapsed();
      _castClock.isPaused = true;
    } else {
      _castClock.startWallMs = Date.now() - _castClock.pausedAtSec * 1000;
      _castClock.isPaused = false;
    }
  }
  function _castClockSeek(seconds) {
    if (!_castClock) return;
    _castClock.pausedAtSec = seconds;
    _castClock.startWallMs = Date.now() - seconds * 1000;
  }

  // ── Hooks que llama player.js en cada acción de transporte — acá es donde
  // el reproductor "sabe" si el audio sale local o transmitido, y actúa
  // distinto según corresponda. ───────────────────────────────────────────
  window._castMirror = {
    onTrackStart(track) {
      if (!window._castTarget || !track || !track.id) return;
      console.log('[cast] nueva pista, espejando a', window._castTarget.name, '->', track.title);
      _muteLocal();
      _castClockStart(track.duration || 0);
      _castSendTrack(track);
    },
    onPlayPause(playing) {
      if (!window._castTarget) return;
      _castClockSetPaused(!playing);
      console.log('[cast]', playing ? 'Play' : 'Pause', '->', window._castTarget.name);
      _castTransport(playing ? 'Play' : 'Pause');
    },
    onSeek(seconds) {
      if (!window._castTarget) return;
      _castClockSeek(seconds);
      clearTimeout(_seekDebounceTimer);
      _seekDebounceTimer = setTimeout(() => _castSeek(seconds), 350);
    },
    onVolumeChange(v) {
      if (!window._castTarget) return;
      clearTimeout(_volumeDebounceTimer);
      _volumeDebounceTimer = setTimeout(() => _castVolume(v), 150);
    },
    // Consultados por el watchdog de player.js en vez de currentAudio
    // mientras hay cast activo (ver nota del reloj de pared, más arriba).
    getElapsed()  { return _castClockElapsed(); },
    isEnded() {
      if (!_castClock || !_castClock.durationSec) return false;
      return _castClockElapsed() >= _castClock.durationSec - 0.5;
    },
  };

  async function _castSendTrack(track) {
    try {
      const r = await fetch('/api/admin/cast/play', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: window._castTarget.id, track_id: track.id }),
      }).then((r) => r.json());
      if (r.status !== 'ok') console.warn('[cast] no se pudo transmitir la pista:', r.message);
      else console.log('[cast] transmitiendo OK:', track.title);
      return r;
    } catch (e) {
      console.warn('[cast] error de red al transmitir la pista:', e);
      return { status: 'error', message: 'Error de red' };
    }
  }

  async function _castTransport(action) {
    try {
      const r = await fetch('/api/admin/cast/transport', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: window._castTarget.id, action }),
      }).then((r) => r.json());
      if (r.status !== 'ok') console.warn(`[cast] ${action} falló:`, r.message);
    } catch (e) { console.warn(`[cast] error de red en ${action}:`, e); }
  }

  async function _castSeek(seconds) {
    try {
      const r = await fetch('/api/admin/cast/seek', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: window._castTarget.id, seconds: Math.round(seconds) }),
      }).then((r) => r.json());
      if (r.status !== 'ok') console.warn('[cast] seek falló:', r.message);
    } catch (e) { console.warn('[cast] error de red en seek:', e); }
  }

  async function _castVolume(v) {
    try {
      const r = await fetch('/api/admin/cast/volume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: window._castTarget.id, volume: Math.round(v * 100) }),
      }).then((r) => r.json());
      if (r.status !== 'ok') console.warn('[cast] volumen: el dispositivo no lo soporta o falló:', r.message);
    } catch (e) { console.warn('[cast] error de red al cambiar volumen:', e); }
  }

  // Al conectar, en vez de IMPONERLE al dispositivo el volumen que tuviera
  // el slider local (podía dejarlo sonando muy fuerte o casi mudo la
  // primera vez), se lee el volumen que el dispositivo YA tiene configurado
  // (perilla física, control remoto, o lo que haya quedado de una sesión
  // anterior) y se refleja en el slider local. Se asigna el .value
  // directamente (sin pasar por setVolume()) para no disparar un
  // SetVolume de vuelta hacia el dispositivo con el valor que acabamos de
  // leer de ahí mismo.
  async function _castSyncVolumeFromDevice(targetId) {
    try {
      const r = await fetch(`/api/admin/cast/volume?target_id=${targetId}`).then((r) => r.json());
      if (r.status === 'ok' && typeof r.volume === 'number') {
        const v = Math.max(0, Math.min(100, r.volume)) / 100;
        const slider = document.getElementById('volume-slider');
        if (slider) slider.value = v;
        if (window.currentAudio) window.currentAudio.volume = v;
        console.log('[cast] volumen detectado en el dispositivo:', r.volume);
      } else {
        console.log('[cast] no se pudo leer el volumen del dispositivo — se mantiene el del slider local:', r.message || '');
      }
    } catch (e) {
      console.warn('[cast] error de red al leer el volumen del dispositivo:', e);
    }
  }

  // ── Heartbeat: reaccionar si el server se cae mientras se transmite ─────
  // El navegador NUNCA le habla directo al dispositivo UPnP en operación
  // normal — todos los comandos (Play/Pause/Stop/Seek/Volume) salen del
  // SERVER (ver las llamadas a fetch('/api/admin/cast/...') de arriba). Si
  // el server deja de responder, ya no hay forma FORMAL de pedirle "Stop"
  // al dispositivo. Lo que sí podemos hacer desde acá:
  //   1) Dejar de fingir que seguimos transmitiendo: limpiar el estado
  //      local de inmediato y avisar en el panel — esto es lo confiable.
  //   2) Como último recurso, intentar un SOAP Stop DIRECTO navegador ->
  //      dispositivo (sin pasar por el server), usando la control_url que
  //      ya tenemos cacheada de la última vez que se listaron los
  //      dispositivos. Esto es best-effort, NO garantizado: la mayoría de
  //      los renderers UPnP no implementan CORS ni responden bien un
  //      preflight, así que el navegador puede bloquear la respuesta antes
  //      de que le llegue — no hay forma de confirmarlo desde JS.
  // Un apagado PROLIJO del server (systemctl stop, redeploy, Ctrl+C, el
  // propio auto-reload del modo debug) ya manda su Stop desde el propio
  // server antes de morir (ver _cast_stop_active_on_shutdown en app.py) —
  // eso SÍ es confiable. Este heartbeat es el respaldo para cuando el
  // server se cae de golpe y no llega a avisar nada.
  const HEARTBEAT_MS = 8000;
  const HEARTBEAT_MAX_FAILS = 2;
  let _heartbeatTimer = null;
  let _heartbeatFails = 0;

  function _heartbeatStart() {
    if (_heartbeatTimer) return;
    _heartbeatFails = 0;
    _heartbeatTimer = setInterval(_heartbeatTick, HEARTBEAT_MS);
  }
  function _heartbeatStop() {
    clearInterval(_heartbeatTimer);
    _heartbeatTimer = null;
    _heartbeatFails = 0;
  }
  async function _heartbeatTick() {
    if (!window._castTarget) { _heartbeatStop(); return; }
    try {
      const r = await fetch('/api/admin/cast/targets', { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      _heartbeatFails = 0;
    } catch (e) {
      _heartbeatFails++;
      console.warn(`[cast] heartbeat sin respuesta del server (${_heartbeatFails}/${HEARTBEAT_MAX_FAILS}):`, e);
      if (_heartbeatFails >= HEARTBEAT_MAX_FAILS) _handleServerUnreachable();
    }
  }

  async function _handleServerUnreachable() {
    const target = window._castTarget;
    if (!target) { _heartbeatStop(); return; }
    const cached = _targetsCache.find((t) => t.id === target.id);
    _heartbeatStop();
    console.warn('[cast] el server no responde — cortando la transmisión localmente');
    window._castTarget = null;
    _castClock = null;
    _setActiveButtonState(false);
    _muteLocal();
    const list = document.getElementById('cast-list');
    if (list) {
      list.innerHTML = `<div class="cast-active-row" style="color:var(--led-red);border-color:var(--led-red)">
        <span>⚠️ Se perdió la conexión con el server — se cortó la transmisión a "${_escapeHtml(target.name)}"</span>
      </div>`;
    }
    // Best-effort: intento directo al dispositivo, sin pasar por el server
    // (ver nota grande más arriba — puede no llegar, no hay forma de saberlo).
    if (cached && cached.control_url) {
      try {
        await fetch(cached.control_url, {
          method: 'POST',
          mode: 'no-cors',
          headers: { 'Content-Type': 'text/xml; charset="utf-8"' },
          body: '<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
              + 's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
              + '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"><InstanceID>0</InstanceID></u:Stop>'
              + '</s:Body></s:Envelope>',
        });
      } catch (e) {
        console.warn('[cast] Stop directo al dispositivo tampoco llegó (esperable sin el server de por medio):', e);
      }
    }
  }

  // ── Panel: abrir/cerrar, listar, buscar ───────────────────────────────────
  // El mismo panel se abre desde dos botones que NO están en el mismo lugar
  // de la pantalla: #cast-btn (barra chica, abajo a la derecha) y
  // #np-cast-btn (reproductor en primer plano, centrado con el resto de los
  // np-action). Por eso el anclaje del panel depende de quién lo abrió — ver
  // #cast-panel / .cast-panel--centered en style.css.
  function toggleCastPanel(anchor) {
    const panel = document.getElementById('cast-panel');
    if (!panel) return;
    const opening = panel.style.display === 'none';
    if (opening) panel.classList.toggle('cast-panel--centered', anchor === 'centered');
    panel.style.display = opening ? 'block' : 'none';
    if (opening) loadCastTargets();
  }

  function _renderCastList(statusLine) {
    const list = document.getElementById('cast-list');
    if (!list) return;

    let html = '';
    if (window._castTarget) {
      html += `<div class="cast-active-row">
        <span>🔊 Salida: <strong>${_escapeHtml(window._castTarget.name)}</strong>${statusLine ? ' — ' + _escapeHtml(statusLine) : ''}</span>
        <button class="cast-stop-btn" onclick="castStop()">Volver a local</button>
      </div>`;
    }

    if (!_targetsCache.length) {
      html += '<span style="color:var(--text-muted);font-size:0.82rem;padding:0.5rem;display:block">'
        + 'Buscando dispositivos…</span>';
    } else {
      html += _targetsCache.map((t) => {
        const isActive = window._castTarget && window._castTarget.id === t.id;
        return `
        <div class="output-item${isActive ? ' cast-item-active' : ''}">
          <div class="output-item-info" onclick="castSelectTarget(${t.id})" style="cursor:pointer">
            <span class="output-name">${isActive ? '🔊 ' : ''}${_escapeHtml(t.name)}</span>
            <span class="output-status" style="color:var(--text-muted)">${_escapeHtml(t.model_name || t.ip || '')}</span>
          </div>
          <button class="cast-remove-btn" onclick="castRemoveTarget(${t.id})" title="Quitar de la lista">✕</button>
        </div>`;
      }).join('');
    }
    list.innerHTML = html;
  }

  async function loadCastTargets() {
    try {
      _targetsCache = await fetch('/api/admin/cast/targets').then((r) => r.json());
      if (!Array.isArray(_targetsCache)) _targetsCache = [];
    } catch (e) {
      console.warn('[cast] no se pudo cargar la lista de dispositivos:', e);
      _targetsCache = [];
    }
    _renderCastList();
  }

  async function castDiscover(silent) {
    const btn = document.getElementById('cast-discover-btn');
    if (btn && !silent) { btn.disabled = true; btn.textContent = '🔍 Buscando… (unos segundos)'; }
    try {
      const r = await fetch('/api/admin/cast/discover', { method: 'POST' }).then((r) => r.json());
      console.log(`[cast] búsqueda terminada, ${r.found ?? '?'} dispositivo(s) con AVTransport`);
    } catch (e) {
      console.warn('[cast] búsqueda falló:', e);
      if (!silent) alert('No se pudo completar la búsqueda.');
    } finally {
      if (btn && !silent) { btn.disabled = false; btn.textContent = '🔍 Buscar dispositivos'; }
      loadCastTargets();
    }
  }

  // ── Elegir dispositivo = "el audio sale por acá a partir de ahora". No
  // hace falta tener nada cargado ni en cola: si ya hay algo sonando, lo
  // transmite de inmediato para que el cambio se sienta instantáneo; si no
  // hay nada, sólo queda "armado" — la próxima vez que se elija una pista,
  // el hook onTrackStart ya sabe mandarla para allá. ─────────────────────
  async function castSelectTarget(targetId) {
    const target = _targetsCache.find((t) => t.id === targetId);
    if (!target) return;
    if (window._castTarget && window._castTarget.id === targetId) return; // ya está elegido

    if (!confirm(`¿Transmitir a "${target.name}"? A partir de ahora, todo lo que reproduzcas va a sonar ahí.`)) return;

    // Si había otro dispositivo activo, avisarle que pare antes de cambiar.
    if (window._castTarget) await _castTransport('Stop');

    window._castTarget = { id: target.id, name: target.name };
    _setActiveButtonState(true);
    _muteLocal();
    _renderCastList('conectando…');
    _heartbeatStart();

    // Detectar el volumen que el dispositivo ya tiene configurado ANTES de
    // mandarle la pista, para no competir con SetAVTransportURI+Play.
    await _castSyncVolumeFromDevice(target.id);

    const track = window._currentTrack;
    if (track && track.id) {
      const startPos = (window.currentAudio && window.currentAudio.currentTime > 1) ? window.currentAudio.currentTime : 0;
      _castClockStart(track.duration || 0, startPos);
      // Puede tardar unos segundos (si es DSD, el server prueba varios
      // formatos hasta encontrar el que el dispositivo acepta — ver
      // _cast_try_send_track en app.py) — por eso el "conectando…" de arriba.
      const r = await _castSendTrack(track);
      if (r.status !== 'ok') {
        alert(r.message || 'No se pudo transmitir a ese dispositivo.');
        _castClock = null;
        _renderCastList();
        return;
      }
      if (startPos > 0) _castSeek(startPos);
      if (window.currentAudio && window.currentAudio.paused) { _castClockSetPaused(true); _castTransport('Pause'); }
    }
    _renderCastList();
  }

  function castStop() {
    if (window._castTarget) _castTransport('Stop');
    window._castTarget = null;
    _castClock = null;
    _setActiveButtonState(false);
    _muteLocal();
    _heartbeatStop();
    _renderCastList();
  }

  async function castRemoveTarget(targetId) {
    if (!confirm('¿Quitar este dispositivo de la lista? (lo podés volver a agregar buscando de nuevo)')) return;
    if (window._castTarget && window._castTarget.id === targetId) castStop();
    try {
      await fetch(`/api/admin/cast/targets/${targetId}`, { method: 'DELETE' });
    } catch (e) { /* noop */ }
    loadCastTargets();
  }

  function _escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  window.toggleCastPanel   = toggleCastPanel;
  window.castDiscover      = castDiscover;
  window.castSelectTarget  = castSelectTarget;
  window.castStop          = castStop;
  window.castRemoveTarget  = castRemoveTarget;

  // Cada vez que se abre la app (carga real de página, no navegación SPA
  // interna) se busca en segundo plano — así la lista ya está poblada
  // cuando se abre el panel, sin tener que buscar a mano primero. El botón
  // "Buscar dispositivos" del panel sigue estando para forzar un
  // re-escaneo si algo nuevo se sumó a la red en el medio.
  loadCastTargets();
  castDiscover(true);
})();
