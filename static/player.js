let queue = [];
let currentIndex = 0;
let currentAudio = null;
let lyricsData = null;
let lyricsInterval = null;
let _reconnectAttempts = 0;   // caps auto-reconnect retries per track (abrupt-stop recovery)
let _lastProgressPos   = 0;   // highest currentTime actually reached since the last reconnect
const MAX_RECONNECT_ATTEMPTS = 8;   // short-lived mobile network drops can chain several times in a row

// Intención del usuario: true mientras "debería estar sonando" (se puso en
// true al arrancar/reanudar, false solo cuando el usuario pausa/detiene a
// propósito). Sirve para distinguir una pausa nuestra de una pausa que nos
// impuso el sistema (interrupción de audio de otra app, llamada, etc.) —
// ver _handleUnexpectedPause() y el watchdog más abajo.
let _shouldBePlaying = false;

// Playlist options — shuffle / repeat. Persisted like the normalize toggle.
let shuffleEnabled = false;
let repeatMode = 'off';   // 'off' | 'all' | 'one'
try {
    shuffleEnabled = localStorage.getItem('orbyte_shuffle') === '1';
    repeatMode = localStorage.getItem('orbyte_repeat') || 'off';
} catch (e) {}

const MUSIC_ROOT = "/mnt/musica/";

// ── Remote diagnostic log (temporal) ─────────────────────────────────────────
// Instrumentación para diagnosticar el corte de reproducción DSD→DSD en 2do
// plano. Como no hay forma práctica de sacar la consola del navegador del
// celular en el momento del bug, cada evento relevante se manda al server
// por sendBeacon (diseñado justo para esto: no espera respuesta, sobrevive
// que la pestaña se vaya a 2do plano o se descargue) y aparece en el mismo
// log de la terminal que ya venís compartiendo, con el prefijo [CLIENT-LOG].
// Puramente aditivo: nunca puede romper la reproducción (todo en try/catch),
// y se puede apagar con DEBUG_REMOTE_LOG=false o borrar entero una vez
// diagnosticado el problema real.
const DEBUG_REMOTE_LOG = true;

function _rlog(event, data) {
    if (!DEBUG_REMOTE_LOG) return;
    try {
        const t = queue[currentIndex];
        const payload = JSON.stringify(Object.assign({
            event,
            t_client: Date.now(),
            hidden: (typeof document !== 'undefined') ? document.hidden : null,
            vis: (typeof document !== 'undefined') ? document.visibilityState : null,
            idx: currentIndex,
            track: t ? t.title : null,
            is_dsd: t ? !!t.is_dsd : null,
        }, data || {}));
        if (navigator.sendBeacon) {
            navigator.sendBeacon('/api/client-log', new Blob([payload], { type: 'application/json' }));
        } else {
            fetch('/api/client-log', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: payload,
                keepalive: true,
            }).catch(() => {});
        }
    } catch (e) { /* el diagnóstico nunca debe poder romper la reproducción */ }
}

// Engancha los listeners de diagnóstico a un <audio> recién creado. Puramente
// aditivo — no reemplaza ni interfiere con los listeners reales ya existentes
// (timeupdate/error/pause/onended), el DOM permite múltiples listeners para
// el mismo evento sin conflicto.
function _attachDiagListeners(audio) {
    ['waiting', 'stalled', 'suspend', 'abort', 'emptied', 'canplay', 'canplaythrough',
     'loadstart', 'loadedmetadata', 'playing', 'play', 'pause', 'ended'].forEach(evt => {
        audio.addEventListener(evt, () => _rlog('audio_' + evt, {
            currentTime: audio.currentTime,
            duration:    audio.duration,
            readyState:  audio.readyState,
            networkState:audio.networkState,
            paused:      audio.paused,
            ended:       audio.ended,
        }));
    });
    audio.addEventListener('error', () => {
        const err = audio.error;
        _rlog('audio_error_event', {
            code:        err ? err.code : null,
            message:     err ? err.message : null,
            currentTime: audio.currentTime,
            src:         audio.currentSrc,
        });
    });
}

document.addEventListener('freeze',  () => _rlog('page_freeze', {}));
document.addEventListener('resume',  () => _rlog('page_resume', {}));
window.addEventListener('pagehide', (e) => _rlog('pagehide', { persisted: e.persisted }));
window.addEventListener('pageshow', (e) => _rlog('pageshow', { persisted: e.persisted }));

// ── SVG Diamond helper — used wherever a LED color indicator is shown ────────
function _makeDiamondSVG(ledColor, size) {
    const sizes = { sm: '11px', md: '13px', lg: '16px', np: '18px' };
    const sz = sizes[size] || '11px';
    return `<svg width="${sz}" height="${sz}" viewBox="0 0 20 22" fill="none" ` +
        `stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">` +
        `<line x1="10" y1="1" x2="10" y2="3.5"/>` +
        `<line x1="7" y1="1.8" x2="8.5" y2="3.5"/>` +
        `<line x1="13" y1="1.8" x2="11.5" y2="3.5"/>` +
        `<path d="M3 8 L7 4.5 L13 4.5 L17 8"/>` +
        `<line x1="3" y1="8" x2="17" y2="8"/>` +
        `<path d="M3 8 L10 19 L17 8"/>` +
        `<line x1="7" y1="4.5" x2="10" y2="8"/>` +
        `<line x1="13" y1="4.5" x2="10" y2="8"/>` +
        `</svg>`;
}

// ── Shuffle / Repeat icon SVGs — same inline format as the rest of the player controls ──
const SHUFFLE_SVG = `<svg class="shuffle-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg>`;
const REPEAT_SVG = `<svg class="repeat-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>`;
const REPEAT_ONE_SVG = `<svg class="repeat-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/><path d="M11 10h1v4"/></svg>`;

// ── Path helpers ──────────────────────────────────────────────────────────────

function buildCoverUrl(path) {
    if (!path) return '';
    let p = path.replace(/^['"]|['"]$/g, '').replace(/^\/+/, '');
    if (p.startsWith('mnt/musica/')) p = p.slice('mnt/musica/'.length);
    return '/cover/' + p.split('/').map(s => encodeURIComponent(s)).join('/');
}

function buildAudioUrl(path) {
    if (!path) return '';
    let p = path.replace(/^['"]|['"]$/g, '').replace(/^\/+/, '');
    if (p.startsWith('mnt/musica/')) p = p.slice('mnt/musica/'.length);
    return '/audio/' + p.split('/').map(s => encodeURIComponent(s)).join('/');
}

function buildDsdStreamUrl(path) {
    if (!path) return '';
    let p = path.replace(/^['"]|['"]$/g, '').replace(/^\/+/, '');
    if (p.startsWith('mnt/musica/')) p = p.slice('mnt/musica/'.length);
    return '/stream-dsd/' + p.split('/').map(s => encodeURIComponent(s)).join('/');
}

// ── DSD prewarm ────────────────────────────────────────────────────────────
// El corte de reproducción en 2do plano resultó estar ligado a que el
// primer pedido a una pista DSD sin cachear puede tardar bastante (el
// server transcodifica el archivo completo antes de mandar el primer
// byte) — eso disparaba el detector de "stream caído" del watchdog, que a
// su vez terminaba dejando al <audio> mudo justo en el peor momento para
// que el navegador congele la pestaña en 2do plano. La forma más directa
// de evitar el problema de raíz es que la pista nunca tenga que esperar
// nada: pedirle al server que la transcodifique ANTES de que le toque
// sonar, mientras lo que esté sonando ahora (DSD o no) sigue reproduciendo
// tranquilo — server-side corre en un hilo aparte sin bloquear nada.
//
// Se dispara: al cargar/agregar pistas a la cola (por si el usuario arma
// toda la playlist de entrada) y en cada avance de pista (por si se está
// escuchando un álbum completo y las pistas se van agregando o quedando
// varias más adelante en la cola con tiempo de sobra para procesarse).
const DSD_PREWARM_LOOKAHEAD = 3;     // cuántas pistas por delante de la actual se precalientan
const _dsdPrewarmed = new Set();     // file_path ya pedidos esta sesión — evita pedidos repetidos

function _prewarmDsd(track) {
    if (!track || !track.is_dsd || !track.file_path) return;
    if (_dsdPrewarmed.has(track.file_path)) return;
    _dsdPrewarmed.add(track.file_path);
    try {
        fetch('/api/prewarm-dsd', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: track.file_path }),
        }).then(r => r.json()).then(d => {
            _rlog('prewarm_dsd_response', { file: track.file_path, status: d.status });
        }).catch(() => {});
    } catch (e) { /* el prewarm es solo una optimización — nunca debe romper nada */ }
}

function _prewarmUpcomingDsd() {
    const end = Math.min(queue.length, currentIndex + 1 + DSD_PREWARM_LOOKAHEAD);
    for (let i = currentIndex + 1; i < end; i++) _prewarmDsd(queue[i]);
}

// ── Queue & playback ──────────────────────────────────────────────────────────

function loadQueue(tracks) {
    queue = tracks.map(t => ({
        id:             t.id,
        title:          t.title          || '',
        artist:         t.artist         || t.artist_name || '',
        artist_id:      t.artist_id      || null,
        album:          t.album_name     || t.album       || '',
        album_id:       t.album_id       || null,
        cover_url:      t.cover_url      || buildCoverUrl(t.cover_path || ''),
        file_path:      t.file_path      || t.filepath    || '',
        audio_url:      t.audio_url      || (() => {
            const fp  = t.file_path || t.filepath || '';
            const ext = fp.split('.').pop().toLowerCase();
            return (ext === 'dsf' || ext === 'dff') ? buildDsdStreamUrl(fp) : buildAudioUrl(fp);
        })(),
        duration:       t.duration       || 0,
        codec:          t.codec          || '',
        is_dsd:         t.is_dsd         || 0,
        is_mqa:         t.is_mqa         || 0,
        led_color:      t.led_color      || 'white',
        format_display: t.format_display || '',
        dsd_rate:       t.dsd_rate       || '',
    }));
    // Notify playlist panel so it reflects the current queue
    document.dispatchEvent(new CustomEvent('queueLoaded', { detail: { tracks: queue } }));
    _prewarmUpcomingDsd();
}

// Varias vistas (browse/home/artist/search/album) hacen `await fetch(...)`
// para traer las pistas de un álbum ANTES de poder llamar a playTrack() — el
// holder del álbum dispara un onclick async. En iOS/Safari, si ese fetch
// tarda lo suficiente, el navegador puede considerar que ya no hay un "user
// gesture" válido y rechazar el play() en silencio — "a veces no comienza".
// Se llama de forma SÍNCRONA, como primera línea del onclick, antes del
// await — mantiene vivo el elemento de audio dentro de la ventana del toque.
function primeAudioForGesture() {
    if (!currentAudio) {
        currentAudio = new Audio();
        currentAudio.addEventListener('timeupdate', updateProgress);
        currentAudio.addEventListener('error', handleAudioError);
        currentAudio.addEventListener('pause', _handleUnexpectedPause);
        currentAudio.addEventListener('play',  () => _syncMediaSessionState(true));
        currentAudio.addEventListener('pause', () => _syncMediaSessionState(false));
        if (normalizeEnabled) _ensureNormalizeGraph();
        window.currentAudio = currentAudio;
    }
    _resumeAudioCtxIfNeeded();
    currentAudio.play().catch(() => {});
}
window.primeAudioForGesture = primeAudioForGesture;

function playTrack(index) {
    if (index < 0 || index >= queue.length) return;
    currentIndex = index;
    window.currentIndex = currentIndex;   // expose for np-overlay active-queue marker
    _reconnectAttempts = 0;               // fresh track — reset abrupt-stop retry budget
    _lastProgressPos   = 0;
    _shouldBePlaying = true;
    _prewarmUpcomingDsd();
    // Re-enable play button now that there's something to play
    const playBtn = document.getElementById('play-btn');
    if (playBtn) playBtn.removeAttribute('data-empty');
    const track = queue[currentIndex];
    _rlog('playTrack_call', { toIndex: index, title: track && track.title, isDsd: !!(track && track.is_dsd) });

    // Expose current track globally so base.html lyrics system can read track.id
    window._currentTrack = track;

    if (track.is_dsd) {
        // Play via ffmpeg stream in the browser; also attempt native DAC via MPD (silent on error)
        const streamUrl = track.audio_url || buildDsdStreamUrl(track.file_path);
        if (!currentAudio) {
            currentAudio = new Audio();
            currentAudio.addEventListener('timeupdate', updateProgress);
            currentAudio.addEventListener('error', handleAudioError);
            currentAudio.addEventListener('pause', _handleUnexpectedPause);
            currentAudio.addEventListener('play',  () => _syncMediaSessionState(true));
            currentAudio.addEventListener('pause', () => _syncMediaSessionState(false));
            _attachDiagListeners(currentAudio);
            // Solo se conecta al Web Audio graph si Normalizar ya está activo.
            // Conectar siempre acá (aunque el usuario nunca use Normalizar) deja
            // TODA la reproducción dependiendo de un AudioContext, que iOS
            // suspende al bloquear pantalla — silenciando el audio sin avisar.
            if (normalizeEnabled) _ensureNormalizeGraph();
        }
        currentAudio.onended = _handleTrackEnded;
        currentAudio.src = streamUrl;
        currentAudio._trackDuration = track.duration || 0;
        currentAudio.load();
        _resumeAudioCtxIfNeeded();
        _rlog('play_call_dsd', { src: streamUrl, audioCtxState: _audioCtx ? _audioCtx.state : null });
        currentAudio.play().then(() => {
            document.getElementById('play-btn').textContent = '⏸';
            _rlog('play_resolved_dsd', { currentTime: currentAudio.currentTime, readyState: currentAudio.readyState });
            // Recién ahora — con el audio del navegador ya confirmado
            // arrancando — se dispara el push al DAC nativo (MPD). Antes se
            // lanzaba en paralelo con .play(), y en un avance automático de
            // playlist en 2do plano (evento 'ended' sin gesto del usuario)
            // esa segunda fetch compitiendo por la misma ventana limitada de
            // ejecución/red que iOS le da a la pestaña en background parece
            // ser lo que dejaba el stream DSD cargando sin avanzar hasta
            // volver a primer plano. Las pistas no-DSD nunca llaman a esto
            // y nunca mostraron el problema — es la asimetría más concreta
            // entre las dos ramas.
            playViaMPD(track.file_path || '', { silent: true });
        }).catch(e => {
            console.error('[DSD] play error:', e);
            _rlog('play_rejected_dsd', { error: String(e), name: e && e.name });
            // No asumir "está sonando" si play() fue rechazado (típico en iOS
            // cuando pasó demasiado tiempo desde el toque del usuario).
            document.getElementById('play-btn').textContent = '▶';
            dispatchPlayerState(false);
        });
        window.currentAudio = currentAudio;
        // Show known duration immediately — DSD stream returns Infinity/NaN
        const tt = document.getElementById('total-time');
        if (tt && track.duration) tt.textContent = formatTime(track.duration);
        updatePlayerBar(track);
        updateVisualizer(track.led_color);
        dispatchPlayerState(true);
        clearSyncedLyrics();
        return;
    }

    if (!currentAudio) {
        currentAudio = new Audio();
        currentAudio.addEventListener('timeupdate', updateProgress);
        currentAudio.addEventListener('error', handleAudioError);
        currentAudio.addEventListener('pause', _handleUnexpectedPause);
        currentAudio.addEventListener('play',  () => _syncMediaSessionState(true));
        currentAudio.addEventListener('pause', () => _syncMediaSessionState(false));
        _attachDiagListeners(currentAudio);
        if (normalizeEnabled) _ensureNormalizeGraph();
    }

    currentAudio.onended = _handleTrackEnded;
    currentAudio.src = track.audio_url || buildAudioUrl(track.file_path);
    currentAudio._trackDuration = track.duration || 0;
    currentAudio.load();
    _resumeAudioCtxIfNeeded();
    _rlog('play_call', { src: currentAudio.src });
    currentAudio.play().then(() => {
        document.getElementById('play-btn').textContent = '⏸';
        _rlog('play_resolved', { currentTime: currentAudio.currentTime, readyState: currentAudio.readyState });
    }).catch(e => {
        console.error('Play error:', e);
        _rlog('play_rejected', { error: String(e), name: e && e.name });
        document.getElementById('play-btn').textContent = '▶';
        dispatchPlayerState(false);
    });
    window.currentAudio = currentAudio;

    updatePlayerBar(track);
    updateVisualizer(track.led_color);
    dispatchPlayerState(true);
    clearSyncedLyrics();
}

function playViaMPD(filepath, { silent = false } = {}) {
    const clean = (filepath || '').replace(/^['"]|['"]$/g, '');
    const statusEl = document.getElementById('mpd-status');
    if (statusEl) statusEl.textContent = '';  // clear stale error immediately
    fetch('/play-mpd', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: clean})
    })
    .then(r => r.json())
    .then(d => {
        if (!statusEl) return;
        if (d.status === 'ok') {
            statusEl.textContent = '✓ DAC';
            statusEl.style.color = 'var(--led-green)';
            setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 4000);
        } else if (!silent) {
            statusEl.textContent = '✗ ' + d.message;
            statusEl.style.color = 'var(--led-red)';
        }
    })
    .catch(() => { if (statusEl) statusEl.textContent = ''; });
}

function updatePlayerBar(track) {
    const cover  = document.getElementById('player-cover');
    const title  = document.getElementById('player-title');
    const artist = document.getElementById('player-artist');
    const album  = document.getElementById('player-album');
    const led    = document.getElementById('player-led');
    const fmt    = document.getElementById('player-format');

    if (cover) {
        if (track.cover_url) {
            cover.src = track.cover_url;
            cover.style.visibility = 'visible';
            cover.onerror = () => {
                cover.src = '';
                cover.style.visibility = 'hidden';
            };
        } else {
            cover.src = '';
            cover.style.visibility = 'hidden';
        }
    }
    if (title)  title.textContent  = track.title;
    if (artist) {
        artist.textContent = track.artist;
        if (track.artist_id) { artist.href = `/artist/${track.artist_id}`; artist.onclick = null; }
    }
    if (album) {
        album.textContent = track.album;
        if (track.album_id) { album.href = `/album/${track.album_id}`; album.onclick = null; }
    }

    // led_color from DB is always the truth — never recompute
    const LED_LABELS = {
        yellow:  'PCM 44.1/48 kHz',
        white:   'PCM 88.2/96/176.4/192/352.8/384 kHz',
        cyan:    'DSD 64/128',
        red:     'DSD 256',
        green:   'MQA',
        blue:    'MQA Studio',
        magenta: 'Original Sample Rate (MQB)',
    };
    const c = (track.led_color || 'white').toLowerCase();
    if (led) {
        led.innerHTML  = _makeDiamondSVG(c, 'np');
        led.className  = 'player-led led-d-' + c;
        led.title      = LED_LABELS[c] || c;
    }

    if (fmt) {
        // format_display from API is already computed server-side — use it
        const label = track.format_display ||
            (track.is_dsd ? (track.dsd_rate || 'DSD') :
             track.is_mqa ? (c === 'blue' ? 'MQA Studio' : c === 'magenta' ? 'MQB' : 'MQA') :
             (track.codec || 'FLAC').toUpperCase());
        fmt.textContent = label;
        fmt.className   = `player-fmt-badge fmt-${c}`;
    }

    document.getElementById('play-btn').textContent = '⏸';
    // Update favorite button state
    if (track.id) {
        const isFav = !!(window._favIds && window._favIds.has(Number(track.id)));
        _updateFavUI(Number(track.id), isFav);
    }
    // Media Session — CarPlay / lock screen
    updateMediaSession(track, true);

    // Tab title — refleja lo que está sonando ahora (ver sección "Tab title" más abajo)
    updateTabTitle();
}

function updateProgress() {
    if (!currentAudio) return;
    const fill = document.getElementById('progress-fill');
    const ct   = document.getElementById('current-time');
    const tt   = document.getElementById('total-time');
    const dur  = (currentAudio.duration && isFinite(currentAudio.duration))
        ? currentAudio.duration : (currentAudio._trackDuration || 0);
    const pct  = dur ? (currentAudio.currentTime / dur) * 100 : 0;
    if (fill) {
        fill.style.width = `${pct}%`;
        // Color progress bar to match current track quality
        const track = queue[currentIndex];
        const led   = track ? (track.led_color || 'white') : 'white';
        fill.className = `progress-fill led-${led}`;
    }
    const thumb = document.getElementById('progress-thumb');
    if (thumb) {
        thumb.style.left = `${pct}%`;
        const track = queue[currentIndex];
        if (track) thumb.style.background = `var(--led-${track.led_color || 'white'})`;
    }
    if (ct)   ct.textContent = formatTime(currentAudio.currentTime);
    if (tt)   tt.textContent = formatTime(dur);
    syncLyrics(currentAudio.currentTime);

    // Stream is healthy again — restore the full retry budget instead of
    // letting it get drained by several small drops in a row.
    if (currentAudio.currentTime > _lastProgressPos + 2) {
        _lastProgressPos = currentAudio.currentTime;
        _reconnectAttempts = 0;
    }
}

// El audio se puede pausar sin que nosotros lo hayamos pedido: una llamada
// entra, Siri interrumpe, otra app (Instagram, etc.) toma el foco de audio.
// iOS dispara un 'pause' nativo en esos casos igual que si el usuario hubiera
// tocado pausa — sin distinguir uno de otro, esa interrupción quedaba
// "pegada" hasta que el usuario volvía a abrir la app y tocaba play a mano.
//
// OJO: si esa "otra app" sigue en primer plano usando el audio (p.ej. un reel
// de Instagram), reintentar acá le pelea el foco de audio y silencia/corta lo
// que el usuario está viendo en la app que sí tiene el foco — exactamente al
// revés de lo que queremos. Por eso solo se reintenta cuando Orbyte mismo
// vuelve a estar visible/en primer plano (ver visibilitychange más abajo);
// mientras estamos en 2do plano, una pausa impuesta se respeta y se deja
// pausada hasta que el usuario vuelva a nuestra app.
function _handleUnexpectedPause() {
    if (!_shouldBePlaying || !currentAudio || currentAudio.ended) {
        _rlog('unexpected_pause_skip', { reason: 'not_should_be_playing_or_no_audio_or_ended', shouldBePlaying: _shouldBePlaying, ended: currentAudio ? currentAudio.ended : null });
        return;
    }
    if (document.hidden) {
        _rlog('unexpected_pause_skip', { reason: 'document_hidden' });
        return;
    }
    _rlog('unexpected_pause_retry', {
        currentTime:  currentAudio.currentTime,
        readyState:   currentAudio.readyState,
        networkState: currentAudio.networkState,
        audioCtxState: _audioCtx ? _audioCtx.state : null,
    });
    _resumeAudioCtxIfNeeded();
    currentAudio.play().then(() => {
        _rlog('unexpected_pause_retry_resolved', { currentTime: currentAudio.currentTime });
    }).catch(e => {
        _rlog('unexpected_pause_retry_rejected', { error: String(e), name: e && e.name });
    });
}

// El sistema (lock screen / Centro de Control) necesita que playbackState
// refleje SIEMPRE el estado real del audio, no solo cuando nosotros llamamos
// a togglePlayPause/playTrack. Si se nos pausa por una interrupción y nunca
// avisamos, iOS sigue pensando que "seguimos reproduciendo" — lo que rompe el
// botón de▶ del lock screen y hace que, al terminar la interrupción, el
// sistema le devuelva el widget de Now Playing a otra sesión (la última que
// sí estaba en un estado consistente) en vez de a nosotros. Patrón oficial:
// https://web.dev/articles/media-session
function _syncMediaSessionState(playing) {
    if ('mediaSession' in navigator) {
        navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
    }
}

function togglePlayPause() {
    // Hard guard: nothing loaded and queue empty → do nothing
    if (!queue.length && !window._currentTrack) return;

    if (!currentAudio || (!currentAudio.src && !currentAudio.currentSrc)) {
        // Audio object exists but has no source — treat as empty
        if (queue.length > 0) playTrack(currentIndex < queue.length ? currentIndex : 0);
        return;
    }
    if (currentAudio.paused) {
        _shouldBePlaying = true;
        _resumeAudioCtxIfNeeded();
        currentAudio.play();
        document.getElementById('play-btn').textContent = '⏸';
        dispatchPlayerState(true);
        if (window._currentTrack) updateMediaSession(window._currentTrack, true);
    } else {
        _shouldBePlaying = false;
        currentAudio.pause();
        document.getElementById('play-btn').textContent = '▶';
        dispatchPlayerState(false);
        if (window._currentTrack) updateMediaSession(window._currentTrack, false);
    }
}

function _randomIndexExcluding(exclude) {
    if (queue.length <= 1) return 0;
    let idx;
    do { idx = Math.floor(Math.random() * queue.length); } while (idx === exclude);
    return idx;
}

function prevTrack() {
    if (shuffleEnabled) { playTrack(_randomIndexExcluding(currentIndex)); return; }
    if (currentIndex > 0) { playTrack(currentIndex - 1); return; }
    if (repeatMode === 'all' && queue.length) { playTrack(queue.length - 1); return; }
}

function nextTrack() {
    _rlog('nextTrack_call', { currentIndex, queueLen: queue.length, shuffleEnabled, repeatMode });
    if (shuffleEnabled) { playTrack(_randomIndexExcluding(currentIndex)); return; }
    if (currentIndex < queue.length - 1) { playTrack(currentIndex + 1); return; }
    if (repeatMode === 'all') { playTrack(0); return; }
    // End of the queue, nothing selected — actually stop (was only faking a
    // stopped UI before while audio kept playing) and rewind to track 0 so
    // pressing play starts the list over from the beginning.
    _stopAndRewind();
}

function _stopAndRewind() {
    if (!queue.length) return;
    playTrack(0);
    // playTrack() de arriba marca _shouldBePlaying = true — esto es una
    // parada real (fin de cola), no una interrupción, así que se anula acá
    // para que el listener de 'pause' no intente reanudar solo.
    _shouldBePlaying = false;
    if (currentAudio) {
        currentAudio.pause();
        currentAudio.currentTime = 0;
    }
    const playBtn = document.getElementById('play-btn');
    if (playBtn) playBtn.textContent = '▶';
    const viz = document.getElementById('player-visualizer');
    if (viz) viz.classList.remove('spinning');
    const fill = document.getElementById('progress-fill');
    if (fill) fill.style.width = '0%';
    const curT = document.getElementById('current-time');
    if (curT) curT.textContent = '0:00';
    dispatchPlayerState(false);
    if (window._currentTrack) updateMediaSession(window._currentTrack, false);
}

function toggleShuffle() {
    shuffleEnabled = !shuffleEnabled;
    try { localStorage.setItem('orbyte_shuffle', shuffleEnabled ? '1' : '0'); } catch (e) {}
    _updateShuffleRepeatButtons();
}
window.toggleShuffle = toggleShuffle;

function cycleRepeat() {
    repeatMode = repeatMode === 'off' ? 'all' : (repeatMode === 'all' ? 'one' : 'off');
    try { localStorage.setItem('orbyte_repeat', repeatMode); } catch (e) {}
    _updateShuffleRepeatButtons();
}
window.cycleRepeat = cycleRepeat;

function _updateShuffleRepeatButtons() {
    [document.getElementById('shuffle-btn'), document.getElementById('np-shuffle-btn')].forEach(b => {
        if (!b) return;
        if (!b.querySelector('.shuffle-svg')) b.innerHTML = SHUFFLE_SVG;
        b.classList.toggle('is-active', shuffleEnabled);
    });
    const iconSvg = repeatMode === 'one' ? REPEAT_ONE_SVG : REPEAT_SVG;
    const label = repeatMode === 'one' ? 'Repetir una pista' : repeatMode === 'all' ? 'Repetir lista' : 'Repetir (desactivado)';
    [document.getElementById('repeat-btn'), document.getElementById('np-repeat-btn')].forEach(b => {
        if (!b) return;
        b.innerHTML = iconSvg;
        b.title = label;
        b.classList.toggle('is-active', repeatMode !== 'off');
    });
}
window._updateShuffleRepeatButtons = _updateShuffleRepeatButtons;
_updateShuffleRepeatButtons();

function seekTo(percent) {
    if (!currentAudio) return;
    // /stream-dsd now serves a fully transcoded, byte-range-seekable file
    // (see app.py), so DSD tracks seek exactly like any other track — no
    // more rebuilding the URL with ?start= and reloading from scratch.
    const dur = (currentAudio.duration && isFinite(currentAudio.duration))
        ? currentAudio.duration
        : (currentAudio._trackDuration || 0);
    if (dur > 0) currentAudio.currentTime = (percent / 100) * dur;
}

function seekFromClick(event, bar) {
    const rect = bar.getBoundingClientRect();
    const pct  = ((event.clientX - rect.left) / rect.width) * 100;
    seekTo(pct);
}

function setVolume(v) { if (currentAudio) currentAudio.volume = v; }

function _handleTrackEnded() {
    // A stream that dies mid-song (ffmpeg pipe closed, network drop) can surface as a
    // normal 'ended' event instead of 'error' — don't treat it as a real end-of-track.
    // Prefer the REAL duration the browser measured from the audio data it actually
    // received over the DB-sourced _trackDuration: those can differ by a couple of
    // seconds after a DSD→FLAC transcode (sample-rate conversion rounding, etc.), and
    // trusting the DB value there made a perfectly normal end-of-track look
    // "premature" — routing it into the error-reconnect path (which re-fetches the
    // SAME just-finished track) instead of into nextTrack(), so the next queued track
    // never even got requested.
    if (!currentAudio) { _rlog('track_ended', { branch: 'no_audio_next' }); nextTrack(); return; }
    const realDur = (currentAudio.duration && isFinite(currentAudio.duration)) ? currentAudio.duration : 0;
    const dur = realDur || currentAudio._trackDuration || 0;
    const pos = currentAudio.currentTime || 0;
    _rlog('track_ended', { dur, pos, realDur, reconnectAttempts: _reconnectAttempts });
    if (dur > 3 && pos < dur - 3 && _reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        _rlog('track_ended_treated_as_premature', { dur, pos });
        handleAudioError({ type: 'premature-end' });
        return;
    }
    if (repeatMode === 'one') { playTrack(currentIndex); return; }
    nextTrack();
}

function handleAudioError(e) {
    const track = queue[currentIndex];
    if (!track || !currentAudio) { console.error('Audio error:', e); return; }

    const lastPos = currentAudio.currentTime || 0;
    const realDur = (currentAudio.duration && isFinite(currentAudio.duration)) ? currentAudio.duration : 0;
    const dur     = realDur || currentAudio._trackDuration || track.duration || 0;
    const nearEnd = dur > 0 && lastPos >= dur - 1.5;
    _rlog('handleAudioError_call', {
        reason: e && e.type, lastPos, dur, realDur, nearEnd, reconnectAttempts: _reconnectAttempts,
        networkState: currentAudio.networkState, readyState: currentAudio.readyState,
    });

    // Stream dropped (network/pipe hiccup) mid-track — auto-reconnect instead of
    // stopping abruptly. /stream-dsd now serves a cached, byte-range-seekable
    // file exactly like /audio does, so DSD and regular tracks reconnect the
    // same way: reload the source and seek back to lastPos once metadata is
    // available. Capped to avoid infinite retry loops, but the budget resets
    // on genuine progress (see updateProgress) so a flaky connection that
    // recovers repeatedly doesn't burn through it on its own.
    //
    // EXCEPTO en 2do plano: reasignar .src/load() dejaba al <audio> sin
    // sonar por un instante (readyState vuelve a 0) mientras la pestaña está
    // oculta — y los logs de dos pruebas independientes muestran que los
    // únicos cortes largos (varios minutos sin ningún evento, ni siquiera
    // del watchdog corriendo en JS puro sin red) empezaron justo después de
    // esta reconexión. Toda transición que NO pasó por acá — incluyendo
    // DSD→DSD sin stall — funcionó perfecto en 2do plano. Todo indica que
    // ese instante sin audio es lo que le da pie a iOS/el navegador para
    // congelar el hilo de JS de la pestaña, y una vez congelado nada de
    // este código puede volver a ejecutarse para recuperarlo. Mientras está
    // oculta, mejor no tocar nada: _handleUnexpectedPause() ya recupera al
    // toque en cuanto la app vuelve a primer plano (ver visibilitychange).
    if (!nearEnd && _reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
        if (document.hidden) {
            _rlog('reconnect_suppressed_hidden', { lastPos, dur, reconnectAttempts: _reconnectAttempts });
            return;
        }
        _reconnectAttempts++;
        console.warn(`[player] Stream dropped at ${lastPos.toFixed(1)}s — reconnecting (intento ${_reconnectAttempts})…`);
        _rlog('reconnect_attempt', { attempt: _reconnectAttempts, lastPos });
        currentAudio.src = track.audio_url ||
            (track.is_dsd ? buildDsdStreamUrl(track.file_path) : buildAudioUrl(track.file_path));
        currentAudio.addEventListener('loadedmetadata', function _seekOnce() {
            currentAudio.removeEventListener('loadedmetadata', _seekOnce);
            if (lastPos > 0) currentAudio.currentTime = lastPos;
        });
        currentAudio._trackDuration = dur;
        currentAudio.load();
        currentAudio.play().catch(() => {});
        return;
    }

    console.error('Audio error:', e);
    if (!nearEnd) {
        // Retry budget exhausted — don't leave playback silently stuck on a
        // dead track for the rest of the session. Move on so the playlist
        // keeps going instead of appearing to have just "stopped".
        console.warn('[player] Giving up on current track after repeated stream drops — skipping to next.');
        _rlog('handleAudioError_giving_up', { reconnectAttempts: _reconnectAttempts, lastPos, dur });
        nextTrack();
        return;
    }
    document.getElementById('play-btn').textContent = '▶';
}

function dispatchPlayerState(playing) {
    document.dispatchEvent(new CustomEvent('playerStateChange', {detail:{playing}}));
}

// ── Media Session API — CarPlay / lock screen / Android Auto ──────────────────
function updateMediaSession(track, playing) {
    if (!('mediaSession' in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
        title:  track.title  || 'Sin título',
        artist: track.artist || '',
        album:  track.album  || '',
        artwork: track.cover_url
            ? [{ src: track.cover_url, sizes: '512x512', type: 'image/jpeg' }]
            : [],
    });
    navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';

    // Action handlers — allow CarPlay/lock-screen controls to work.
    // _resumeAudioCtxIfNeeded() primero: si el AudioContext quedó suspendido
    // por el bloqueo de pantalla, sin esto los botones del lock screen
    // "no hacen nada" (audio internamente sigue mudo aunque currentAudio
    // reporte estar reproduciendo).
    navigator.mediaSession.setActionHandler('play',         () => { _rlog('mediasession_action', { action: 'play' });  _shouldBePlaying = true;  _resumeAudioCtxIfNeeded(); currentAudio && currentAudio.play(); dispatchPlayerState(true);  });
    navigator.mediaSession.setActionHandler('pause',        () => { _rlog('mediasession_action', { action: 'pause' }); _shouldBePlaying = false; currentAudio && currentAudio.pause(); dispatchPlayerState(false); });
    navigator.mediaSession.setActionHandler('previoustrack',() => { _rlog('mediasession_action', { action: 'previoustrack' }); _resumeAudioCtxIfNeeded(); prevTrack(); });
    navigator.mediaSession.setActionHandler('nexttrack',    () => { _rlog('mediasession_action', { action: 'nexttrack' });     _resumeAudioCtxIfNeeded(); nextTrack(); });
    navigator.mediaSession.setActionHandler('seekto', details => {
        _rlog('mediasession_action', { action: 'seekto', seekTime: details.seekTime });
        _resumeAudioCtxIfNeeded();
        if (currentAudio && details.seekTime != null) currentAudio.currentTime = details.seekTime;
    });
}
window.updateMediaSession = updateMediaSession;

// ── Visualizer (animated vinyl/CD in player bar) ──────────────────────────────

function updateVisualizer(ledColor) {
    const viz = document.getElementById('player-visualizer');
    if (!viz) return;
    // Only animate when something is actually queued
    if (!queue.length && !window._currentTrack) {
        viz.classList.remove('spinning');
        return;
    }
    const c = ledColor || 'white';
    viz.style.setProperty('--led-current', `var(--led-${c})`);
    viz.classList.add('spinning');
}

function resetPlayerBar() {
    _shouldBePlaying = false;
    // Stop audio completely
    if (currentAudio) {
        currentAudio.pause();
        currentAudio.src = '';
    }
    // Reset internal state
    queue = [];
    currentIndex = 0;
    window.currentIndex = 0;
    window._currentTrack = null;
    lyricsData = null;

    // Reset visual elements
    const ids = {
        'player-cover':  null,
        'player-title':  '—',
        'player-artist': '',
        'player-album':  '',
        'play-btn':      '▶',
        'current-time':  '0:00',
        'total-time':    '0:00',
    };
    Object.entries(ids).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (id === 'player-cover') { el.src = ''; el.style.visibility = 'hidden'; }
        else if (el.tagName === 'A') el.textContent = val;
        else el.textContent = val;
    });

    // Stop the visualizer spinning
    const viz = document.getElementById('player-visualizer');
    if (viz) viz.classList.remove('spinning');

    // Disable play button visually — nothing to play
    const playBtn = document.getElementById('play-btn');
    if (playBtn) playBtn.setAttribute('data-empty', 'true');

    // Reset progress bar
    const fill = document.getElementById('progress-fill');
    if (fill) fill.style.width = '0%';

    // Clear synced lyrics interval
    if (lyricsInterval) { clearInterval(lyricsInterval); lyricsInterval = null; }

    // Tab title — nada sonando, vuelve al título original de la página
    updateTabTitle();

    // Notify listeners that nothing is playing
    document.dispatchEvent(new CustomEvent('playerStateChange', { detail: { playing: false } }));
    document.dispatchEvent(new CustomEvent('queueLoaded', { detail: { tracks: [] } }));
}
window.resetPlayerBar = resetPlayerBar;

// ── Lyrics sync ───────────────────────────────────────────────────────────────

function clearSyncedLyrics() {
    lyricsData = null;
    const el = document.getElementById('player-lyrics-lines');
    if (el) el.innerHTML = '';
}

function parseSyncedLyrics(synced) {
    // Format: [mm:ss.xx] lyric line
    const lines = [];
    const regex = /\[(\d+):(\d+\.\d+)\]\s*(.*)/g;
    let match;
    while ((match = regex.exec(synced)) !== null) {
        const time = parseInt(match[1]) * 60 + parseFloat(match[2]);
        lines.push({time, text: match[3]});
    }
    return lines.sort((a,b) => a.time - b.time);
}

function syncLyrics(currentTime) {
    if (!lyricsData || !lyricsData.length) return;
    const el = document.getElementById('player-lyrics-lines');
    if (!el) return;
    let active = 0;
    for (let i = 0; i < lyricsData.length; i++) {
        if (currentTime >= lyricsData[i].time) active = i;
    }
    el.querySelectorAll('.lyric-line').forEach((line, i) => {
        line.classList.toggle('active', i === active);
        if (i === active) line.scrollIntoView({block:'nearest', behavior:'smooth'});
    });
}

async function loadPlayerLyrics(artist, title, trackId) {
    const panel = document.getElementById('player-lyrics-panel');
    const lines = document.getElementById('player-lyrics-lines');
    if (!panel || !lines) return;

    panel.style.display = 'block';
    lines.innerHTML = '<span style="color:var(--text-secondary);padding:1rem;display:block">Buscando letra…</span>';

    try {
        let url = `/api/lyrics?artist=${encodeURIComponent(artist)}&title=${encodeURIComponent(title)}`;
        if (trackId) url += `&track_id=${trackId}`;
        const d = await fetch(url).then(r => r.json());
        if (d.has_synced && d.synced) {
            lyricsData = parseSyncedLyrics(d.synced);
            lines.innerHTML = lyricsData.map((l, i) =>
                `<div class="lyric-line" data-i="${i}">${l.text || '♪'}</div>`
            ).join('');
        } else if (d.has_lyrics && d.lyrics) {
            lyricsData = null;
            lines.innerHTML = d.lyrics.split('\n').map(l =>
                `<div class="lyric-line static">${l || '&nbsp;'}</div>`
            ).join('');
        } else {
            lines.innerHTML = '<span style="color:var(--text-muted);padding:1rem;display:block">Letra no disponible</span>';
        }
    } catch(e) {
        lines.innerHTML = '<span style="color:var(--led-red);padding:1rem;display:block">Error al obtener letra</span>';
    }
}

window.loadPlayerLyrics = loadPlayerLyrics;

// ── Keyboard ──────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    switch(e.code) {
        case 'Space':      e.preventDefault(); togglePlayPause(); break;
        case 'ArrowLeft':  if (currentAudio) currentAudio.currentTime = Math.max(0, currentAudio.currentTime - 10); break;
        case 'ArrowRight': { const _d2=(currentAudio&&isFinite(currentAudio.duration)?currentAudio.duration:currentAudio&&currentAudio._trackDuration||0); if(_d2>0) currentAudio.currentTime=Math.min(_d2,currentAudio.currentTime+10); } break;
    }
});

function formatTime(s) {
    if (!s || s < 0) return '0:00';
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
    return `${m}:${String(sec).padStart(2,'0')}`;
}

// ── Favorites ─────────────────────────────────────────────────────────────────

async function toggleFavorite(trackId) {
    // Accept trackId directly or read from current track
    const tid = Number(trackId || (window._currentTrack && window._currentTrack.id));
    if (!tid) { console.warn('[Fav] No track loaded'); return; }

    const btn = document.getElementById('player-fav-btn');
    // Optimistic UI update immediately
    const wasAdded = !window._favIds || !window._favIds.has(tid);
    if (!window._favIds) window._favIds = new Set();
    if (wasAdded) {
        window._favIds.add(tid);
    } else {
        window._favIds.delete(tid);
    }
    _updateFavUI(tid, wasAdded);

    try {
        const d = await fetch('/api/favorites/toggle', {
            method:  'POST',
            headers: {'Content-Type': 'application/json'},
            body:    JSON.stringify({track_id: tid})
        }).then(r => r.json());

        if (!d.action) {
            // Server error — revert optimistic update
            if (wasAdded) window._favIds.delete(tid);
            else          window._favIds.add(tid);
            _updateFavUI(tid, !wasAdded);
            console.error('[Fav] Server error:', d);
            return;
        }
        const isFav = d.action === 'added';
        // Ensure state matches server
        if (isFav) window._favIds.add(tid);
        else       window._favIds.delete(tid);
        _updateFavUI(tid, isFav);
        console.debug('[Fav]', d.action, 'track', tid, '| total:', d.total);
    } catch(e) {
        console.error('[Fav] fetch error:', e);
    }
}

function _updateFavUI(tid, isFav) {
    const heartOn  = `<svg class="heart-svg" viewBox="0 0 24 24"><path fill="#f43f5e" stroke="#f43f5e" stroke-width="1.5" stroke-linejoin="round" d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>`;
    const heartOff = `<svg class="heart-svg" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>`;

    // Player bar fav button
    const btn = document.getElementById('player-fav-btn');
    if (btn) {
        btn.innerHTML = isFav ? heartOn : heartOff;
        btn.title     = isFav ? 'Quitar de favoritos' : 'Añadir a favoritos';
        btn.classList.toggle('is-fav', isFav);
    }
    // NP overlay fav button
    const npFav = document.getElementById('np-fav-btn');
    if (npFav) {
        npFav.innerHTML = isFav ? heartOn : heartOff;
        npFav.classList.toggle('np-fav-active', isFav);
        npFav.title = isFav ? 'Quitar de favoritos' : 'Añadir a favoritos';
    }
    // Any inline fav buttons on the page (album tracklist, track page)
    document.querySelectorAll(`[data-fav-id="${tid}"]`).forEach(el => {
        el.innerHTML = isFav ? heartOn : heartOff;
        el.classList.toggle('is-fav', isFav);
    });
}
window.toggleFavorite = toggleFavorite;

// ── Volume / Mute ─────────────────────────────────────────────────────────────────

let _muted = false;

function toggleMute() {
    if (!currentAudio) return;
    _muted = !_muted;
    currentAudio.muted = _muted;
    const icon   = document.getElementById('vol-icon');
    const slider = document.getElementById('volume-slider');
    if (icon)   icon.textContent     = _muted ? '🔇' : '🔊';
    if (slider) slider.style.opacity = _muted ? '0.4' : '1';
}

// ── Tab title ("reproducción en curso" en la pestaña del navegador) ───────────
// Formato: "Orbyte - Artista - Pista". "Orbyte - " queda siempre fijo (es el
// nombre de la app); solo "Artista - Pista" se desliza tipo marquee en loop
// cuando no entra completo. El ícono de mute NO se incluye acá — ya vive en
// su propio control (#vol-icon) y sería redundante repetirlo en el título.
// Cuando no hay nada sonando, se restaura el <title> original de la página.

const _originalDocTitle = document.title;   // título propio de cada página (home, artist, album, etc.)
const TAB_TITLE_PREFIX = 'Orbyte - ';       // parte fija, nunca se desliza
const TAB_TITLE_MAX = 28;                   // caracteres visibles de "Artista - Pista" antes del marquee
const TAB_TITLE_SEPARATOR = '     •     ';  // separador para que el loop no se corte feo
const TAB_TITLE_SCROLL_MS = 350;            // velocidad del desplazamiento

let _titleMarqueeTimer = null;
let _titleLoopText = '';
let _titleScrollPos = 0;

function _buildTrackText(track) {
    const artist = track.artist || 'Desconocido';
    const title  = track.title  || 'Sin título';
    return `${artist} - ${title}`;
}

function _stopTitleMarquee() {
    if (_titleMarqueeTimer) {
        clearInterval(_titleMarqueeTimer);
        _titleMarqueeTimer = null;
    }
}

function updateTabTitle() {
    _stopTitleMarquee();

    const track = window._currentTrack;
    if (!track) {
        document.title = _originalDocTitle;
        return;
    }

    const trackText = _buildTrackText(track);

    if (trackText.length <= TAB_TITLE_MAX) {
        document.title = TAB_TITLE_PREFIX + trackText;
        return;
    }

    // Marquee: "Orbyte - " queda fijo; solo "Artista - Pista" se desliza en loop
    _titleLoopText  = trackText + TAB_TITLE_SEPARATOR;
    _titleScrollPos = 0;
    document.title  = TAB_TITLE_PREFIX + _titleLoopText.slice(0, TAB_TITLE_MAX);

    _titleMarqueeTimer = setInterval(() => {
        _titleScrollPos = (_titleScrollPos + 1) % _titleLoopText.length;
        const doubled = _titleLoopText + _titleLoopText;
        document.title = TAB_TITLE_PREFIX + doubled.slice(_titleScrollPos, _titleScrollPos + TAB_TITLE_MAX);
    }, TAB_TITLE_SCROLL_MS);
}
window.updateTabTitle = updateTabTitle;

// ── Volume normalization — per-track loudness targeting (Web Audio API) ────────
// Off by default: currentAudio plays natively, with zero Web Audio involvement,
// until the user enables this. When on, a real-time RMS analyser continuously
// measures each track's OWN loudness and drives a gain node toward a fixed
// target level — quiet tracks (DSD in particular) get boosted, already-loud
// tracks get pulled back, so every track lands at roughly the same perceived
// volume instead of all receiving the same fixed boost regardless of source.
// A fast safety limiter sits after the gain stage purely to catch clipping on
// unexpected transients; it does not add loudness on its own. Nothing here
// touches the source stream, the transcode, or the bitrate/format.
let normalizeEnabled = false;
try { normalizeEnabled = localStorage.getItem('orbyte_normalize') === '1'; } catch (e) {}

const NORM_TARGET_RMS = 0.12;   // ≈ -18.4 dBFS — reference loudness every track is pulled toward
const NORM_MIN_GAIN   = 0.35;   // don't cut more than ~-9 dB, even on already-loud masters
const NORM_MAX_GAIN   = 5.0;    // don't boost more than ~+14 dB (avoids amplifying noise floor)
const NORM_SMOOTH_SEC = 1.0;    // how fast gain follows measured loudness (avoids pumping)
const NORM_TICK_MS    = 200;

let _audioCtx     = null;
let _normSource   = null;
let _normAnalyser = null;
let _normData     = null;
let _normGain     = null;
let _normLimiter  = null;
let _normTimer    = null;

function _ensureNormalizeGraph() {
    if (!currentAudio || _normSource) return;   // no audio yet, or already wired up
    try {
        _audioCtx     = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
        _audioCtx.addEventListener('statechange', () => _rlog('audiocontext_statechange', { state: _audioCtx.state }));
        _normSource   = _audioCtx.createMediaElementSource(currentAudio);
        _normAnalyser = _audioCtx.createAnalyser();
        _normAnalyser.fftSize = 1024;
        _normData     = new Float32Array(_normAnalyser.fftSize);
        _normGain     = _audioCtx.createGain();
        _normLimiter  = _audioCtx.createDynamicsCompressor();
        // Fixed safety limiter — only catches peaks the gain stage pushes past
        // -1 dBFS, it never shapes or "warms" the sound like a mastering compressor.
        _normLimiter.threshold.value = -1;
        _normLimiter.knee.value      = 0;
        _normLimiter.ratio.value     = 20;
        _normLimiter.attack.value    = 0.003;
        _normLimiter.release.value   = 0.15;
        _normSource.connect(_normAnalyser);
        _normAnalyser.connect(_normGain);
        _normGain.connect(_normLimiter);
        _normLimiter.connect(_audioCtx.destination);
        _applyNormalizeState();
    } catch (e) {
        console.warn('[normalize] audio graph init failed:', e);
    }
}

// Con Normalizar activo, el audio pasa por un AudioContext — y iOS lo
// suspende al bloquear la pantalla o pasar la app a segundo plano, cortando
// el sonido en silencio (currentAudio sigue "reproduciendo" pero mudo).
// Se llama antes de cualquier play/prev/next, y también apenas la pestaña
// vuelve a estar visible (desbloqueo de pantalla).
// iOS/Safari usa el estado 'interrupted' (no 'suspended') específicamente
// para este caso — resume() saca al contexto de cualquiera de los dos.
function _resumeAudioCtxIfNeeded() {
    if (_audioCtx && (_audioCtx.state === 'suspended' || _audioCtx.state === 'interrupted')) {
        _audioCtx.resume().catch(() => {});
    }
}

document.addEventListener('visibilitychange', () => {
    _rlog('visibilitychange', { hidden: document.hidden, visibilityState: document.visibilityState });
    if (!document.hidden) _handleUnexpectedPause();   // no-op si no corresponde (ver _shouldBePlaying)
});

// ── Watchdog de reproducción en 2do plano ───────────────────────────────────
// Última red de seguridad: aunque el listener de 'pause' y el resume de
// AudioContext cubren la mayoría de las interrupciones, un evento 'ended' o
// 'pause' se puede perder del todo si el navegador frena el hilo de JS
// mientras la app está en 2do plano (pasa en Android/Chrome-Brave tras un
// rato largo). Cada pocos segundos se revisa si "debería estar sonando" y
// realmente lo está, y si no, se intenta recuperar sin esperar a que el
// usuario reabra la app.
let _watchdogLastTime   = -1;
let _watchdogStallTicks = 0;

setInterval(() => {
    if (!_shouldBePlaying || !currentAudio) { _watchdogStallTicks = 0; return; }

    _rlog('watchdog_tick', {
        currentTime:  currentAudio.currentTime,
        paused:       currentAudio.paused,
        ended:        currentAudio.ended,
        networkState: currentAudio.networkState,
        readyState:   currentAudio.readyState,
        stallTicks:   _watchdogStallTicks,
        audioCtxState: _audioCtx ? _audioCtx.state : null,
    });

    // El 'ended' nunca llegó pero la pista ya terminó — avanzar igual.
    if (currentAudio.ended) { _rlog('watchdog_branch', { branch: 'ended' }); _handleTrackEnded(); return; }

    // Quedó pausado por el sistema y el listener de 'pause' no lo recuperó
    // (p.ej. se perdió el evento) — reintentar.
    if (currentAudio.paused) { _rlog('watchdog_branch', { branch: 'paused' }); _handleUnexpectedPause(); return; }

    // Reporta estar reproduciendo pero currentTime no avanza — pipe/decoder
    // trabado, o (para DSD) el server sigue transcodificando: en un
    // cache-miss, /stream-dsd no manda el primer byte hasta terminar de
    // transcodificar el archivo completo, lo que en discos largos puede
    // tardar bastante más que 8s sin que haya ningún problema real. Antes
    // eso se confundía con un stream caído y disparaba una reconexión
    // innecesaria — por eso a DSD se le da mucho más margen (~32s) antes
    // de considerarlo un stall genuino.
    const t = currentAudio.currentTime;
    if (t === _watchdogLastTime) {
        _watchdogStallTicks++;
        const _track = queue[currentIndex];
        const stallThreshold = (_track && _track.is_dsd) ? 8 : 2;   // ticks de 4s
        if (_watchdogStallTicks >= stallThreshold) {
            _watchdogStallTicks = 0;
            _rlog('watchdog_branch', { branch: 'stall_triggering_reconnect', t, stallThreshold });
            handleAudioError({ type: 'watchdog-stall' });
        }
    } else {
        _watchdogStallTicks = 0;
    }
    _watchdogLastTime = t;
}, 4000);

function _normTick() {
    if (!normalizeEnabled || !_normAnalyser || !currentAudio || currentAudio.paused) return;
    _normAnalyser.getFloatTimeDomainData(_normData);
    let sum = 0;
    for (let i = 0; i < _normData.length; i++) { const v = _normData[i]; sum += v * v; }
    const rms = Math.sqrt(sum / _normData.length);
    if (rms < 0.002) return;   // silence/near-silence — don't chase the noise floor
    let target = NORM_TARGET_RMS / rms;
    target = Math.min(NORM_MAX_GAIN, Math.max(NORM_MIN_GAIN, target));
    _normGain.gain.setTargetAtTime(target, _audioCtx.currentTime, NORM_SMOOTH_SEC);
}

function _applyNormalizeState() {
    if (!_normGain) return;
    if (normalizeEnabled) {
        if (!_normTimer) _normTimer = setInterval(_normTick, NORM_TICK_MS);
    } else if (_normTimer) {
        clearInterval(_normTimer);
        _normTimer = null;
        _normGain.gain.setTargetAtTime(1.0, _audioCtx.currentTime, 0.3);   // ease back to unity
    }
}

function _updateNormalizeButtons() {
    const bar = document.getElementById('normalize-btn');
    const np  = document.getElementById('np-normalize-btn');
    if (bar) bar.classList.toggle('is-active', normalizeEnabled);
    if (np)  np.classList.toggle('np-action-active', normalizeEnabled);
}

function toggleNormalize() {
    normalizeEnabled = !normalizeEnabled;
    try { localStorage.setItem('orbyte_normalize', normalizeEnabled ? '1' : '0'); } catch (e) {}
    // iOS trata el audio que pasa por Web Audio API (AudioContext) como
    // "ambiental" y lo silencia apenas la app deja de estar en primer plano
    // — es una restricción del sistema operativo, no algo que podamos
    // evitar desde acá. Se avisa una sola vez, la primera vez que se activa.
    if (normalizeEnabled) {
        let warned = false;
        try { warned = localStorage.getItem('orbyte_normalize_warned') === '1'; } catch (e) {}
        if (!warned) {
            alert('Con Normalizar activo, la reproducción se silencia si bloqueás la pantalla o cambiás de app (restricción de iOS/Android para audio procesado, no un error de Orbyte). Dejá la app en primer plano mientras la uses.');
            try { localStorage.setItem('orbyte_normalize_warned', '1'); } catch (e) {}
        }
    }
    _ensureNormalizeGraph();
    _resumeAudioCtxIfNeeded();
    _applyNormalizeState();
    _updateNormalizeButtons();
}
window.toggleNormalize = toggleNormalize;
_updateNormalizeButtons();

// ── Expose globals ─────────────────────────────────────────────────────────────

// Normalize a raw track object into the player's internal format
function _normalizeTrack(t) {
    return {
        id:             t.id,
        title:          t.title          || '',
        artist:         t.artist         || t.artist_name || '',
        artist_id:      t.artist_id      || null,
        album:          t.album_name     || t.album       || '',
        album_id:       t.album_id       || null,
        cover_url:      t.cover_url      || buildCoverUrl(t.cover_path || ''),
        file_path:      t.file_path      || t.filepath    || '',
        audio_url:      t.audio_url      || (() => {
            const fp  = t.file_path || t.filepath || '';
            const ext = fp.split('.').pop().toLowerCase();
            return (ext === 'dsf' || ext === 'dff') ? buildDsdStreamUrl(fp) : buildAudioUrl(fp);
        })(),
        duration:       t.duration       || 0,
        codec:          t.codec          || '',
        is_dsd:         t.is_dsd         || 0,
        is_mqa:         t.is_mqa         || 0,
        led_color:      t.led_color      || 'white',
        format_display: t.format_display || '',
        dsd_rate:       t.dsd_rate       || '',
    };
}

// Prepend a track at the current position+1, immediately play it, queue continues after
function prependAndPlay(track) {
    const t = _normalizeTrack(track);
    // Insert right after current position only if something is actually
    // playing right now (same rule as prependTracksAndPlay). Otherwise the
    // queue is empty/just-cleared, so start fresh with only this track --
    // never resurrect stale tracks left over from before.
    const insertAt = (queue.length > 0 && currentAudio && !currentAudio.paused)
        ? currentIndex + 1
        : 0;
    if (insertAt === 0) {
        queue = [t];
    } else {
        queue.splice(insertAt, 0, t);
    }
    document.dispatchEvent(new CustomEvent('queueLoaded', { detail: { tracks: queue } }));
    playTrack(insertAt);
}
window.prependAndPlay = prependAndPlay;

// Insert an array of tracks at the current position (or top if nothing playing),
// preserving everything already in the queue after them, then play from startIdx
function prependTracksAndPlay(tracks, startIdx) {
    const normalized = tracks.map(_normalizeTrack);
    // If queue is empty (e.g. after clearPlaylist), always insert at 0
    const insertAt = (queue.length > 0 && currentAudio && !currentAudio.paused)
        ? currentIndex + 1
        : 0;
    if (insertAt === 0) {
        // Replacing the whole queue — don't keep stale tracks
        queue = normalized;
    } else {
        queue.splice(insertAt, 0, ...normalized);
    }
    document.dispatchEvent(new CustomEvent('queueLoaded', { detail: { tracks: queue } }));
    playTrack(insertAt === 0 ? (startIdx || 0) : insertAt + (startIdx || 0));
}
window.prependTracksAndPlay = prependTracksAndPlay;

// Append a single track to the existing queue without interrupting playback
function appendToQueue(track) {
    const normalized = _normalizeTrack(track);
    queue.push(normalized);
    document.dispatchEvent(new CustomEvent('queueLoaded', { detail: { tracks: queue } }));
    _prewarmDsd(normalized);   // el push puede caer fuera de la ventana de lookahead — precalentar directo
}
window.appendToQueue = appendToQueue;
window.loadQueue  = loadQueue;
window.toggleMute = toggleMute;

window.playTrack = playTrack;
Object.defineProperty(window, 'currentIndex', { get: () => currentIndex });
window.togglePlayPause = togglePlayPause;
window.prevTrack = prevTrack;
window.nextTrack = nextTrack;
window.seekTo = seekTo;
window.setVolume = setVolume;
