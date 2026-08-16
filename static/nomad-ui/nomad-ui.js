const t = document.documentElement;
t.classList.add("nomad-modern-runtime");
const e = () => {
  t.style.setProperty("--nomad-vh", `${window.innerHeight * 0.01}px`);
};
e();
window.addEventListener("resize", e, { passive: !0 });
window.visualViewport?.addEventListener("resize", e, { passive: !0 });
//# sourceMappingURL=nomad-ui.js.map
