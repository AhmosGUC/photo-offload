// Connected phone - browse what's on the iPhone right now, pick items, import them.
// Talks to /api/phone/* (viewer/phone.py). Nothing here can delete anything.

// $, esc, enc, getJSON, post and the shared observers/viewer helpers come from common.js.
const PAGE = 150;
const SHOWS = [["new", "Not backed up"], ["done", "Already backed up"], ["all", "Everything"]];
const KINDS = [["all", "All types"], ["photo", "Photos"], ["video", "▶ Videos"], ["live", "◉ Live Photos"], ["screenshot", "▣ Screenshots"]];

const state = { show: "new", kind: "all", sort: "date_desc", items: [], total: 0, bytes: 0,
                loading: false, done: false, gen: 0 };
const selected = new Map();            // uuid -> size
let lastClicked = -1;
let phone = { state: "idle" };         // last /api/phone/status
let pollTimer = null;

// ------------------------------------------------------------ helpers
function human(n) {
  for (const u of ["B", "KB", "MB", "GB", "TB"]) {
    if (n < 1024 || u === "TB") return u === "B" ? `${n} B` : `${n.toFixed(n < 10 ? 1 : 0)} ${u}`;
    n /= 1024;
  }
}
const dur = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

// ------------------------------------------------------------ devices + status
let devicesLoading = false;
async function loadDevices() {
  if (devicesLoading) return;            // a slow usbmuxd call must not pile up
  devicesLoading = true;
  const sel = $("#device");
  try {
    const { devices, error } = await getJSON("/api/phone/devices");
    const keep = sel.value;
    sel.innerHTML = devices.length
      ? devices.map((d) => `<option value="${esc(d.udid)}">${esc(d.name)}</option>`).join("")
      : `<option value="">No phone plugged in</option>`;
    if (devices.some((d) => d.udid === keep)) sel.value = keep;
    $("#connect").disabled = !devices.length || busy();
    $("#devhint").textContent = error || (devices.length ? "" : "Plug the iPhone in with a cable and unlock it.");
  } catch (e) {
    $("#devhint").textContent = "Server not reachable.";
  } finally {
    devicesLoading = false;
  }
}

const busy = () => phone.state === "scanning" || phone.state === "importing";

let polling = false;
async function poll() {                  // never two status loops at once
  if (polling) return;
  polling = true;
  try { await pollOnce(); } finally { polling = false; }
}
async function pollOnce() {
  clearTimeout(pollTimer);
  if (document.hidden) return;           // resumed by visibilitychange below
  const prev = phone.state;
  try { phone = await getJSON("/api/phone/status"); } catch { pollTimer = setTimeout(poll, 3000); return; }
  renderStatus();
  const live = (s) => s === "ready" || s === "importing";
  if (!live(prev) && live(phone.state)) refresh();          // scan finished (or page just opened)
  if (phone.state === "importing" && !jobSeen) {            // page reloaded mid-import: show progress
    jobSeen = "running";
    $("#importbox").hidden = false;
  }
  if (phone.job) renderJob();
  if (!busy()) loadDevices();
  pollTimer = setTimeout(poll, busy() ? 700 : 4000);
}

function renderStatus() {
  const box = $("#statusbox"), bar = $("#statusbar").parentElement;
  const s = phone;
  $("#connect").disabled = busy() || !$("#device").value;
  $("#connect").textContent = s.device && s.state !== "idle" ? "Reconnect" : "Connect";
  box.classList.toggle("error", s.state === "error");
  if (s.state === "scanning" || s.state === "error" || s.state === "idle") {
    box.hidden = false;
    let text = esc(s.message || "");
    if (s.state === "idle") text = "Pick your phone on the left and press <b>Connect</b>. Keep it unlocked.";
    const p = s.progress;
    if (p && p.total) {
      const pct = (100 * p.done) / p.total;
      text += ` <span class="hint">${p.unit === "bytes" ? `${human(p.done)} / ${human(p.total)}`
                                                        : `${p.done.toLocaleString()} / ${p.total.toLocaleString()}`}</span>`;
      bar.classList.remove("indeterminate");
      $("#statusbar").style.width = pct + "%";
    } else {
      bar.classList.toggle("indeterminate", s.state === "scanning");
      $("#statusbar").style.width = "0";
    }
    bar.hidden = s.state !== "scanning";
    $("#statustext").innerHTML = text;
  } else box.hidden = true;

  const c = s.counts;
  $("#summary").hidden = !s.device || !c.total;
  if (s.device && c.total) {
    $("#summary").innerHTML = `
      <span>${esc(s.device.name)}</span><span class="n muted">iOS ${esc(s.device.ios || "?")}</span>
      <span>On the phone</span><span class="n">${c.total.toLocaleString()} · ${human(c.total_bytes)}</span>
      <span>Not backed up</span><span class="n">${c.new.toLocaleString()} · ${human(c.new_bytes)}</span>
      <span>Backed up</span><span class="n">${c.done.toLocaleString()}</span>
      ${c.not_on_phone ? `<span class="muted">iCloud only (skipped)</span><span class="n muted">${c.not_on_phone.toLocaleString()}</span>` : ""}`;
  }
  $("#title").textContent = s.device ? s.device.name : "Connected phone";
  renderSelection();
}

$("#connect").onclick = async () => {
  const udid = $("#device").value;
  if (!udid) return;
  clearSelection();
  state.items = []; $("#grid").innerHTML = ""; $("#count").textContent = "";
  try { phone = await post("/api/phone/connect", { udid }); } catch (e) { alert(e.message); }
  poll();
};

// ------------------------------------------------------------ sidebar filters
function renderFilters() {
  $("#show").innerHTML = SHOWS.map(([k, l]) => `<a href="#" data-k="${k}" class="${state.show === k ? "active" : ""}"><span>${l}</span></a>`).join("");
  $("#kind").innerHTML = KINDS.map(([k, l]) => `<a href="#" data-k="${k}" class="${state.kind === k ? "active" : ""}"><span>${l}</span></a>`).join("");
}
$("#show").onclick = (e) => { const a = e.target.closest("a"); if (a) { e.preventDefault(); state.show = a.dataset.k; refresh(); } };
$("#kind").onclick = (e) => { const a = e.target.closest("a"); if (a) { e.preventDefault(); state.kind = a.dataset.k; refresh(); } };
$("#sort").onchange = (e) => { state.sort = e.target.value; refresh(); };
$("#menu").onclick = () => document.body.classList.toggle("menu-open");
$("#zoom").oninput = (e) => document.documentElement.style.setProperty("--tile", e.target.value + "px");

// ------------------------------------------------------------ grid
function refresh() {
  renderFilters();
  document.body.classList.remove("menu-open");
  state.gen++;
  Object.assign(state, { items: [], total: 0, loading: false, done: false });
  lastClicked = -1;
  $("#grid").innerHTML = "";
  $("#empty").hidden = true;
  $("#main").scrollTop = 0;
  if (phone.state === "ready" || phone.state === "importing") loadMore();
  else $("#count").textContent = "";
}

async function loadMore() {
  if (state.loading || state.done) return;
  state.loading = true;
  const gen = state.gen;
  const p = new URLSearchParams({ show: state.show, kind: state.kind, sort: state.sort,
                                  offset: state.items.length, limit: PAGE });
  let data;
  try { data = await getJSON("/api/phone/items?" + p); }
  catch { state.loading = false; return; }
  if (gen !== state.gen) return;
  const start = state.items.length;
  state.items.push(...data.items);
  state.total = data.total; state.bytes = data.bytes;
  state.done = state.items.length >= data.total;
  state.loading = false;
  $("#count").textContent = `${data.total.toLocaleString()} items · ${human(data.bytes)}`;
  $("#empty").hidden = data.total > 0;
  $("#empty").textContent = state.show === "new" ? "Everything here is already backed up 🎉" : "Nothing here.";
  data.items.forEach((it, i) => addTile(it, start + i));
  if (!state.done && $("#sentinel").getBoundingClientRect().top < innerHeight + 600) loadMore();
}

function addTile(it, idx) {
  const t = document.createElement("div");
  t.className = "tile" + (it.backed_up ? " backed" : "") + (selected.has(it.uuid) ? " sel" : "");
  t.dataset.idx = idx;
  makeTileFocusable(t, it.file);
  const tag = it.kind === "video" ? `▶ ${it.duration ? dur(it.duration) : ""}`
            : it.live ? "◉ LIVE" : it.screenshot ? "▣" : "";
  t.innerHTML =
    `<span class="check">✓</span>` +
    (tag ? `<span class="tl">${tag}</span>` : "") +
    (it.backed_up ? `<span class="done">✓ Backed up</span>` : "") +
    `<button class="zoomin" title="Preview">⤢</button>` +
    `<span class="size">${human(it.size)}</span>`;
  t.title = `${it.file} · ${new Date(it.date).toLocaleString()}`;
  const img = document.createElement("img");
  img.decoding = "async";
  img.alt = it.file;
  img.dataset.src = `/api/phone/thumb/${enc(it.uuid)}`;
  img.onload = () => img.classList.add("loaded");
  img.onerror = () => { img.remove(); t.classList.add("noimg"); };
  t.prepend(img);
  $("#grid").appendChild(t);
  tileObserver.observe(img);
}

// Only ask for thumbnails of tiles near the screen - each one is a trip to the phone.
const tileObserver = lazyImageObserver("600px 0px");
watchSentinel(() => loadMore());
tileKeys($("#grid"));

// ------------------------------------------------------------ selection
$("#grid").onclick = (e) => {
  const t = e.target.closest(".tile");
  if (!t) return;
  const idx = +t.dataset.idx;
  if (e.target.closest(".zoomin")) return openViewer(idx);
  if (e.shiftKey && lastClicked >= 0) {          // range: make it match the clicked tile's new state
    const on = !selected.has(state.items[idx].uuid);
    const [a, b] = [Math.min(lastClicked, idx), Math.max(lastClicked, idx)];
    for (let i = a; i <= b; i++) setSelected(state.items[i], on, i);
  } else toggle(state.items[idx], idx);
  lastClicked = idx;
  renderSelection();
};
$("#grid").ondblclick = (e) => { const t = e.target.closest(".tile"); if (t) openViewer(+t.dataset.idx); };

function setSelected(it, on, idx) {            // pass idx: indexOf per item is O(n²) on big ranges
  if (!it || it.backed_up) return;
  if (on) selected.set(it.uuid, it.size); else selected.delete(it.uuid);
  const t = $(`#grid .tile[data-idx="${idx ?? state.items.indexOf(it)}"]`);
  if (t) t.classList.toggle("sel", on);
}
const toggle = (it, idx) => setSelected(it, !selected.has(it.uuid), idx);

function clearSelection() {
  selected.clear();
  document.querySelectorAll("#grid .tile.sel").forEach((t) => t.classList.remove("sel"));
  renderSelection();
}

function renderSelection() {
  const n = selected.size;
  let bytes = 0;
  for (const s of selected.values()) bytes += s;
  $("#selinfo").innerHTML = n ? `<b>${n.toLocaleString()} selected</b> · ${human(bytes)}`
                              : "Click photos to select them · Shift-click selects a range";
  $("#selnone").hidden = !n;
  $("#import").disabled = !n || phone.state !== "ready";
  $("#import").textContent = n ? `Import ${n.toLocaleString()}` : "Import";
  $("#selall").disabled = phone.state !== "ready" && phone.state !== "importing";
}

$("#selnone").onclick = clearSelection;
$("#selall").onclick = async () => {
  // everything matching the current filter, not just what's scrolled into view
  const p = new URLSearchParams({ show: state.show, kind: state.kind });
  let items;
  try { ({ items } = await getJSON("/api/phone/ids?" + p)); }
  catch (e) { alert("Couldn't select all: " + e.message); return; }
  for (const i of items) if (!i.backed_up) selected.set(i.uuid, i.size);
  document.querySelectorAll("#grid .tile:not(.backed)").forEach((t) => t.classList.add("sel"));
  renderSelection();
};

// ------------------------------------------------------------ import
let jobSeen = null;

$("#import").onclick = () => {
  let bytes = 0;
  for (const s of selected.values()) bytes += s;
  jobSeen = null;
  $("#ib-title").textContent = `Import ${selected.size.toLocaleString()} items?`;
  $("#ib-text").innerHTML = `${human(bytes)} will be copied to<br><code>${esc(phone.folder || settings.backup_root || "the backup folder")}/media</code>`;
  $("#ib-detail").textContent = "Each file is SHA-256 verified. Nothing is deleted from the phone.";
  $("#ib-barwrap").hidden = true;
  $("#ib-go").hidden = false; $("#ib-go").disabled = false;
  $("#ib-cancel").textContent = "Cancel";
  $("#importbox").hidden = false;
};

$("#ib-go").onclick = async () => {
  $("#ib-go").disabled = true;
  try {
    jobSeen = "starting";
    await post("/api/phone/import", { uuids: [...selected.keys()] });
    phone.state = "importing";
    poll();
  } catch (e) {
    jobSeen = null;
    $("#ib-detail").textContent = "Couldn't start: " + e.message;
    $("#ib-go").disabled = false;
  }
};

$("#ib-cancel").onclick = async () => {
  if (phone.state === "importing" && jobSeen) {
    $("#ib-cancel").disabled = true;
    $("#ib-cancel").textContent = "Stopping after this file…";
    try {
      await post("/api/phone/cancel");
    } catch (e) {
      $("#ib-cancel").disabled = false;
      $("#ib-cancel").textContent = "Stop";
      $("#ib-detail").textContent = "Couldn't stop: " + e.message;
    }
  } else {
    $("#importbox").hidden = true;
  }
};

let rate = { t: 0, b: 0, speed: 0 };
function renderJob() {
  const j = phone.job;
  if (!jobSeen || $("#importbox").hidden) return;
  $("#ib-go").hidden = true;
  $("#ib-barwrap").hidden = false;
  const pct = j.bytes ? (100 * j.done_bytes) / j.bytes : (j.finished ? 100 : 0);
  $("#ib-bar").style.width = pct + "%";

  if (!j.finished) {
    const now = Date.now();
    if (rate.t && now > rate.t) {
      const s = (j.done_bytes - rate.b) / ((now - rate.t) / 1000);
      rate.speed = rate.speed ? rate.speed * 0.8 + s * 0.2 : s;   // smoothed
    }
    rate = { t: now, b: j.done_bytes, speed: rate.speed };
    const eta = rate.speed > 0 ? Math.round((j.bytes - j.done_bytes) / rate.speed) : null;
    $("#ib-title").textContent = `Importing ${j.done_items.toLocaleString()} / ${j.items.toLocaleString()}`;
    $("#ib-text").textContent = `${human(j.done_bytes)} of ${human(j.bytes)}` +
      (rate.speed > 0 ? ` · ${human(rate.speed)}/s` : "") + (eta != null ? ` · about ${dur(eta)} left` : "");
    $("#ib-detail").textContent = j.current ? `Copying ${j.current}` : "";
    $("#ib-cancel").textContent = $("#ib-cancel").disabled ? "Stopping after this file…" : "Stop";
    return;
  }

  // finished
  rate = { t: 0, b: 0, speed: 0 };
  $("#ib-title").textContent = j.error ? "Import stopped" : j.cancelled ? "Import stopped" : "Import finished";
  const parts = [`${j.copied.toLocaleString()} copied`];
  if (j.duplicate) parts.push(`${j.duplicate} duplicates (same file already backed up)`);
  if (j.already) parts.push(`${j.already} were already backed up`);
  if (j.failed) parts.push(`<b style="color:#ff7a7a">${j.failed} failed</b>`);
  $("#ib-text").innerHTML = (j.error ? `<b style="color:#ff7a7a">${esc(j.error)}</b><br>` : "") + parts.join(" · ");
  $("#ib-detail").textContent = j.log ? `Log: ${j.log}` : "";
  $("#ib-cancel").disabled = false;
  $("#ib-cancel").textContent = "Close";
  if (jobSeen !== "done") {
    jobSeen = "done";
    clearSelection();
    refresh();               // imported items now show as backed up (or drop out of "Not backed up")
  }
}

// ------------------------------------------------------------ preview
let cur = -1;
async function openViewer(idx) {
  if (idx < 0) return;
  if (idx >= state.items.length) { if (state.done) return; await loadMore(); if (idx >= state.items.length) return; }
  cur = idx;
  const it = state.items[idx];
  showViewer();
  const img = $("#v-img");
  img.alt = it.file;
  img.src = `/api/phone/thumb/${enc(it.uuid)}`;             // instant, then the sharp one
  const full = new Image();
  full.onload = () => { if (cur === idx) img.src = full.src; };
  full.src = `/api/phone/preview/${enc(it.uuid)}`;
  renderViewerMeta();
}
function renderViewerMeta() {
  const it = state.items[cur];
  const d = new Date(it.date).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  const kind = it.kind === "video" ? `▶ Video${it.duration ? " " + dur(it.duration) : ""} (preview shows a still)`
             : it.live ? "◉ Live Photo" : it.screenshot ? "▣ Screenshot" : "Photo";
  $("#v-meta").innerHTML = `<strong>${d}</strong><span>${kind}</span><span>${human(it.size)}</span>` +
    (it.favorite ? `<span class="fav">♥</span>` : "") +
    `<span style="color:var(--muted)">${esc(it.file)} · ${cur + 1}/${state.total}</span>`;
  const b = $("#v-select");
  b.hidden = it.backed_up;
  b.classList.toggle("on", selected.has(it.uuid));
  b.textContent = selected.has(it.uuid) ? "✓ Selected" : "Select";
}
function closeViewer() { $("#viewer").hidden = true; $("#v-img").removeAttribute("src"); cur = -1; }
$("#v-select").onclick = () => { toggle(state.items[cur], cur); renderSelection(); renderViewerMeta(); };
$(".v-close").onclick = closeViewer;
$(".v-prev").onclick = () => openViewer(cur - 1);
$(".v-next").onclick = () => openViewer(cur + 1);
viewerKeys(() => ({
  Escape: closeViewer,
  ArrowRight: () => openViewer(cur + 1),
  ArrowLeft: () => openViewer(cur - 1),
  s: () => $("#v-select").click(),
  " ": () => $("#v-select").click(),
}));

// ------------------------------------------------------------ backup folder
let settings = { backup_root: "", configured: true };

function nativeApi() {   // pywebview injects its API a moment after the page loads
  if (window.pywebview?.api?.pick_folder) return Promise.resolve(window.pywebview.api);
  return new Promise((res) => {
    const t = setTimeout(() => res(null), 1500);          // plain browser: no native picker
    window.addEventListener("pywebviewready", () => { clearTimeout(t); res(window.pywebview.api); }, { once: true });
  });
}
function renderRoot() {
  $("#rootpath").textContent = settings.configured ? settings.backup_root
    : `No folder chosen yet (using ${settings.backup_root})`;
}
async function loadSettings() {
  try {
    settings = await getJSON("/api/settings");
  } catch {
    $("#rootpath").textContent = "Couldn't read settings (server not reachable).";
    return;
  }
  renderRoot();
  if (!settings.configured) {
    alert("Welcome! First, choose the folder where your photo backups should be saved.\n" +
          "(This app never deletes anything from your phone.)");
    chooseFolder();
  }
}
async function chooseFolder() {
  const api = await nativeApi();
  const path = api ? await api.pick_folder() : prompt("Backup folder (full path):", settings.backup_root);
  if (!path) return;
  try {
    settings = await post("/api/settings/backup-root", { path });
  } catch (e) {
    alert("Couldn't use that folder:\n" + e.message);
    return;
  }
  location.reload();      // start fresh: library index + phone page now use the new folder
}
$("#pickroot").onclick = chooseFolder;

// ------------------------------------------------------------ start
loadSettings();
renderFilters();
renderSelection();
loadDevices().then(poll);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
