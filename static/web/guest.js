/* =====================================================================
   Orbyte web — modo invitado de la playlist colaborativa (Ticket C-03)
   Usa el núcleo (core.js) y define solo lo propio del invitado: tocar una
   pista = agregarla a la playlist colaborativa, su cupo, "Mis pistas",
   apodo/avatar y el pedido del delegado. Sin reproductor ni ajustes.
   ===================================================================== */
(function () {
  "use strict";
  const { el, escapeHtml } = ORB;
  const G = { me: null, inSession: new Map(), busy: new Set(), avatars: null };

  // ---------- Estado del invitado ----------
  async function loadMe() {
    let r;
    try { r = await ORB.api("/api/collab/yo"); } catch (e) { return; }
    if (!r || !r.active) { sessionEnded(); return; }
    G.me = r;
    G.inSession = new Map((r.session_tracks || []).map((t) => [t.id, t]));
    paintHeader(); paintQuota(); refreshAddButtons();
    if (!document.getElementById("tab-mine").classList.contains("hidden")) renderMine();
  }
  function sessionEnded() {
    document.getElementById("app-shell").innerHTML =
      `<div class="full-state"><img class="logo" src="/static/web/assets/logo-icon.png" alt=""><div class="page-title gold-text">Sesión terminada</div>
       <div>El anfitrión cerró la playlist colaborativa. ¡Gracias por sumar música!</div></div>`;
    ORB.closeAllOverlays();
  }
  ORB.onAuthLost = (data) => { if (data && data.error === "collab_session_ended") sessionEnded(); };

  // Ticket C-04: las pistas del modo infinito del anfitrión llegan como de un
  // participante más ("Modo infinito"); su avatar es el mismo ∞ del botón.
  const INFINITO_SVG = '<svg class="av-infinito" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12c-2-2.67-4-4-6-4a4 4 0 1 0 0 8c2 0 4-1.33 6-4Zm0 0c2 2.67 4 4 6 4a4 4 0 0 0 0-8c-2 0-4 1.33-6 4Z"/></svg>';
  function avatarHtml(av, name) {
    if (av && av.type === "infinito") return INFINITO_SVG;
    if (av && av.type === "image" && av.url) return `<img src="${escapeHtml(av.url)}" alt="">`;
    return escapeHtml((av && av.text) || (name || "?").slice(0, 2).toUpperCase());
  }
  function paintHeader() {
    const me = G.me; if (!me) return;
    document.getElementById("user-name").textContent = me.name;
    document.getElementById("avatar").innerHTML = avatarHtml(me.avatar, me.name);
    // Ticket C-04: el número de "Mis pistas" son las que ya agregó (no las
    // que le quedan; el cupo restante sigue en el banner de Inicio).
    const badge = document.getElementById("mine-badge");
    const n = (me.mine || []).length;
    badge.textContent = n;
    badge.classList.toggle("hidden", !n);
  }
  function paintQuota() {
    const me = G.me, q = document.getElementById("quota-banner"); if (!me || !q) return;
    const h = me.window_hours === 1 ? "1 hora" : `${me.window_hours} horas`;
    q.innerHTML = `<div class="star">✦</div><div class="txt"><div class="title">Playlist colaborativa</div>
      <div class="subtitle">Tocá <b style="color:var(--accent-bright)">+</b> en una pista para sumarla · máx. ${me.max_tracks} cada ${h}</div></div>
      <div class="num">${me.remaining}</div>`;
    q.onclick = () => switchTab("mine");
  }

  // ---------- Agregar pistas ----------
  function addButton(t) {
    const b = el("button", "add-btn");
    b.dataset.addFor = String(t.id);
    paintAddButton(b, t.id);
    b.onclick = (e) => { e.stopPropagation(); addTrack(t); };
    return b;
  }
  function paintAddButton(b, id) {
    const inS = G.inSession.has(id);
    b.classList.toggle("done", inS);
    b.classList.toggle("busy", G.busy.has(id));
    b.textContent = inS ? "✓" : "+";
    b.title = inS ? "Ya está en la playlist" : "Agregar a la playlist colaborativa";
    const row = b.closest(".filtered-track-row");
    if (row) row.classList.toggle("is-added", inS);
  }
  function refreshAddButtons() {
    document.querySelectorAll(".add-btn[data-add-for]").forEach((b) => paintAddButton(b, parseInt(b.dataset.addFor, 10)));
  }
  async function addTrack(t, confirmAlbum) {
    if (G.busy.has(t.id)) return;
    const ya = G.inSession.get(t.id);
    if (ya) { ORB.toast(`«${t.title}» ya está en la playlist (la agregó ${ya.added_by}).`); return; }
    G.busy.add(t.id); refreshAddButtons();
    let r;
    try { r = await ORB.api("/api/collab/add", { method: "POST", body: { track_id: t.id, confirm_album: !!confirmAlbum } }); }
    catch (e) { r = { status: "error", message: e.message }; }
    G.busy.delete(t.id);
    if (r.status === "ok") {
      G.inSession.set(t.id, Object.assign({}, t, { added_by: G.me ? G.me.name : "vos", dispatched: false }));
      if (G.me) { G.me.remaining = r.remaining; G.me.used += 1; G.me.mine = (G.me.mine || []).concat([G.inSession.get(t.id)]); }
      ORB.toast(`Agregada ✓ · te quedan ${r.remaining}`);
      paintHeader(); paintQuota(); refreshAddButtons();
      loadMe();
      return;
    }
    refreshAddButtons();
    if (r.status === "album_warning") {
      const ok = await ORB.confirm("¿Otra del mismo álbum?", r.message, "Agregar igual", "Elegir otra");
      if (ok) addTrack(t, true);
    } else if (r.status === "duplicate") {
      ORB.toast(r.message); loadMe();
    } else if (r.status === "limit") {
      ORB.modal("Llegaste a tu cupo", r.message);
    } else if (r.status === "expired") {
      sessionEnded();
    } else {
      ORB.toast(r.message || "No se pudo agregar. Probá de nuevo.");
    }
  }

  // El núcleo pregunta qué hacer con cada pista
  ORB.actions.trackRight = (t) => addButton(t);
  ORB.actions.onTrackTap = (t) => addTrack(t);
  ORB.actions.trackMenu = (t) => {
    const ya = G.inSession.get(t.id);
    return [ya
      ? { ico: "✓", label: `Ya está en la playlist (${ya.added_by})`, disabled: true }
      : { ico: "+", label: "Agregar a la playlist colaborativa", run: () => addTrack(t) }];
  };

  // ---------- Pestañas ----------
  const TABS = ["home", "search", "mine"];
  function switchTab(name) {
    ORB.closeAllOverlays();
    TABS.forEach((n) => document.getElementById("tab-" + n).classList.toggle("hidden", n !== name));
    document.querySelectorAll(".main-tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    if (name === "mine") { renderMine(); loadMe(); }
  }

  // ---------- Inicio ----------
  async function loadHome() {
    const body = document.getElementById("home-body");
    body.innerHTML = '<div class="filtered-loading">Cargando...</div>';
    try { ORB.renderHome(body, await ORB.api("/api/v1/home/facets")); }
    catch (e) {
      body.innerHTML = `<div class="search-state">${escapeHtml(e.message)}<br><button class="pl-action-btn" style="margin-top:12px">Reintentar</button></div>`;
      body.querySelector("button").onclick = loadHome;
    }
  }

  // ---------- Mis pistas ----------
  function stateChip(t) {
    return el("span", "state-chip " + (t.dispatched ? "ok" : "wait"), t.dispatched ? "En la cola" : "En espera");
  }
  function byLine(t) {
    return el("div", "track-by", `<span class="mini-av">${avatarHtml(t.added_by_avatar, t.added_by)}</span>${escapeHtml(t.added_by)}`);
  }
  function renderMine() {
    const root = document.getElementById("tab-mine"), me = G.me;
    if (!me) { root.innerHTML = '<div class="filtered-loading">Cargando...</div>'; return; }
    root.innerHTML = "";
    const pct = me.max_tracks ? Math.min(100, Math.round((me.used / me.max_tracks) * 100)) : 0;
    const h = me.window_hours === 1 ? "1 hora" : `${me.window_hours} horas`;
    const quota = el("div", "cfg-section", `<div class="cfg-header">Tu cupo</div>
      <div class="cfg-card"><div class="info-row"><span class="k">Te quedan</span><span><b style="color:var(--accent-bright)">${me.remaining}</b> de ${me.max_tracks}</span></div>
      <div class="info-row" style="display:block"><div class="quota-track"><span style="width:${pct}%"></span></div></div></div>
      <div class="cfg-footer">El cupo se libera solo: cuenta lo que agregaste en las últimas ${h}.</div>`);
    root.appendChild(quota);
    if (me.can_pull) {
      // Ticket C-04: el botón recarga la cola del anfitrión directamente. No
      // hay que avisarle a nadie: su app revisa este pedido cada ~6 s y carga
      // sola las pistas nuevas en su reproductor.
      const sec = el("div", "cfg-section", `<div class="cfg-header">Delegado</div>`);
      const pend = me.pending_count || 0;
      const btn = el("button", "orbitron-big-btn",
        me.pull_pending ? "Recargando cola…" : `↻&nbsp; Recargar cola${pend ? ` (${pend} nueva${pend > 1 ? "s" : ""})` : ""}`);
      btn.disabled = me.pull_pending || !pend;
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = "Recargando cola…";
        try { await ORB.api("/api/collab/solicitar-pull", { method: "POST", body: {} }); }
        catch (e) { ORB.toast(e.message); loadMe(); return; }
        esperarRecarga(pend);
      };
      sec.appendChild(btn);
      sec.appendChild(el("div", "cfg-footer", pend
        ? "Carga en la cola del anfitrión las pistas que sumaron todos y todavía no están en la cola."
        : "No hay pistas nuevas por cargar: todo lo agregado ya está en la cola."));
      root.appendChild(sec);
    }
    const mine = el("div", "cfg-section", `<div class="cfg-header">Tus pistas (${me.mine.length})</div>`);
    if (!me.mine.length) mine.appendChild(el("div", "cfg-sub", "Todavía no agregaste nada. Buscá una canción o explorá el Inicio y tocá +."));
    me.mine.slice().reverse().forEach((t) => mine.appendChild(ORB.makeTrackRow(t, { right: stateChip(t) })));
    root.appendChild(mine);
    const others = (me.session_tracks || []).filter((t) => !me.mine.some((m) => m.id === t.id));
    if (others.length) {
      const all = el("div", "cfg-section", `<div class="cfg-header">Lo que sumaron los demás (${others.length})</div>`);
      others.slice().reverse().forEach((t) => all.appendChild(ORB.makeTrackRow(t, { right: stateChip(t), below: byLine(t) })));
      root.appendChild(all);
    }
  }

  // Ticket C-04: tras "Recargar cola", confirmar cuando la app del anfitrión
  // efectivamente cargó las pistas (o avisar si no respondió).
  async function esperarRecarga(cuantas) {
    for (let i = 0; i < 12; i++) {
      await new Promise((r) => setTimeout(r, 2500));
      await loadMe();
      if (G.me && !G.me.pull_pending) {
        ORB.toast(cuantas ? `Cola actualizada ✓ (${cuantas} pista${cuantas > 1 ? "s" : ""})` : "Cola actualizada ✓");
        return;
      }
    }
    ORB.toast("La app del anfitrión no respondió todavía; se cargará apenas la abra.");
  }

  // ---------- Perfil (apodo + avatar) ----------
  async function openProfile() {
    const me = G.me; if (!me) return;
    const sh = ORB.sheet("Tu perfil");
    let cat = me.avatar_category || "femeninos", file = me.avatar_file || null, tab = cat;
    const head = el("div", "prof-head");
    const big = el("div", "prof-avatar", ""); big.style.cssText = "width:70px;height:70px;font-size:26px";
    const who = el("div", "", `<div class="prof-name">${escapeHtml(me.name)}</div><div class="cfg-sub">Invitado de la playlist colaborativa</div>`);
    head.appendChild(big); head.appendChild(who);
    sh.body.appendChild(head);
    const nameSec = el("div", "cfg-section", `<div class="cfg-header">Apodo</div><div class="cfg-card prof-card-pad"><input class="prof-input" maxlength="40" value="${escapeHtml(me.name)}" placeholder="Cómo te van a ver los demás"></div>`);
    const input = nameSec.querySelector("input");
    sh.body.appendChild(nameSec);
    const avSec = el("div", "cfg-section", `<div class="cfg-header">Avatar</div>`);
    const card = el("div", "cfg-card prof-card-pad");
    const seg = el("div", "cfg-seg", '<button data-c="femeninos">Femeninos</button><button data-c="masculinos">Masculinos</button>');
    const grid = el("div", "prof-avatar-grid", '<div class="cfg-sub">Cargando avatares…</div>');
    card.appendChild(seg); card.appendChild(grid); avSec.appendChild(card);
    sh.body.appendChild(avSec);
    const paintBig = () => {
      const name = input.value.trim() || me.name;
      big.innerHTML = file ? `<img src="/static/avatares/${cat}/${encodeURIComponent(file)}" alt="">` : escapeHtml(name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase());
    };
    const paintGrid = () => {
      seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.c === tab));
      if (!G.avatars) return;
      grid.innerHTML = "";
      const none = el("div", "prof-avatar-cell" + (!file ? " on" : ""), "Aa"); none.title = "Sin imagen (iniciales)";
      none.onclick = () => { file = null; paintGrid(); paintBig(); };
      grid.appendChild(none);
      (G.avatars[tab] || []).forEach((f) => {
        const c = el("div", "prof-avatar-cell" + (file === f && cat === tab ? " on" : ""), `<img src="/static/avatares/${tab}/${encodeURIComponent(f)}" loading="lazy" alt="">`);
        c.onclick = () => { cat = tab; file = f; paintGrid(); paintBig(); };
        grid.appendChild(c);
      });
    };
    seg.querySelectorAll("button").forEach((b) => b.onclick = () => { tab = b.dataset.c; paintGrid(); });
    input.oninput = paintBig;
    paintBig(); paintGrid();
    if (!G.avatars) { try { G.avatars = await ORB.api("/api/v1/avatars"); paintGrid(); } catch (e) { grid.innerHTML = '<div class="cfg-sub">No se pudieron cargar los avatares.</div>'; } }
    const save = el("button", "orbitron-big-btn", "Guardar");
    save.onclick = async () => {
      save.disabled = true;
      try {
        await ORB.api("/api/collab/perfil", { method: "POST", body: { name: input.value.trim(), avatar_category: file ? cat : "", avatar_file: file || "" } });
        ORB.toast("Guardado ✓"); sh.close(); loadMe();
      } catch (e) { ORB.toast(e.message); save.disabled = false; }
    };
    sh.body.appendChild(save);
    const out = el("button", "prof-logout", "Salir de la playlist colaborativa");
    out.onclick = async () => { if (await ORB.confirm("¿Salir?", "Vas a dejar de poder agregar pistas desde este teléfono. Si volvés a escanear el QR, retomás donde estabas.", "Salir", "Cancelar")) location.href = "/colab/salir"; };
    sh.body.appendChild(out);
  }

  // ---------- Arranque ----------
  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("tab-home")) return;   // pantalla de sesión terminada
    document.querySelectorAll(".main-tab").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
    document.getElementById("avatar").onclick = openProfile;
    document.getElementById("user-name").onclick = openProfile;
    ORB.mountSearch(document.getElementById("tab-search"));
    loadHome(); loadMe();
    // estado de "en espera"/"en la cola" y cupo, sin recargar la página
    setInterval(() => { if (!document.hidden) loadMe(); }, 20000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadMe(); });
  });
})();
