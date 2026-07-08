let queue = [];
let currentIndex = 0;
let currentAudio = null;
let lyricsData = null;
let lyricsInterval = null;

const MUSIC_ROOT = "/mnt/musica/";

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
}

function playTrack(index) {
    if (index < 0 || index >= queue.length) return;
    currentIndex = index;
    window.currentIndex = currentIndex;   // expose for np-overlay active-queue marker
    // Re-enable play button now that there's something to play
    const playBtn = document.getElementById('play-btn');
    if (playBtn) playBtn.removeAttribute('data-empty');
    const track = queue[currentIndex];

    // Expose current track globally so base.html lyrics system can read track.id
    window._currentTrack = track;

    if (track.is_dsd) {
        // Play via ffmpeg stream in the browser; also attempt native DAC via MPD (silent on error)
        const streamUrl = track.audio_url || buildDsdStreamUrl(track.file_path);
        if (!currentAudio) {
            currentAudio = new Audio();
            currentAudio.addEventListener('timeupdate', updateProgress);
            currentAudio.addEventListener('error', handleAudioError);
        }
        currentAudio.onended = () => nextTrack();
        currentAudio.src = streamUrl;
        currentAudio._trackDuration = track.duration || 0;
        currentAudio.load();
        currentAudio.play().catch(e => console.error('[DSD] play error:', e));
        window.currentAudio = currentAudio;
        // Show known duration immediately — DSD stream returns Infinity/NaN
        const tt = document.getElementById('total-time');
        if (tt && track.duration) tt.textContent = formatTime(track.duration);
        playViaMPD(track.file_path || '', { silent: true });
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
    }

    currentAudio.onended = () => nextTrack();
    currentAudio.src = track.audio_url || buildAudioUrl(track.file_path);
    currentAudio._trackDuration = track.duration || 0;
    currentAudio.load();
    currentAudio.play().catch(e => console.error('Play error:', e));
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
        currentAudio.play();
        document.getElementById('play-btn').textContent = '⏸';
        dispatchPlayerState(true);
        if (window._currentTrack) updateMediaSession(window._currentTrack, true);
    } else {
        currentAudio.pause();
        document.getElementById('play-btn').textContent = '▶';
        dispatchPlayerState(false);
        if (window._currentTrack) updateMediaSession(window._currentTrack, false);
    }
}

function prevTrack() {
    if (currentIndex > 0) playTrack(currentIndex - 1);
}

function nextTrack() {
    if (currentIndex < queue.length - 1) {
        playTrack(currentIndex + 1);
    } else {
        document.getElementById('play-btn').textContent = '▶';
        dispatchPlayerState(false);
    }
}

function seekTo(percent) {
    if (!currentAudio) return;
    const track = queue[currentIndex];

    // DSD streams are non-seekable (live transcoded FLAC pipe).
    // Restart ffmpeg from the desired position via the ?start= server param.
    if (track && track.is_dsd) {
        const dur = currentAudio._trackDuration || track.duration || 0;
        if (!dur) return;
        const seekSec = Math.max(0, (percent / 100) * dur);
        const baseUrl = buildDsdStreamUrl(track.file_path);
        const seekUrl = `${baseUrl}?start=${seekSec.toFixed(2)}`;
        const wasPlaying = !currentAudio.paused;
        currentAudio.src = seekUrl;
        currentAudio._trackDuration = dur;          // preserve known duration
        currentAudio.load();
        if (wasPlaying) currentAudio.play().catch(e => console.error('[DSD seek]', e));
        return;
    }

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

function handleAudioError(e) {
    const track = queue[currentIndex];

    // DSD: stream can drop mid-song (network/pipe timeout). Auto-reconnect.
    if (track && track.is_dsd && currentAudio) {
        const lastPos = currentAudio.currentTime || 0;
        const dur     = currentAudio._trackDuration || track.duration || 0;
        if (dur > 0 && lastPos > 0.5 && lastPos < dur - 2) {
            console.warn(`[DSD] Stream dropped at ${lastPos.toFixed(1)}s — reconnecting…`);
            const url = `${buildDsdStreamUrl(track.file_path)}?start=${lastPos.toFixed(2)}`;
            currentAudio.src = url;
            currentAudio._trackDuration = dur;
            currentAudio.load();
            currentAudio.play().catch(() => {});
            return;
        }
    }

    console.error('Audio error:', e);
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

    // Action handlers — allow CarPlay/lock-screen controls to work
    navigator.mediaSession.setActionHandler('play',         () => { currentAudio && currentAudio.play(); dispatchPlayerState(true);  });
    navigator.mediaSession.setActionHandler('pause',        () => { currentAudio && currentAudio.pause(); dispatchPlayerState(false); });
    navigator.mediaSession.setActionHandler('previoustrack',() => prevTrack());
    navigator.mediaSession.setActionHandler('nexttrack',    () => nextTrack());
    navigator.mediaSession.setActionHandler('seekto', details => {
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
    // Insert right after current position (or at 0 if nothing playing)
    const insertAt = currentAudio ? currentIndex + 1 : 0;
    queue.splice(insertAt, 0, t);
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
