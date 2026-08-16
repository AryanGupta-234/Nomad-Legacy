const u = ".lyrics-panel.fullscreen", y = (t) => {
  const e = t.getBoundingClientRect(), s = getComputedStyle(t);
  return e.width > 0 && e.height > 0 && s.display !== "none" && s.visibility !== "hidden";
}, m = (t) => t.matches('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]') || !!t.closest('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]'), h = (t) => t.matches('[class*="lyrics-panel-header"], [class*="lyrics-header"], [class*="lyrics-toolbar"], [class*="lyrics-search"], [class*="lyrics-tabs"]');
function f(t) {
  const e = t.querySelector("[data-lyrics-scroll], .lyrics-content, .lyrics-scroll, .lyrics-panel-body");
  return e && y(e) && !m(e) ? e : [...t.querySelectorAll("*")].filter(y).filter((r) => !m(r) && !h(r)).filter((r) => r.scrollHeight > r.clientHeight + 24).sort((r, o) => o.scrollHeight - o.clientHeight - (r.scrollHeight - r.clientHeight))[0] ?? null;
}
function g(t) {
  return t.querySelector('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]');
}
function w(t) {
  const e = f(t), s = g(t);
  t.dataset.nomadLayoutReady = "true", e && (e.dataset.lyricsScroll = "true", e.style.setProperty("overflow-y", "auto", "important"), e.style.setProperty("overflow-x", "hidden", "important"), e.style.setProperty("min-height", "0", "important"), e.style.setProperty("min-width", "0", "important"), e.style.setProperty("height", "100%", "important"), e.style.setProperty("scrollbar-gutter", "stable both-edges", "important")), s && (s.dataset.lyricsRail = "true", s.style.setProperty("overflow-y", "auto", "important"), s.style.setProperty("overflow-x", "hidden", "important"), s.style.setProperty("min-height", "0", "important"), s.style.setProperty("height", "100%", "important"));
}
function p() {
  document.documentElement.classList.add("nomad-lyrics-runtime");
  const e = () => {
    const r = document.querySelector(u);
    r && w(r);
  };
  if (e(), window.addEventListener("resize", e, { passive: !0 }), window.visualViewport?.addEventListener("resize", e, { passive: !0 }), "ResizeObserver" in window) {
    const r = new ResizeObserver(e), o = () => {
      const n = document.querySelector(u);
      n && r.observe(n);
    };
    document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", o, { once: !0 }) : o();
  }
  new MutationObserver(() => requestAnimationFrame(e)).observe(document.body, { childList: !0, subtree: !0, attributes: !0, attributeFilter: ["class", "style"] });
}
document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", p, { once: !0 }) : p();
const i = document.documentElement;
i.classList.add("nomad-modern-runtime");
const c = (t) => `${Math.max(0, Math.round(t))}px`, S = () => {
  const t = window.visualViewport, e = t?.height ?? window.innerHeight, s = t?.width ?? window.innerWidth;
  i.style.setProperty("--nomad-vh", `${e * 0.01}px`), i.style.setProperty("--nomad-viewport-height", c(e)), i.style.setProperty("--nomad-viewport-width", c(s)), i.style.setProperty("--nomad-is-compact", s <= 900 ? "1" : "0");
}, b = () => {
  const t = document.querySelector(".app-titlebar");
  t && i.style.setProperty("--nomad-measured-header", c(t.getBoundingClientRect().height));
  const e = [
    "[data-player]",
    "#player",
    "#music-player",
    ".music-player",
    ".bottom-player",
    ".player-bar",
    ".now-playing-bar"
  ];
  let s = 0;
  for (const r of e) {
    const o = document.querySelector(r);
    if (!o) continue;
    const n = o.getBoundingClientRect(), l = window.getComputedStyle(o);
    l.display !== "none" && l.visibility !== "hidden" && n.height > 0 && (s = Math.max(s, n.height));
  }
  if (s > 0) {
    const r = Number.parseFloat(
      getComputedStyle(i).getPropertyValue("--nomad-safe-bottom") || "0"
    ) || 0;
    i.style.setProperty("--nomad-measured-player", c(s + r + 12));
  }
}, d = () => {
  const t = Array.from(document.querySelectorAll(
    '[id*="discover" i], [class*="discover" i], [data-tab="discover"], [data-page="discover"]'
  ));
  for (const e of t) {
    if (e.closest(".sidebar, .sidebar-nav, .nav-group")) continue;
    e.classList.add("nomad-discover-surface");
    const s = e.querySelectorAll(
      '.grid, .cards, .card-grid, [class*="grid" i], [class*="cards" i], [class*="results" i]'
    );
    for (const o of s)
      o.classList.add("nomad-discover-grid");
    const r = e.querySelectorAll(
      '.card, [class*="card" i], article, [role="article"]'
    );
    for (const o of r)
      o.closest(".sidebar, .sidebar-nav") || o.classList.add("nomad-discover-card");
  }
}, v = async () => {
  try {
    const t = await fetch("/api/discover", { headers: { Accept: "application/json" } });
    if (!t.ok) return;
    const e = await t.json();
    i.dataset.youtubeConfigured = e.configured ? "true" : "false", i.dataset.discoverTrackCount = String(e.tracks?.length ?? 0), i.dataset.discoverRecommendationCount = String(e.recommendations?.length ?? 0);
  } catch {
    i.dataset.youtubeConfigured = "false";
  }
}, a = () => {
  S(), b(), d();
};
a();
window.addEventListener("resize", a, { passive: !0 });
window.addEventListener("orientationchange", a, { passive: !0 });
window.visualViewport?.addEventListener("resize", a, { passive: !0 });
window.visualViewport?.addEventListener("scroll", a, { passive: !0 });
document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", () => {
  d(), v();
}, { once: !0 }) : (d(), v());
"MutationObserver" in window && new MutationObserver(() => d()).observe(document.body, { childList: !0, subtree: !0 });
if ("ResizeObserver" in window) {
  const t = new ResizeObserver(() => b()), e = () => {
    const s = document.querySelector(".app-titlebar");
    s && t.observe(s);
    for (const r of ["[data-player]", "#player", "#music-player", ".music-player", ".bottom-player", ".player-bar", ".now-playing-bar"]) {
      const o = document.querySelector(r);
      o && t.observe(o);
    }
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", e, { once: !0 }) : e();
}
//# sourceMappingURL=nomad-ui.js.map
