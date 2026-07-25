// ── Playlist colaborativa: panel del anfitrión (/admin/colaborativa) ───────
(function () {
  'use strict';

  async function refreshEstado() {
    let data;
    try {
      data = await fetch('/api/admin/colaborativa/estado').then((r) => r.json());
    } catch (e) {
      return;
    }
    if (!data.active) return;
    const countEl = document.getElementById('collab-participant-count');
    const pendingEl = document.getElementById('collab-pending-count');
    const listEl = document.getElementById('collab-participant-list');
    if (countEl) countEl.textContent = data.participants.length;
    if (pendingEl) pendingEl.textContent = data.pending_count;
    if (listEl) {
      listEl.innerHTML = data.participants.length
        ? data.participants.map((p) =>
            `<li><span>${p.name}</span><span style="color:var(--text-muted)">${p.joined_at}</span></li>`
          ).join('')
        : '<li style="color:var(--text-muted)">Todavía nadie se unió.</li>';
    }
  }

  window.collabPullQueue = async function () {
    let tracks;
    try {
      tracks = await fetch('/api/admin/colaborativa/cola-pendiente').then((r) => r.json());
    } catch (e) {
      alert('No se pudo conectar con el servidor.');
      return;
    }
    if (!Array.isArray(tracks) || !tracks.length) {
      alert('No hay pistas nuevas para cargar.');
      return;
    }
    // Se agregan una por una a TU cola local con la función que ya existe
    // (appendToQueue) — desde ahí las manejás con total libertad, como
    // cualquier otra pista (reordenar/quitar/reproducir ya lo hace el
    // playlist-panel de siempre, no hace falta tocarlo).
    tracks.forEach((t) => { if (window.appendToQueue) window.appendToQueue(t); });
    alert(`Se cargaron ${tracks.length} pista(s) nueva(s) a tu playlist.`);
    refreshEstado();
  };

  if (document.getElementById('collab-participant-list')) {
    refreshEstado();
    setInterval(refreshEstado, 6000);
  }
})();
