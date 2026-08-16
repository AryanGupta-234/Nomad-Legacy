const y = ".lyrics-panel.fullscreen", m = (e) => {
  const t = e.getBoundingClientRect(), r = getComputedStyle(e);
  return t.width > 0 && t.height > 0 && r.display !== "none" && r.visibility !== "hidden";
}, p = (e) => e.matches('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]') || !!e.closest('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]'), g = (e) => e.matches('[class*="lyrics-panel-header"], [class*="lyrics-header"], [class*="lyrics-toolbar"], [class*="lyrics-search"], [class*="lyrics-tabs"]');
function w(e) {
  const t = e.querySelector("[data-lyrics-scroll], .lyrics-content, .lyrics-scroll, .lyrics-panel-body");
  return t && m(t) && !p(t) ? t : [...e.querySelectorAll("*")].filter(m).filter((s) => !p(s) && !g(s)).filter((s) => s.scrollHeight > s.clientHeight + 24).sort((s, o) => o.scrollHeight - o.clientHeight - (s.scrollHeight - s.clientHeight))[0] ?? null;
}
function S(e) {
  return e.querySelector('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]');
}
function L(e) {
  const t = w(e), r = S(e);
  e.dataset.nomadLayoutReady = "true", t && (t.dataset.lyricsScroll = "true", t.style.setProperty("overflow-y", "auto", "important"), t.style.setProperty("overflow-x", "hidden", "important"), t.style.setProperty("min-height", "0", "important"), t.style.setProperty("min-width", "0", "important"), t.style.setProperty("height", "100%", "important"), t.style.setProperty("scrollbar-gutter", "stable both-edges", "important")), r && (r.dataset.lyricsRail = "true", r.style.setProperty("overflow-y", "auto", "important"), r.style.setProperty("overflow-x", "hidden", "important"), r.style.setProperty("min-height", "0", "important"), r.style.setProperty("height", "100%", "important"));
}
function f() {
  document.documentElement.classList.add("nomad-lyrics-runtime");
  const t = () => {
    const s = document.querySelector(y);
    s && L(s);
  };
  if (t(), window.addEventListener("resize", t, { passive: !0 }), window.visualViewport?.addEventListener("resize", t, { passive: !0 }), "ResizeObserver" in window) {
    const s = new ResizeObserver(t), o = () => {
      const c = document.querySelector(y);
      c && s.observe(c);
    };
    document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", o, { once: !0 }) : o();
  }
  new MutationObserver(() => requestAnimationFrame(t)).observe(document.body, { childList: !0, subtree: !0, attributes: !0, attributeFilter: ["class", "style"] });
}
document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", f, { once: !0 }) : f();
async function A(e) {
  try {
    const t = await fetch("/api/discover", {
      method: "GET",
      headers: { Accept: "application/json" },
      credentials: "same-origin",
      signal: e
    });
    if (!t.ok) return null;
    const r = await t.json();
    return {
      configured: !!r?.configured,
      source: r?.source,
      channel_id: r?.channel_id,
      count: Number(r?.count || r?.tracks?.length || 0),
      tracks: Array.isArray(r?.tracks) ? r.tracks : [],
      recent: Array.isArray(r?.recent) ? r.recent : [],
      recommendations: Array.isArray(r?.recommendations) ? r.recommendations : [],
      artists: Array.isArray(r?.artists) ? r.artists : []
    };
  } catch {
    return null;
  }
}
function q(e) {
  if (!Number.isFinite(e) || !e || e < 0) return "";
  const t = Math.floor(e);
  return `${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
}
const n = document.documentElement;
n.classList.add("nomad-modern-runtime");
const d = (e) => `${Math.max(0, Math.round(e))}px`, C = () => {
  const e = window.visualViewport, t = e?.height ?? window.innerHeight, r = e?.width ?? window.innerWidth;
  n.style.setProperty("--nomad-vh", `${t * 0.01}px`), n.style.setProperty("--nomad-viewport-height", d(t)), n.style.setProperty("--nomad-viewport-width", d(r)), n.style.setProperty("--nomad-is-compact", r <= 900 ? "1" : "0");
}, v = () => {
  const e = document.querySelector(".app-titlebar");
  e && n.style.setProperty("--nomad-measured-header", d(e.getBoundingClientRect().height));
  const t = ["[data-player]", "#player", "#music-player", ".music-player", ".bottom-player", ".player-bar", ".now-playing-bar"];
  let r = 0;
  for (const s of t) {
    const o = document.querySelector(s);
    if (!o) continue;
    const c = o.getBoundingClientRect(), i = getComputedStyle(o);
    i.display !== "none" && i.visibility !== "hidden" && c.height > 0 && (r = Math.max(r, c.height));
  }
  r > 0 && n.style.setProperty("--nomad-measured-player", d(r + 12));
}, b = () => Array.from(document.querySelectorAll(
  '[id*="discover" i], [class*="discover" i], [data-tab="discover"], [data-page="discover"]'
)).filter((e) => !e.closest(".sidebar, .sidebar-nav, .nav-group")), l = () => {
  for (const e of b())
    e.classList.add("nomad-discover-surface"), e.dataset.discoverModern = "true", e.querySelectorAll('.grid, .cards, .card-grid, [class*="grid" i], [class*="cards" i], [class*="results" i]').forEach((t) => t.classList.add("nomad-discover-grid")), e.querySelectorAll('.card, [class*="card" i], article, [role="article"]').forEach((t) => {
      t.closest(".sidebar, .sidebar-nav") || t.classList.add("nomad-discover-card");
    });
}, h = async () => {
  const e = await A();
  if (e) {
    n.dataset.youtubeConfigured = String(e.configured), n.dataset.discoverTrackCount = String(e.count ?? e.tracks.length), n.dataset.discoverRecommendationCount = String(e.recommendations.length);
    for (const t of b()) {
      t.dataset.discoverSource = e.source ?? "unknown", t.dataset.discoverTrackCount = String(e.tracks.length);
      const r = Array.from(t.querySelectorAll('.card, [class*="card" i], article, [role="article"]')), s = [...e.recent, ...e.recommendations];
      r.slice(0, s.length).forEach((o, c) => {
        const i = s[c];
        if (i) {
          if (o.dataset.nomadTrackId = i.id, o.dataset.nomadProvider = i.provider, i.thumbnail && !o.querySelector("img")) {
            const a = document.createElement("img");
            a.src = i.thumbnail, a.alt = i.title, a.loading = "lazy", a.className = "discover-modern-artwork", o.prepend(a);
          }
          if (o.classList.add("discover-modern-card"), !o.querySelector("[data-discover-meta]")) {
            const a = document.createElement("div");
            a.dataset.discoverMeta = "true", a.className = "discover-modern-meta", a.textContent = [i.artist, q(i.duration)].filter(Boolean).join(" · "), o.append(a);
          }
        }
      });
    }
  }
}, u = () => {
  C(), v(), l();
};
u();
window.addEventListener("resize", u, { passive: !0 });
window.addEventListener("orientationchange", u, { passive: !0 });
window.visualViewport?.addEventListener("resize", u, { passive: !0 });
document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", () => {
  l(), h();
}, { once: !0 }) : (l(), h());
if ("MutationObserver" in window) {
  let e = !1;
  new MutationObserver(() => {
    e || (e = !0, requestAnimationFrame(() => {
      e = !1, l();
    }));
  }).observe(document.body, { childList: !0, subtree: !0 });
}
if ("ResizeObserver" in window) {
  const e = new ResizeObserver(() => v()), t = () => {
    const r = document.querySelector(".app-titlebar");
    r && e.observe(r);
    for (const s of ["[data-player]", "#player", "#music-player", ".music-player", ".bottom-player", ".player-bar", ".now-playing-bar"]) {
      const o = document.querySelector(s);
      o && e.observe(o);
    }
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", t, { once: !0 }) : t();
}
//# sourceMappingURL=nomad-ui.js.map
