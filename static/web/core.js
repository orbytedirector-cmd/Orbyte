/* =====================================================================
   Orbyte web — núcleo compartido (Ticket C-03)
   Componentes de Orbyte-Desktop (app.js) rehechos para el navegador:
   Inicio con facetas, vista filtrada, álbum, artista, Buscar y Búsqueda
   avanzada. No sabe nada de invitados: lo que pasa al tocar una pista y
   qué opciones tiene su menú lo decide quien lo usa (ORB.actions), así
   la misma base sirve después para la web general.
   ===================================================================== */
(function (global) {
  "use strict";
  const ORB = {};
  global.ORB = ORB;
  const ASSETS = "/static/web/assets/";

  // ---- Tablas (copiadas tal cual de Desktop app.js) ----
  const LED_COLORS = { yellow: "#F5C518", white: "#F0F0F0", cyan: "#00D4FF", red: "#FF2244", green: "#00E676", blue: "#2979FF", magenta: "#E040FB" };
  const LED_LABELS = { yellow: "PCM CD Quality", white: "PCM Hi-Res", cyan: "DSD 64/128", red: "DSD 256", green: "MQA", blue: "MQA Studio", magenta: "Original Sample Rate" };
  const MOMENTO_LABELS = { morning: "Mañana", evening: "Tarde", night: "Noche", sleep: "Para dormir", party: "Fiesta", workout: "Ejercicio", focus: "Concentración", anytime: "Cualquier momento" };
  const ERA_LABELS = { early_rock_era: "Early Rock", british_invasion_era: "British Invasion", classic_rock_era: "Classic Rock", nwobhm_synth_era: "NWOBHM / Synth", grunge_alternative_era: "Grunge / Alternative", post_millennial_era: "Post-millennial", streaming_era: "Streaming Era", current_era: "Actualidad" };
  const IDIOMA_NAMES = {
    en: "Inglés", es: "Español", de: "Alemán", fr: "Francés", pt: "Portugués", it: "Italiano",
    ja: "Japonés", ko: "Coreano", nl: "Holandés", so: "Somalí", fi: "Finlandés", ru: "Ruso",
    sw: "Suajili", hr: "Croata", no: "Noruego", sv: "Sueco", da: "Danés", pl: "Polaco",
    tr: "Turco", ar: "Árabe", zh: "Chino", hi: "Hindi", th: "Tailandés", id: "Indonesio",
    ro: "Rumano", el: "Griego", he: "Hebreo", cs: "Checo", hu: "Húngaro", uk: "Ucraniano", vi: "Vietnamita",
  };
  const IDIOMA_FLAG_LANGS = new Set(Object.keys(IDIOMA_NAMES));
  const ERA_DECADES = { early_rock_era: "50-60s", british_invasion_era: "60s", classic_rock_era: "70s", nwobhm_synth_era: "80s", grunge_alternative_era: "90s", post_millennial_era: "2000s", streaming_era: "2010s", current_era: "2020s+" };
  const MOMENTO_BADGE_COLORS = {
    anytime: { bg: "#3B2E0D", fg: "#C8920A" }, evening: { bg: "#3B1F12", fg: "#FF9142" },
    morning: { bg: "#3B2B08", fg: "#FFC93D" }, night: { bg: "#171A38", fg: "#7A8CFF" },
    sleep: { bg: "#1A1438", fg: "#8C6BF2" }, workout: { bg: "#3B1212", fg: "#FF5959" },
    party: { bg: "#380F29", fg: "#F259BF" }, focus: { bg: "#0A2924", fg: "#59D9B8" },
  };
  const MOMENTO_ICONS = { anytime: "♪", evening: "🌇", morning: "☀", night: "🌙", sleep: "💤", workout: "♥", party: "🎉", focus: "◎" };
  const CLOUD_TINTS = {
    mood: { tint: "#B8A6EB", background: "#1C1730" }, tema: { tint: "#7ADBBF", background: "#0D211C" },
    genre: { tint: "#E8B84A", background: "#1F1705" }, idioma: { tint: "#6BC7F0", background: "#0A1C26" },
  };
  const FACET_LABELS = { led: "Calidad · LED iFi Zen DAC", mood: "Mood", momento: "Momento del día", era: "Era musical", tema: "Tema lírico", genre: "Género / Subgénero", idioma: "Idioma" };
  // Mismo mapeo que Desktop (home_backend.FACET_FILTER_TYPE): "tema" -> "tema_lirico".
  const FACET_FILTER_TYPE = { led: "led", mood: "mood", momento: "momento", era: "era", tema: "tema_lirico", genre: "genre", idioma: "idioma" };
  const COUNTRY_ISO = {"United States": "us", "United Kingdom": "gb", "England": "gb", "Scotland": "gb", "Wales": "gb", "Northern Ireland": "gb", "Germany": "de", "France": "fr", "Italy": "it", "Spain": "es", "Sweden": "se", "Norway": "no", "Finland": "fi", "Denmark": "dk", "Netherlands": "nl", "Australia": "au", "Canada": "ca", "Brazil": "br", "Argentina": "ar", "Mexico": "mx", "Chile": "cl", "Colombia": "co", "Venezuela": "ve", "Japan": "jp", "South Korea": "kr", "Ireland": "ie", "Portugal": "pt", "Greece": "gr", "Poland": "pl", "Switzerland": "ch", "Iceland": "is", "New Zealand": "nz", "South Africa": "za", "Jamaica": "jm", "Cuba": "cu", "Dominican Republic": "do", "Puerto Rico": "pr", "Philippines": "ph", "Kazakhstan": "kz", "Indonesia": "id"};

  const ledColor = (v) => LED_COLORS[v] || null;
  const capitalize = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  const ledLabel = (v) => LED_LABELS[v] || capitalize(v);
  const momentoLabel = (v) => MOMENTO_LABELS[v] || capitalize(v);
  const eraLabel = (v) => ERA_LABELS[v] || capitalize(String(v).replace(/_/g, " "));
  const idiomaName = (v) => IDIOMA_NAMES[v] || String(v).toUpperCase();
  const langFlagSrc = (c) => (IDIOMA_FLAG_LANGS.has(c) ? ASSETS + "flags/" + c + ".svg" : null);
  const countryFlagSrc = (n) => (COUNTRY_ISO[n] ? ASSETS + "flags/country/" + COUNTRY_ISO[n] + ".svg" : null);
  function maxCountOf(values) { let m = 1; for (const v of values) if (v.count > m) m = v.count; return m; }
  function formatTime(seconds) {
    if (!isFinite(seconds) || seconds < 0) return "0:00";
    const t = Math.floor(seconds), m = Math.floor(t / 60), s = t % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }
  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html !== undefined) e.innerHTML = html; return e; }
  // Portadas reducidas (Ticket D-13 del backend: /cover/...?w=N) — en celular
  // no tiene sentido bajar el original de varios MB.
  function coverUrl(url, w) { return url ? url + (url.includes("?") ? "&" : "?") + "w=" + (w || 240) : null; }
  function setCover(node, url, w) { const u = coverUrl(url, w); if (u) { node.style.backgroundImage = `url("${u}")`; node.textContent = ""; } }
  const ICONS = { back: '<path d="m15 18-6-6 6-6"/>', close: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>' };
  const svgIcon = (n) => `<svg class="ico-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[n] || ""}</svg>`;
  Object.assign(ORB, { el, escapeHtml, formatTime, coverUrl, setCover, ledColor, ledLabel, momentoLabel, eraLabel, idiomaName, langFlagSrc, countryFlagSrc, capitalize, svgIcon });

  // ---- API ----
  ORB.onAuthLost = null;
  ORB.api = async function (path, opts) {
    opts = opts || {};
    const init = { credentials: "same-origin", method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    let r;
    try { r = await fetch(path, init); } catch (e) { throw new Error("Sin conexión con el servidor"); }
    let data = null;
    try { data = await r.json(); } catch (e) { /* sin cuerpo */ }
    if (r.status === 401 && ORB.onAuthLost) ORB.onAuthLost(data);
    if (!r.ok) {
      const err = new Error((data && (data.message || data.error)) || ("HTTP " + r.status));
      err.status = r.status; err.data = data; throw err;
    }
    return data;
  };
  const qs = (o) => Object.keys(o).filter((k) => o[k] !== undefined && o[k] !== null && o[k] !== "")
    .map((k) => [].concat(o[k]).map((v) => encodeURIComponent(k) + "=" + encodeURIComponent(v)).join("&")).join("&");

  // ---- Toast, modal, hojas ----
  ORB.toast = function (msg) {
    let t = document.getElementById("orbyte-toast");
    if (!t) { t = el("div", "orbyte-toast"); t.id = "orbyte-toast"; document.body.appendChild(t); }
    t.textContent = msg; t.classList.add("show");
    clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600);
  };
  ORB.modal = function (title, msg, buttons) {
    return new Promise((resolve) => {
      const ov = el("div", "modal-overlay");
      const box = el("div", "modal-box", `<h3>${escapeHtml(title)}</h3><p>${escapeHtml(msg)}</p>`);
      const row = el("div", "modal-buttons");
      (buttons || [{ label: "OK", value: true, primary: true }]).forEach((b) => {
        const btn = el("button", "btn" + (b.primary ? " primary" : ""), escapeHtml(b.label));
        btn.onclick = () => { ov.remove(); resolve(b.value); };
        row.appendChild(btn);
      });
      box.appendChild(row); ov.appendChild(box);
      ov.addEventListener("click", (e) => { if (e.target === ov) { ov.remove(); resolve(null); } });
      document.body.appendChild(ov);
    });
  };
  ORB.confirm = (title, msg, si, no) => ORB.modal(title, msg, [{ label: no || "No", value: false }, { label: si || "Sí", value: true, primary: true }]);
  // Menú de opciones: en celular, hoja que sube desde abajo (en Desktop es un menú flotante).
  ORB.actionSheet = function (titleHtml, items) {
    const ov = el("div", "action-sheet-overlay");
    const sheet = el("div", "action-sheet");
    if (titleHtml) sheet.appendChild(el("div", "action-sheet-title", titleHtml));
    items.forEach((it) => {
      if (!it) { sheet.appendChild(el("div", "context-menu-sep")); return; }
      const row = el("div", "context-menu-item" + (it.disabled ? " disabled" : "") + (it.danger ? " danger" : ""),
        `<span class="ico">${it.ico || ""}</span><span>${escapeHtml(it.label)}</span>`);
      if (!it.disabled) row.onclick = () => { ov.remove(); it.run(); };
      sheet.appendChild(row);
    });
    ov.appendChild(sheet);
    ov.addEventListener("click", (e) => { if (e.target === ov) ov.remove(); });
    document.body.appendChild(ov);
  };
  ORB.sheet = function (title, opts) {
    opts = opts || {};
    const ov = el("div", "sheet-overlay" + (opts.sub ? " sub" : ""));
    const box = el("div", "sheet-box");
    const bar = el("div", "sheet-bar", `<div class="sheet-title gold-text">${escapeHtml(title)}</div>`);
    const close = el("button", "sheet-close", escapeHtml(opts.closeLabel || "Cerrar"));
    bar.appendChild(close);
    const body = el("div", "sheet-body");
    box.appendChild(bar); box.appendChild(body); ov.appendChild(box);
    const api = { overlay: ov, box, body, bar, close: () => { ov.remove(); if (opts.onClose) opts.onClose(); } };
    close.onclick = api.close;
    ov.addEventListener("click", (e) => { if (e.target === ov) api.close(); });
    document.body.appendChild(ov);
    return api;
  };

  // ---- Pila de vistas a pantalla completa ----
  // El botón "atrás" del celular también cierra la vista de arriba.
  const stack = [];
  ORB.openOverlay = function (opts) {
    const v = el("div", "overlay-view");
    const head = el("div", "filtered-header");
    const back = el("button", "icon-btn np-btn-md", svgIcon("back"));
    back.title = "Volver";
    const tb = el("div", "filtered-title-block", `<div class="filtered-title">${escapeHtml(opts.title || "")}</div><div class="filtered-subtitle">${escapeHtml(opts.subtitle || "")}</div>`);
    head.appendChild(back); head.appendChild(tb);
    if (opts.headerRight) head.appendChild(opts.headerRight);
    v.appendChild(head);
    const scroll = el("div", "filtered-scroll");
    const view = { node: v, head, scroll, setTitle(t, s) { tb.children[0].textContent = t || ""; tb.children[1].textContent = s || ""; }, close: () => history.back() };
    if (opts.beforeScroll) v.appendChild(opts.beforeScroll);
    v.appendChild(scroll);
    back.onclick = () => history.back();
    document.body.appendChild(v);
    stack.push(view);
    history.pushState({ orbOverlay: stack.length }, "");
    return view;
  };
  window.addEventListener("popstate", function () {
    const v = stack.pop();
    if (v) { v.node.remove(); if (v.onClose) v.onClose(); }
    // si había una hoja abierta encima, también se cierra
    document.querySelectorAll(".action-sheet-overlay").forEach((n) => n.remove());
  });
  ORB.closeAllOverlays = function () { while (stack.length) history.back(); };

  // ---- Acciones (las define quien usa el núcleo) ----
  ORB.actions = {
    trackRight: null,      // (t) -> Element a la derecha de la fila (ej. botón +)
    trackRowClass: null,   // (t) -> clase extra de la fila
    onTrackTap: null,      // (t, row) -> acción al tocar la fila
    trackMenu: null,       // (t) -> [{label, ico, run}] opciones extra al principio del menú
    albumMenu: null,       // (a) -> [{...}]
  };

  // ---- Fila de pista (makeFilteredTrackRow + makeMenuTrackRow de Desktop) ----
  function trackSubtitle(t, opts) {
    const parts = [];
    if (!opts.hideArtist && (t.artist_name || t.artist)) parts.push(t.artist_name || t.artist);
    if (!opts.hideAlbum && t.album_name) parts.push(t.album_name);
    return parts.join(" · ");
  }
  ORB.makeTrackRow = function (t, opts) {
    opts = opts || {};
    const extra = ORB.actions.trackRowClass ? ORB.actions.trackRowClass(t) : "";
    const row = el("div", "filtered-track-row" + (extra ? " " + extra : ""));
    row.dataset.trackId = String(t.id);
    const cover = el("div", "cover", "♪");
    setCover(cover, t.cover_url, 96);
    const info = el("div", "info", `<div class="title">${escapeHtml(t.title)}</div>`);
    const sub = trackSubtitle(t, opts);
    if (sub) info.appendChild(el("div", "artist", escapeHtml(sub)));
    if (t.format_display) {
      const c = ledColor(t.format_color || t.led_color);
      info.appendChild(el("div", "format-badge", (c ? `<span class="dot" style="background:${c}"></span>` : "") + escapeHtml(t.format_display)));
    }
    if (opts.below) info.appendChild(opts.below);
    row.appendChild(cover); row.appendChild(info);
    if (t.duration) row.appendChild(el("div", "duration", formatTime(t.duration)));
    if (opts.right) row.appendChild(opts.right);
    else if (ORB.actions.trackRight) { const r = ORB.actions.trackRight(t); if (r) row.appendChild(r); }
    const more = el("div", "more-btn", "⋯");
    more.title = "Más opciones";
    more.onclick = (e) => { e.stopPropagation(); ORB.openTrackMenu(t); };
    row.appendChild(more);
    row.onclick = () => { if (ORB.actions.onTrackTap) ORB.actions.onTrackTap(t, row); else ORB.openTrackMenu(t); };
    return row;
  };
  ORB.openTrackMenu = function (t) {
    const items = (ORB.actions.trackMenu ? ORB.actions.trackMenu(t) : []).slice();
    if (items.length) items.push(null);
    if (t.artist_id) items.push({ ico: "♫", label: "Ir al artista", run: () => ORB.openArtist(t.artist_id, t.artist_name) });
    if (t.album_id) items.push({ ico: "◫", label: "Ir al álbum", run: () => ORB.openAlbum({ id: t.album_id, name: t.album_name, artist_name: t.artist_name }) });
    if (items[items.length - 1] === null) items.pop();
    ORB.actionSheet(`<b>${escapeHtml(t.title)}</b> · ${escapeHtml(t.artist_name || t.artist || "")}`, items);
  };

  // ---- Tarjeta de álbum (makeAlbumCard) ----
  ORB.makeAlbumCard = function (a, opts) {
    opts = opts || {};
    const card = el("div", "filtered-album-card");
    const cover = el("div", "cover", "♪");
    setCover(cover, a.cover_url, 320);
    const meta = opts.hideArtist ? (a.year || "") : [a.artist_name, a.year].filter(Boolean).join(" · ");
    card.appendChild(cover);
    card.appendChild(el("div", "name", escapeHtml(a.name)));
    card.appendChild(el("div", "meta", escapeHtml(meta)));
    card.onclick = () => ORB.openAlbum(a);
    return card;
  };

  // ---- Inicio: facetas (renderHome de Desktop) ----
  function makeSectionCard(title, content, expanded) {
    const card = el("div", "section-card" + (expanded ? " expanded" : ""));
    const header = el("div", "section-header", `<div class="title">${escapeHtml(title)}</div><div class="chevron">›</div>`);
    header.onclick = () => card.classList.toggle("expanded");
    const body = el("div", "section-content");
    if (content) body.appendChild(content); else body.appendChild(el("div", "empty-note", "No hay valores para esta faceta todavía."));
    card.appendChild(header); card.appendChild(body);
    return card;
  }
  function makeDistributionCard(opts) {
    const c = el("div", "distribution-card");
    c.innerHTML = `<div class="row">${opts.dot ? `<span class="dot" style="background:${opts.dot}"></span>` : ""}${opts.leading ? `<span class="leading-text">${escapeHtml(opts.leading)}</span>` : ""}<span class="label">${escapeHtml(opts.label)}</span><span class="count">${opts.count}</span></div><div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, Math.round(opts.frac * 100))}%;background:${opts.bar}"></div></div>`;
    c.onclick = opts.onClick;
    return c;
  }
  function makeTagPill(text, count, tint, flag, onClick) {
    const p = el("div", "tag-pill", (flag ? `<img class="flag" src="${flag}" alt="" onerror="this.remove()">` : "") + `<span>${escapeHtml(text)}</span><span class="count">${count}</span>`);
    p.style.color = tint.tint; p.style.background = tint.background;
    p.onclick = onClick;
    return p;
  }
  function withSectionSearch(content, items) {
    if (items.length <= 10) return content;
    const wrap = el("div", "section-search-wrap");
    const s = el("div", "section-search", `<span class="search-ico">⌕</span><input placeholder="Buscar en esta sección…">`);
    const empty = el("div", "section-search-empty hidden");
    const input = s.querySelector("input");
    input.oninput = () => {
      const q = input.value.trim().toLowerCase();
      let shown = 0;
      Array.from(content.children).forEach((ch) => {
        const ok = !q || (ch.dataset.search || ch.textContent).toLowerCase().includes(q);
        ch.style.display = ok ? "" : "none"; if (ok) shown++;
      });
      empty.textContent = `Sin resultados para “${input.value.trim()}”.`;
      empty.classList.toggle("hidden", shown > 0 || !q);
    };
    wrap.appendChild(s); wrap.appendChild(content); wrap.appendChild(empty);
    return wrap;
  }
  function valueListSheet(title, values, labelFn, onPick) {
    const sh = ORB.sheet(title);
    const search = el("div", "chiplist-search", `<span class="search-ico">⌕</span><input placeholder="Buscar…">`);
    sh.body.style.padding = "0"; sh.body.style.gap = "0";
    sh.body.appendChild(search);
    const list = el("div", "chiplist-list");
    sh.body.appendChild(list);
    const draw = (q) => {
      list.innerHTML = "";
      values.filter((v) => !q || labelFn(v.value).toLowerCase().includes(q)).forEach((v) => {
        const row = el("div", "chiplist-row", `<span class="lbl">${escapeHtml(labelFn(v.value))}</span><span class="cnt">${v.count}</span>`);
        row.onclick = () => { sh.close(); onPick(v.value); };
        list.appendChild(row);
      });
    };
    search.querySelector("input").oninput = (e) => draw(e.target.value.trim().toLowerCase());
    draw("");
  }

  ORB.renderHome = function (container, f) {
    container.innerHTML = "";
    const pick = (key) => (value) => ORB.openFiltered(key, FACET_LABELS[key], value);
    // Calidad
    if (f.led && f.led.length) {
      const g = el("div", "grid-2col"), m = maxCountOf(f.led);
      f.led.forEach((v) => g.appendChild(makeDistributionCard({ dot: ledColor(v.value), label: ledLabel(v.value), count: v.count, frac: v.count / m, bar: ledColor(v.value) || "#C8920A", onClick: () => pick("led")(v.value) })));
      container.appendChild(makeSectionCard(FACET_LABELS.led, g));
    }
    const cloud = (key, values, labelFn, flagFn) => {
      const c = el("div", "tag-cloud");
      values.forEach((v) => {
        const p = makeTagPill(labelFn ? labelFn(v.value) : v.value, v.count, CLOUD_TINTS[key], flagFn ? flagFn(v.value) : null, () => pick(key)(v.value));
        p.dataset.search = labelFn ? labelFn(v.value) : v.value;
        c.appendChild(p);
      });
      return c;
    };
    if (f.mood && f.mood.length) container.appendChild(makeSectionCard(FACET_LABELS.mood, withSectionSearch(cloud("mood", f.mood, capitalize), f.mood)));
    if (f.momento && f.momento.length) {
      const g = el("div", "grid-2col");
      f.momento.forEach((v) => {
        const col = MOMENTO_BADGE_COLORS[v.value] || { bg: "#1A1A1A", fg: "#C8920A" };
        const c = el("div", "momento-card", `<div class="badge" style="background:${col.bg};color:${col.fg}">${MOMENTO_ICONS[v.value] || "♪"}</div><span class="label">${escapeHtml(momentoLabel(v.value))}</span><span class="count">${v.count}</span>`);
        c.onclick = () => pick("momento")(v.value);
        g.appendChild(c);
      });
      container.appendChild(makeSectionCard(FACET_LABELS.momento, g));
    }
    if (f.era && f.era.length) {
      const g = el("div", "grid-2col"), m = maxCountOf(f.era);
      f.era.forEach((v) => g.appendChild(makeDistributionCard({ leading: ERA_DECADES[v.value], label: eraLabel(v.value), count: v.count, frac: v.count / m, bar: "#C8920A", onClick: () => pick("era")(v.value) })));
      container.appendChild(makeSectionCard(FACET_LABELS.era, g));
    }
    if (f.tema && f.tema.length) container.appendChild(makeSectionCard(FACET_LABELS.tema, withSectionSearch(cloud("tema", f.tema, capitalize), f.tema)));
    if (f.genre && f.genre.length) {
      const all = (f.all_genres && f.all_genres.length ? f.all_genres : f.genre).slice().sort((a, b) => b.count - a.count);
      const c = cloud("genre", all.slice(0, 10));
      if (all.length > 10) {
        const more = el("div", "see-more-chip", `Ver los ${all.length} →`);
        more.onclick = () => valueListSheet(FACET_LABELS.genre, all, (v) => v, pick("genre"));
        c.appendChild(more);
      }
      container.appendChild(makeSectionCard(FACET_LABELS.genre, c));
    }
    if (f.idioma && f.idioma.length) {
      const main = f.idioma.filter((v) => v.count >= 50), rest = f.idioma.filter((v) => v.count < 50);
      const c = cloud("idioma", main.length ? main : f.idioma, idiomaName, langFlagSrc);
      if (main.length && rest.length) {
        const more = el("div", "see-more-chip", `Otros (${rest.length}) →`);
        more.onclick = () => valueListSheet(FACET_LABELS.idioma, rest, idiomaName, pick("idioma"));
        c.appendChild(more);
      }
      container.appendChild(makeSectionCard(FACET_LABELS.idioma, c));
    }
    if (f.recent_albums && f.recent_albums.length) {
      container.appendChild(el("div", "", '<div id="recent-albums-title">Últimos agregados</div>'));
      const row = el("div", ""); row.id = "recent-albums-row";
      f.recent_albums.forEach((a) => {
        const item = el("div", "recent-album");
        const cov = el("div", "cover", "♪"); setCover(cov, a.cover_url, 240);
        item.appendChild(cov); item.appendChild(el("div", "name", escapeHtml(a.name)));
        item.onclick = () => ORB.openAlbum(a);
        row.appendChild(item);
      });
      container.appendChild(row);
    }
  };

  // ---- Vista filtrada (openFilteredView) ----
  ORB.openFiltered = function (facetKey, label, value) {
    const ft = FACET_FILTER_TYPE[facetKey] || facetKey;
    const shown = facetKey === "led" ? ledLabel(value) : facetKey === "momento" ? momentoLabel(value)
      : facetKey === "era" ? eraLabel(value) : facetKey === "idioma" ? idiomaName(value) : capitalize(String(value));
    const tabs = el("div", "filtered-tabs", '<button class="filtered-tab active" data-t="albums">Álbumes</button><button class="filtered-tab" data-t="tracks">Pistas</button>');
    const v = ORB.openOverlay({ title: label, subtitle: shown, beforeScroll: tabs });
    const st = { tab: "albums", albums: [], aPage: 0, aTotal: 1, tracks: [], tPage: 0, tTotal: 1, busy: false };
    const load = async () => {
      if (st.busy) return;
      st.busy = true;
      try {
        if (st.tab === "albums") {
          const r = await ORB.api("/api/v1/albums?" + qs({ filter_type: ft, filter_value: value, page: st.aPage + 1 }));
          st.albums = st.albums.concat(r.albums || []); st.aPage = r.page || st.aPage + 1; st.aTotal = r.total_pages || 1;
        } else {
          const r = await ORB.api("/api/v1/meta/tracks?" + qs({ field: ft, value: value, page: st.tPage + 1, sort: "popularidad", dir: "desc" }));
          st.tracks = st.tracks.concat(r.tracks || []); st.tPage = r.page || st.tPage + 1; st.tTotal = r.total_pages || 1;
        }
      } catch (e) { v.scroll.innerHTML = `<div class="filtered-empty">${escapeHtml(e.message)}</div>`; st.busy = false; return; }
      st.busy = false; draw();
    };
    const draw = () => {
      v.scroll.innerHTML = "";
      if (st.tab === "albums") {
        if (!st.albums.length) { v.scroll.innerHTML = '<div class="filtered-empty">No hay álbumes para este valor.</div>'; return; }
        const g = el("div", "filtered-albums-grid");
        st.albums.forEach((a) => g.appendChild(ORB.makeAlbumCard(a)));
        v.scroll.appendChild(g);
        if (st.aPage < st.aTotal) v.scroll.appendChild(loadMore(load));
      } else {
        if (!st.tracks.length) { v.scroll.innerHTML = '<div class="filtered-empty">No hay pistas para este valor.</div>'; return; }
        const list = el("div");
        st.tracks.forEach((t) => list.appendChild(ORB.makeTrackRow(t)));
        v.scroll.appendChild(list);
        if (st.tPage < st.tTotal) v.scroll.appendChild(loadMore(load));
      }
    };
    tabs.querySelectorAll(".filtered-tab").forEach((b) => b.onclick = () => {
      st.tab = b.dataset.t;
      tabs.querySelectorAll(".filtered-tab").forEach((x) => x.classList.toggle("active", x === b));
      const loaded = st.tab === "albums" ? st.aPage : st.tPage;
      if (!loaded) { v.scroll.innerHTML = '<div class="filtered-loading">Cargando...</div>'; load(); } else draw();
    });
    v.scroll.innerHTML = '<div class="filtered-loading">Cargando...</div>';
    load();
    v.refresh = draw;
    return v;
  };
  function loadMore(fn) {
    const b = el("button", "filtered-load-more", "Cargar más");
    b.onclick = () => { b.textContent = "Cargando..."; b.disabled = true; fn(); };
    return b;
  }

  // ---- Álbum (openAlbumInFilteredView / renderAlbumDetail) ----
  ORB.openAlbum = function (a) {
    const v = ORB.openOverlay({ title: a.name || "Álbum", subtitle: a.artist_name || "" });
    v.scroll.innerHTML = '<div class="filtered-loading">Cargando...</div>';
    (async () => {
      try {
        const r = await ORB.api(`/api/v1/albums/${a.id}/tracks`);
        const tracks = (r.tracks || []).map((t) => Object.assign({ artist_name: a.artist_name, album_id: a.id, artist_id: a.artist_id || t.artist_id }, t));
        v.scroll.innerHTML = "";
        if (!tracks.length) { v.scroll.innerHTML = '<div class="filtered-empty">Este álbum no tiene pistas.</div>'; return; }
        if (ORB.actions.albumHeader) { const h = ORB.actions.albumHeader(a, tracks); if (h) v.scroll.appendChild(h); }
        const list = el("div");
        tracks.forEach((t) => list.appendChild(ORB.makeTrackRow(t, { hideArtist: true, hideAlbum: true })));
        v.scroll.appendChild(list);
      } catch (e) { v.scroll.innerHTML = `<div class="filtered-empty">${escapeHtml(e.message)}</div>`; }
    })();
    return v;
  };

  // ---- Artista (renderArtist) ----
  ORB.openArtist = function (id, name) {
    const v = ORB.openOverlay({ title: name || "Artista" });
    v.scroll.innerHTML = '<div class="filtered-loading">Cargando...</div>';
    (async () => {
      let d;
      try { d = await ORB.api(`/api/v1/artist/${id}`); } catch (e) { v.scroll.innerHTML = `<div class="filtered-empty">${escapeHtml(e.message)}</div>`; return; }
      v.setTitle(d.name || name || "Artista");
      v.scroll.innerHTML = "";
      const fmt = (n) => Number(n).toLocaleString("es-CL");
      const flag = countryFlagSrc(d.nationality);
      const head = el("div", "artist-head");
      head.appendChild(el("div", "artist-name-row", `<div class="artist-name gold-text">${escapeHtml(d.name)}</div>${flag ? `<img class="flag" src="${flag}" alt="" onerror="this.remove()">` : ""}`));
      const stats = [];
      if (d.total_tracks) stats.push(`<span><b>${fmt(d.total_tracks)}</b> pistas</span>`);
      if (d.albums && d.albums.length) stats.push(`<span><b>${d.albums.length}</b> álbumes</span>`);
      if (d.lastfm_listeners) stats.push(`<span><b>${fmt(d.lastfm_listeners)}</b> oyentes</span>`);
      if (stats.length) head.appendChild(el("div", "artist-stats", stats.join("")));
      if (d.genres && d.genres.length) head.appendChild(el("div", "pl-chips", d.genres.slice(0, 6).map((g) => `<span class="pl-chip gold">${escapeHtml(g)}</span>`).join("")));
      if (d.bio) {
        const bio = el("div", "artist-bio clamped", escapeHtml(d.bio));
        const more = el("div", "artist-bio-more", "Ver más");
        more.onclick = () => { const c = bio.classList.toggle("clamped"); more.textContent = c ? "Ver más" : "Ver menos"; };
        head.appendChild(bio); head.appendChild(more);
      }
      v.scroll.appendChild(head);
      const tabsDef = [];
      if (d.albums && d.albums.length) tabsDef.push(["General", () => {
        const g = el("div", "filtered-albums-grid");
        d.albums.forEach((a) => g.appendChild(ORB.makeAlbumCard(Object.assign({ artist_name: d.name, artist_id: d.id }, a), { hideArtist: true })));
        return g;
      }]);
      if (d.top_tracks && d.top_tracks.length) tabsDef.push(["🔥 Populares", () => {
        const l = el("div");
        d.top_tracks.forEach((t) => l.appendChild(ORB.makeTrackRow(Object.assign({ artist_name: d.name, artist_id: d.id }, t), { hideArtist: true })));
        return l;
      }]);
      if (!tabsDef.length) { v.scroll.appendChild(el("div", "filtered-empty", "Este artista no tiene álbumes.")); return; }
      const tabs = el("div", "artist-tabs"), body = el("div");
      tabsDef.forEach(([label, fn], i) => {
        const b = el("button", "artist-tab" + (i === 0 ? " active" : ""), escapeHtml(label));
        b.onclick = () => { tabs.querySelectorAll(".artist-tab").forEach((x) => x.classList.toggle("active", x === b)); body.innerHTML = ""; body.appendChild(fn()); };
        tabs.appendChild(b);
      });
      v.scroll.appendChild(tabs); v.scroll.appendChild(body);
      body.appendChild(tabsDef[0][1]());
    })();
    return v;
  };

  // ---- Buscar (renderSearch) ----
  ORB.mountSearch = function (root) {
    root.innerHTML = `<div class="page-title gold-text">Buscar</div>
      <div class="search-field"><span class="search-ico">⌕</span><input type="search" class="search-input" placeholder="Artistas, álbumes o canciones..." autocomplete="off" spellcheck="false" enterkeyhint="search"><span class="search-clear hidden">✕</span></div>
      <div class="adv-link"><span class="ico">☰</span>Búsqueda avanzada<span class="chev">›</span></div>
      <div class="search-body"></div>`;
    const input = root.querySelector(".search-input"), clear = root.querySelector(".search-clear"), body = root.querySelector(".search-body");
    root.querySelector(".adv-link").onclick = () => ORB.openAdvanced();
    let timer = null, seq = 0;
    const state = (txt) => { body.innerHTML = `<div class="search-state">${txt}</div>`; };
    state("Buscá artistas, álbumes o canciones en tu biblioteca.");
    const run = async (q) => {
      const my = ++seq;
      state("Buscando…");
      let r;
      try { r = await ORB.api("/api/v1/search?" + qs({ q })); } catch (e) { if (my === seq) state(escapeHtml(e.message)); return; }
      if (my !== seq) return;
      const ar = r.artists || [], al = r.albums || [], tr = r.tracks || [];
      if (!ar.length && !al.length && !tr.length) { state(`Sin resultados para “${escapeHtml(q)}”.`); return; }
      body.innerHTML = "";
      if (ar.length) {
        body.appendChild(el("div", "search-section-title", "Artistas"));
        const list = el("div", "fav-artist-list");
        ar.forEach((a) => {
          const n = a.album_count || 0;
          const sub = [a.most_common_genre, n + " álbum" + (n === 1 ? "" : "es")].filter(Boolean).join(" · ");
          const row = el("div", "fav-artist-row", `<div class="fav-artist-icon">👤</div><div class="fav-artist-info"><div class="fav-artist-name">${escapeHtml(a.name || "?")}</div><div class="fav-artist-sub">${escapeHtml(sub)}</div></div><div class="pl-row-chevron">›</div>`);
          row.onclick = () => ORB.openArtist(a.id, a.name);
          list.appendChild(row);
        });
        body.appendChild(list);
      }
      if (al.length) {
        body.appendChild(el("div", "search-section-title", "Álbumes"));
        const g = el("div", "filtered-albums-grid");
        al.forEach((a) => g.appendChild(ORB.makeAlbumCard(a)));
        body.appendChild(g);
      }
      if (tr.length) {
        body.appendChild(el("div", "search-section-title", "Pistas"));
        const l = el("div");
        tr.slice(0, 20).forEach((t) => l.appendChild(ORB.makeTrackRow(t)));
        body.appendChild(l);
      }
    };
    input.oninput = () => {
      const q = input.value.trim();
      clear.classList.toggle("hidden", !input.value);
      clearTimeout(timer);
      if (q.length < 2) { seq++; state("Buscá artistas, álbumes o canciones en tu biblioteca."); return; }
      timer = setTimeout(() => run(q), 300);
    };
    input.onkeydown = (e) => { if (e.key === "Enter") { input.blur(); const q = input.value.trim(); if (q.length >= 2) { clearTimeout(timer); run(q); } } };
    clear.onclick = () => { input.value = ""; input.oninput(); input.focus(); };
    ORB.refreshSearch = () => { const q = input.value.trim(); if (q.length >= 2) run(q); };
  };

  // ---- Búsqueda avanzada (renderAdv + showAdvFiltersSheet) ----
  const ADV_SORTS = {
    albums: [["popularidad", "Popularidad"], ["nombre", "Nombre"], ["artista", "Artista"], ["anio", "Año"], ["pistas", "N° de pistas"]],
    tracks: [["popularidad", "Popularidad"], ["titulo", "Título"], ["artista", "Artista"], ["anio", "Año"], ["calidad", "Calidad"], ["bpm", "BPM"], ["energia", "Energía"], ["bailabilidad", "Bailabilidad"]],
  };
  let advOptions = null;
  ORB.openAdvanced = function () {
    const filterBtn = el("button", "pl-action-btn", '☰ Filtros<span class="adv-badge hidden"></span>');
    const controls = el("div", "adv-controls hidden", `<div class="adv-mode"><button class="adv-mode-btn active" data-mode="albums">⊞ Álbumes</button><button class="adv-mode-btn" data-mode="tracks">♪ Pistas</button></div><div class="spacer"></div><select class="pl-sort"></select><button class="pl-action-btn adv-dir">↓</button>`);
    const v = ORB.openOverlay({ title: "Búsqueda avanzada", headerRight: filterBtn, beforeScroll: controls });
    const st = { applied: {}, mode: "albums", sort: "popularidad", dir: "desc", items: [], page: 0, totalPages: 1, total: 0, busy: false, searched: false, seq: 0 };
    const sortSel = controls.querySelector(".pl-sort"), dirBtn = controls.querySelector(".adv-dir");
    const fillSorts = () => { sortSel.innerHTML = ADV_SORTS[st.mode].map(([k, l]) => `<option value="${k}"${k === st.sort ? " selected" : ""}>${l}</option>`).join(""); };
    const countFilters = () => Object.values(st.applied).reduce((n, a) => n + a.length, 0);
    const intro = () => {
      v.scroll.innerHTML = "";
      const box = el("div", "adv-intro", '<div class="ico">☰</div><div>Combiná género, mood, era, popularidad y más para encontrar exactamente lo que buscás.</div>');
      const b = el("button", "orbitron-big-btn", "Elegir filtros"); b.style.maxWidth = "260px"; b.style.margin = "18px auto 0";
      b.onclick = openFilters; box.appendChild(b); v.scroll.appendChild(box);
    };
    const params = (page) => Object.assign({ view: st.mode, page, sort: st.sort, dir: st.dir }, st.applied);
    const search = async (reset) => {
      if (reset) { st.items = []; st.page = 0; st.totalPages = 1; st.seq++; v.scroll.innerHTML = '<div class="filtered-loading">Buscando…</div>'; }
      if (st.busy || st.page >= st.totalPages) return;
      st.busy = true; const my = st.seq;
      try {
        const r = await ORB.api("/api/v1/search/advanced?" + qs(params(st.page + 1)));
        if (my !== st.seq) return;
        st.items = st.items.concat(st.mode === "tracks" ? (r.tracks || []) : (r.albums || []));
        st.page = r.page || st.page + 1; st.totalPages = r.total_pages || 1; st.total = r.total || st.items.length;
        st.searched = true; draw();
      } catch (e) { v.scroll.innerHTML = `<div class="filtered-empty">${escapeHtml(e.message)}</div>`; }
      finally { st.busy = false; }
    };
    const summaryHtml = (applied) => {
      const parts = SECTIONS.filter((s) => (applied[s.key] || []).length).map((s) =>
        `<div class="adv-summary-item"><span class="k">${escapeHtml(s.title)}:</span> <span class="v">${applied[s.key].map((x) => escapeHtml(s.label({ value: x, count: 0 }, true))).join('<span class="sep">, </span>')}</span></div>`);
      return parts.length ? `<div class="adv-summary">${parts.join("")}</div>` : '<div class="adv-summary-empty">Sin filtros: toda la biblioteca.</div>';
    };
    const draw = () => {
      const n = countFilters();
      const badge = filterBtn.querySelector(".adv-badge"); badge.textContent = n; badge.classList.toggle("hidden", !n);
      controls.classList.toggle("hidden", !st.searched);
      if (!st.searched) { intro(); return; }
      v.scroll.innerHTML = "";
      const sum = el("div", "adv-summary-results", summaryHtml(st.applied)); sum.onclick = openFilters;
      v.scroll.appendChild(sum);
      v.scroll.appendChild(el("div", "adv-count", st.total + (st.mode === "tracks" ? " pistas" : " álbumes")));
      if (!st.items.length) { v.scroll.appendChild(el("div", "filtered-empty", st.mode === "tracks" ? "Ninguna pista matchea estos filtros." : "Ningún álbum matchea estos filtros.")); return; }
      if (st.mode === "tracks") { const l = el("div"); st.items.forEach((t) => l.appendChild(ORB.makeTrackRow(t))); v.scroll.appendChild(l); }
      else { const g = el("div", "filtered-albums-grid"); st.items.forEach((a) => g.appendChild(ORB.makeAlbumCard(a))); v.scroll.appendChild(g); }
      if (st.page < st.totalPages) v.scroll.appendChild(el("div", "filtered-loading", "Cargando más…"));
    };
    v.scroll.addEventListener("scroll", () => {
      if (st.searched && !st.busy && st.page < st.totalPages && v.scroll.scrollTop + v.scroll.clientHeight > v.scroll.scrollHeight - 400) search(false);
    });
    controls.querySelectorAll(".adv-mode-btn").forEach((b) => b.onclick = () => {
      st.mode = b.dataset.mode; st.sort = "popularidad";
      controls.querySelectorAll(".adv-mode-btn").forEach((x) => x.classList.toggle("active", x === b));
      fillSorts(); search(true);
    });
    sortSel.onchange = () => { st.sort = sortSel.value; search(true); };
    dirBtn.onclick = () => { st.dir = st.dir === "desc" ? "asc" : "desc"; dirBtn.textContent = st.dir === "desc" ? "↓" : "↑"; search(true); };
    filterBtn.onclick = openFilters;
    fillSorts(); intro();

    let SECTIONS = [];
    async function openFilters() {
      if (!advOptions) {
        try { advOptions = await ORB.api("/api/v1/search/advanced/options"); }
        catch (e) { ORB.modal("Error", e.message || "No se pudieron cargar los filtros."); return; }
      }
      const o = advOptions;
      const fv = (arr) => (arr || []).map((x) => (typeof x === "object" ? x : { value: String(x), count: 0 }));
      const genres = (() => { const seen = {}, out = []; fv(o.genres_primary).concat(fv(o.genres_classic)).forEach((g) => { if (!seen[g.value]) { seen[g.value] = 1; out.push(g); } }); return out.sort((a, b) => b.count - a.count); })();
      const withCount = (v, plain) => (plain ? v.value : v.value + " (" + v.count + ")");
      SECTIONS = [
        { title: "Calidad", key: "calidad", opts: fv(o.quality_options), label: (v) => v.value },
        { title: "Género", key: "genero", opts: genres, label: withCount, topN: 10 },
        { title: "Mood", key: "mood", opts: fv(o.moods), label: withCount },
        { title: "Momento del día", key: "momento", opts: fv(o.momentos), label: (v) => momentoLabel(v.value) },
        { title: "Era musical", key: "era", opts: fv(o.eras), label: (v) => eraLabel(v.value) },
        { title: "Categoría / Tema", key: "tema", opts: fv(o.temas), label: withCount },
        { title: "Idioma", key: "idioma", opts: fv(o.languages), label: (v) => idiomaName(v.value), threshold: 50, flag: langFlagSrc },
        { title: "País de origen", key: "pais", opts: fv(o.nationalities), label: withCount, topN: 10, flag: countryFlagSrc },
        { title: "Año", key: "anio", opts: fv(o.available_years).sort((a, b) => String(b.value).localeCompare(String(a.value))), label: (v) => v.value, topN: 10 },
        { title: "Popularidad", key: "popularidad", opts: fv(o.popularity_buckets), label: (v) => { const n = parseInt(v.value, 10); return isNaN(n) ? v.value : "★".repeat(n); }, buckets: true },
        { title: "Energía", key: "energia", opts: fv(o.energy_buckets), label: (v) => capitalize(v.value), buckets: true },
        { title: "Bailabilidad", key: "bailabilidad", opts: fv(o.danceability_buckets), label: (v) => capitalize(v.value), buckets: true },
      ];
      const draft = JSON.parse(JSON.stringify(st.applied));
      const sh = ORB.sheet("Filtros", { closeLabel: "Cancelar" });
      const summary = el("div", "sheet-summary"); sh.box.insertBefore(summary, sh.body);
      const footer = el("div", "sheet-footer"); sh.box.appendChild(footer);
      const flagHtml = (src) => (src ? `<img class="fflag" src="${src}" alt="" onerror="this.remove()">` : "");
      const toggle = (key, value) => { const a = draft[key] || (draft[key] = []); const i = a.indexOf(value); if (i >= 0) a.splice(i, 1); else a.push(value); if (!a.length) delete draft[key]; };
      const render = () => {
        summary.innerHTML = summaryHtml(draft);
        sh.body.innerHTML = "";
        SECTIONS.forEach((sec) => {
          if (!sec.opts.length) return;
          const sel = draft[sec.key] || [];
          const wrap = el("div", "fsec");
          wrap.appendChild(el("div", "fsec-head", escapeHtml(sec.title) + (sel.length ? `<span class="fsec-count">${sel.length}</span>` : "")));
          const chips = el("div", sec.buckets ? "fbuckets" : "fchips");
          let visible = sec.opts, rest = [];
          if (sec.topN) { visible = sec.opts.slice(0, sec.topN); rest = sec.opts.slice(sec.topN); }
          if (sec.threshold) { visible = sec.opts.filter((x) => x.count >= sec.threshold); rest = sec.opts.filter((x) => x.count < sec.threshold); if (!visible.length) { visible = sec.opts; rest = []; } }
          // lo elegido de "rest" también se muestra como chip
          rest.filter((x) => sel.includes(x.value)).forEach((x) => visible = visible.concat([x]));
          visible.forEach((opt) => {
            const b = el("button", "fchip" + (sel.includes(opt.value) ? " on" : ""), flagHtml(sec.flag && sec.flag(opt.value)) + escapeHtml(sec.label(opt)));
            b.onclick = () => { toggle(sec.key, opt.value); render(); };
            chips.appendChild(b);
          });
          if (rest.length) {
            const more = el("button", "fchip more", sec.threshold ? `Otros (${rest.length}) →` : `Ver los ${sec.opts.length} →`);
            more.onclick = () => chipList(sec, () => render());
            chips.appendChild(more);
          }
          wrap.appendChild(chips);
          sh.body.appendChild(wrap);
        });
        const n = Object.values(draft).reduce((k, a) => k + a.length, 0);
        footer.innerHTML = "";
        const clear = el("button", "btn link", "Limpiar"); clear.onclick = () => { Object.keys(draft).forEach((k) => delete draft[k]); render(); };
        const apply = el("button", "btn primary", n ? `Aplicar (${n})` : "Ver todo");
        apply.onclick = () => { st.applied = JSON.parse(JSON.stringify(draft)); sh.close(); search(true); };
        footer.appendChild(clear); footer.appendChild(el("div", "spacer")); footer.appendChild(apply);
      };
      const chipList = (sec, done) => {
        const sub = ORB.sheet(sec.title, { sub: true, closeLabel: "Listo", onClose: done });
        sub.body.style.padding = "0"; sub.body.style.gap = "0";
        const search = el("div", "chiplist-search", `<span class="search-ico">⌕</span><input placeholder="Buscar…">`);
        const list = el("div", "chiplist-list");
        sub.body.appendChild(search); sub.body.appendChild(list);
        const drawList = (q) => {
          list.innerHTML = "";
          sec.opts.filter((x) => !q || String(sec.label(x)).toLowerCase().includes(q)).forEach((x) => {
            const on = (draft[sec.key] || []).includes(x.value);
            const row = el("div", "chiplist-row" + (on ? " on" : ""), `<span class="lbl">${flagHtml(sec.flag && sec.flag(x.value))}${escapeHtml(sec.label(x))}</span><span class="chk">${on ? "✓" : ""}</span>`);
            row.onclick = () => { toggle(sec.key, x.value); drawList(q); };
            list.appendChild(row);
          });
        };
        search.querySelector("input").oninput = (e) => drawList(e.target.value.trim().toLowerCase());
        drawList("");
      };
      render();
    }
    return v;
  };
})(window);
