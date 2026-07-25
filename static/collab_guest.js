// ── Playlist colaborativa: modo invitado ────────────────────────────────────
// Se carga DESPUÉS de player.js (ver base.html) y pisa las mismas funciones
// globales que ya usan album.html/browse.html/track.html/search.html/etc.
// para "reproducir/añadir a cola" — así ningún otro template necesita
// cambiar una sola línea: los botones de siempre ahora mandan la pista a la
// cola colaborativa del servidor en vez de tocarla acá.
(function () {
  'use strict';

  function notify(msg) {
    // Mismo patrón que el resto de la app (alert()) — no existe un sistema
    // de toasts propio, no vale la pena inventar uno para esto.
    alert(msg);
  }

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return res.json();
  }

  async function addOne(track, opts) {
    opts = opts || {};
    if (!track || !track.id) return false;
    let r;
    try {
      r = await postJSON('/api/collab/add', {
        track_id: track.id,
        confirm_duplicate: !!opts.confirmDuplicate,
        confirm_album: !!opts.confirmAlbum,
      });
    } catch (e) {
      notify('No se pudo conectar con el servidor para agregar la pista.');
      return false;
    }
    if (r.status === 'ok') {
      notify(`«${track.title}» se agregó a la playlist colaborativa. Te quedan ${r.remaining} pistas por ahora.`);
      return true;
    }
    if (r.status === 'duplicate') {
      if (confirm(r.message + '\n\n¿Agregarla igual?')) {
        return addOne(track, { confirmDuplicate: true, confirmAlbum: opts.confirmAlbum });
      }
      return false;
    }
    if (r.status === 'album_warning') {
      if (confirm(r.message + '\n\n¿Agregar igual?')) {
        return addOne(track, { confirmDuplicate: opts.confirmDuplicate, confirmAlbum: true });
      }
      return false;
    }
    notify(r.message || 'No se pudo agregar la pista.');
    return false;
  }

  async function addBatch(tracks) {
    const ids = (tracks || []).map((t) => t && t.id).filter(Boolean);
    if (!ids.length) return;
    let r;
    try {
      r = await postJSON('/api/collab/add-batch', { track_ids: ids });
    } catch (e) {
      notify('No se pudo conectar con el servidor para agregar el álbum.');
      return;
    }
    if (r.status !== 'ok') {
      notify(r.message || 'No se pudo agregar el álbum.');
      return;
    }
    let msg = `Se agregaron ${r.added} pista(s) a la playlist colaborativa.`;
    if (r.skipped_duplicates) msg += ` ${r.skipped_duplicates} ya estaban en la cola.`;
    if (r.capped_by_limit) msg += ' Llegaste al máximo de pistas permitido — el resto no se agregó.';
    notify(msg);
    if (r.album_notice) notify(r.album_notice);
  }

  // Un invitado no tiene reproductor propio: estas quedan como no-ops
  // defensivos (player-bar/NP overlay/playlist-panel ya están ocultos por
  // CSS, así que en la práctica nunca deberían dispararse).
  window.playTrack = function () {};
  window.loadQueue = function () {};

  window.appendToQueue = function (track) { addOne(track); };
  window.prependAndPlay = function (track) { addOne(track); };
  window.prependTracksAndPlay = function (tracks) {
    const list = Array.isArray(tracks) ? tracks : [tracks];
    if (list.length > 1) addBatch(list);
    else addOne(list[0]);
  };

  window.toggleFavorite = function () {
    notify('Los favoritos son de las cuentas de usuario — como invitado de la playlist colaborativa no podés usarlos.');
  };
})();
