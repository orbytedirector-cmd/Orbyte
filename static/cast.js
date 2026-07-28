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
  }

  // ── Hooks que llama player.js en cada acción de transporte — acá es donde
  // el reproductor "sabe" si el audio sale local o transmitido, y actúa
  // distinto según corresponda. ───────────────────────────────────────────
  window._castMirror = {
    onTrackStart(track) {
      if (!window._castTarget || !track || !track.id) return;
      console.log('[cast] nueva pista, espejando a', window._castTarget.name, '->', track.title);
      _muteLocal();
      _castSendTrack(track);
    },
    onPlayPause(playing) {
      if (!window._castTarget) return;
      console.log('[cast]', playing ? 'Play' : 'Pause', '->', window._castTarget.name);
      _castTransport(playing ? 'Play' : 'Pause');
    },
    onSeek(seconds) {
      if (!window._castTarget) return;
      clearTimeout(_seekDebounceTimer);
      _seekDebounceTimer = setTimeout(() => _castSeek(seconds), 350);
    },
    onVolumeChange(v) {
      if (!window._castTarget) return;
      clearTimeout(_volumeDebounceTimer);
      _volumeDebounceTimer = setTimeout(() => _castVolume(v), 150);
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

  // ── Panel: abrir/cerrar, listar, buscar ───────────────────────────────────
  function toggleCastPanel() {
    const panel = document.getElementById('cast-panel');
    if (!panel) return;
    const opening = panel.style.display === 'none';
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

    const track = window._currentTrack;
    if (track && track.id) {
      // Puede tardar unos segundos (si es DSD, el server prueba varios
      // formatos hasta encontrar el que el dispositivo acepta — ver
      // _cast_try_send_track en app.py) — por eso el "conectando…" de arriba.
      const r = await _castSendTrack(track);
      if (r.status !== 'ok') {
        alert(r.message || 'No se pudo transmitir a ese dispositivo.');
        _renderCastList();
        return;
      }
      if (window.currentAudio && window.currentAudio.currentTime > 1) {
        _castSeek(window.currentAudio.currentTime);
      }
      const slider = document.getElementById('volume-slider');
      if (slider) _castVolume(parseFloat(slider.value));
      if (window.currentAudio && window.currentAudio.paused) _castTransport('Pause');
    }
    _renderCastList();
  }

  function castStop() {
    if (window._castTarget) _castTransport('Stop');
    window._castTarget = null;
    _setActiveButtonState(false);
    _muteLocal();
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
