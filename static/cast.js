// ── "Reproducir en…" (UPnP/DLNA) — panel del admin ──────────────────────────
// Solo se carga para el admin (ver base.html). window._currentTrack ya lo
// expone player.js (lo usa el sistema de letras) — mismo dato, sin duplicar.
//
// window._castTarget: null cuando el audio sale por el dispositivo local, o
// {id, name} cuando salió elegido como salida. Elegir un dispositivo es una
// decisión de "a partir de ahora el audio sale por acá" — no hace falta que
// haya nada sonando ni en cola para elegirlo; player.js simplemente consulta
// este estado cada vez que hace algo (cambia de pista, pausa, busca) y actúa
// en consecuencia vía los hooks de más abajo. Mientras hay un dispositivo
// elegido, el <audio> local queda muteado (volume=0): sigue "sonando" en
// silencio nada más para que toda la lógica existente de progreso/fin-de-
// pista/auto-avance siga funcionando sin tocarla.
(function () {
  'use strict';

  let _targetsCache = [];
  let _seekDebounceTimer = null;

  function _muteLocal(mute) {
    if (window.currentAudio) window.currentAudio.volume = mute ? 0 : 1;
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
      _muteLocal(true);
      _castSendTrack(track);
    },
    onPlayPause(playing) {
      if (!window._castTarget) return;
      _castTransport(playing ? 'Play' : 'Pause');
    },
    onSeek(seconds) {
      if (!window._castTarget) return;
      clearTimeout(_seekDebounceTimer);
      _seekDebounceTimer = setTimeout(() => _castSeek(seconds), 350);
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
    } catch (e) {
      console.warn('[cast] error de red al transmitir la pista:', e);
    }
  }

  async function _castTransport(action) {
    try {
      await fetch('/api/admin/cast/transport', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: window._castTarget.id, action }),
      });
    } catch (e) { /* noop */ }
  }

  async function _castSeek(seconds) {
    try {
      await fetch('/api/admin/cast/seek', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: window._castTarget.id, seconds: Math.round(seconds) }),
      });
    } catch (e) { /* noop */ }
  }

  // ── Panel: abrir/cerrar, listar, buscar ───────────────────────────────────
  function toggleCastPanel() {
    const panel = document.getElementById('cast-panel');
    if (!panel) return;
    const opening = panel.style.display === 'none';
    panel.style.display = opening ? 'block' : 'none';
    if (opening) loadCastTargets();
  }

  function _renderCastList() {
    const list = document.getElementById('cast-list');
    if (!list) return;

    let html = '';
    if (window._castTarget) {
      html += `<div class="cast-active-row">
        <span>🔊 Salida: <strong>${_escapeHtml(window._castTarget.name)}</strong></span>
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
      _targetsCache = [];
    }
    _renderCastList();
  }

  async function castDiscover(silent) {
    const btn = document.getElementById('cast-discover-btn');
    if (btn && !silent) { btn.disabled = true; btn.textContent = '🔍 Buscando… (unos segundos)'; }
    try {
      await fetch('/api/admin/cast/discover', { method: 'POST' });
    } catch (e) {
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
    _muteLocal(true);
    _renderCastList();

    // Si ya hay algo cargado (sonando o pausado), lo manda ya mismo para
    // que el cambio de salida sea inmediato — sin esto habría que esperar
    // a la próxima pista para escuchar algo en el dispositivo nuevo.
    const track = window._currentTrack;
    if (track && track.id) {
      await _castSendTrack(track);
      if (window.currentAudio && window.currentAudio.currentTime > 1) {
        _castSeek(window.currentAudio.currentTime);
      }
      if (window.currentAudio && window.currentAudio.paused) _castTransport('Pause');
    }
  }

  function castStop() {
    if (window._castTarget) _castTransport('Stop');
    window._castTarget = null;
    _setActiveButtonState(false);
    _muteLocal(false);
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
