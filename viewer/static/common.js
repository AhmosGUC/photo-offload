// Shared by app.js (backup library) and phone.js (connected phone).
// Plain globals, loaded before each page script - no build step.

const $ = (s) => document.querySelector(s);
const enc = encodeURIComponent;

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const getJSON = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const post = (url, body) => getJSON(url, { method: "POST", headers: { "Content-Type": "application/json" },
                                           body: JSON.stringify(body || {}) });

// Lazy images: only request thumbnails for tiles about to be visible.
function lazyImageObserver(rootMargin) {
  const obs = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      e.target.src = e.target.dataset.src;
      obs.unobserve(e.target);
    }
  }, { root: $("#main"), rootMargin });
  return obs;
}

// Infinite scroll: call loadMore when the sentinel comes into range.
function watchSentinel(loadMore) {
  new IntersectionObserver((e) => { if (e[0].isIntersecting) loadMore(); },
    { root: $("#main"), rootMargin: "1200px 0px" }).observe($("#sentinel"));
}

// Tiles are focusable buttons: Enter / Space act like a click.
function makeTileFocusable(t, label) {
  t.tabIndex = 0;
  t.setAttribute("role", "button");
  if (label) t.setAttribute("aria-label", label);
}
function tileKeys(grid) {
  grid.addEventListener("keydown", (e) => {
    const t = e.target.closest && e.target.closest(".tile");
    if (!t || e.target !== t || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    t.click();
  });
}

// Sidebar <a href="#"> links: never let the "#" reach the URL.
function noHashNav(nav) {
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) e.preventDefault(); });
}

// Open the full-screen viewer and move focus into it.
function showViewer() {
  const v = $("#viewer");
  const wasHidden = v.hidden;
  v.hidden = false;
  if (wasHidden) $(".v-close")?.focus();
}

// Keys while the viewer is open. actions() returns {key: fn}. Every key we use is
// marked handled - otherwise the Mac app window plays the system alert sound.
function viewerKeys(actions) {
  document.addEventListener("keydown", (e) => {
    if ($("#viewer").hidden) return;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const a = actions();
    if (a[k]) { e.preventDefault(); a[k](); }
  });
}
