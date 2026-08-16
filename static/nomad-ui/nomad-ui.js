const o = document.documentElement;
o.classList.add("nomad-modern-runtime");
const a = (e) => `${Math.max(0, Math.round(e))}px`, p = () => {
  const e = window.visualViewport, r = e?.height ?? window.innerHeight, t = e?.width ?? window.innerWidth;
  o.style.setProperty("--nomad-vh", `${r * 0.01}px`), o.style.setProperty("--nomad-viewport-height", a(r)), o.style.setProperty("--nomad-viewport-width", a(t)), o.style.setProperty("--nomad-is-compact", t <= 900 ? "1" : "0");
}, c = () => {
  const e = document.querySelector(".app-titlebar");
  e && o.style.setProperty("--nomad-measured-header", a(e.getBoundingClientRect().height));
  const r = [
    "[data-player]",
    "#player",
    "#music-player",
    ".music-player",
    ".bottom-player",
    ".player-bar",
    ".now-playing-bar"
  ];
  let t = 0;
  for (const i of r) {
    const n = document.querySelector(i);
    if (!n) continue;
    const d = n.getBoundingClientRect(), l = window.getComputedStyle(n);
    l.display !== "none" && l.visibility !== "hidden" && d.height > 0 && (t = Math.max(t, d.height));
  }
  if (t > 0) {
    const i = Number.parseFloat(
      getComputedStyle(o).getPropertyValue("--nomad-safe-bottom") || "0"
    ) || 0;
    o.style.setProperty("--nomad-measured-player", a(t + i + 12));
  }
}, s = () => {
  p(), c();
};
s();
window.addEventListener("resize", s, { passive: !0 });
window.addEventListener("orientationchange", s, { passive: !0 });
window.visualViewport?.addEventListener("resize", s, { passive: !0 });
window.visualViewport?.addEventListener("scroll", s, { passive: !0 });
if ("ResizeObserver" in window) {
  const e = new ResizeObserver(() => c()), r = () => {
    const t = document.querySelector(".app-titlebar");
    t && e.observe(t);
    for (const i of ["[data-player]", "#player", "#music-player", ".music-player", ".bottom-player", ".player-bar", ".now-playing-bar"]) {
      const n = document.querySelector(i);
      n && e.observe(n);
    }
  };
  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", r, { once: !0 }) : r();
}
//# sourceMappingURL=nomad-ui.js.map
