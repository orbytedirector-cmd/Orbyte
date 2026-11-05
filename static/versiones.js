/* Versiones de un álbum — modal con pestañas «Calidad» y «Ediciones».
 * Lo abren: la carta en abanico de la vista de artista, el botón «⧉ +N versiones»
 * de las tarjetas (búsqueda, filtros, inicio, favoritos) y «⧉ Otras versiones»
 * de la ficha del álbum.  Datos: GET /api/v1/album/<id>/versiones. */
(function () {
  'use strict';
  let datos = null, pestana = 'calidad', overlay = null;

  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function chipDentro(q) {
    if (!q) return '';
    const dos = q.mixto && q.color2 ? `<span class="vx-led vx-led-2" style="--led:var(--led-${esc(q.color2)})"></span>` : '';
    return `<span class="vx-led" style="--led:var(--led-${esc(q.color || 'yellow')})"></span>${dos}${esc(q.texto)}`;
  }
  function chip(q, grande) {
    return q ? `<span class="vx-chip${grande ? ' vx-chip-lg' : ''}">${chipDentro(q)}</span>` : '';
  }

  async function reproducir(id, btn) {
    if (window.primeAudioForGesture) window.primeAudioForGesture();
    const txt = btn ? btn.textContent : '';
    if (btn) btn.textContent = '…';
    try {
      const tracks = await fetch(`/api/album/${id}/tracks`).then(r => r.json());
      if (Array.isArray(tracks) && tracks.length) {
        const ok = tracks.filter(t => t.audio_url || t.file_path);
        const q = ok.length ? ok : tracks;
        if (window.prependTracksAndPlay) window.prependTracksAndPlay(q, 0);
        else if (window.loadQueue) { window.loadQueue(q); window.playTrack(0); }
      }
    } catch (e) { console.error('versiones: reproducir', e); }
    if (btn) btn.textContent = txt;
  }

  function filaCalidad(v) {
    const marca = v.es_frente ? '<span class="vx-star">★ Original</span>'
      : (v.es_original ? '<span class="vx-orig">Edición original</span>' : '');
    const edicion = esc(v.edicion) + (v.year ? ' · ' + esc(v.year) : '');
    const info = [v.n_discos > 1 ? `${v.n_discos} discos` : (v.track_count ? `${v.track_count} pistas` : ''),
      v.duracion_fmt].filter(Boolean).join(' · ');
    const play = v.es_frente
      ? `<button type="button" class="vx-btn vx-btn-gold" data-play="${v.id}">▶ Reproducir</button>`
      : `<button type="button" class="vx-btn vx-btn-round" data-play="${v.id}" aria-label="Reproducir ${esc(v.edicion)} ${esc(v.calidad ? v.calidad.texto : '')}">▶</button>`;
    return `<div class="vx-row${v.es_frente ? ' vx-sel' : ''}">
      <div class="vx-row-q">${chip(v.calidad, true)}${marca}</div>
      <div class="vx-row-t"><span class="vx-row-name">${edicion}</span>
        <span class="vx-row-sub">${esc(info)}</span>
        ${v.edicion_fuente ? `<span class="vx-row-src">${esc(v.edicion_fuente)}</span>` : ''}</div>
      <div class="vx-row-b">${play}<a class="vx-btn" href="/album/${v.id}">Abrir</a></div>
    </div>`;
  }

  function tarjetaEdicion(e) {
    const calidades = e.versiones.map(v =>
      `<a class="vx-qbtn${v.id === datos.frente_id ? ' vx-qsel' : ''}" href="/album/${v.id}">${chipDentro(v.calidad)}</a>`).join('');
    const marca = e.es_original
      ? '<span class="vx-star">★ Edición original · portada de la carta</span>'
      : (datos.puede_marcar ? `<button type="button" class="vx-link" data-marcar="${e.album_id}" title="Solo administrador: si la detección eligió mal la edición original">Marcar como original</button>` : '');
    const sub = [e.year, e.publisher, e.track_count ? `${e.track_count} pistas` : '', e.duracion_fmt].filter(Boolean).join(' · ');
    const portada = e.cover_url ? `<img src="${esc(e.cover_url)}" alt="" class="vx-ed-cover">` : '<span class="vx-ed-cover"></span>';
    return `<div class="vx-ed${e.es_original ? ' vx-sel' : ''}">
      <div class="vx-ed-head">${portada}<div class="vx-ed-t">${marca}
        <span class="vx-ed-name">${esc(e.etiqueta)}</span><span class="vx-row-sub">${esc(sub)}</span></div></div>
      <div class="vx-ed-q"><span class="vx-cap">Calidades</span><div class="vx-qs">${calidades}</div></div>
      ${e.edicion_fuente ? `<span class="vx-row-src">${esc(e.edicion_fuente)}</span>` : ''}
    </div>`;
  }

  function pistas() {
    if (!datos.pistas_resumen) return '';
    const dif = (datos.pistas_diferencias || []).map(d => {
      const p = [];
      if (d.extra.length) p.push(`<b>Solo en ${esc(d.edicion)}:</b> ${d.extra.map(esc).join(', ')}`);
      if (d.faltan.length) p.push(`<b>Le faltan a ${esc(d.edicion)}:</b> ${d.faltan.map(esc).join(', ')}`);
      return `<div class="vx-dif">${p.join('<br>')}</div>`;
    }).join('');
    return `<div class="vx-pistas"><span>${esc(datos.pistas_resumen)}</span>${dif}</div>`;
  }

  function pintar() {
    const m = overlay.querySelector('.vx-modal');
    const nEd = datos.n_ediciones;
    const portada = datos.cover_url ? `<img src="${esc(datos.cover_url)}" alt="" class="vx-cover">` : '<span class="vx-cover"></span>';
    const cuerpo = pestana === 'calidad'
      ? `<p class="vx-hint">Todas las versiones, de mayor a menor calidad.</p>
         <div class="vx-list">${datos.versiones.map(filaCalidad).join('')}</div>
         <p class="vx-foot">La carta de la discografía muestra la portada y el nombre de la edición original; ▶ la reproduce en su mejor calidad. Escuchas, favoritos y playlists siguen siendo de cada versión.</p>`
      : `<p class="vx-hint">Cada edición con las calidades que tienes de ella. Toca una calidad para abrirla.</p>
         <div class="vx-eds">${datos.ediciones.map(tarjetaEdicion).join('')}</div>${pistas()}`;
    m.innerHTML = `
      <div class="vx-head">${portada}
        <div class="vx-head-t"><span class="vx-title">${esc(datos.titulo)}</span>
          <span class="vx-sub">${esc(datos.artist_name || '')}${datos.year ? ' · ' + esc(datos.year) : ''} · ${datos.n_versiones} versiones${nEd > 1 ? ` en ${nEd} ediciones` : ''}</span></div>
        <button type="button" class="vx-close" aria-label="Cerrar">✕</button></div>
      <div class="vx-tabs" role="tablist">
        <button type="button" role="tab" data-tab="calidad" aria-selected="${pestana === 'calidad'}">CALIDAD · ${datos.n_versiones}</button>
        <button type="button" role="tab" data-tab="ediciones" aria-selected="${pestana === 'ediciones'}">EDICIONES · ${nEd}</button></div>
      <div class="vx-body">${cuerpo}</div>`;
  }

  function cerrar() { if (overlay) overlay.hidden = true; document.removeEventListener('keydown', teclas); }
  function teclas(e) { if (e.key === 'Escape') cerrar(); }

  async function abrir(albumId, tab) {
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'vx-overlay';
      overlay.innerHTML = '<div class="vx-modal" role="dialog" aria-modal="true" aria-label="Versiones del álbum"></div>';
      document.body.appendChild(overlay);
      overlay.addEventListener('click', async e => {
        if (e.target === overlay || e.target.closest('.vx-close')) return cerrar();
        const t = e.target.closest('[data-tab]');
        if (t) { pestana = t.dataset.tab; return pintar(); }
        const p = e.target.closest('[data-play]');
        if (p) return reproducir(+p.dataset.play, p);
        const mk = e.target.closest('[data-marcar]');
        if (mk) {
          mk.disabled = true;
          const r = await fetch(`/api/v1/album/${mk.dataset.marcar}/marcar-original`, { method: 'POST' });
          if (r.ok) { await cargar(mk.dataset.marcar); pintar(); }
          else { mk.disabled = false; mk.textContent = 'No se pudo marcar'; }
        }
      });
    }
    pestana = tab || 'calidad';
    overlay.hidden = false;
    overlay.querySelector('.vx-modal').innerHTML = '<p class="vx-hint" style="padding:2rem">Cargando versiones…</p>';
    document.addEventListener('keydown', teclas);
    try { await cargar(albumId); pintar(); }
    catch (e) { overlay.querySelector('.vx-modal').innerHTML = '<p class="vx-hint" style="padding:2rem">No se pudieron cargar las versiones.</p>'; }
  }

  async function cargar(id) {
    const r = await fetch(`/api/v1/album/${id}/versiones`, { credentials: 'same-origin' });
    if (!r.ok) throw new Error(r.status);
    datos = await r.json();
  }

  // Vista de artista: interruptor «Agrupar versiones» (se recuerda en este navegador).
  function agrupar(on) {
    document.querySelectorAll('.album-grid').forEach(g => g.classList.toggle('vx-sueltas', !on));
    try { localStorage.setItem('orbyte.agruparVersiones', on ? '1' : '0'); } catch (e) {}
  }
  function iniciarInterruptor() {
    const sw = document.getElementById('vx-agrupar');
    if (!sw) return;
    let on = true;
    try { on = localStorage.getItem('orbyte.agruparVersiones') !== '0'; } catch (e) {}
    sw.checked = on; agrupar(on);
    sw.addEventListener('change', () => agrupar(sw.checked));
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciarInterruptor);
  else iniciarInterruptor();

  window.abrirVersiones = abrir;
})();
