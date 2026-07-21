// HiRes Browser — Service Worker
// Caches static assets for offline shell; audio and API always go to network.
const CACHE_NAME = 'hires-v3';

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

// Fetch — network-first for API/audio; cache-first for static assets
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always fetch from network: API calls, audio streams, DSD streams, covers,
  // el panel de administración (datos siempre en vivo) y las páginas de login/
  // signup/logout (no tiene sentido cachear pantallas de autenticación).
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/audio/') ||
      url.pathname.startsWith('/stream-dsd/') ||
      url.pathname.startsWith('/cover/') ||
      url.pathname.startsWith('/play-mpd') ||
      url.pathname.startsWith('/admin/') ||
      url.pathname === '/login' ||
      url.pathname === '/signup' ||
      url.pathname === '/logout' ||
      e.request.method !== 'GET') {
    return; // let browser handle normally
  }

  // Static assets: cache-first with network fallback
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.match(e.request).then(cached =>
        cached || fetch(e.request).then(res => {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
          return res;
        })
      )
    );
    return;
  }

  // HTML pages: network-first, fall back to cache
  e.respondWith(
    fetch(e.request).then(res => {
      const clone = res.clone();
      caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      return res;
    }).catch(() => caches.match(e.request))
  );
});
