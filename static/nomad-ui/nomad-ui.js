const d = ".lyrics-panel.fullscreen", y = (t) => {
  const e = t.getBoundingClientRect(), r = getComputedStyle(t);
  return e.width > 0 && e.height > 0 && r.display !== "none" && r.visibility !== "hidden";
}, u = (t) => t.matches('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]') || !!t.closest('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]'), h = (t) => t.matches('[class*="lyrics-panel-header"], [class*="lyrics-header"], [class*="lyrics-toolbar"], [class*="lyrics-search"], [class*="lyrics-tabs"]');
function b(t) {
  const e = t.querySelector("[data-lyrics-scroll], .lyrics-content, .lyrics-scroll, .lyrics-panel-body");
  return e && y(e) && !u(e) ? e : [...t.querySelectorAll("*")].filter(y).filter((s) => !u(s) && !h(s)).filter((s) => s.scrollHeight > s.clientHeight + 24).sort((s, o) => o.scrollHeight - o.clientHeight - (s.scrollHeight - s.clientHeight))[0] ?? null;
}
function w(t) {
  return t.querySelector('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]');
}
function f(t) {
  const e = b(t), r = w(t);
  t.dataset.nomadLayoutReady = "true", e && (e.dataset.lyricsScroll = "true", e.style.setProperty("overflow-y", "auto", "important"), e.style.setProperty("overflow-x", "hidden", "important"), e.style.setProperty("min-height", "0", "important"), e.style.setProperty("min-width", "0", "important"), e.style.setProperty("height", "100%", "important"), e.style.setProperty("scrollbar-gutter", "stable both-edges", "important")), r && (r.dataset.lyricsRail = "true", r.style.setProperty("overflow-y", "auto", "important"), r.style.setProperty("overflow-x", "hidden", "important"), r.style.setProperty("min-height", "0", "important"), r.style.setProperty("height", "100%", "important"));
}
function p() {
  document.documentElement.classList.add("nomad-lyrics-runtime");
  const e = () => {
    const s = document.querySelector(d);
    s && f(s);
  };
  if (e(), window.addEventListener("resize", e, { passive: !0 }), window.visualViewport?.addEventListener("resize", e, { passive: !0 }), "ResizeObserver" in window) {
    const s = new ResizeObserver(e), o = () => {
      const n = document.querySelector(d);
      n && s.observe(n);
    };
    document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", o, { once: !0 }) : o();
  }
  new MutationObserver(() => requestAnimationFrame(e)).observe(document.body, { childList: !0, subtree: !0, attributes: !0, attributeFilter: ["class", "style"] });
}
document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", p, { once: !0 }) : p();
const i = document.documentElement;
i.classList.add("nomad-modern-runtime");
const l = (t) => `${Math.max(0, Math.round(t))}px`, v = () => {
  const t = window.visualViewport, e = t?.height ?? window.innerHeight, r = t?.width ?? window.innerWidth;
  i.style.setProperty("--nomad-vh", `${e * 0.01}px`), i.style.setProperty("--nomad-viewport-height", l(e)), i.style.setProperty("--nomad-viewport-width", l(r)), i.style.setProperty("--nomad-is-compact", r <= 900 ? "1" : "0");
}, m = () => {
  const t = document.querySelector(".app-titlebar");
  t && i.style.setProperty("--nomad-measured-header", l(t.getBoundingClientRect().height));
  const e = [
    "[data-player]",
    "#player",
    "#music-player",
    ".music-player",
    ".bottom-player",
    ".player-bar",
    ".now-playing-bar"
  ];
  let r = 0;
  for (const s of e) {
    const o = document.querySelector(s);
    if (!o) continue;
    const n = o.getBoundingClientRect(), c = window.getComputedStyle(o);
    c.display !== "none" && c.visibility !== "hidden" && n.height > 0 && (r = Math.max(r, n.height));
  }
  if (r > 0) {
    const s = Number.parseFloat(
      getComputedStyle(i).getPropertyValue("--nomad-safe-bottom") || "0"
    ) || 0;
    i.style.setProperty("--nomad-measured-player", l(r + s + 12));
  }
}, a = () => {
  v(), m();
};
a();
window.addEventListener("resize", a, { passive: !0 });
window.addEventListener("orientationchange", a, { passive: !0 });
window.visualViewport?.addEventListener("resize", a, { passive: !0 });
window.visualViewport?.addEventListener("scroll", a, { passive: !0 });
if ("ResizeObserver" in window) {
  const t = new ResizeObserver(() => m()), e = () => {
    const r = document.querySelector(".app-titlebar");
    r && t.observe(r);
    for (const s of ["[data-player]", "#player", "#music-player", ".music-player", ".bottom-player", ".player-bar", ".now-playing-bar"]) {
      const o = document.querySelector(s);
      o && t.observe(o);
    }
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", e, { once: !0 }) : e();
}
//# sourceMappingURL=nomad-ui.js.map
