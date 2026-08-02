// ── Playlist colaborativa: panel del anfitrión (/admin/colaborativa) ───────
(function () {
  'use strict';

  // Evita disparar el auto-pull dos veces en paralelo si un poll llega
  // mientras el anterior todavía está en vuelo (ver refreshEstado).
  let pulling = false;

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
        ? data.participants.map((p) => `
            <li>
              <span>${p.name}${p.can_pull ? ' <span class="collab-delegate-badge" title="Puede pedir la actualización de la cola remotamente">★ delegado</span>' : ''}</span>
              <span class="collab-participant-right">
                <span style="color:var(--text-muted)">${p.joined_at}</span>
                <button type="button" class="track-btn ghost collab-delegate-btn"
                        onclick="collabSetDelegate(${p.id}, ${p.can_pull ? 'false' : 'true'})">
                  ${p.can_pull ? 'Quitar permiso' : 'Dar permiso'}
                </button>
              </span>
            </li>`
          ).join('')
        : '<li style="color:var(--text-muted)">Todavía nadie se unió.</li>';
    }

    // El delegado pidió, desde su celular, que se cargue lo último — se
    // atiende solo, sin que el anfitrión tenga que tocar nada (ver ticket:
    // no quiere distraerse manejando). /api/admin/colaborativa/cola-pendiente
    // limpia el pedido apenas lo atiende, así el próximo poll ya no lo
    // vuelve a ver y esto no se repite en loop.
    if (data.pull_requested && !pulling) {
      collabPullQueue(true, data.pull_requested_by_name);
    }
  }

  window.collabSetDelegate = async function (participantId, enable) {
    try {
      await fetch(`/admin/colaborativa/participante/${participantId}/permiso`, { method: 'POST' });
    } catch (e) {
      alert('No se pudo conectar con el servidor.');
      return;
    }
    refreshEstado();
  };

  window.collabPullQueue = async function (auto, requestedByName) {
    pulling = true;
    const statusEl = document.getElementById('collab-pull-status');
    let tracks;
    try {
      tracks = await fetch('/api/admin/colaborativa/cola-pendiente').then((r) => r.json());
    } catch (e) {
      pulling = false;
      if (!auto) alert('No se pudo conectar con el servidor.');
      return;
    }
    if (!Array.isArray(tracks) || !tracks.length) {
      pulling = false;
      // Manual: mismo alert() de siempre. Automático (pedido remoto): nada
      // de alert() — el anfitrión puede estar manejando — solo se actualiza
      // el texto de estado del panel.
      if (!auto) {
        alert('No hay pistas nuevas para cargar.');
      } else if (statusEl) {
        statusEl.textContent = requestedByName
          ? `${requestedByName} pidió actualizar, pero no había pistas nuevas.`
          : 'Se pidió actualizar, pero no había pistas nuevas.';
      }
      return;
    }
    // Se agregan una por una a TU cola local con la función que ya existe
    // (appendToQueue) — desde ahí las manejás con total libertad, como
    // cualquier otra pista (reordenar/quitar/reproducir ya lo hace el
    // playlist-panel de siempre, no hace falta tocarlo).
    tracks.forEach((t) => { if (window.appendToQueue) window.appendToQueue(t); });
    if (!auto) {
      alert(`Se cargaron ${tracks.length} pista(s) nueva(s) a tu playlist.`);
    } else if (statusEl) {
      statusEl.textContent = requestedByName
        ? `${requestedByName} actualizó la cola: se cargaron ${tracks.length} pista(s) nueva(s).`
        : `Se cargaron ${tracks.length} pista(s) nueva(s) (pedido remoto).`;
    }
    pulling = false;
    refreshEstado();
  };

  if (document.getElementById('collab-participant-list')) {
    refreshEstado();
    setInterval(refreshEstado, 6000);
  }
})();
