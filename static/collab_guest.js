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
      // Bloqueo duro: ya está en la playlist, no se ofrece agregarla de nuevo.
      notify(r.message);
      return false;
    }
    if (r.status === 'album_warning') {
      if (confirm(r.message + '\n\n¿Agregar igual?')) {
        return addOne(track, { confirmAlbum: true });
      }
      return false;
    }
    notify(r.message || 'No se pudo agregar la pista.');
    return false;
  }

  // Un invitado nunca puede agregar un álbum/lista completa de una — solo
  // pistas sueltas (ver ticket). Si "reproducir álbum" (o cualquier otra
  // acción de "reproducir todo": Top tracks del artista, favoritos,
  // resultados de búsqueda…) dispara esto con varias pistas del MISMO
  // álbum, lo mandamos a la ficha del álbum para que las sume de a una.
  // Si son de álbumes mezclados (ej. "reproducir todo" en una búsqueda) no
  // hay a dónde redirigirlo con sentido, así que solo se le avisa.
  function handleBulk(list) {
    const firstAlbumId = list[0] && list[0].album_id;
    const sameAlbum = firstAlbumId && list.every((t) => t && t.album_id === firstAlbumId);
    if (sameAlbum) {
      notify('Como invitado no podés agregar el álbum completo de una — te llevamos a su ficha para que sumes las pistas que quieras, una por una.');
      window.location.href = `/album/${firstAlbumId}`;
    } else {
      notify('Como invitado solo podés agregar canciones de a una — entrá a cada pista o álbum y agregalas individualmente.');
    }
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
    if (list.length <= 1) { addOne(list[0]); return; }
    handleBulk(list);
  };

  window.toggleFavorite = function () {
    notify('Los favoritos son de las cuentas de usuario — como invitado de la playlist colaborativa no podés usarlos.');
  };
})();
