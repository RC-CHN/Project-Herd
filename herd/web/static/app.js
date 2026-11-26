/* Project Herd — Scenario-Agnostic, Zero-LLM Deterministic Story-First WebUI */
const TOD_ZH = {
  dawn: "黎明", morning: "清晨", forenoon: "上午", noon: "正午",
  afternoon: "午后", dusk: "黄昏", evening: "入夜", night: "深夜", midnight: "子夜",
};
const PALETTE = [
  "#58b4e3", "#df5444", "#d8aa5c", "#64be92", "#c982ec",
  "#f0a830", "#72b6a1", "#e07a9a", "#7c94d4", "#a3be6c",
  "#d9825b", "#6db5c8", "#9e86d9", "#c9a66b",
];

const $ = id => document.getElementById(id);

const app = {
  runs: [],
  runName: null,
  meta: null,
  tickCache: new Map(), // tickIdx -> tick payload
  allTicks: null,
  allTicksCommitted: -1,
  tickIdx: 0,
  mode: "chronicle", // "chronicle" | "pov"
  povActor: null,
  fogMode: false,
  spotlightPlace: null,
  showQuiet: false,
  liveFollow: true,
  labOpen: false,
  playing: false,
  playTimer: null,
  pollTimer: null,
  layout: {},
};

function escapeHtml(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function svgEl(tag, attrs = {}, text = null) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text !== null) el.textContent = text;
  return el;
}
function hashStr(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}
function roleColor(roleName) {
  const custom = app.meta?.theme?.group_colors;
  if (custom && custom[roleName]) return custom[roleName];
  return PALETTE[hashStr(roleName) % PALETTE.length];
}
function roleInfo(pid) {
  return app.meta?.roles?.[pid] || { id: pid, display: pid, role: "unknown", label: pid, tag: pid, initial_size: 1 };
}
function placeLabel(pl) { return app.meta?.places?.[pl]?.label || pl; }
function itemMeta(it, tk) {
  return tk?.dynamic_items?.[it] || app.meta?.items?.[it] || { label: it, kind: "fungible" };
}
function itemLabel(it, tk) { return itemMeta(it, tk).label || it; }
function todLabel(tod) { return TOD_ZH[tod] || tod || ""; }

/* ---------------- SCENARIO-AGNOSTIC MAP AUTO-LAYOUT ---------------- */
function computeLayout(places) {
  const ids = Object.keys(places);
  const out = {};
  const padX = 76, padY = 62, spanX = 488, spanY = 392;
  ids.forEach((id, idx) => {
    const pos = places[id].pos;
    let x, y;
    if (Array.isArray(pos) && pos.length === 2) {
      x = padX + pos[0] * spanX;
      y = padY + pos[1] * spanY;
    } else {
      const ang = (2 * Math.PI * idx) / Math.max(1, ids.length);
      x = 320 + Math.cos(ang) * 200;
      y = 260 + Math.sin(ang) * 155;
    }
    const labelLen = places[id].label.length;
    const w = Math.max(96, Math.min(134, 42 + labelLen * 13));
    out[id] = { x, y, w, h: 46 };
  });
  // Gentle collision relaxation so dense topologies (e.g., 13 nodes) stay crisp & readable
  for (let iter = 0; iter < 40; iter++) {
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const a = out[ids[i]], b = out[ids[j]];
        const dx = b.x - a.x, dy = b.y - a.y;
        const minX = (a.w + b.w) / 2 + 12;
        const minY = (a.h + b.h) / 2 + 12;
        if (Math.abs(dx) < minX && Math.abs(dy) < minY) {
          const pushX = (minX - Math.abs(dx)) * 0.25 * (dx >= 0 ? 1 : -1);
          const pushY = (minY - Math.abs(dy)) * 0.25 * (dy >= 0 ? 1 : -1);
          a.x -= pushX; b.x += pushX;
          a.y -= pushY; b.y += pushY;
        }
      }
    }
    ids.forEach(id => {
      const n = out[id];
      n.x = Math.max(n.w / 2 + 10, Math.min(630 - n.w / 2, n.x));
      n.y = Math.max(48, Math.min(462, n.y));
    });
  }
  return out;
}

/* ---------------- THEME APPLICATION ---------------- */
function applyTheme(themeObj, manual = false) {
  const preset = typeof themeObj === "string"
    ? themeObj
    : (themeObj && themeObj.preset) || "maritime-1894";
  document.documentElement.setAttribute("data-theme", preset);
  if (!manual && $("themeSel").querySelector(`option[value="${preset}"]`)) {
    $("themeSel").value = preset;
  }
  if (themeObj && typeof themeObj === "object" && themeObj.palette) {
    Object.entries(themeObj.palette).forEach(([k, v]) => {
      if (k.startsWith("--")) document.documentElement.style.setProperty(k, v);
    });
  }
}

/* ---------------- API LOADING ---------------- */
async function apiGet(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function loadRuns(preserveSelection = false) {
  const data = await apiGet("/api/runs");
  app.runs = data.runs || [];
  const sel = $("runSel");
  sel.innerHTML = app.runs.length
    ? app.runs.map(r => `<option value="${escapeHtml(r.name)}">${escapeHtml(r.name)} (T${r.tick})</option>`).join("")
    : `<option value="">未发现 .sqlite 存档</option>`;
  if (!app.runs.length) {
    $("storyScroll").innerHTML = `<div class="chapter-banner"><div class="chapter-heading">runs/ 目录暂无 .sqlite 存档</div></div>`;
    return;
  }
  if (!preserveSelection || !app.runs.some(r => r.name === app.runName)) {
    app.runName = app.runs[0].name;
  }
  sel.value = app.runName;
  await loadMeta(!preserveSelection);
}

async function loadMeta(resetSelection = false) {
  if (!app.runName) return;
  const prevCommitted = app.meta ? app.meta.committed_ticks : -1;
  const meta = await apiGet(`/api/run/${encodeURIComponent(app.runName)}/meta`);
  app.meta = meta;
  app.layout = computeLayout(meta.places);
  $("scenarioTitle").textContent = meta.title;
  const newTicks = meta.committed_ticks > prevCommitted;
  if (resetSelection) {
    app.tickCache.clear();
    app.allTicks = null;
    app.allTicksCommitted = -1;
    applyTheme(meta.theme);
    app.tickIdx = Math.max(0, meta.committed_ticks - 1);
    app.spotlightPlace = null;
    const individuals = Object.values(meta.roles).filter(r => r.initial_size === 1);
    app.povActor = (individuals[0] || Object.values(meta.roles)[0])?.id || null;
    updateLiveFooter();
    updateLiveBtn();
    await renderCurrent({ preserveScroll: false });
    return;
  }
  updateLiveFooter();
  if (!newTicks) {
    // Only staging progress changed; never rebuild story DOM or disturb scroll position.
    if (meta.committed_ticks === 0) {
      const openingPill = document.querySelector("#storyScroll .meta-pill");
      if (openingPill) {
        openingPill.textContent = `第 0 幕正在演算（${meta.staged}/${meta.alive}），首幕落盘后将自动呈现`;
      }
    }
    return;
  }
  app.allTicks = null;
  if (app.liveFollow) {
    const scroller = $("storyScroll");
    const readingMidPage = scroller && scroller.scrollTop > 60;
    if (app.mode === "pov") {
      app.tickIdx = Math.max(0, meta.committed_ticks - 1);
      updateLiveBtn();
      await renderCurrent({ preserveScroll: true });
    } else if (prevCommitted <= 0 || !readingMidPage) {
      app.tickIdx = Math.max(0, meta.committed_ticks - 1);
      app.spotlightPlace = null;
      updateLiveBtn();
      await renderCurrent({ preserveScroll: false });
    } else {
      // User is scrolled down reading the current tick: keep their place & notify on the Live button.
      renderFilmstrip();
      updateLiveBtn();
    }
  } else {
    renderFilmstrip();
    updateLiveBtn();
  }
}

async function getTick(idx) {
  const safeIdx = app.meta.committed_ticks === 0 ? 0 : Math.max(0, Math.min(app.meta.committed_ticks - 1, idx));
  if (app.meta.committed_ticks > 0 && app.tickCache.has(safeIdx)) {
    return app.tickCache.get(safeIdx);
  }
  const tk = await apiGet(`/api/run/${encodeURIComponent(app.runName)}/tick/${safeIdx}`);
  if (!tk.opening) app.tickCache.set(safeIdx, tk);
  return tk;
}

async function getAllTicks() {
  if (app.allTicks && app.allTicksCommitted === app.meta.committed_ticks) {
    return app.allTicks;
  }
  const data = await apiGet(`/api/run/${encodeURIComponent(app.runName)}/all_ticks`);
  app.allTicks = data.ticks || [];
  app.allTicksCommitted = app.meta.committed_ticks;
  app.allTicks.forEach(tk => { if (!tk.opening) app.tickCache.set(tk.tick, tk); });
  return app.allTicks;
}

function updateLiveFooter() {
  if (!app.meta) return;
  const m = app.meta;
  const denom = Math.max(1, m.alive);
  const pct = Math.min(100, Math.round((m.staged / denom) * 100));
  $("liveLabel").textContent = m.staged > 0 ? `第 ${m.committed_ticks} 幕演算中` : `已落盘 ${m.committed_ticks} 幕`;
  $("liveStage").textContent = m.staged > 0 ? `${m.staged}/${m.alive}` : `存活 ${m.alive}/${m.residents}`;
  $("liveBar").style.width = `${m.staged > 0 ? pct : 100}%`;
}

/* ---------------- ZERO-LLM DETERMINISTIC SCENE DIRECTOR ---------------- */
function describeAction(actorId, dec, tk) {
  const act = dec.action;
  const tgt = dec.target || "";
  const it = dec.item || "";
  const qty = dec.qty || "1";
  const st = tk.people[actorId];
  const fromPl = st.from_place;
  const toPl = st.place;
  const modeLabel = dec.roe ? (app.meta.rules.force_modes?.[dec.roe]?.label || dec.roe) : "";
  const roeNote = dec.roe ? `【切换交战规则 → ${modeLabel}】 ` : "";

  if (act === "force") {
    if (tgt.startsWith("person:")) return roeNote + `向 ${roleInfo(tgt.slice(7)).display} 发起强攻`;
    if (tgt.startsWith("path:")) return roeNote + `强冲通往「${placeLabel(tgt.slice(5))}」的封锁线`;
    if (tgt === "items") return roeNote + `强行抢夺房间内受看护的物品`;
  }
  if (act === "move") {
    if (toPl !== fromPl) return roeNote + `从「${placeLabel(fromPl)}」前往「${placeLabel(toPl)}」`;
    return roeNote + `试图前往「${placeLabel(tgt)}」（被封锁拦回）`;
  }
  if (act === "take") return roeNote + `拾取了 ${qty} 份「${itemLabel(it, tk)}」`;
  if (act === "give") {
    const rec = tgt === "here" ? "房间地面" : roleInfo(tgt).display;
    return roeNote + `将 ${qty} 份「${itemLabel(it, tk)}」交予 ${rec}`;
  }
  if (act === "write") {
    const sealTxt = it ? `并加盖「${itemLabel(it, tk)}」` : "";
    return roeNote + `起草了一份文书${sealTxt}${tgt === "post" ? "，张贴上墙" : ""}`;
  }
  if (act === "post") return roeNote + `将文书 ${tgt} 张贴于公墙`;
  if (act === "produce") return roeNote + (tgt ? `动用印刷设施复制文书 ${tgt}` : `在设施处筹集物资`);
  if (act === "endorse") return roeNote + `联署了公墙文书 ${tgt}`;
  if (act === "offer") return roeNote + `发出本地邀约（出 ${qty} ${itemLabel(it, tk)}，索 ${tgt}）`;
  if (act === "accept") return roeNote + `接受了本地邀约 ${tgt}`;
  if (act === "guard") {
    const scope = tgt === "path:*" ? "全屋出入口与地面物品" : tgt === "items" ? "屋内地面物品" : tgt.startsWith("path:") ? `通往「${placeLabel(tgt.slice(5))}」的通道` : tgt.startsWith("person:") ? `${roleInfo(tgt.slice(7)).display} 身侧` : tgt;
    return roeNote + `把守${scope}`;
  }
  if (act === "eat") return roeNote + `食用食物恢复体力`;
  return roeNote.trim();
}

function isSpecialItem(it, tk) {
  const m = itemMeta(it, tk);
  return m.kind === "seal" || m.kind === "remains";
}

function buildScenes(tk) {
  const byPlace = {};
  Object.keys(app.meta.places).forEach(pl => {
    byPlace[pl] = { place: pl, score: 0, tags: new Set(), combats: [], docs: [], remains: [], beats: [] };
  });

  // 1. Remains lying on floor
  Object.entries(tk.places).forEach(([pl, pd]) => {
    Object.entries(pd.items || {}).forEach(([it, qty]) => {
      if (qty > 0 && itemMeta(it, tk).kind === "remains") {
        byPlace[pl].remains.push({ id: it, qty, ...itemMeta(it, tk) });
        byPlace[pl].score += 22;
      }
    });
  });

  // 2. Combats
  (tk.trace || []).filter(e => e.type === "combat").forEach(c => {
    const sc = byPlace[c.place];
    if (!sc) return;
    sc.combats.push(c);
    const isLethal = app.meta.rules.force_modes?.[c.mode]?.lethal;
    sc.score += 120 + (isLethal ? 40 : 0);
    sc.tags.add(isLethal ? "⚔️ 致命冲突" : "💥 武装推挤");
    const totalCas = Object.values(c.casualties || {}).reduce((s, v) => s + v, 0);
    if (totalCas > 0) sc.tags.add(`🩸 伤亡 ${totalCas}`);
    if (c.routed && Object.keys(c.routed).length) sc.tags.add("🏃 溃退");
    if (c.deaths && c.deaths.length) sc.tags.add(`☠️ 阵亡/溃散 ${c.deaths.length}`);
  });

  // 3. New / posted docs
  (tk.trace || []).filter(e => e.type === "doc" && (e.kind === "write" || e.kind === "post")).forEach(de => {
    const sc = byPlace[de.place];
    const doc = tk.docs[de.doc_id];
    if (!sc || !doc) return;
    if (!sc.docs.some(x => x.id === doc.id)) {
      sc.docs.push(doc);
      sc.score += doc.seal ? 55 : 32;
      sc.tags.add(doc.seal ? "👑 盖印文书" : "📜 新文书");
    }
  });

  // 4. Actor beats
  const quietActors = [];
  Object.entries(tk.decisions || {}).forEach(([aid, dec]) => {
    const st = tk.people[aid];
    if (!st) return;
    const pl = st.from_place;
    const sc = byPlace[pl];
    if (!sc) return;

    const noteOps = (tk.note_ops || {})[aid] || [];
    const special = isSpecialItem(dec.item || "", tk);
    const isRemoteWhisper = Boolean(
      dec.whisper && dec.whisper_to && dec.whisper_to.split(",").some(t => tk.people[t.trim()] && tk.people[t.trim()].from_place !== pl)
    );
    const hasSpeech = Boolean(dec.speech && dec.speech.trim());
    const hasWhisper = Boolean(dec.whisper && dec.whisper.trim());
    const hasNote = noteOps.some(op => op.op === "write");
    const isElite = roleInfo(aid).initial_size === 1;
    const isLethalRoe = dec.roe && app.meta.rules.force_modes?.[dec.roe]?.lethal;

    let beatScore = 0;
    if (dec.action === "force") beatScore += 60;
    if (isLethalRoe) { beatScore += 50; sc.tags.add("🔴 致命戒备"); }
    if (special) { beatScore += 50; sc.tags.add(`📌 ${itemLabel(dec.item, tk)}`); }
    if (isRemoteWhisper) { beatScore += 30; sc.tags.add("📻 跨房通联"); }
    if (dec.action === "write" || dec.action === "post") beatScore += 35;
    if (dec.action === "offer" || dec.action === "accept") { beatScore += 35; sc.tags.add("🤝 契约交易"); }
    if (hasWhisper) beatScore += 18;
    if (hasSpeech) beatScore += isElite ? 18 : 8;
    if (hasNote) beatScore += isElite ? 16 : 6;
    if (["take","give","move","produce"].includes(dec.action)) beatScore += 10;

    if (beatScore < 12 && !hasSpeech && !hasWhisper && !hasNote) {
      quietActors.push({ aid, place: pl, action: dec.action });
      return;
    }

    sc.beats.push({
      aid, dec, st, noteOps, beatScore, isRemoteWhisper,
      direction: describeAction(aid, dec, tk),
    });
    sc.score += Math.min(beatScore, 45);
  });

  const scenes = Object.values(byPlace)
    .filter(sc => sc.combats.length || sc.docs.length || sc.beats.length || sc.remains.length)
    .map(sc => {
      sc.beats.sort((a, b) => b.beatScore - a.beatScore);
      return sc;
    })
    .sort((a, b) => b.score - a.score);

  return { scenes, quietActors };
}

/* ---------------- RENDERERS ---------------- */
async function renderCurrent({ preserveScroll = true } = {}) {
  if (!app.meta) return;
  const scroller = $("storyScroll");
  const prevScroll = scroller ? scroller.scrollTop : 0;
  const tk = await getTick(app.tickIdx);
  $("eraSubtitle").textContent = tk.opening
    ? `开局待演 · 第 1 日 ${todLabel(tk.time_of_day)}（第 0 幕演算中）`
    : `第 ${tk.day} 日 ${todLabel(tk.time_of_day)} · 第 ${tk.tick} 幕`;
  $("modeChronicle").classList.toggle("active", app.mode === "chronicle");
  $("modePov").classList.toggle("active", app.mode === "pov");
  $("fogBtn").classList.toggle("active", app.fogMode);
  $("fogBtn").textContent = app.fogMode ? `🕵️ 迷雾视角(${roleInfo(app.povActor).display})` : "👁️ 上帝视角";

  const { scenes, quietActors } = buildScenes(tk);
  if (!app.spotlightPlace || !app.meta.places[app.spotlightPlace]) {
    app.spotlightPlace = scenes.length ? scenes[0].place : Object.keys(app.meta.places)[0];
  }

  renderStageMap(tk, scenes);
  renderCastStrip(tk);
  if (app.mode === "chronicle") {
    renderChronicle(tk, scenes, quietActors);
  } else {
    const allTicks = await getAllTicks();
    renderPov(app.povActor, allTicks);
  }
  if (scroller) scroller.scrollTop = preserveScroll ? prevScroll : 0;
  renderFilmstrip();
  renderLabDrawer(tk);
  updateLiveBtn();
}

function renderStageMap(tk, scenes) {
  const gRoads = $("gRoads"), gNodes = $("gNodes"), gArcs = $("gArcs");
  gRoads.innerHTML = gNodes.innerHTML = gArcs.innerHTML = "";

  const seen = new Set();
  Object.values(app.meta.places).forEach(pl => {
    pl.paths.forEach(nb => {
      const k = [pl.id, nb].sort().join("--");
      if (seen.has(k)) return;
      seen.add(k);
      const a = app.layout[pl.id], b = app.layout[nb];
      if (!a || !b) return;
      gRoads.appendChild(svgEl("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "stage-road" }));
    });
  });

  const povPlace = app.povActor && tk.people[app.povActor] ? tk.people[app.povActor].place : null;
  const focusPlace = app.mode === "pov" && povPlace ? povPlace : app.spotlightPlace;
  $("stageCaption").innerHTML = app.mode === "pov" && app.povActor
    ? `POV 聚光灯：${escapeHtml(roleInfo(app.povActor).display)} <span>@ ${escapeHtml(placeLabel(focusPlace))}</span>`
    : `舞台聚光灯：${escapeHtml(placeLabel(focusPlace))} <span>点击房间或右侧场景切镜</span>`;

  const combatPlaces = new Set((tk.trace || []).filter(e => e.type === "combat").map(e => e.place));
  const counts = {};
  Object.values(tk.people).forEach(st => { if (st.alive) counts[st.place] = (counts[st.place] || 0) + st.size; });

  Object.values(app.meta.places).forEach(pl => {
    const pos = app.layout[pl.id];
    if (!pos) return;
    const fogged = app.fogMode && povPlace && pl.id !== povPlace;
    const isFocus = pl.id === focusPlace;
    const hasBlood = !fogged && combatPlaces.has(pl.id);
    const hasDrama = !fogged && scenes.some(s => s.place === pl.id);
    const cls = `stage-node ${isFocus ? "spotlight" : ""} ${hasBlood ? "blood" : ""} ${(fogged || (!isFocus && !hasDrama)) ? "dimmed" : ""}`;
    const g = svgEl("g", { class: cls });
    const x0 = pos.x - pos.w / 2, y0 = pos.y - pos.h / 2;
    g.appendChild(svgEl("rect", { x: x0, y: y0, width: pos.w, height: pos.h, rx: 8, class: "node-box" }));
    g.appendChild(svgEl("text", { x: pos.x, y: y0 + 19, "text-anchor": "middle", class: "node-name" },
      pl.label + (tk.places[pl.id]?.comms ? " 📡" : "")
    ));
    const roomItems = Object.entries(tk.places[pl.id]?.items || {}).filter(([, q]) => q > 0);
    const hasRemains = roomItems.some(([k]) => itemMeta(k, tk).kind === "remains");
    const hasSeal = roomItems.some(([k]) => itemMeta(k, tk).kind === "seal") ||
      Object.values(tk.people).some(st => st.alive && st.place === pl.id && Object.entries(st.inv).some(([k, q]) => q > 0 && itemMeta(k, tk).kind === "seal"));
    const subBits = fogged ? ["???"] : [`${counts[pl.id] || 0}人`];
    if (!fogged && hasBlood) subBits.push("⚔️");
    if (!fogged && hasRemains) subBits.push("☠️");
    if (!fogged && hasSeal) subBits.push("👑");
    g.appendChild(svgEl("text", { x: pos.x, y: y0 + 35, "text-anchor": "middle", class: "node-sub" }, subBits.join(" · ")));

    g.onclick = () => {
      app.spotlightPlace = pl.id;
      app.mode = "chronicle";
      renderCurrent().then(() => {
        const el = document.querySelector(`[data-scene-place="${pl.id}"]`);
        if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    };
    gNodes.appendChild(g);
  });

  // Rout arcs
  (tk.trace || []).filter(e => e.type === "combat" && e.routed).forEach(c => {
    Object.entries(c.routed).forEach(([aid, dest]) => {
      const fromPl = tk.people[aid]?.from_place;
      if (!fromPl || fromPl === dest) return;
      if (app.fogMode && povPlace && fromPl !== povPlace && dest !== povPlace) return;
      const a = app.layout[fromPl], b = app.layout[dest];
      if (!a || !b) return;
      gArcs.appendChild(svgEl("path", { d: `M ${a.x} ${a.y} Q ${(a.x+b.x)/2} ${(a.y+b.y)/2 - 24} ${b.x} ${b.y}`, class: "arc-rout", "marker-end": "url(#mRout)" }));
    });
  });

  // Remote whispers touching focusPlace / POV actor
  const drawn = new Set();
  Object.entries(tk.decisions || {}).forEach(([aid, dec]) => {
    if (!dec.whisper || !dec.whisper_to) return;
    const fromPl = tk.people[aid]?.from_place;
    dec.whisper_to.split(",").map(s => s.trim()).forEach(tid => {
      const toPl = tk.people[tid]?.from_place;
      if (!fromPl || !toPl || fromPl === toPl) return;
      if ((app.mode === "pov" || app.fogMode) && aid !== app.povActor && tid !== app.povActor) return;
      if (app.mode === "chronicle" && !app.fogMode && fromPl !== focusPlace && toPl !== focusPlace) return;
      const k = `${fromPl}->${toPl}`;
      if (drawn.has(k)) return;
      drawn.add(k);
      const a = app.layout[fromPl], b = app.layout[toPl];
      gArcs.appendChild(svgEl("path", { d: `M ${a.x} ${a.y} Q ${(a.x+b.x)/2} ${(a.y+b.y)/2 - 32} ${b.x} ${b.y}`, class: "arc-radio", "marker-end": "url(#mRadio)" }));
    });
  });

  // Moves touching focusPlace
  const moved = new Set();
  Object.entries(tk.people).forEach(([aid, st]) => {
    if (st.from_place === st.place) return;
    if ((app.mode === "pov" || app.fogMode) && aid !== app.povActor && st.place !== povPlace && st.from_place !== povPlace) return;
    if (app.mode === "chronicle" && !app.fogMode && st.from_place !== focusPlace && st.place !== focusPlace) return;
    const k = `${st.from_place}->${st.place}`;
    if (moved.has(k)) return;
    moved.add(k);
    const a = app.layout[st.from_place], b = app.layout[st.place];
    if (!a || !b) return;
    gArcs.appendChild(svgEl("path", { d: `M ${a.x} ${a.y} Q ${(a.x+b.x)/2} ${(a.y+b.y)/2 + 20} ${b.x} ${b.y}`, class: "arc-move", "marker-end": "url(#mMove)" }));
  });
}

function renderCastStrip(tk) {
  const allRoles = Object.values(app.meta.roles);
  const individuals = allRoles.filter(r => r.initial_size === 1);
  const cast = (individuals.length ? individuals : allRoles).slice(0, 22);
  const alivePop = Object.values(tk.people).filter(p => p.alive).reduce((s, p) => s + p.size, 0);
  const deadUnits = Object.values(tk.people).filter(p => !p.alive).length;
  $("aliveSummary").textContent = `在场 ${alivePop}/${app.meta.total_initial_pop} 人 · 已退场 ${deadUnits} 单位`;

  $("castPills").innerHTML = cast.map(r => {
    const st = tk.people[r.id];
    const active = app.mode === "pov" && app.povActor === r.id;
    return `<button class="cast-pill ${!st?.alive ? "dead" : ""} ${active ? "active" : ""}" data-pov="${r.id}">${escapeHtml(r.display)}</button>`;
  }).join("");
  $("castPills").querySelectorAll("[data-pov]").forEach(btn => {
    btn.onclick = () => {
      app.povActor = btn.dataset.pov;
      app.mode = "pov";
      renderCurrent();
    };
  });
}

function renderChronicle(tk, scenes, quietActors) {
  if (tk.opening) {
    $("storyScroll").innerHTML = `
      <div class="chapter-banner">
        <div class="chapter-kicker">序幕 · 第 1 日 ${todLabel(tk.time_of_day)}</div>
        <div class="chapter-heading">${escapeHtml(app.meta.title)}</div>
        <p style="margin-top:10px;color:var(--text-dim);">${escapeHtml(app.meta.briefing)}</p>
        <div class="chapter-meta">
          <span class="meta-pill">第 0 幕正在演算（${app.meta.staged}/${app.meta.alive}），首幕落盘后将自动呈现</span>
        </div>
      </div>`;
    return;
  }

  const povPlace = app.fogMode && app.povActor ? tk.people[app.povActor]?.place : null;
  const visibleScenes = povPlace ? scenes.filter(s => s.place === povPlace) : scenes;
  const totalCombats = (tk.trace || []).filter(e => e.type === "combat").length;
  const totalDeaths = (tk.trace || []).filter(e => e.type === "death").length;
  const totalDocs = (tk.trace || []).filter(e => e.type === "doc" && e.kind === "write").length;

  let html = `
    <div class="chapter-banner">
      <div class="chapter-kicker">第 ${tk.day} 日 · ${todLabel(tk.time_of_day)} · 第 ${tk.tick} 幕</div>
      <div class="chapter-heading">${visibleScenes.length ? `焦点：${visibleScenes.slice(0, 2).map(s => placeLabel(s.place)).join("、")}` : "本幕相对平静"}</div>
      <div class="chapter-meta">
        <span class="meta-pill">活跃场景 ${visibleScenes.length} 处</span>
        ${totalCombats ? `<span class="meta-pill blood">⚔️ 冲突 ${totalCombats} 起</span>` : ""}
        ${totalDeaths ? `<span class="meta-pill blood">☠️ 阵亡/溃散 ${totalDeaths} 单位</span>` : ""}
        ${totalDocs ? `<span class="meta-pill">📜 新出文书 ${totalDocs} 份</span>` : ""}
      </div>
    </div>
  `;

  visibleScenes.forEach(sc => {
    const focused = sc.place === app.spotlightPlace;
    html += `
      <article class="scene ${focused ? "focused" : ""}" data-scene-place="${sc.place}">
        <div class="scene-head">
          <div class="scene-place">📍 ${escapeHtml(placeLabel(sc.place))}</div>
          <div class="scene-tags">
            ${Array.from(sc.tags).map(t => `<span class="scene-tag ${t.includes("⚔️") || t.includes("🩸") || t.includes("☠️") || t.includes("🔴") ? "blood" : t.includes("👑") || t.includes("📌") ? "gold" : ""}">${escapeHtml(t)}</span>`).join("")}
          </div>
        </div>
        <div class="scene-body">
          ${sc.remains.length ? `<div class="remains-strip">☠️ 房间地面遗骸：${sc.remains.map(r => escapeHtml(r.label)).join("、")}</div>` : ""}
          ${sc.combats.map(renderClashStrip).join("")}
          ${sc.docs.map(d => renderBroadsheet(d, tk)).join("")}
          ${sc.beats.slice(0, 9).map(b => renderBeat(b, tk)).join("")}
          ${sc.beats.length > 9 ? `<div style="font-size:12px;color:var(--text-dim);padding-top:8px;">……本房间另有 ${sc.beats.length - 9} 个次要单位表态</div>` : ""}
        </div>
      </article>
    `;
  });

  if (quietActors.length && !povPlace) {
    const quietPlaces = [...new Set(quietActors.map(q => placeLabel(q.place)))].join("、");
    html += `
      <div class="quiet-fold" id="quietFold">
        <div>其余 <b>${quietActors.length}</b> 个单位在 ${escapeHtml(quietPlaces)} 静候、休整或原地值守 ${app.showQuiet ? "▾" : "▸ 点击展开"}</div>
        ${app.showQuiet ? `<div class="quiet-detail">${quietActors.map(q => `${escapeHtml(roleInfo(q.aid).display)}(${escapeHtml(placeLabel(q.place))}·${q.action})`).join(" / ")}</div>` : ""}
      </div>`;
  }

  $("storyScroll").innerHTML = html;
  $("storyScroll").querySelectorAll(".scene").forEach(el => {
    el.querySelector(".scene-head").onclick = () => {
      app.spotlightPlace = el.dataset.scenePlace;
      renderStageMap(tk, scenes);
      $("storyScroll").querySelectorAll(".scene").forEach(s => s.classList.toggle("focused", s === el));
    };
  });
  $("storyScroll").querySelectorAll("[data-jump-pov]").forEach(el => {
    el.onclick = () => {
      app.povActor = el.dataset.jumpPov;
      app.mode = "pov";
      renderCurrent();
    };
  });
  const qf = $("quietFold");
  if (qf) qf.onclick = () => { app.showQuiet = !app.showQuiet; renderChronicle(tk, scenes, quietActors); };
}

function renderClashStrip(c) {
  const modeLabel = app.meta.rules.force_modes?.[c.mode]?.label || c.mode;
  const atk = c.attackers.map(a => roleInfo(a).display).join("、");
  const def = c.defenders.map(d => roleInfo(d).display).join("、");
  const casList = Object.entries(c.casualties || {}).filter(([, v]) => v > 0).map(([a, v]) => `${roleInfo(a).display} -${v}人`);
  const routList = Object.entries(c.routed || {}).map(([a, dest]) => `${roleInfo(a).display} 溃退至「${placeLabel(dest)}」`);
  const deathList = (c.deaths || []).map(a => `${roleInfo(a).display} 阵亡/彻底溃散`);
  return `
    <div class="clash-strip">
      <div class="clash-title">⚔️ ${escapeHtml(modeLabel)} · ${c.winner === "attackers" ? "攻方突破" : "守方顶住"}</div>
      <div class="clash-detail">
        <div><b>攻方：</b>${escapeHtml(atk)}</div>
        <div><b>守方：</b>${escapeHtml(def)}</div>
        ${casList.length ? `<div style="color:var(--blood);margin-top:3px;"><b>伤亡：</b>${escapeHtml(casList.join("；"))}</div>` : ""}
        ${routList.length ? `<div style="margin-top:2px;"><b>溃退：</b>${escapeHtml(routList.join("；"))}</div>` : ""}
        ${deathList.length ? `<div style="color:var(--blood);font-weight:700;margin-top:2px;"><b>☠️ 退场：</b>${escapeHtml(deathList.join("；"))}</div>` : ""}
      </div>
    </div>`;
}

function renderBroadsheet(doc, tk) {
  return `
    <div class="broadsheet">
      <div class="broadsheet-top">
        <span>📜 ${doc.seal ? `盖印文书 · ${escapeHtml(itemLabel(doc.seal, tk))}` : "公墙布告 / 文书"}（${escapeHtml(roleInfo(doc.author).display)} 执笔）</span>
        <span>联署权重 ${doc.signed_weight}</span>
      </div>
      <div class="broadsheet-text">${escapeHtml(doc.text)}</div>
    </div>`;
}

function renderBeat(b, tk) {
  const r = roleInfo(b.aid);
  const color = roleColor(r.role);
  const showWhisper = !app.fogMode || b.aid === app.povActor || (b.dec.whisper_to || "").split(",").map(s => s.trim()).includes(app.povActor);
  const showNotes = !app.fogMode || b.aid === app.povActor;
  const whTargets = b.dec.whisper_to
    ? b.dec.whisper_to.split(",").map(t => t.trim()).filter(Boolean).map(t => {
        const remote = tk.people[t] && tk.people[t].from_place !== b.st.from_place;
        return `${roleInfo(t).display}${remote ? `(@${placeLabel(tk.people[t].from_place)})` : ""}`;
      }).join("、")
    : "";
  const writtenNotes = showNotes ? b.noteOps.filter(op => op.op === "write") : [];
  const isLethal = b.st.roe && app.meta.rules.force_modes?.[b.st.roe]?.lethal;

  return `
    <div class="beat">
      <div class="beat-who">
        <span class="speaker-name" data-jump-pov="${b.aid}" title="点击追看TA的个人 POV 故事线">
          <i class="speaker-dot" style="background:${color}"></i>
          ${escapeHtml(r.display)}
          <span class="speaker-id">${b.aid}</span>
        </span>
        <span style="font-size:11px;color:var(--blood);">${isLethal ? `🔴 ${escapeHtml(app.meta.rules.force_modes[b.st.roe].label)}` : ""}</span>
      </div>
      ${b.direction ? `<div class="stage-direction">▸ ${escapeHtml(b.direction)}</div>` : ""}
      ${b.dec.speech ? `<div class="dialogue">「${escapeHtml(b.dec.speech)}」</div>` : ""}
      ${showWhisper && b.dec.whisper ? `
        <div class="whisper-line">
          <div class="whisper-meta">${b.isRemoteWhisper ? "📻 跨房通联" : "🤫 附耳低声"} → ${escapeHtml(whTargets)}</div>
          <div>${escapeHtml(b.dec.whisper)}</div>
        </div>` : ""}
      ${writtenNotes.map(n => `
        <div class="inner-note">
          <div class="inner-note-label">✒️ 内心密记 · ${escapeHtml(n.key)}</div>
          <div>${escapeHtml(n.text)}</div>
        </div>`).join("")}
    </div>`;
}

function renderPov(aid, allTicks) {
  if (!aid || !allTicks.length) return;
  const r = roleInfo(aid);
  const latestTk = allTicks[Math.min(app.tickIdx, allTicks.length - 1)];
  const latest = latestTk.people[aid];
  let html = `
    <div class="pov-card">
      <div class="chapter-kicker">人物 POV 追剧线 · 零 LLM 原始历程</div>
      <div class="pov-title">${escapeHtml(r.display)} <span class="speaker-id" style="font-size:13px;">${aid} · 对外称谓「${escapeHtml(r.tag)}」</span></div>
      <div class="pov-sub">
        <span>当前位置：<b>${escapeHtml(placeLabel(latest.place))}</b></span>
        <span>状态：<b>${latest.alive ? `存活（规模 ${latest.size}/${r.initial_size}）` : "☠️ 已退场/阵亡"}</b></span>
        <span>随身物品：${escapeHtml(Object.entries(latest.inv).filter(([,v])=>v>0).map(([k, v]) => `${itemLabel(k, latestTk)}×${v}`).join("、") || "无")}</span>
      </div>
    </div>`;

  allTicks.forEach((tk, idx) => {
    if (tk.opening) return;
    const dec = tk.decisions?.[aid];
    const st = tk.people[aid];
    const noteOps = (tk.note_ops || {})[aid] || [];
    const myCombats = (tk.trace || []).filter(e => e.type === "combat" && (e.attackers.includes(aid) || e.defenders.includes(aid)));
    const incomingWhispers = Object.entries(tk.decisions || {})
      .filter(([src, d]) => d.whisper && (d.whisper_to || "").split(",").map(s => s.trim()).includes(aid))
      .map(([src, d]) => ({ src, text: d.whisper, fromPl: tk.people[src]?.from_place }));
    const dir = dec ? describeAction(aid, dec, tk) : "";
    const hasContent = (dec && (dec.speech || dec.whisper || dir)) || noteOps.length || myCombats.length || incomingWhispers.length;
    if (!hasContent) return;

    html += `
      <article class="scene ${idx === app.tickIdx ? "focused" : ""}" data-pov-tick="${idx}">
        <div class="scene-head">
          <div class="scene-place">第 ${tk.day} 日 ${todLabel(tk.time_of_day)} · 第 ${tk.tick} 幕 · ${escapeHtml(placeLabel(st.from_place))}</div>
          <div class="scene-tags">
            ${!st.alive ? `<span class="scene-tag blood">☠️ 本幕退场</span>` : ""}
          </div>
        </div>
        <div class="scene-body">
          ${myCombats.map(renderClashStrip).join("")}
          ${incomingWhispers.map(w => `
            <div class="whisper-line" style="margin-bottom:8px;">
              <div class="whisper-meta">📩 收到 ${escapeHtml(roleInfo(w.src).display)}(@${escapeHtml(placeLabel(w.fromPl))}) 私语</div>
              <div>${escapeHtml(w.text)}</div>
            </div>`).join("")}
          ${dec ? renderBeat({
            aid, dec, st, noteOps,
            isRemoteWhisper: Boolean(dec.whisper && dec.whisper_to && dec.whisper_to.split(",").some(t => tk.people[t.trim()] && tk.people[t.trim()].from_place !== st.from_place)),
            direction: dir,
          }, tk) : ""}
        </div>
      </article>`;
  });

  $("storyScroll").innerHTML = html;
  $("storyScroll").querySelectorAll("[data-pov-tick]").forEach(el => {
    el.querySelector(".scene-head").onclick = () => {
      app.tickIdx = Number(el.dataset.povTick);
      app.liveFollow = false;
      updateLiveBtn();
      renderCurrent();
    };
  });
}

function renderFilmstrip() {
  const summaries = app.meta.tick_summaries || [];
  if (!summaries.length) {
    $("stripTicks").innerHTML = `<button class="tick-chip active"><span>⏳ T0</span><small>演算中</small></button>`;
    return;
  }
  $("stripTicks").innerHTML = summaries.map((s, idx) => {
    const badge = s.deaths ? "☠️" : s.combats ? "⚔️" : s.writes ? "📜" : "·";
    return `
      <button class="tick-chip ${idx === app.tickIdx ? "active" : ""} ${(s.combats || s.deaths) ? "has-blood" : ""}" data-tick="${idx}">
        <span>${badge} T${s.tick}</span>
        <small>D${s.day} ${todLabel(s.time_of_day)}</small>
      </button>`;
  }).join("");
  $("stripTicks").querySelectorAll("[data-tick]").forEach(btn => {
    btn.onclick = () => {
      app.tickIdx = Number(btn.dataset.tick);
      app.spotlightPlace = null;
      app.liveFollow = (app.tickIdx === summaries.length - 1);
      updateLiveBtn();
      renderCurrent({ preserveScroll: false });
    };
  });
  const activeChip = $("stripTicks").querySelector(".tick-chip.active");
  if (activeChip) activeChip.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
}

function renderLabDrawer(tk) {
  const tel = app.meta.telemetry || {};
  const sums = app.meta.tick_summaries || [];
  const aliveUnits = Object.values(tk.people).filter(p => p.alive);
  const deadUnits = Object.entries(tk.people).filter(([, p]) => !p.alive);

  // Build simple SVG sparklines for IEMP
  const spark = (vals, color) => {
    if (!vals.length) return "";
    const maxV = Math.max(1, ...vals);
    const pts = vals.map((v, i) => {
      const x = 10 + (vals.length === 1 ? 160 : (320 * i) / (vals.length - 1));
      const y = 92 - (74 * v) / maxV;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    return `<polyline fill="none" stroke="${color}" stroke-width="2" points="${pts}" />`;
  };
  const cumCas = [];
  sums.reduce((acc, s) => { const n = acc + s.casualties; cumCas.push(n); return n; }, 0);

  const modelsHtml = Object.entries(tel.by_model || {}).map(([mod, m]) => `
    <div style="margin-bottom:8px;">
      <div class="lab-stat"><span>${escapeHtml(mod)}</span><span>${m.turns}轮 · ${m.avg_steps}步 · 缓存 ${m.cache_rate}%</span></div>
      <div class="bar-track"><div class="bar-fill" style="width:${m.cache_rate}%;background:var(--accent);"></div></div>
    </div>`).join("");

  const seals = Object.entries(app.meta.items).filter(([, v]) => v.kind === "seal").map(([k]) => k);
  const remains = Object.keys(tk.dynamic_items || {});
  const trackedItems = [...seals, ...remains];

  $("labBody").innerHTML = `
    <div class="lab-sec" style="margin-top:0;">全局概览</div>
    <div class="lab-stat"><span>存档</span><span>${escapeHtml(app.meta.name)}</span></div>
    <div class="lab-stat"><span>已落盘 / 演算中</span><span>${app.meta.committed_ticks} 幕 / ${app.meta.staged}/${app.meta.alive}</span></div>
    <div class="lab-stat"><span>存活单位 / 退场</span><span>${aliveUnits.length} / ${deadUnits.length}</span></div>
    <div class="lab-stat"><span>累计冲突 / 伤亡</span><span>${tel.total_combats || 0} 起 / ${tel.cumulative_casualties || 0} 人</span></div>
    <div class="lab-stat"><span>上下文压缩总次</span><span>${tel.total_compressions || 0}</span></div>

    <div class="lab-sec">IEMP 四维走势（归一化）</div>
    <svg viewBox="0 0 340 104" style="width:100%;height:102px;background:var(--paper);border:1px solid var(--border);border-radius:6px;">
      ${spark(sums.map(s => s.weighted_sigs), "var(--ok)")}
      ${spark(sums.map(s => s.total_edibles), "var(--accent)")}
      ${spark(cumCas, "var(--blood)")}
      ${spark(sums.map(s => s.active_guards), "var(--whisper)")}
    </svg>
    <div style="display:flex;gap:10px;font-size:10.5px;margin-top:4px;font-family:var(--font-sans);">
      <span style="color:var(--ok);">━ I联署</span>
      <span style="color:var(--accent);">━ E补给</span>
      <span style="color:var(--blood);">━ M伤亡</span>
      <span style="color:var(--whisper);">━ P封锁</span>
    </div>

    <div class="lab-sec">模型归因与 KV 缓存</div>
    ${modelsHtml || "暂无模型统计"}

    <div class="lab-sec">印信与遗骸当前位置</div>
    ${trackedItems.map(it => {
      const holder = Object.entries(tk.people).find(([, p]) => p.alive && (p.inv[it] || 0) > 0);
      const floor = Object.entries(tk.places).find(([, pl]) => (pl.items[it] || 0) > 0);
      const where = holder ? `${roleInfo(holder[0]).display} @${placeLabel(holder[1].place)}` : floor ? `地面 @${placeLabel(floor[0])}` : "—";
      return `<div class="lab-stat"><span>${escapeHtml(itemLabel(it, tk))}</span><span>${escapeHtml(where)}</span></div>`;
    }).join("") || "无"}

    <div class="lab-sec">已阵亡 / 溃散名单</div>
    <div style="line-height:1.7;">
      ${deadUnits.length ? deadUnits.map(([pid, p]) => `• ${pid} ${escapeHtml(roleInfo(pid).display)} @${escapeHtml(placeLabel(p.place))}`).join("<br/>") : "暂无"}
    </div>
  `;
}

function updateLiveBtn() {
  const btn = $("liveBtn");
  btn.classList.toggle("live-on", app.liveFollow);
  const latestIdx = app.meta ? Math.max(0, app.meta.committed_ticks - 1) : 0;
  if (app.liveFollow && app.meta && app.meta.committed_ticks > 0 && app.tickIdx < latestIdx) {
    btn.textContent = `🔴 新幕 T${latestIdx} · 点击跳转`;
  } else {
    btn.textContent = app.liveFollow ? "🔴 实时追更" : "⚪ 已暂停追更";
  }
}

/* ---------------- WIRE CONTROLS ---------------- */
$("runSel").onchange = e => { app.runName = e.target.value; loadMeta(true); };
$("themeSel").onchange = e => applyTheme(e.target.value, true);
$("modeChronicle").onclick = () => { app.mode = "chronicle"; renderCurrent({ preserveScroll: false }); };
$("modePov").onclick = () => { app.mode = "pov"; renderCurrent({ preserveScroll: false }); };
$("fogBtn").onclick = () => { app.fogMode = !app.fogMode; renderCurrent({ preserveScroll: true }); };
$("liveBtn").onclick = () => {
  const latestIdx = app.meta ? Math.max(0, app.meta.committed_ticks - 1) : 0;
  if (app.liveFollow && app.meta && app.tickIdx < latestIdx) {
    // Jump directly to the newly arrived tick without toggling liveFollow off.
    app.tickIdx = latestIdx;
    app.spotlightPlace = null;
    renderCurrent({ preserveScroll: false });
    return;
  }
  app.liveFollow = !app.liveFollow;
  updateLiveBtn();
  if (app.liveFollow && app.meta) {
    app.tickIdx = latestIdx;
    app.spotlightPlace = null;
    renderCurrent({ preserveScroll: false });
  }
};
$("labToggle").onclick = () => { app.labOpen = !app.labOpen; $("labDrawer").classList.toggle("open", app.labOpen); };
$("labClose").onclick = () => { app.labOpen = false; $("labDrawer").classList.remove("open"); };
$("btnPrev").onclick = () => {
  app.tickIdx = Math.max(0, app.tickIdx - 1);
  app.liveFollow = false;
  app.spotlightPlace = null;
  updateLiveBtn();
  renderCurrent({ preserveScroll: false });
};
$("btnNext").onclick = () => {
  const maxIdx = Math.max(0, (app.meta?.committed_ticks || 1) - 1);
  app.tickIdx = Math.min(maxIdx, app.tickIdx + 1);
  app.spotlightPlace = null;
  renderCurrent({ preserveScroll: false });
};
$("btnPlay").onclick = () => {
  app.playing = !app.playing;
  $("btnPlay").textContent = app.playing ? "⏸ 暂停" : "▶ 播放";
  if (app.playing) {
    app.liveFollow = false;
    updateLiveBtn();
    app.playTimer = setInterval(() => {
      const maxIdx = Math.max(0, (app.meta?.committed_ticks || 1) - 1);
      app.tickIdx = app.tickIdx >= maxIdx ? 0 : app.tickIdx + 1;
      app.spotlightPlace = null;
      renderCurrent();
    }, 3000);
  } else {
    clearInterval(app.playTimer);
  }
};
window.addEventListener("keydown", e => {
  if (e.target.tagName === "SELECT" || e.target.tagName === "INPUT") return;
  if (e.key === "ArrowLeft") $("btnPrev").click();
  else if (e.key === "ArrowRight") $("btnNext").click();
  else if (e.key === " ") { e.preventDefault(); $("btnPlay").click(); }
});

// Boot + 4-second live polling
loadRuns(false).then(() => {
  app.pollTimer = setInterval(() => {
    if (app.runName) loadMeta(false).catch(() => {});
  }, 4000);
});
