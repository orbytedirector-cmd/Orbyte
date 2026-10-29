// Registro de escuchas y sesiones de la web (mismos datos que envían iOS y Android a
// /api/v1/behavior/*). Lo usan las estadísticas del administrador. Mira la posición que
// publica player.js (window._orbytePos) una vez por segundo: suma solo el tiempo que de verdad
// avanzó (un salto con la barra no cuenta como escuchado) y, al cambiar de pista o cerrar la
// página, envía la escucha anterior. Nunca interrumpe nada: si falla, se pierde el dato y ya.
(function () {
  if (window._orbyteEscuchas) return;
  window._orbyteEscuchas = true;
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID()
    : 'x' + Date.now().toString(36) + Math.random().toString(36).slice(2));
  const enviar = (url, datos) => {
    try {
      const b = new Blob([JSON.stringify(datos)], { type: 'application/json' });
      if (navigator.sendBeacon && navigator.sendBeacon(url, b)) return;
    } catch (e) {}
    fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(datos),
                 keepalive: true, credentials: 'same-origin' }).catch(() => {});
  };

  // ── sesión de uso (una por pestaña) ──
  let sesion = null;
  try { sesion = sessionStorage.getItem('orbyte_sesion'); } catch (e) {}
  if (!sesion) {
    sesion = uuid();
    try { sessionStorage.setItem('orbyte_sesion', sesion); } catch (e) {}
    enviar('/api/behavior/session/start', { client_session_id: sesion });
  }

  // ── escuchas ──
  let actual = null;    // { id, seg, ultimo, max, dur }
  function cerrar() {
    if (!actual) return;
    const a = actual;
    actual = null;
    if (a.seg < 1 && a.max < 1) return;              // ni empezó a sonar
    enviar('/api/behavior/listen', {
      client_event_id: uuid(), track_id: a.id, duration_played_seconds: Math.round(a.seg),
      completed: a.dur > 0 && a.max >= a.dur - 3, source: 'web' });
  }
  setInterval(() => {
    const p = window._orbytePos;
    const id = window._currentTrack && window._currentTrack.id;
    if (!id) { cerrar(); return; }
    if (!actual || actual.id !== id) {
      cerrar();
      actual = { id, seg: 0, ultimo: (p && p.id === id) ? p.t : 0, max: 0, dur: 0 };
      return;
    }
    if (!p || p.id !== id) return;
    const d = p.t - actual.ultimo;
    if (d > 0 && d <= 3) actual.seg += d;             // avanzó sonando (no un salto con la barra)
    actual.ultimo = p.t;
    actual.max = Math.max(actual.max, p.t);
    actual.dur = p.dur || actual.dur;
  }, 1000);
  window.addEventListener('pagehide', () => {
    cerrar();
    enviar('/api/behavior/session/end', { client_session_id: sesion });
    try { sessionStorage.removeItem('orbyte_sesion'); } catch (e) {}
  });
})();
