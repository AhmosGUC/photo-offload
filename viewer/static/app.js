// Photo Backup viewer - plain JS, no build step.
// State lives in the URL hash (#phone=..&view=..&album=..) so reload/back keep your place.

// $, esc, enc, getJSON, post and the shared observers/viewer helpers come from common.js.
const PAGE = 120;
const VIEWS = [
  ["all", "All Photos"], ["favorites", "♥ Favorites"], ["live", "◉ Live Photos"],
  ["videos", "▶ Videos"], ["screenshots", "▣ Screenshots"], ["downloaded", "⤓ Downloaded"],
  ["hidden", "Hidden"],
];

const state = { phone: "", view: "all", album: "", items: [], total: 0, loading: false, done: false, gen: 0 };
let lastMonth = null, lastTiles = null;

// ------------------------------------------------------------ url state
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  state.phone = p.get("phone") || "";
  state.view = p.get("view") || "all";
  state.album = p.get("album") || "";
}
function writeHash() {
  const p = new URLSearchParams();
  if (state.phone) p.set("phone", state.phone);
  if (state.view !== "all") p.set("view", state.view);
  if (state.album) p.set("album", state.album);
  history.replaceState(null, "", "#" + p.toString());
}
function navigate(patch) {
  Object.assign(state, patch);
  writeHash();
  document.body.classList.remove("menu-open");
  refresh();
}

// ------------------------------------------------------------ sidebar
async function loadSidebar() {
  const q = state.phone ? `?phone=${encodeURIComponent(state.phone)}` : "";
  let s;
  try { s = await getJSON("/api/summary" + q); } catch { return; }   // keep the old sidebar

  const sel = $("#phone");
  sel.innerHTML = `<option value="">All phones</option>` +
    s.phones.map((p) => `<option value="${esc(p.id)}">${esc(p.name)} (${p.count})</option>`).join("");
  sel.value = state.phone;

  $("#views").innerHTML = VIEWS.map(([k, label]) =>
    `<a href="#" data-view="${k}" class="${!state.album && state.view === k ? "active" : ""}">
       <span>${label}</span><span class="n">${s.views[k]}</span></a>`).join("");

  $("#albums").innerHTML = s.albums.map((a) =>
    `<a href="#" data-album="${esc(a.name)}" class="${state.album === a.name ? "active" : ""}">
       <span>${esc(a.name)}</span><span class="n">${a.count}</span></a>`).join("");
}

$("#phone").onchange = (e) => navigate({ phone: e.target.value, album: "" });
$("#views").onclick = (e) => { const a = e.target.closest("a"); if (a) { e.preventDefault(); navigate({ view: a.dataset.view, album: "" }); } };
$("#albums").onclick = (e) => { const a = e.target.closest("a"); if (a) { e.preventDefault(); navigate({ album: a.dataset.album, view: "all" }); } };
$("#menu").onclick = () => document.body.classList.toggle("menu-open");
$("#reindex").onclick = async () => {
  $("#reindex").textContent = "Rescanning…";
  try {
    await post("/api/reindex");
  } catch {
    $("#count").textContent = "Rescan failed";
    return;
  } finally {
    $("#reindex").textContent = "↻ Rescan backup";
  }
  refresh();
};

// ------------------------------------------------------------ grid + infinite scroll
function refresh() {
  if (!$("#viewer").hidden) closeViewer();
  state.gen++;                                    // drop responses from the previous filter
  Object.assign(state, { items: [], total: 0, loading: false, done: false });
  lastMonth = lastTiles = null;
  $("#grid").innerHTML = "";
  $("#empty").hidden = true;
  $("#main").scrollTop = 0;
  $("#title").textContent = state.album || VIEWS.find((v) => v[0] === state.view)[1].replace(/^\W+/, "");
  loadSidebar();
  loadMore();
}

async function loadMore() {
  if (state.loading || state.done) return;
  state.loading = true;
  const gen = state.gen;
  const p = new URLSearchParams({ view: state.view, offset: state.items.length, limit: PAGE });
  if (state.phone) p.set("phone", state.phone);
  if (state.album) p.set("album", state.album);
  let data;
  try {
    data = await getJSON("/api/items?" + p);
  } catch {
    if (gen === state.gen) { state.loading = false; $("#count").textContent = "Couldn't load items"; }
    return;
  }
  if (gen !== state.gen) return;                  // filter changed while we waited

  const start = state.items.length;
  state.items.push(...data.items);
  state.total = data.total;
  state.done = state.items.length >= data.total;
  state.loading = false;
  $("#count").textContent = `${data.total.toLocaleString()} items`;
  $("#empty").hidden = data.total > 0;
  data.items.forEach((it, i) => addTile(it, start + i));

  // still room on screen? keep filling
  if (!state.done && $("#sentinel").getBoundingClientRect().top < innerHeight + 600) loadMore();
}

// Screenshots and downloaded items get a clear label so they stand out in All Photos.
const KIND_BADGE = {
  screenshot: `<span class="kind k-shot" title="Screenshot">▣<b> Screenshot</b></span>`,
  saved: `<span class="kind k-saved" title="Downloaded / saved from another app">⤓<b> Downloaded</b></span>`,
};

const monthFmt = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });

function addTile(it, idx) {
  const d = it.date ? new Date(it.date) : null;
  const month = d ? monthFmt.format(d) : "Unknown date";
  if (month !== lastMonth) {
    lastMonth = month;
    $("#grid").insertAdjacentHTML("beforeend", `<div class="month">${esc(month)}</div><div class="tiles"></div>`);
    lastTiles = $("#grid").lastElementChild;
  }
  const t = document.createElement("div");
  t.className = "tile";
  t.dataset.idx = idx;
  makeTileFocusable(t, it.file);
  t.innerHTML =
    (it.live ? `<span class="badge b-tl">◉ LIVE</span>` : "") +
    (KIND_BADGE[it.mkind] || "") +
    (it.kind === "video" ? `<span class="badge b-br">▶</span>` : "") +
    (it.favorite ? `<span class="badge b-bl">♥</span>` : "");
  const img = document.createElement("img");
  img.decoding = "async";
  img.alt = it.file;
  img.dataset.src = thumbUrl(it);                 // real src is set when the tile nears the screen
  img.onload = () => img.classList.add("loaded");
  img.onerror = () => { img.remove(); t.classList.add("noimg"); };
  t.prepend(img);
  lastTiles.appendChild(t);
  tileObserver.observe(img);
}

const tileObserver = lazyImageObserver("800px 0px");   // thumbnails only near the screen
watchSentinel(() => loadMore());                        // infinite scroll
tileKeys($("#grid"));

$("#grid").onclick = (e) => { const t = e.target.closest(".tile"); if (t) openViewer(+t.dataset.idx); };
$("#zoom").oninput = (e) => document.documentElement.style.setProperty("--tile", e.target.value + "px");

// ------------------------------------------------------------ viewer
let cur = -1;
const vImg = $("#v-img"), vLive = $("#v-live"), vVideo = $("#v-video"), vLiveBtn = $("#v-livebtn");

const thumbUrl = (it) => `/thumb/${enc(it.phone)}/${enc(it.file)}`;
const previewUrl = (it) => `/preview/${enc(it.phone)}/${enc(it.file)}`;
const mediaUrl = (it, f = it.file) => `/media/${enc(it.phone)}/${enc(f)}`;

async function openViewer(idx) {
  if (idx < 0) return;
  if (idx >= state.items.length) {               // walked past what's loaded: fetch more
    if (state.done) return;
    await loadMore();
    if (idx >= state.items.length) return;
  }
  cur = idx;
  const it = state.items[idx];
  showViewer();
  stopMedia();

  if (it.kind === "video") {
    vImg.hidden = true;
    vVideo.hidden = false;
    vVideo.poster = thumbUrl(it);
    vVideo.src = mediaUrl(it);
    applySound();
    vVideo.play().catch(() => { vVideo.muted = true; vVideo.play().catch(() => {}); });
  } else {
    vVideo.hidden = true;
    vImg.hidden = false;
    vImg.alt = it.file;
    vImg.src = thumbUrl(it);                     // instant low-res, then swap to full preview
    const full = new Image();
    full.onload = () => { if (cur === idx) vImg.src = full.src; };
    full.src = previewUrl(it);
  }

  vLiveBtn.hidden = !it.live;
  if (it.live) {
    vLive.src = mediaUrl(it, it.live);
    if (autoLive) playLive();                     // otherwise: press LIVE (or the photo) to play
  }

  $("#v-download").href = mediaUrl(it) + "?download=true";
  renderMeta(it);
  prefetch(idx + 1);
}

function renderMeta(it) {
  const d = it.date ? new Date(it.date) : null;
  const parts = [];
  if (it.favorite) parts.push(`<span class="fav">♥</span>`);
  if (KIND_BADGE[it.mkind]) parts.push(KIND_BADGE[it.mkind].replace('class="kind', 'class="kind inline'));
  parts.push(`<strong>${d ? d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "Unknown date"}</strong>`);
  if (it.lat != null)
    parts.push(`<a href="https://maps.google.com/?q=${it.lat},${it.lon}" target="_blank" rel="noopener">📍 ${it.lat.toFixed(4)}, ${it.lon.toFixed(4)}</a>`);
  it.albums.forEach((a) => parts.push(`<span class="tag" data-album="${esc(a)}">${esc(a)}</span>`));
  if (it.caption) parts.push(`<em>“${esc(it.caption)}”</em>`);
  parts.push(`<span style="color:var(--muted)">${esc(it.file)} · ${cur + 1}/${state.total}</span>`);
  $("#v-meta").innerHTML = parts.join("");
}

function prefetch(idx) {
  const it = state.items[idx];
  if (it && it.kind === "image") new Image().src = previewUrl(it);
}

// Sound + auto-play Live: saved in config.json via /api/settings (not localStorage:
// the app gets a new port every launch, which wipes the browser's storage).
// If the browser blocks autoplay with sound, fall back to muted for that play only.
let muted = false, autoLive = true;
const vSound = $("#v-sound"), vAutoLive = $("#v-autolive");
const savePref = (key, value) => post("/api/settings/pref", { key, value }).catch(() => {});
function renderAutoLive() {
  vAutoLive.textContent = `Auto Live: ${autoLive ? "On" : "Off"}`;
  vAutoLive.classList.toggle("on", autoLive);
}
function toggleAutoLive() {
  autoLive = !autoLive;
  savePref("auto_live", autoLive);
  renderAutoLive();
  if (!autoLive) stopLive();
}
vAutoLive.onclick = toggleAutoLive;
getJSON("/api/settings").then(({ prefs }) => {
  if (!prefs) return;
  muted = prefs.muted; autoLive = prefs.auto_live;
  applySound(); renderAutoLive();
}).catch(() => {});
renderAutoLive();
function applySound() {
  vLive.muted = vVideo.muted = muted;
  vSound.textContent = muted ? "🔇" : "🔊";
}
function toggleSound() {
  muted = !muted;
  savePref("muted", muted);
  applySound();
}
vSound.onclick = toggleSound;
applySound();

function playLive() {
  vLive.hidden = false;
  vLive.currentTime = 0;
  vLiveBtn.classList.add("playing");
  applySound();
  vLive.play().catch((err) => {
    if (err.name === "NotAllowedError" && !vLive.muted) {
      vLive.muted = true;                         // browser said no to sound - play silently
      vSound.textContent = "🔇";
      vLive.play().catch(() => stopLive());
    } else stopLive();
  });
}
function stopLive() {
  vLive.pause();
  vLive.hidden = true;
  vLiveBtn.classList.remove("playing");
}
vLive.onended = stopLive;

function stopMedia() {
  vVideo.pause(); vVideo.removeAttribute("src"); vVideo.load();
  stopLive(); vLive.removeAttribute("src");
}
function closeViewer() {
  stopMedia();
  $("#viewer").hidden = true;
  cur = -1;
}

// Press and hold LIVE (or the photo) to replay, like on the phone.
vLiveBtn.onpointerdown = playLive;
vImg.onpointerdown = () => { if (state.items[cur]?.live) playLive(); };
vImg.onpointerup = vImg.onpointerleave = () => { if (!vLive.hidden && vLive.currentTime > 0.3) stopLive(); };

$(".v-close").onclick = closeViewer;
$(".v-prev").onclick = () => openViewer(cur - 1);
$(".v-next").onclick = () => openViewer(cur + 1);
$("#v-meta").onclick = (e) => {
  const t = e.target.closest(".tag");
  if (t) { closeViewer(); navigate({ album: t.dataset.album, view: "all" }); }
};
viewerKeys(() => ({
  Escape: closeViewer,
  ArrowRight: () => openViewer(cur + 1),
  ArrowLeft: () => openViewer(cur - 1),
  " ": () => { if (state.items[cur]?.live) playLive(); },
  m: toggleSound,
  l: toggleAutoLive,
}));

// ------------------------------------------------------------ start

window.addEventListener("hashchange", () => { readHash(); refresh(); });
readHash();
refresh();
