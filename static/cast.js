// ── "Reproducir en…" (UPnP/DLNA) — panel del admin ──────────────────────────
// Solo se carga para el admin (ver base.html). Usa window._currentTrack, que
// player.js ya expone para el sistema de letras — mismo dato, sin duplicar
// nada.
(function () {
  'use strict';

  function toggleCastPanel() {
    const panel = document.getElementById('cast-panel');
    if (!panel) return;
    const opening = panel.style.display === 'none';
    panel.style.display = opening ? 'block' : 'none';
    if (opening) loadCastTargets();
  }

  async function loadCastTargets() {
    const list = document.getElementById('cast-list');
    if (!list) return;
    let targets;
    try {
      targets = await fetch('/api/admin/cast/targets').then((r) => r.json());
    } catch (e) {
      list.innerHTML = '<span style="color:var(--led-red);font-size:0.82rem;padding:0.5rem;display:block">Error al cargar dispositivos.</span>';
      return;
    }
    if (!Array.isArray(targets) || !targets.length) {
      list.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem;padding:0.5rem;display:block">'
        + 'Sin dispositivos guardados todavía — apretá "Buscar dispositivos".</span>';
      return;
    }
    list.innerHTML = targets.map((t) => `
      <div class="output-item">
        <div class="output-item-info">
          <span class="output-name">${_escapeHtml(t.name)}</span>
          <span class="output-status" style="color:var(--text-muted)">${_escapeHtml(t.model_name || t.ip || '')}</span>
        </div>
        <button class="cast-play-btn" onclick="castPlayHere(${t.id})" title="Reproducir la pista actual acá">▶</button>
        <button class="cast-remove-btn" onclick="castRemoveTarget(${t.id})" title="Quitar de la lista">✕</button>
      </div>
    `).join('');
  }

  async function castDiscover() {
    const btn = document.getElementById('cast-discover-btn');
    const list = document.getElementById('cast-list');
    if (btn) { btn.disabled = true; btn.textContent = '🔍 Buscando… (unos segundos)'; }
    try {
      const r = await fetch('/api/admin/cast/discover', { method: 'POST' }).then((r) => r.json());
      if (list && r.status === 'ok' && r.found === 0) {
        list.innerHTML = '<span style="color:var(--text-muted);font-size:0.82rem;padding:0.5rem;display:block">'
          + 'No respondió ningún dispositivo. Confirmá que estén prendidos y en una entrada de red.</span>';
      }
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
    try {
      const r = await fetch('/api/admin/cast/play', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_id: targetId, track_id: track.id }),
      }).then((r) => r.json());
      if (r.status === 'ok') {
        alert(`Sonando en ${r.device}.`);
      } else {
        alert(r.message || 'No se pudo transmitir a ese dispositivo.');
      }
    } catch (e) {
      alert('No se pudo conectar con el servidor.');
    }
  }

  async function castRemoveTarget(targetId) {
    if (!confirm('¿Quitar este dispositivo de la lista? (lo podés volver a agregar buscando de nuevo)')) return;
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

  window.toggleCastPanel = toggleCastPanel;
  window.castDiscover    = castDiscover;
  window.castPlayHere    = castPlayHere;
  window.castRemoveTarget = castRemoveTarget;
})();
