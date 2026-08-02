let queue = [];
let currentIndex = 0;
let currentAudio = null;
let lyricsData = null;
let lyricsInterval = null;
let _reconnectAttempts = 0;   // caps auto-reconnect retries per track (abrupt-stop recovery)
let _lastProgressPos   = 0;   // highest currentTime actually reached since the last reconnect
const MAX_RECONNECT_ATTEMPTS = 8;   // short-lived mobile network drops can chain several times in a row
let _unexpectedPauseRetries = 0;    // reintentos "gentiles" (solo .play(), sin tocar .src) ya usados en 2do plano para la pista actual
const MAX_HIDDEN_PAUSE_RETRIES = 6;  // ver comentario en _handleUnexpectedPause — cada intento espera HIDDEN_PAUSE_RETRY_DELAY_MS
const HIDDEN_PAUSE_RETRY_DELAY_MS = 250;
let _hiddenStallRetries = 0;   // mismo tipo de reintento gentil que _unexpectedPauseRetries, pero para un stall (handleAudioError) detectado en 2do plano — presupuesto separado porque es un evento distinto, ver handleAudioError

// Cuántos cambios de pista AUTOMÁTICOS (por 'ended', sin ningún toque del
// usuario de por medio) van encadenados desde la última vez que la pestaña
// estuvo visible. La evidencia acumulada (varias pruebas cruzadas PC/Android/
// iOS, incluida una con Flac-DSD-Flac-DSD que descartó que fuera específico
// de DSD) muestra que la falla de reproducción en 2do plano se concentra
// consistentemente en la 2da transición automática de la sesión, sin
// importar el formato de las pistas — nunca en la 1ra, casi nunca después de
// la 2da. Se expone en cada línea de log para que este patrón quede visible
// de entrada, sin tener que reconstruirlo a mano de los timestamps cada vez.
let _bgAutoAdvanceCount = 0;

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

// ── Persistencia de cola/reproducción (sobrevive F5 y navegación) ───────────
// Antes, un F5 (o simplemente navegar a otra página — esta app no es una
// SPA, cada click de verdad recarga) perdía la playlist entera y la posición
// de reproducción sin ningún aviso. sessionStorage es la elección correcta
// acá: sobrevive recargas y navegación dentro de la MISMA pestaña/sesión,
// pero no resucita una cola de hace días si se abre una pestaña nueva.
//
// Al recuperar el estado NO se arranca la reproducción sola — los
// navegadores no dejan hacer autoplay al cargar la página sin un gesto del
// usuario, y aunque dejaran, tampoco tendría sentido: se deja todo listo,
// pausado, en el punto exacto donde estaba, para retomar con un toque en
// Play.
const QUEUE_STORAGE_KEY      = 'orbyte_queue_state';
const QUEUE_STORAGE_MAX_AGE_MS = 12 * 3600 * 1000;   // 12h — no resucitar algo de hace demasiado

function _persistQueueState() {
    try {
        if (!queue.length) { sessionStorage.removeItem(QUEUE_STORAGE_KEY); return; }
        const state = {
            queue,
            currentIndex,
            currentTime:  currentAudio ? (currentAudio.currentTime || 0) : 0,
            wasPlaying:   !!(currentAudio && !currentAudio.paused),
            shuffleEnabled, repeatMode,
            savedAt: Date.now(),
        };
        sessionStorage.setItem(QUEUE_STORAGE_KEY, JSON.stringify(state));
    } catch (e) { /* storage lleno o no disponible — no es motivo para romper nada */ }
}

function _restoreQueueState() {
    let raw;
    try { raw = sessionStorage.getItem(QUEUE_STORAGE_KEY); } catch (e) { return; }
    if (!raw) return;
    let state;
    try { state = JSON.parse(raw); } catch (e) { return; }
    if (!state || !Array.isArray(state.queue) || !state.queue.length) return;
    if (Date.now() - (state.savedAt || 0) > QUEUE_STORAGE_MAX_AGE_MS) return;

    queue = state.queue;
    currentIndex = Math.min(Math.max(0, state.currentIndex || 0), queue.length - 1);
    window.currentIndex = currentIndex;
    if (typeof state.shuffleEnabled === 'boolean') shuffleEnabled = state.shuffleEnabled;
    if (typeof state.repeatMode === 'string') repeatMode = state.repeatMode;

    const track = queue[currentIndex];
    if (!track) return;
    window._currentTrack = track;

    if (!currentAudio) {
        currentAudio = new Audio();
        currentAudio.addEventListener('timeupdate', updateProgress);
        currentAudio.addEventListener('error', handleAudioError);
        currentAudio.addEventListener('pause', _handleUnexpectedPause);
        currentAudio.addEventListener('play',  () => _syncMediaSessionState(true));
        currentAudio.addEventListener('pause', () => _syncMediaSessionState(false));
        _attachDiagListeners(currentAudio);
    }
    currentAudio.src = track.audio_url ||
        (track.is_dsd ? buildDsdStreamUrl(track.file_path) : buildAudioUrl(track.file_path));
    currentAudio._trackDuration = track.duration || 0;
    currentAudio.load();
    const restoreTime = state.currentTime || 0;
    if (restoreTime > 0) {
        currentAudio.addEventListener('loadedmetadata', function _seekOnce() {
            currentAudio.removeEventListener('loadedmetadata', _seekOnce);
            try { currentAudio.currentTime = restoreTime; } catch (e) {}
        });
    }
    window.currentAudio = currentAudio;

    updatePlayerBar(track);
    const playBtn = document.getElementById('play-btn');
    if (playBtn) { playBtn.textContent = '▶'; playBtn.removeAttribute('data-empty'); }
    updateVisualizer(track.led_color);
    dispatchPlayerState(false);
    document.dispatchEvent(new CustomEvent('queueLoaded', { detail: { tracks: queue } }));
    if (typeof _updateShuffleRepeatButtons === 'function') _updateShuffleRepeatButtons();
    _prewarmUpcomingDsd();
    _rlog('queue_restored', { restoredIndex: currentIndex, restoredTime: restoreTime, queueLen: queue.length });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _restoreQueueState);
} else {
    _restoreQueueState();
}

// Guardar en los momentos que importan: cuando cambia la cola/pista, y
// justo antes de que la página se vaya (F5, cerrar, navegar) — este último
// es el que de verdad importa para no perder los últimos segundos de
// posición reproducida.
window.addEventListener('pagehide',    _persistQueueState);
window.addEventListener('beforeunload', _persistQueueState);

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
            bgAutoAdvanceCount: _bgAutoAdvanceCount,
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

// transition_gap_ms mide el silencio REAL que vive el que escucha en cada
// cambio de pista: desde el instante en que se sabe que hace falta un
// recurso de audio nuevo (playTrack de una pista fría, un swap de ventana,
// o el arranque de una reconexión tras un error) hasta que el <audio>
// realmente vuelve a sonar (evento 'playing'). Se guarda una sola vez por
// hueco — si hay varios reintentos en el medio, el reloj arrancó en el
// primero, no se reinicia en cada intento. Un cruce de borde DENTRO de una
// ventana combinada nunca toca esto (nunca hay nada que esperar ahí, por
// diseño) — por eso ese caso no aparece con este evento, lo cual ya es en
// sí mismo la métrica: cuenta cuántas transiciones NO tuvieron hueco.
// Este es el número pensado para comparar de igual a igual una sesión con
// crossfade activado contra una sin activar.
let _transitionGapStartedAt = null;
function _markTransitionGapStart() {
    if (_transitionGapStartedAt === null) _transitionGapStartedAt = performance.now();
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
    audio.addEventListener('playing', () => {
        if (_transitionGapStartedAt !== null) {
            _rlog('transition_gap_ms', {
                ms: Math.round(performance.now() - _transitionGapStartedAt),
                crossfadeEnabled, chainWindowSize: crossfadeEnabled ? CHAIN_WINDOW_SIZE : null,
            });
            _transitionGapStartedAt = null;
        }
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
    _persistQueueState();
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
    _markTransitionGapStart();   // cualquier playTrack() implica pedir un recurso de audio nuevo — acá arranca a contar el posible silencio, hasta que 'playing' lo cierre
    _chainIndices = null;   // cambio manual de pista — lo que estuviera sonando de una ventana combinada ya no aplica
    _chainBoundaries = [];
    _chainOffsetSec = 0;
    _chainSwapInFlight = false;
    currentIndex = index;
    window.currentIndex = currentIndex;   // expose for np-overlay active-queue marker
    _reconnectAttempts = 0;               // fresh track — reset abrupt-stop retry budget
    _unexpectedPauseRetries = 0;
    _hiddenStallRetries = 0;
    if (!document.hidden) _bgAutoAdvanceCount = 0;   // arranque en primer plano — reponer la cuenta
    _lastProgressPos   = 0;
    _shouldBePlaying = true;
    _prewarmUpcomingDsd();
    // Re-enable play button now that there's something to play
    const playBtn = document.getElementById('play-btn');
    if (playBtn) playBtn.removeAttribute('data-empty');
    const track = queue[currentIndex];
    _rlog('playTrack_call', { toIndex: index, title: track && track.title, isDsd: !!(track && track.is_dsd), crossfadeEnabled });

    // Expose current track globally so base.html lyrics system can read track.id
    window._currentTrack = track;

    // Crossfade (ventana rodante de N pistas): si está activo, se pide el
    // archivo combinado de la ventana actual (currentIndex + hasta
    // CHAIN_WINDOW_SIZE-1 siguientes) en vez del archivo normal de una sola
    // pista — la transición entre pistas DENTRO de la ventana se maneja
    // después en _checkChainBoundary(), sin volver a tocar .src/.play()
    // nunca; el paso a la SIGUIENTE ventana lo maneja
    // _maybeSwapToNextChainWindow(). Si esta ventana ya falló una vez
    // (_chainGaveUpKey), no se reintenta en bucle — se reproduce la pista
    // sola y listo.
    let audioSrc = track.audio_url ||
        (track.is_dsd ? buildDsdStreamUrl(track.file_path) : buildAudioUrl(track.file_path));
    let chainWindow = null;
    if (crossfadeEnabled && track.duration) {
        const win = _computeChainWindow(currentIndex);
        if (win.length > 1 && _chainWindowKey(win) !== _chainGaveUpKey) {
            const chainUrl = buildTrackChainUrl(win, crossfadeDurationSec);
            if (chainUrl) {
                audioSrc = chainUrl;
                chainWindow = win;
                _prewarmChainIfNeeded(_computeChainWindow(win[win.length - 1] + 1));
            }
        }
    }
    if (chainWindow) {
        _chainIndices    = chainWindow;
        _chainBoundaries = _computeChainBoundaries(chainWindow);
    } else {
        _chainIndices    = null;
        _chainBoundaries = [];
    }
    _chainOffsetSec = 0;

    if (track.is_dsd) {
        // Play via ffmpeg stream in the browser; also attempt native DAC via MPD (silent on error)
        const streamUrl = audioSrc;
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
        _persistQueueState();
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
    currentAudio.src = audioSrc;
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
    _persistQueueState();
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

    // Si se está reproduciendo una ventana combinada, currentAudio.currentTime
    // cuenta desde el principio del ARCHIVO combinado — _chainOffsetSec es 0
    // mientras se escucha la primera pista de la ventana, y pasa a valer el
    // borde correspondiente apenas se cruza (ver _checkChainBoundary), para
    // que lo que se MUESTRA siga reflejando la posición dentro de la pista
    // visible, no de la ventana entera.
    const displayTime = Math.max(0, currentAudio.currentTime - _chainOffsetSec);
    const dur = currentAudio._trackDuration || 0;
    const pct  = dur ? (displayTime / dur) * 100 : 0;
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
    if (ct)   ct.textContent = formatTime(displayTime);
    if (tt)   tt.textContent = formatTime(dur);
    syncLyrics(displayTime);
    _updateMediaSessionPosition(displayTime, dur);

    // Stream is healthy again — restore the full retry budget instead of
    // letting it get drained by several small drops in a row.
    if (currentAudio.currentTime > _lastProgressPos + 2) {
        _lastProgressPos = currentAudio.currentTime;
        _reconnectAttempts = 0;
        _hiddenStallRetries = 0;
    }

    // Si se está reproduciendo una ventana combinada: revisar si ya se
    // cruzó algún borde interno, y si falta poco para el final real del
    // archivo, intentar pasar a la ventana siguiente. Esto reemplaza por
    // completo al mecanismo de fundido superpuesto anterior (ver
    // comentario grande más arriba).
    if (_chainIndices) {
        _checkChainBoundary();
        _maybeSwapToNextChainWindow();
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
        // A diferencia de handleAudioError (que reasigna .src y llama load(),
        // la acción que arrastraba al freeze de varios minutos en iOS), acá
        // solo se vuelve a llamar .play() sobre el MISMO elemento — sin
        // tocar el buffer ni resetear readyState. Confirmado con logs reales
        // en Android: una pista DSD ya cacheada (prewarm) y con
        // readyState=4 (datos completos) puede igual quedar pausada por el
        // sistema a los pocos milisegundos de arrancar en 2do plano, sin que
        // haya habido ningún stall ni reconexión de por medio — para ESE
        // caso puntual, reintentar .play() es una operación liviana y seria
        // razonable, no el gesto disruptivo que causaba el freeze.
        //
        // Se limita a MAX_HIDDEN_PAUSE_RETRIES intentos por pista, con
        // HIDDEN_PAUSE_RETRY_DELAY_MS de por medio entre cada uno, y solo si
        // ya hay buffer de sobra (readyState>=3, HAVE_FUTURE_DATA) — así no
        // se confunde con un stall real (que ya maneja el watchdog aparte) y
        // no se insiste indefinidamente por si la pausa es en realidad una
        // llamada entrante o otra app tomándose el foco de audio de verdad,
        // donde SÍ hay que respetarla y no pelearla (ver comentario arriba).
        //
        // El delay es a propósito: los logs muestran el propio navegador
        // pausando y despausando el MISMO elemento en ciclos de apenas
        // 15-20ms varias veces seguidas antes de asentarse — reintentar al
        // toque, a esa misma velocidad, es competir contra ese ciclo en vez
        // de darle margen para resolverse solo.
        if (currentAudio.readyState >= 3 && _unexpectedPauseRetries < MAX_HIDDEN_PAUSE_RETRIES) {
            _unexpectedPauseRetries++;
            const attempt      = _unexpectedPauseRetries;
            const audioRef     = currentAudio;
            const indexAtCall  = currentIndex;
            _rlog('unexpected_pause_retry_hidden_scheduled', {
                attempt, currentTime: currentAudio.currentTime, readyState: currentAudio.readyState,
                delayMs: HIDDEN_PAUSE_RETRY_DELAY_MS,
            });
            setTimeout(() => {
                // Puede haber cambiado de pista, o haber vuelto a primer
                // plano (donde ya corre otro camino de recuperación) mientras
                // esperábamos — no pisar nada si el contexto ya cambió.
                if (currentAudio !== audioRef || currentIndex !== indexAtCall || !document.hidden || audioRef.ended) {
                    _rlog('unexpected_pause_retry_hidden_stale', { attempt });
                    return;
                }
                if (!audioRef.paused) {
                    _rlog('unexpected_pause_retry_hidden_already_playing', { attempt });
                    return;
                }
                _rlog('unexpected_pause_retry_hidden', {
                    attempt, currentTime: audioRef.currentTime, readyState: audioRef.readyState,
                });
                audioRef.play().then(() => {
                    _rlog('unexpected_pause_retry_hidden_resolved', { attempt, currentTime: audioRef.currentTime });
                }).catch(e => {
                    _rlog('unexpected_pause_retry_hidden_rejected', { attempt, error: String(e), name: e && e.name });
                });
            }, HIDDEN_PAUSE_RETRY_DELAY_MS);
            return;
        }
        _rlog('unexpected_pause_skip', { reason: 'document_hidden', readyState: currentAudio.readyState, retriesUsed: _unexpectedPauseRetries });
        if (_unexpectedPauseRetries >= MAX_HIDDEN_PAUSE_RETRIES) _showCrossfadeHint();
        return;
    }
    _unexpectedPauseRetries = 0;   // ya en primer plano — reponer el margen para la próxima vez que se oculte
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
    if (document.hidden) _bgAutoAdvanceCount++;
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
    // Si esto dispara mientras se reproducía una ventana combinada, todos
    // sus bordes internos ya se cruzaron hace rato (ver _checkChainBoundary,
    // llamado en cada timeupdate) y el swap a la siguiente ventana o no
    // hacía falta (fin de cola) o no llegó a tiempo — esto es simplemente
    // el final real del archivo combinado. Se limpia el estado por las
    // dudas y se sigue el flujo normal.
    _chainIndices = null;
    _chainBoundaries = [];
    _chainOffsetSec = 0;
    _chainSwapInFlight = false;

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

    // OJO: antes esto abandonaba el combo A+B acá mismo, ANTES de intentar
    // reconectar — es decir, un solo drop de red transitorio (algo muy común
    // en el momento exacto de un stall que dispara este handler) tiraba el
    // crossfade a la basura para el resto de esa pista, aunque el archivo
    // combinado ya estuviera 100% cacheado en el server (confirmado en los
    // logs: "prewarm_pair_response: already_cached" segundos antes del
    // drop). El resultado visible era el corte seco que el crossfade existe
    // para evitar: la pista actual llegaba a su propio final (más corto que
    // el combo, porque el combo mide A+B) y de ahí saltaba con un
    // nextTrack() normal — hard reload con su buffering, en vez de la
    // transición sin cortes que ya estaba sonando. Ahora la decisión de
    // abandonar la ventana se posterga hasta más abajo, después de intentar
    // reconectar A LA MISMA ventana (ver bloque de reconexión) — sólo se la
    // da por perdida si ese reintento también agota el presupuesto o la
    // pista ya está por terminar.
    const wasChained = Array.isArray(_chainIndices) && _chainIndices.length > 1;
    _markTransitionGapStart();   // si el error pasó a mitad de una pista ya sonando (no justo al arrancar), acá es donde arranca el silencio real

    const lastPos = currentAudio.currentTime || 0;
    const realDur = (currentAudio.duration && isFinite(currentAudio.duration)) ? currentAudio.duration : 0;
    const dur     = realDur || currentAudio._trackDuration || track.duration || 0;
    const nearEnd = dur > 0 && lastPos >= dur - 1.5;
    _rlog('handleAudioError_call', {
        reason: e && e.type, lastPos, dur, realDur, nearEnd, reconnectAttempts: _reconnectAttempts,
        networkState: currentAudio.networkState, readyState: currentAudio.readyState, wasChained,
        chainSize: wasChained ? _chainIndices.length : 1,
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
            // Antes esto no hacía nada más que loguear y volver — la pista
            // quedaba trabada hasta que el usuario reabría la app a mano
            // (justo el patrón que _bgAutoAdvanceCount viene marcando: casi
            // siempre en la 2da transición automática en 2do plano). Seguimos
            // sin tocar .src/.load() acá — eso es lo que causaba el freeze
            // largo de varios minutos — pero antes de rendirnos del todo se
            // intenta lo mismo que ya está probado como seguro en
            // _handleUnexpectedPause para este mismo escenario (oculto,
            // buffer ya lleno): sólo volver a llamar .play() sobre el MISMO
            // elemento. Cubre el caso confirmado en los logs de Android de un
            // stream con readyState alto que igual queda mudo por el sistema
            // sin que haya habido ningún drop de datos real — un .play() sin
            // reasignar nada puede destrabarlo; si el stall es de verdad
            // (todavía sin buffer suficiente), esto no hace nada pero tampoco
            // puede empeorar nada.
            if (currentAudio.readyState >= 3 && _hiddenStallRetries < MAX_HIDDEN_PAUSE_RETRIES) {
                _hiddenStallRetries++;
                const attempt     = _hiddenStallRetries;
                const audioRef    = currentAudio;
                const indexAtCall = currentIndex;
                _rlog('hidden_stall_retry_scheduled', {
                    attempt, lastPos, dur, wasChained, readyState: currentAudio.readyState,
                    delayMs: HIDDEN_PAUSE_RETRY_DELAY_MS,
                });
                setTimeout(() => {
                    if (currentAudio !== audioRef || currentIndex !== indexAtCall || !document.hidden || audioRef.ended) {
                        _rlog('hidden_stall_retry_stale', { attempt });
                        return;
                    }
                    _rlog('hidden_stall_retry', { attempt, currentTime: audioRef.currentTime, readyState: audioRef.readyState });
                    audioRef.play().then(() => {
                        _rlog('hidden_stall_retry_resolved', { attempt, currentTime: audioRef.currentTime });
                    }).catch(e2 => {
                        _rlog('hidden_stall_retry_rejected', { attempt, error: String(e2), name: e2 && e2.name });
                    });
                }, HIDDEN_PAUSE_RETRY_DELAY_MS);
            } else {
                _rlog('reconnect_suppressed_hidden', {
                    lastPos, dur, reconnectAttempts: _reconnectAttempts, wasChained,
                    hiddenStallRetriesUsed: _hiddenStallRetries,
                });
            }
            return;
        }
        _reconnectAttempts++;
        // Si el drop pasó reproduciendo una ventana combinada, reconectar A
        // LA MISMA ventana en vez de degradar a la pista sola — es un FLAC
        // servido con Range igual que cualquier otro (ver _serve_audio en
        // el server), así que reconecta exactamente igual. _chainIndices/
        // _chainBoundaries/_chainOffsetSec no se tocan, así que
        // _checkChainBoundary()/_maybeSwapToNextChainWindow() siguen
        // funcionando normal después de esto.
        const chainUrl = wasChained ? buildTrackChainUrl(_chainIndices, crossfadeDurationSec) : null;
        const reconnectSrc = chainUrl ||
            (track.audio_url || (track.is_dsd ? buildDsdStreamUrl(track.file_path) : buildAudioUrl(track.file_path)));
        console.warn(`[player] Stream dropped at ${lastPos.toFixed(1)}s — reconnecting (intento ${_reconnectAttempts})…`);
        _rlog('reconnect_attempt', { attempt: _reconnectAttempts, lastPos, wasChained, toChainUrl: !!chainUrl });
        currentAudio.src = reconnectSrc;
        currentAudio.addEventListener('loadedmetadata', function _seekOnce() {
            currentAudio.removeEventListener('loadedmetadata', _seekOnce);
            if (lastPos > 0) currentAudio.currentTime = lastPos;
        });
        // _trackDuration siempre representa la duración de la pista visible
        // actual (la primera del tramo aún no cruzado, cuando hay ventana)
        // — nunca la duración real del recurso cargado, que al reconectar
        // una ventana mide TODA la ventana y pisaría el total/progreso
        // mostrado con un valor mucho más largo que el de la pista que en
        // realidad se está viendo/escuchando.
        currentAudio._trackDuration = track.duration || currentAudio._trackDuration || 0;
        currentAudio.load();
        currentAudio.play().catch(() => {});
        return;
    }

    // A partir de acá se da por perdido el intento actual (se agotó el
    // presupuesto de reconexión, o la pista ya está casi en su final real).
    // Si era una ventana combinada, ahora sí se abandona para esta
    // combinación — el próximo playTrack()/_maybeSwapToNextChainWindow()
    // para este mismo punto de la cola ya no la va a volver a pedir.
    if (wasChained) {
        _chainGaveUpKey  = _chainWindowKey(_chainIndices);
        _chainIndices    = null;
        _chainBoundaries = [];
        _chainOffsetSec  = 0;
        _chainSwapInFlight = false;
    }

    console.error('Audio error:', e);
    if (!nearEnd) {
        // Retry budget exhausted — don't leave playback silently stuck on a
        // dead track for the rest of the session. Move on so the playlist
        // keeps going instead of appearing to have just "stopped".
        console.warn('[player] Giving up on current track after repeated stream drops — skipping to next.');
        _rlog('handleAudioError_giving_up', { reconnectAttempts: _reconnectAttempts, lastPos, dur, wasChained });
        nextTrack();
        return;
    }
    document.getElementById('play-btn').textContent = '▶';
}

function dispatchPlayerState(playing) {
    document.dispatchEvent(new CustomEvent('playerStateChange', {detail:{playing}}));
}

// ── Media Session API — CarPlay / lock screen / Android Auto ──────────────────
// setPositionState() es lo que le dice al widget de bloqueo (iOS/Android)
// DÓNDE está la reproducción — sin esto, el sistema infiere su propia
// posición asumiendo avance continuo desde la última vez que playbackState
// pasó a 'playing', totalmente desconectado del <audio> real. Eso es lo que
// hacía que el lock screen mostrara un current time/tiempo restante que no
// coincidía con lo que sonaba: cada avance automático de pista (nextTrack),
// cada cruce de borde interno de una ventana combinada (_checkChainBoundary — el título/
// portada cambian pero playbackState nunca se toca, así que el sistema ni
// se entera de que "empezó una pista nueva") y cada reconexión por drop de
// red dejaban al sistema con su propio reloj interno cada vez más
// desalineado del real. Se llama con la MISMA pareja (displayTime, dur) que
// ya usa la barra de progreso del reproductor — nunca currentAudio.currentTime
// crudo, que durante un combo mide el archivo combinado entero, no la pista
// que se ve en pantalla.
function _updateMediaSessionPosition(displayTime, dur) {
    if (!('mediaSession' in navigator) || typeof navigator.mediaSession.setPositionState !== 'function') return;
    if (!dur || !isFinite(dur) || dur <= 0) return;
    const position = Math.min(Math.max(0, displayTime || 0), dur);
    try {
        navigator.mediaSession.setPositionState({
            duration: dur,
            playbackRate: (currentAudio && currentAudio.playbackRate) || 1,
            position,
        });
    } catch (e) { /* posición inválida en un instante de transición — se corrige en el próximo tick */ }
}

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
    // Corregir la posición YA, sin esperar al próximo timeupdate — importante
    // sobre todo al cruzar el borde de un combo, donde el título cambia pero
    // ningún evento 'play'/'pause' se dispara para avisarle al sistema.
    if (currentAudio) {
        const displayTime = Math.max(0, currentAudio.currentTime - _chainOffsetSec);
        const dur = currentAudio._trackDuration || track.duration || 0;
        _updateMediaSessionPosition(displayTime, dur);
    }

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
    _persistQueueState();   // queue ya está vacía acá — esto limpia sessionStorage
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
// ── Crossfade — ventana rodante de N pistas (3ra versión) ─────────────────────
// La 1ra versión (dos <audio> simultáneos superpuestos) quedó bloqueada por
// Chrome en 2do plano: "The play() request was interrupted because
// video-only background media was paused to save power" — Chrome sólo
// tolera UNA sesión de audio "reclamada" (la de navigator.mediaSession) por
// pestaña; un segundo <audio> reproduciendo en paralelo se trata como
// contenido no reclamado y se corta.
//
// Este mecanismo evita el problema de raíz en vez de pelearlo: el servidor
// combina varias pistas seguidas (CHAIN_WINDOW_SIZE) en UN SOLO archivo (con
// un fundido corto en cada borde interno), y el navegador lo reproduce de
// punta a punta como si fuera una sola pista. Nunca hay un segundo .play()
// para las transiciones DENTRO de la ventana — así que nunca se dispara la
// política que bloqueaba la 1ra versión. Cuando currentTime cruza uno de
// esos bordes internos, sólo se actualiza la UI (título, portada, metadata)
// en silencio; el audio nunca se corta ni se reinicia.
//
// La 2da versión (sólo pares — pista actual + siguiente) resolvió el caso
// confirmado por log (la 2da transición automática en 2do plano es la que
// más falla), pero cada cambio de pista seguía siendo un momento de riesgo
// si la app llevaba un buen rato oculta — exactamente lo que rondas
// posteriores de prueba (sesiones largas, transporte público, teléfono
// bloqueado) volvieron a mostrar. Esta versión generaliza el mismo
// mecanismo a una VENTANA de N pistas que se va renovando sola a medida que
// avanza la sesión (ver _maybeSwapToNextChainWindow) — el riesgo de cada
// cambio de ARCHIVO pasa a ser 1 cada CHAIN_WINDOW_SIZE-1 pistas en vez de
// 1 en cada una. Sigue sin ser "toda la cola de una" (demasiado caro de
// transcodificar por adelantado y no compatible con una cola que el usuario
// sigue editando en vivo) — es una cinta transportadora, no un tramo fijo.
let crossfadeEnabled = false;
try { crossfadeEnabled = localStorage.getItem('orbyte_crossfade') === '1'; } catch (e) {}

let crossfadeDurationSec = 4;
try {
    const storedDur = parseFloat(localStorage.getItem('orbyte_crossfade_duration'));
    if (!isNaN(storedDur) && storedDur > 0) crossfadeDurationSec = storedDur;
} catch (e) {}
const CROSSFADE_MIN_SEC = 1;
const CROSSFADE_MAX_SEC = 10;

// Tamaño de la ventana rodante: cuántas pistas seguidas se combinan en un
// solo archivo. Con 2 (el valor original), CADA cambio de pista es un
// momento de riesgo si la app está profundamente en 2do plano — con esto
// en 3+, ese riesgo aparece una vez cada CHAIN_WINDOW_SIZE-1 transiciones
// en vez de en todas. Subirlo cuesta más CPU/tiempo de transcode por
// ventana en el server (ver "[chain-build]" en su log) — arrancar en 3 y
// ajustar con datos reales de sesiones largas antes de subirlo más.
const CHAIN_WINDOW_SIZE = 3;

// Cuánto antes del final REAL de la ventana actual se intenta el cambio a
// la siguiente — bastante más que el propio fundido para tener margen de
// reintento si el primer intento cae oculto (ver _maybeSwapToNextChainWindow).
function _chainSwapLeadSec() { return Math.max(6, crossfadeDurationSec + 2); }

// Si currentAudio está reproduciendo una ventana combinada, acá se guarda
// qué índices de cola entraron (ej. [1,2,3]) y en qué segundo del archivo
// combinado está cada borde interno — null/[] cuando se reproduce una
// pista sola.
let _chainIndices       = null;   // índices de queue[] incluidos en el archivo que se está reproduciendo AHORA
let _chainBoundaries    = [];     // [{index, atSec}] — un elemento por cada borde interno restante por cruzar, en orden
let _chainOffsetSec     = 0;      // cuánto restarle a currentAudio.currentTime para el tiempo "real" de la pista visible
let _chainGaveUpKey     = null;   // key (índices unidos por '-') de la última ventana que falló y no hay que reintentar en bucle
let _chainSwapInFlight  = false;  // ya se disparó un cambio de ventana y se está esperando a que resuelva — evita reasignar .src en cada tick mientras tanto
let _nextChainPrewarmed = null;   // key de la última ventana para la que ya se pidió prewarm — evita pedirlo de nuevo en cada tick

function _chainWindowKey(indices) { return (indices || []).join('-'); }

// Ventana de hasta CHAIN_WINDOW_SIZE índices de cola arrancando en
// startIndex. Si hay menos pistas que eso hasta el final de la cola y
// repeatMode==='all', envuelve al principio (sin repetir la misma pista
// dos veces dentro de la MISMA ventana — playlists más chicas que la
// ventana simplemente quedan con una ventana más corta).
function _computeChainWindow(startIndex) {
    const out = [];
    for (let i = startIndex; i < queue.length && out.length < CHAIN_WINDOW_SIZE; i++) out.push(i);
    if (repeatMode === 'all' && queue.length) {
        for (let i = 0; out.length < CHAIN_WINDOW_SIZE && i < queue.length; i++) {
            if (out.includes(i)) break;
            out.push(i);
        }
    }
    return out;
}

function _computeChainBoundaries(indices) {
    const boundaries = [];
    let cumulative = 0;
    for (let k = 0; k < indices.length; k++) {
        if (k > 0) boundaries.push({ index: indices[k], atSec: cumulative });
        cumulative += (queue[indices[k]] && queue[indices[k]].duration) || 0;
    }
    return boundaries;
}

// fin/fout resuelven la ambigüedad que el server no puede resolver solo:
// ¿la primera pista de esta ventana viene de un fundido anterior (otra
// ventana que ya venía sonando), y sigue algo después de la última como
// para que valga la pena que se apague en vez de terminar seca? Ver
// comentario grande en _build_track_chain (app.py).
function buildTrackChainUrl(indices, fadeSec) {
    if (!indices || indices.length < 2) return null;
    const ids = indices.map(i => queue[i] && queue[i].id).filter(Boolean);
    if (ids.length !== indices.length) return null;
    const fadeInFirst = indices[0] > 0 ? 1 : 0;
    const fadeOutLast = indices[indices.length - 1] < queue.length - 1 ? 1 : 0;
    return `/api/track-chain/${ids.join(',')}?fade=${fadeSec}&fin=${fadeInFirst}&fout=${fadeOutLast}`;
}

function _prewarmChainIfNeeded(indices) {
    if (!crossfadeEnabled || !indices || indices.length < 2) return;
    const key = _chainWindowKey(indices);
    if (_nextChainPrewarmed === key) return;   // ya pedido para esta misma ventana — no repetir en cada tick
    const url = buildTrackChainUrl(indices, crossfadeDurationSec);
    if (!url) return;
    _nextChainPrewarmed = key;
    const t0 = performance.now();
    _rlog('chain_prewarm_requested', { key, n: indices.length, titles: indices.map(i => queue[i] && queue[i].title) });
    try {
        fetch(url.replace('/api/track-chain/', '/api/prewarm-chain/'), { method: 'POST' })
            .then(r => r.json())
            .then(d => _rlog('chain_prewarm_response', { key, status: d.status, elapsedMs: Math.round(performance.now() - t0) }))
            .catch(() => {});
    } catch (e) { /* el prewarm es sólo una optimización — nunca debe romper nada */ }
}

// Se llama desde updateProgress() en cada timeupdate mientras se reproduce
// una ventana combinada — cuando currentTime cruza un borde interno,
// "cambia de pista" sin tocar el audio para nada, sólo actualizando la UI.
// while() en vez de if() porque un salto grande de currentTime entre dos
// ticks (típico tras un rato de throttling en 2do plano) puede cruzar más
// de un borde de una sola vez.
function _checkChainBoundary() {
    if (!_chainIndices || !currentAudio || !_chainBoundaries.length) return;
    while (_chainBoundaries.length && currentAudio.currentTime >= _chainBoundaries[0].atSec) {
        const boundary = _chainBoundaries.shift();
        const nextTrackObj = queue[boundary.index];
        if (!nextTrackObj) continue;
        _rlog('chain_boundary_crossed', {
            newIndex: boundary.index, title: nextTrackObj.title, atTime: currentAudio.currentTime,
            windowPos: _chainIndices.indexOf(boundary.index) + 1, chainSize: _chainIndices.length,
        });
        _chainOffsetSec = boundary.atSec;
        currentIndex = boundary.index;
        window.currentIndex = currentIndex;
        window._currentTrack = nextTrackObj;
        currentAudio._trackDuration = nextTrackObj.duration || 0;
        _reconnectAttempts = 0;
        _unexpectedPauseRetries = 0;
        _hiddenStallRetries = 0;
        _lastProgressPos = currentAudio.currentTime;
        if (!document.hidden) _bgAutoAdvanceCount = 0;

        updatePlayerBar(nextTrackObj);
        updateVisualizer(nextTrackObj.led_color);
        dispatchPlayerState(true);
        clearSyncedLyrics();
        _prewarmUpcomingDsd();
        _persistQueueState();
    }
}

// Se llama desde updateProgress() y desde el watchdog mientras se
// reproduce una ventana — cuando falta poco para el final REAL del
// archivo combinado actual, intenta pasar a la ventana siguiente sin
// cortar audio: reasigna .src al próximo combo (que debería estar
// cacheado — se pidió su prewarm apenas arrancó ESTA ventana, ver
// _prewarmChainIfNeeded) y sigue reproduciendo. chain_swap_* queda en el
// log de cada intento — con eso se puede medir qué tan seguido el prewarm
// llega a tiempo y qué tan seguido el swap cae oculto (chain_swap_deferred_hidden)
// para decidir si CHAIN_WINDOW_SIZE necesita subir.
function _maybeSwapToNextChainWindow() {
    if (!_chainIndices || _chainSwapInFlight || !currentAudio) return;
    const totalDur = (currentAudio.duration && isFinite(currentAudio.duration)) ? currentAudio.duration : 0;
    if (!totalDur) return;
    const remaining = totalDur - currentAudio.currentTime;
    if (remaining > _chainSwapLeadSec()) return;

    const lastIndex   = _chainIndices[_chainIndices.length - 1];
    const nextWindow  = _computeChainWindow(lastIndex + 1);
    if (nextWindow.length < 2) return;   // fin de cola sin repeat, o queda una sola pista suelta — nada que encadenar; _handleTrackEnded()/nextTrack() se hacen cargo como siempre

    const key = _chainWindowKey(nextWindow);
    if (key === _chainGaveUpKey) return;   // esta combinación ya falló una vez — no insistir en bucle

    const url = buildTrackChainUrl(nextWindow, crossfadeDurationSec);
    if (!url) return;

    if (document.hidden) {
        // Mismo motivo que en handleAudioError: reasignar .src en 2do plano
        // es justo lo que congelaba el hilo de JS en pruebas anteriores. Acá
        // el riesgo es menor (el audio actual sigue sonando bien, no
        // venimos de un error) pero se registra igual — si en la práctica
        // estos swaps SÍ andan bien ocultos, se puede sacar esta restricción
        // en una próxima vuelta con los datos de chain_swap_deferred_hidden.
        _rlog('chain_swap_deferred_hidden', { key, remaining, chainSize: nextWindow.length });
        return;   // se reintenta solo en el próximo tick mientras quede margen
    }

    _chainSwapInFlight = true;
    _markTransitionGapStart();   // reasignar .src acá también puede cortar el audio un instante — se mide igual que cualquier otra transición
    const t0 = performance.now();
    _rlog('chain_swap_scheduled', { key, remaining, chainSizeFrom: _chainIndices.length, chainSizeTo: nextWindow.length });
    currentAudio.src = url;
    currentAudio.load();
    currentAudio.play().then(() => {
        _chainSwapInFlight = false;
        _rlog('chain_swap_resolved', { key, elapsedMs: Math.round(performance.now() - t0) });
    }).catch(e => {
        _chainSwapInFlight = false;
        _rlog('chain_swap_rejected', { key, error: String(e), name: e && e.name, elapsedMs: Math.round(performance.now() - t0) });
        // No se revierte el estado de la ventana — igual que en handleAudioError,
        // se sigue adelante y se deja que el watchdog / _handleUnexpectedPause
        // recuperen este mismo elemento si hace falta.
    });

    _chainIndices    = nextWindow;
    _chainBoundaries = _computeChainBoundaries(nextWindow);
    _chainOffsetSec  = 0;
    currentIndex     = nextWindow[0];
    window.currentIndex = currentIndex;
    const newFirstTrack = queue[currentIndex];
    window._currentTrack = newFirstTrack;
    currentAudio._trackDuration = (newFirstTrack && newFirstTrack.duration) || 0;
    _reconnectAttempts = 0; _unexpectedPauseRetries = 0; _hiddenStallRetries = 0;
    _lastProgressPos = 0;
    if (!document.hidden) _bgAutoAdvanceCount = 0;
    if (newFirstTrack) {
        updatePlayerBar(newFirstTrack);
        updateVisualizer(newFirstTrack.led_color);
    }
    clearSyncedLyrics();
    _persistQueueState();

    _prewarmChainIfNeeded(_computeChainWindow(nextWindow[nextWindow.length - 1] + 1));
}

// Aviso puntual, no invasivo: un toast chico que se muestra UNA sola vez en
// total (se recuerda en localStorage), sólo cuando de verdad se topó con el
// corte que el crossfade existe para evitar — no se ofrece a las apuradas ni
// se repite cada vez.
function _showCrossfadeHint() {
    if (crossfadeEnabled) return;
    try { if (localStorage.getItem('orbyte_crossfade_hint_shown') === '1') return; } catch (e) {}
    try { localStorage.setItem('orbyte_crossfade_hint_shown', '1'); } catch (e) {}
    try {
        const toast = document.createElement('div');
        toast.textContent = '💡 Si la reproducción se corta al cambiar de pista en 2do plano, probá activar "Crossfade" en el reproductor.';
        toast.style.cssText = 'position:fixed;left:50%;bottom:90px;transform:translateX(-50%);' +
            'max-width:90vw;background:rgba(20,20,24,0.94);color:#fff;padding:10px 16px;' +
            'border-radius:10px;font-size:13px;line-height:1.4;z-index:9999;' +
            'box-shadow:0 4px 16px rgba(0,0,0,0.35);text-align:center;opacity:0;transition:opacity .4s ease;';
        document.body.appendChild(toast);
        setTimeout(() => { toast.style.opacity = '1'; }, 10);
        setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 500); }, 6000);
    } catch (e) {}
}

function _updateCrossfadeButtons() {
    const bar = document.getElementById('crossfade-btn');
    const np  = document.getElementById('np-crossfade-btn');
    if (bar) bar.classList.toggle('is-active', crossfadeEnabled);
    if (np)  np.classList.toggle('np-action-active', crossfadeEnabled);
}

function toggleCrossfade() {
    crossfadeEnabled = !crossfadeEnabled;
    try { localStorage.setItem('orbyte_crossfade', crossfadeEnabled ? '1' : '0'); } catch (e) {}
    _rlog('crossfade_toggle', { crossfadeEnabled, chainWindowSize: CHAIN_WINDOW_SIZE, fadeSec: crossfadeDurationSec });
    _updateCrossfadeButtons();
}
window.toggleCrossfade = toggleCrossfade;

function setCrossfadeDuration(seconds) {
    const v = Math.min(CROSSFADE_MAX_SEC, Math.max(CROSSFADE_MIN_SEC, Number(seconds) || crossfadeDurationSec));
    crossfadeDurationSec = v;
    try { localStorage.setItem('orbyte_crossfade_duration', String(v)); } catch (e) {}
    return v;
}
window.setCrossfadeDuration = setCrossfadeDuration;
window.getCrossfadeDuration = () => crossfadeDurationSec;

const CROSSFADE_SVG = `<svg class="crossfade-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12c3-6 6-6 9 0s6 6 9 0"/><path d="M3 12c3 6 6 6 9 0s6-6 9 0" opacity="0.4"/></svg>`;

// La plantilla no trae un botón de Crossfade (funcionalidad agregada después) —
// se inserta uno al lado de "Normalizar", copiándole la clase para que quede
// visualmente igual a los demás controles del reproductor, sin tocar HTML/CSS.
function _ensureCrossfadeButton() {
    if (!document.getElementById('crossfade-btn')) {
        const normalizeBtn = document.getElementById('normalize-btn');
        if (normalizeBtn) {
            const btn = document.createElement('button');
            btn.id = 'crossfade-btn';
            btn.className = normalizeBtn.className;
            btn.title = 'Crossfade — combina esta pista y la siguiente en un solo archivo para que nunca haya un corte al pasar de una a otra en 2do plano';
            btn.innerHTML = CROSSFADE_SVG;
            btn.onclick = toggleCrossfade;
            normalizeBtn.insertAdjacentElement('afterend', btn);
        }
    }
    if (!document.getElementById('np-crossfade-btn')) {
        const npNormalizeBtn = document.getElementById('np-normalize-btn');
        if (npNormalizeBtn) {
            const btn = document.createElement('button');
            btn.id = 'np-crossfade-btn';
            btn.className = npNormalizeBtn.className;
            btn.title = 'Crossfade — combina pistas consecutivas para que no se corten en 2do plano';
            btn.innerHTML = CROSSFADE_SVG;
            btn.onclick = toggleCrossfade;
            npNormalizeBtn.insertAdjacentElement('afterend', btn);
        }
    }
    _updateCrossfadeButtons();
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _ensureCrossfadeButton);
} else {
    _ensureCrossfadeButton();
}


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
    if (!document.hidden) _bgAutoAdvanceCount = 0;
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

    // El watchdog corre en su propio setInterval, independiente de
    // 'timeupdate' — le da al swap de ventana una segunda oportunidad de
    // ejecutarse si 'timeupdate' viene throttled en 2do plano.
    if (_chainIndices) _maybeSwapToNextChainWindow();

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
    _persistQueueState();
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
    _persistQueueState();
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
