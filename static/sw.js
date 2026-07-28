// HiRes Browser — Service Worker
// Caches static assets for offline shell; audio and API always go to network.
//
// v4: cache-first para /static/ quedó mordiendo la cola cada vez que se
// actualizaba player.js/cast.js sin acordarse de subir el ?v= de la URL —
// el navegador seguía sirviendo la copia vieja cacheada indefinidamente, sin
// importar cuántas veces se corrigiera el archivo en el server. Bumpear
// CACHE_NAME acá fuerza a tirar TODO lo cacheado hasta ahora (ver
// 'activate' más abajo), y de acá en más /static/ usa network-first: en LAN
// el costo extra es nada, y esta clase de bug (arreglo que nunca se ve
// reflejado porque quedó cacheado) deja de poder pasar.
const CACHE_NAME = 'hires-v4';

const STATIC_ASSETS = [
  '/',
  '/static/style.css',
  '/static/player.js',
  '/static/manifest.json',
];

// Install — pre-cache static shell
self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(c => c.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

// Activate — clean up old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch — network-first para todo lo navegable/estático; audio/API nunca
// pasan por acá (se excluyen más abajo).
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always fetch from network: API calls, audio streams, DSD streams, covers,
  // el panel de administración (datos siempre en vivo) y las páginas de login/
  // signup/logout (no tiene sentido cachear pantallas de autenticación).
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/audio/') ||
      url.pathname.startsWith('/stream-dsd/') ||
      url.pathname.startsWith('/cover/') ||
      url.pathname.startsWith('/cast-audio/') ||
      url.pathname.startsWith('/cast-cover/') ||
      url.pathname.startsWith('/play-mpd') ||
      url.pathname.startsWith('/admin/') ||
      url.pathname === '/login' ||
      url.pathname === '/signup' ||
      url.pathname === '/logout' ||
      e.request.method !== 'GET') {
    return; // let browser handle normally
  }

  // Estáticos y páginas HTML: network-first, cache solo como respaldo si no
  // hay red (antes era cache-first para /static/, que es justo lo que
  // causaba servir versiones viejas para siempre).
  e.respondWith(
    fetch(e.request).then(res => {
      const clone = res.clone();
      caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      return res;
    }).catch(() => caches.match(e.request))
  );
});
