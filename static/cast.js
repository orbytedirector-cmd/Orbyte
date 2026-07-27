// ── "Reproducir en…" (UPnP/DLNA) — panel del admin ──────────────────────────
// Solo se carga para el admin (ver base.html). window._currentTrack ya lo
// expone player.js (lo usa el sistema de letras) — mismo dato, sin duplicar.
//
// window._castTarget: null cuando no hay transmisión activa, o
// {id, name} del dispositivo. Mientras está seteado:
//   - el <audio> local queda muteado (volume=0) — sigue "sonando" en
//     silencio nada más que para que toda la lógica existente de progreso/
//     fin-de-pista/auto-avance de player.js siga funcionando sin tocarla.
//   - window._castMirror (definido acá abajo) espeja cada cambio de pista,
//     play/pause y seek al dispositivo — así el reproductor y la
//     transmisión son UNA sola cosa, no dos independientes.
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

  // ── Hooks que llama player.js en cada acción de transporte ────────────────
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
      // Debounce: arrastrar la barra de progreso dispara esto muchas veces
      // por segundo — no tiene sentido mandarle un SOAP call al receiver por
      // cada pixel de arrastre.
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
      if (r.status !== 'ok') {
        console.warn('[cast] no se pudo transmitir la pista:', r.message);
      }
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
    } catch (e) { /* noop — no interrumpir la UI local por esto */ }
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

  // ── Panel: abrir/cerrar, listar, buscar, empezar/parar transmisión ───────
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
        <span>🔊 Transmitiendo a <strong>${_escapeHtml(window._castTarget.name)}</strong></span>
        <button class="cast-stop-btn" onclick="castStop()">Detener</button>
      </div>`;
    }

    if (!_targetsCache.length) {
      html += '<span style="color:var(--text-muted);font-size:0.82rem;padding:0.5rem;display:block">'
        + 'Sin dispositivos guardados todavía — apretá "Buscar dispositivos".</span>';
    } else {
      html += _targetsCache.map((t) => `
        <div class="output-item">
          <div class="output-item-info">
            <span class="output-name">${_escapeHtml(t.name)}</span>
            <span class="output-status" style="color:var(--text-muted)">${_escapeHtml(t.model_name || t.ip || '')}</span>
          </div>
          <button class="cast-play-btn" onclick="castPlayHere(${t.id})" title="Transmitir la pista actual acá">▶</button>
          <button class="cast-remove-btn" onclick="castRemoveTarget(${t.id})" title="Quitar de la lista">✕</button>
        </div>
      `).join('');
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

  async function castDiscover() {
    const btn = document.getElementById('cast-discover-btn');
    if (btn) { btn.disabled = true; btn.textContent = '🔍 Buscando… (unos segundos)'; }
    try {
      await fetch('/api/admin/cast/discover', { method: 'POST' });
    } catch (e) {
      alert('No se pudo completar la búsqueda.');
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = '🔍 Buscar dispositivos'; }
      loadCastTargets();
    }
  }

  async function castPlayHere(targetId) {
    const track = window._currentTrack;
    if (!track || !track.id) {
      alert('Reproducí algo primero — necesito saber qué pista mandar.');
      return;
    }
    const target = _targetsCache.find((t) => t.id === targetId);
    if (!target) return;

    window._castTarget = { id: target.id, name: target.name };
    _setActiveButtonState(true);
    _muteLocal(true);
    _renderCastList();

    const r = await (async () => {
      try {
        return await fetch('/api/admin/cast/play', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target_id: target.id, track_id: track.id }),
        }).then((r) => r.json());
      } catch (e) {
        return { status: 'error', message: 'No se pudo conectar con el servidor.' };
      }
    })();

    if (r.status !== 'ok') {
      alert(r.message || 'No se pudo transmitir a ese dispositivo.');
      // No dejar el estado "activo" mintiendo si el primer envío falló.
      window._castTarget = null;
      _setActiveButtonState(false);
      _muteLocal(false);
      _renderCastList();
      return;
    }
    // Arrancar desde donde esté el audio local (si ya venía sonando).
    if (window.currentAudio && window.currentAudio.currentTime > 1) {
      _castSeek(window.currentAudio.currentTime);
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

  window.toggleCastPanel  = toggleCastPanel;
  window.castDiscover     = castDiscover;
  window.castPlayHere     = castPlayHere;
  window.castStop         = castStop;
  window.castRemoveTarget = castRemoveTarget;
})();
