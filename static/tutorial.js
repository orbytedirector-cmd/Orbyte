/* ═══════════════════════════════════════════════════════════════════════════════
   Orbyte — Tutorial de bienvenida / recorrido guiado
   ─────────────────────────────────────────────────────────────────────────────
   - Al cargar Home por primera vez, pregunta si el usuario quiere seguir un
     recorrido guiado o saltarlo (modal).
   - El recorrido es una serie de "pasos" con spotlight (resalta un elemento
     real de la interfaz) + un tooltip explicativo con Anterior/Siguiente.
   - Se puede reabrir en cualquier momento desde el botón flotante 🎓.
   - No depende de ningún framework; usa únicamente las funciones globales que
     ya expone base.html/player.js (navigateTo, togglePlaylist, openNowPlaying,
     closeNowPlaying, etc.) — este script se carga después de esas, al final
     de <body>, así que ya existen en window cuando corre.
   ══════════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var STORAGE_KEY = 'orbyte_tutorial_status'; // '' | 'skipped' | 'completed'

  function getStatus() {
    try { return localStorage.getItem(STORAGE_KEY) || ''; } catch (e) { return ''; }
  }
  function setStatus(v) {
    try { localStorage.setItem(STORAGE_KEY, v); } catch (e) { /* private mode, etc. */ }
  }
  function markSkippedIfNotCompleted() {
    if (getStatus() !== 'completed') setStatus('skipped');
  }

  function wait(ms) { return new Promise(function (res) { setTimeout(res, ms); }); }
  function safeCall(fn) {
    if (typeof fn !== 'function') return;
    try { return fn(); } catch (e) { console.warn('[tutorial] step hook error:', e); }
  }
  function isMobileViewport() {
    return window.matchMedia('(max-width: 640px)').matches;
  }
  function isElementVisible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && !!el.offsetParent;
  }

  // ── Steps ──────────────────────────────────────────────────────────────────
  // Home-only targets (quality-grid, genre-chips, fav-banner, adv-search-open-btn)
  // are safe because the tour always starts from Home (see start()).
  function ensureNPOpenIfPlaying() {
    if (window._currentTrack && typeof window.openNowPlaying === 'function') {
      window.openNowPlaying();
    }
  }
  function closeNPIfOpen() {
    var ov = document.getElementById('np-overlay');
    if (ov && ov.classList.contains('open') && typeof window.closeNowPlaying === 'function') {
      window.closeNowPlaying();
    }
  }
  function openPlaylistIfClosed() {
    var p = document.getElementById('playlist-panel');
    if (p && !p.classList.contains('open') && typeof window.togglePlaylist === 'function') {
      window.togglePlaylist();
    }
  }
  function closePlaylistIfOpen() {
    var p = document.getElementById('playlist-panel');
    if (p && p.classList.contains('open') && typeof window.togglePlaylist === 'function') {
      window.togglePlaylist();
    }
  }

  var STEPS = [
    {
      id: 'intro', target: null,
      title: '🎧 Bienvenido a Orbyte',
      body: 'Orbyte es tu reproductor personal de música en alta resolución — FLAC, DSD y MQA — servido directo desde tu propia biblioteca. Este recorrido rápido te muestra dónde está todo.'
    },
    {
      id: 'search', target: '#search-input',
      title: '🔍 Buscador',
      body: 'Escribe el nombre de un artista, álbum o pista y presiona Enter para buscar en toda tu biblioteca.'
    },
    {
      id: 'alphabet', target: '.alphabet-bar',
      title: '🔤 Búsqueda manual por letra',
      body: 'Toca cualquier letra para ver todos los artistas cuyo nombre empieza con ella — ideal para explorar sin escribir nada.'
    },
    {
      id: 'advanced-search', target: '.adv-search-open-btn',
      title: '🎛️ Búsqueda avanzada',
      body: 'Combina calidad de audio, mood, género, año, popularidad y energía al mismo tiempo para encontrar justo lo que quieres escuchar.'
    },
    {
      id: 'quality', target: '.quality-grid',
      title: '💿 Filtros por calidad — LED del DAC',
      body: 'Cada color coincide con el LED de tu iFi Zen DAC V2: amarillo (PCM estándar), blanco (Hi-Res), cian/rojo (DSD) y verde/azul (MQA). Toca una categoría para explorar solo esa calidad.'
    },
    {
      id: 'genres', target: '.genre-chips',
      title: '🎭 Mood, era, idioma y género',
      body: 'Más abajo en Inicio encontrarás secciones para navegar tu música por estado de ánimo, década, idioma y género musical.'
    },
    {
      id: 'favorites', target: '.fav-banner',
      title: '❤️ Favoritos',
      body: 'Marca cualquier pista con el corazón ♡ y accede rápido a todas tus favoritas desde aquí.'
    },
    {
      id: 'lyrics', target: '#lyrics-toggle-btn',
      title: '🎵 Letra',
      body: 'Muestra la letra de la pista actual, sincronizada línea por línea cuando está disponible.'
    },
    {
      id: 'normalize', target: '#normalize-btn',
      title: '🔊 Normalizador de volumen',
      body: 'Sube automáticamente el volumen de pistas silenciosas o en DSD, sin recodificar el audio.'
    },
    {
      id: 'repeat', target: '#repeat-btn',
      title: '🔁 Repetir',
      body: 'Alterna entre repetir toda la cola, repetir solo la pista actual, o desactivar la repetición.'
    },
    {
      id: 'shuffle', target: '#shuffle-btn',
      title: '🔀 Aleatorio',
      body: 'Reproduce tu cola actual en orden aleatorio.'
    },
    {
      id: 'fullscreen', target: '#player-cover-wrap',
      title: '🖼️ Reproductor en primer plano',
      body: 'Toca la carátula para abrir el reproductor a pantalla completa, con la portada en grande, el progreso y todos los controles.'
    },
    {
      id: 'similar-tracks', target: '#np-similar-btn',
      enter: function () { ensureNPOpenIfPlaying(); },
      title: '🎧 Pistas similares',
      body: 'Desde el reproductor a pantalla completa, este botón te sugiere pistas parecidas a la que estás escuchando.',
      fallbackBody: 'Cuando reproduzcas una pista y abras el reproductor a pantalla completa, este botón (arriba a la izquierda) te sugiere pistas parecidas a la que suena.'
    },
    {
      id: 'similar-artists', target: '#np-similar-artists-btn',
      enter: function () { ensureNPOpenIfPlaying(); },
      title: '🎤 Artistas similares',
      body: 'Este otro botón del reproductor a pantalla completa te muestra artistas con un estilo parecido al que suena.',
      fallbackBody: 'Cuando haya una pista sonando, este botón (arriba a la derecha del reproductor a pantalla completa) te muestra artistas con un estilo parecido.',
      exit: function () { closeNPIfOpen(); }
    },
    {
      id: 'playlist-open', target: '.playlist-icon-btn',
      enter: function () { closeNPIfOpen(); openPlaylistIfClosed(); },
      title: '📃 Tu Playlist',
      body: 'Aquí se acumulan las pistas que vayas encolando. Tócalo para abrir o cerrar el panel de tu cola de reproducción.'
    },
    {
      id: 'playlist-import', target: '#pl-import-btn',
      enter: function () { openPlaylistIfClosed(); },
      title: '⬆ Importar playlist M3U',
      body: 'Carga un archivo .m3u y Orbyte intentará emparejar cada pista con tu biblioteca local automáticamente.'
    },
    {
      id: 'playlist-export', target: '#pl-export-btn',
      enter: function () { openPlaylistIfClosed(); },
      title: '⬇ Exportar playlist M3U',
      body: 'Descarga tu cola actual como un archivo .m3u estándar, compatible con cualquier reproductor.',
      exit: function () { closePlaylistIfOpen(); }
    },
    {
      id: 'outro', target: null,
      title: '✅ ¡Listo!',
      body: 'Ya conoces lo esencial de Orbyte. Puedes reabrir este recorrido cuando quieras desde el botón 🎓 flotante. ¡A disfrutar tu música!'
    }
  ];

  // ── Engine state ──────────────────────────────────────────────────────────
  var currentIndex = -1;
  var active = false;
  var backdropEl, highlightEl, tooltipEl, welcomeEl, fabEl;
  var HIGHLIGHT_PAD = 8;

  function ensureDom() {
    if (backdropEl) return;

    backdropEl = document.createElement('div');
    backdropEl.className = 'tour-backdrop';
    backdropEl.addEventListener('wheel', function (e) { e.preventDefault(); }, { passive: false });
    backdropEl.addEventListener('touchmove', function (e) { e.preventDefault(); }, { passive: false });

    highlightEl = document.createElement('div');
    highlightEl.className = 'tour-highlight';

    tooltipEl = document.createElement('div');
    tooltipEl.className = 'tour-tooltip';
    tooltipEl.innerHTML =
      '<button type="button" class="tour-close" aria-label="Cerrar tutorial">✕</button>' +
      '<span class="tour-count"></span>' +
      '<h3 class="tour-title"></h3>' +
      '<p class="tour-body"></p>' +
      '<div class="tour-actions">' +
        '<button type="button" class="tour-btn tour-btn-skip">Saltar tutorial</button>' +
        '<div class="tour-actions-nav">' +
          '<button type="button" class="tour-btn tour-btn-prev">‹ Anterior</button>' +
          '<button type="button" class="tour-btn tour-btn-next">Siguiente ›</button>' +
        '</div>' +
      '</div>';

    document.body.appendChild(backdropEl);
    document.body.appendChild(highlightEl);
    document.body.appendChild(tooltipEl);

    tooltipEl.querySelector('.tour-close').addEventListener('click', function () { endTour('skipped'); });
    tooltipEl.querySelector('.tour-btn-skip').addEventListener('click', function () { endTour('skipped'); });
    tooltipEl.querySelector('.tour-btn-prev').addEventListener('click', prevStep);
    tooltipEl.querySelector('.tour-btn-next').addEventListener('click', function () {
      if (currentIndex >= STEPS.length - 1) endTour('completed');
      else showStep(currentIndex + 1);
    });
  }

  function onKeydown(e) {
    if (!active) return;
    if (e.key === 'Escape') { endTour('skipped'); return; }
    var blockKeys = [' ', 'ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End'];
    if (blockKeys.indexOf(e.key) !== -1) e.preventDefault();
    if (e.key === 'ArrowRight' || e.key === 'Enter') {
      if (currentIndex >= STEPS.length - 1) endTour('completed');
      else showStep(currentIndex + 1);
    } else if (e.key === 'ArrowLeft') {
      prevStep();
    }
  }

  function prevStep() {
    if (currentIndex > 0) showStep(currentIndex - 1);
  }

  function positionTooltip(el) {
    tooltipEl.classList.remove('placement-top', 'placement-bottom', 'placement-center');

    if (isMobileViewport()) {
      tooltipEl.classList.add(el ? 'placement-bottom' : 'placement-center');
      tooltipEl.style.top = '';
      tooltipEl.style.left = '';
      return;
    }
    if (!el) {
      tooltipEl.classList.add('placement-center');
      return;
    }

    var r = el.getBoundingClientRect();
    var margin = 16;
    var ttRect = tooltipEl.getBoundingClientRect();
    var ttW = ttRect.width || 320;
    var ttH = ttRect.height || 160;
    var spaceBelow = window.innerHeight - r.bottom;
    var spaceAbove = r.top;
    var top, placement;

    if (spaceBelow >= ttH + margin || spaceBelow >= spaceAbove) {
      placement = 'placement-bottom';
      top = r.bottom + margin;
    } else {
      placement = 'placement-top';
      top = r.top - ttH - margin;
    }
    top = Math.min(Math.max(margin, top), window.innerHeight - ttH - margin);

    var left = r.left + r.width / 2 - ttW / 2;
    left = Math.min(Math.max(margin, left), window.innerWidth - ttW - margin);

    tooltipEl.classList.add(placement);
    tooltipEl.style.top = top + 'px';
    tooltipEl.style.left = left + 'px';
  }

  function renderVisual(el, step) {
    var visible = isElementVisible(el);

    backdropEl.classList.toggle('tour-no-target', !visible);

    if (visible) {
      var r = el.getBoundingClientRect();
      highlightEl.style.display = 'block';
      highlightEl.style.top = (r.top - HIGHLIGHT_PAD) + 'px';
      highlightEl.style.left = (r.left - HIGHLIGHT_PAD) + 'px';
      highlightEl.style.width = (r.width + HIGHLIGHT_PAD * 2) + 'px';
      highlightEl.style.height = (r.height + HIGHLIGHT_PAD * 2) + 'px';
    } else {
      highlightEl.style.display = 'none';
    }

    tooltipEl.querySelector('.tour-count').textContent = 'Paso ' + (currentIndex + 1) + ' de ' + STEPS.length;
    tooltipEl.querySelector('.tour-title').textContent = step.title;
    tooltipEl.querySelector('.tour-body').textContent =
      (visible || !step.fallbackBody) ? step.body : step.fallbackBody;

    var prevBtn = tooltipEl.querySelector('.tour-btn-prev');
    var nextBtn = tooltipEl.querySelector('.tour-btn-next');
    prevBtn.disabled = currentIndex === 0;
    nextBtn.textContent = currentIndex === STEPS.length - 1 ? 'Finalizar ✓' : 'Siguiente ›';

    positionTooltip(visible ? el : null);
    requestAnimationFrame(function () { positionTooltip(visible ? el : null); });
    tooltipEl.classList.add('tour-visible');
  }

  function showStep(index) {
    var prevDef = STEPS[currentIndex];
    if (prevDef) safeCall(prevDef.exit);

    currentIndex = Math.max(0, Math.min(index, STEPS.length - 1));
    var step = STEPS[currentIndex];

    tooltipEl.classList.remove('tour-visible');

    Promise.resolve(safeCall(step.enter)).then(function () {
      return wait(step.target ? 80 : 0);
    }).then(function () {
      var el = step.target ? document.querySelector(step.target) : null;
      if (el && isElementVisible(el)) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return wait(380).then(function () { return document.querySelector(step.target); });
      }
      return el;
    }).then(function (el) {
      renderVisual(el, step);
    });
  }

  function onResize() {
    if (!active) return;
    var step = STEPS[currentIndex];
    if (!step) return;
    var el = step.target ? document.querySelector(step.target) : null;
    renderVisual(el, step);
  }

  function cleanupOpenedUI() {
    try { closeNPIfOpen(); } catch (e) { /* noop */ }
    try { closePlaylistIfOpen(); } catch (e) { /* noop */ }
    var op = document.getElementById('outputs-panel');
    if (op && op.style.display !== 'none' && typeof window.toggleOutputsPanel === 'function') {
      try { window.toggleOutputsPanel(); } catch (e) { /* noop */ }
    }
  }

  function endTour(reason) {
    if (!active) return;
    var lastDef = STEPS[currentIndex];
    if (lastDef) safeCall(lastDef.exit);
    cleanupOpenedUI();

    active = false;
    document.removeEventListener('keydown', onKeydown);
    window.removeEventListener('resize', onResize);
    tooltipEl.classList.remove('tour-visible');
    backdropEl.remove();
    highlightEl.remove();
    tooltipEl.remove();
    backdropEl = highlightEl = tooltipEl = null;

    if (reason === 'completed') setStatus('completed');
    else markSkippedIfNotCompleted();
  }

  function beginTour() {
    ensureDom();
    active = true;
    currentIndex = -1;
    document.addEventListener('keydown', onKeydown);
    window.addEventListener('resize', onResize);
    showStep(0);
  }

  function start() {
    hideWelcomeModal();
    if (active) return;
    if (window.location.pathname !== '/') {
      if (typeof window.navigateTo === 'function') {
        window.navigateTo('/');
        wait(450).then(beginTour);
      } else {
        window.location.href = '/';
      }
    } else {
      beginTour();
    }
  }

  // ── Welcome modal ─────────────────────────────────────────────────────────
  function buildWelcomeModal() {
    if (welcomeEl) return;
    welcomeEl = document.createElement('div');
    welcomeEl.className = 'tour-welcome-overlay';
    welcomeEl.innerHTML =
      '<div class="tour-welcome-card">' +
        '<div class="tour-welcome-icon">🎧</div>' +
        '<h2 class="tour-welcome-title">¡Bienvenido a Orbyte!</h2>' +
        '<p class="tour-welcome-body">¿Quieres un recorrido rápido por la app? Te mostramos el buscador, los filtros, los controles del reproductor y cómo importar/exportar tus playlists — toma menos de un minuto.</p>' +
        '<div class="tour-welcome-actions">' +
          '<button type="button" class="track-btn ghost" id="tour-welcome-skip">Ahora no</button>' +
          '<button type="button" class="track-btn primary" id="tour-welcome-start">Comenzar recorrido</button>' +
        '</div>' +
      '</div>';
    welcomeEl.addEventListener('click', function (e) {
      if (e.target === welcomeEl) hideWelcomeModal(true);
    });
    document.body.appendChild(welcomeEl);
    welcomeEl.querySelector('#tour-welcome-start').addEventListener('click', start);
    welcomeEl.querySelector('#tour-welcome-skip').addEventListener('click', function () { hideWelcomeModal(true); });
    requestAnimationFrame(function () { welcomeEl.classList.add('tour-visible'); });
  }

  function hideWelcomeModal(markSkipped) {
    if (!welcomeEl) return;
    if (markSkipped) markSkippedIfNotCompleted();
    welcomeEl.classList.remove('tour-visible');
    var el = welcomeEl;
    welcomeEl = null;
    setTimeout(function () { el.remove(); }, 250);
  }

  function maybeShowWelcome() {
    if (window.location.pathname !== '/') return;
    if (getStatus()) return; // ya se mostró antes (completado o saltado)
    wait(700).then(buildWelcomeModal);
  }

  // ── Floating relaunch button ─────────────────────────────────────────────
  function buildFab() {
    fabEl = document.createElement('button');
    fabEl.type = 'button';
    fabEl.className = 'tour-fab';
    fabEl.title = 'Ver tutorial de Orbyte';
    fabEl.setAttribute('aria-label', 'Ver tutorial de Orbyte');
    fabEl.textContent = '🎓';
    fabEl.addEventListener('click', function () { start(); });
    document.body.appendChild(fabEl);
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  buildFab();
  maybeShowWelcome();

  window.OrbyteTutorial = { start: start, restart: start };
})();
