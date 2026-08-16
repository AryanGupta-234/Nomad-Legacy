import './styles/lyrics-runtime.css';

const ROOT = '.nomad-modern-runtime';
const PANEL = '.lyrics-panel.fullscreen';

const isVisible = (el: HTMLElement): boolean => {
  const r = el.getBoundingClientRect();
  const s = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
};

const isSidebar = (el: HTMLElement): boolean =>
  el.matches('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]') ||
  Boolean(el.closest('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]'));

const isChrome = (el: HTMLElement): boolean =>
  el.matches('[class*="lyrics-panel-header"], [class*="lyrics-header"], [class*="lyrics-toolbar"], [class*="lyrics-search"], [class*="lyrics-tabs"]');

function findScrollHost(panel: HTMLElement): HTMLElement | null {
  const explicit = panel.querySelector<HTMLElement>('[data-lyrics-scroll], .lyrics-content, .lyrics-scroll, .lyrics-panel-body');
  if (explicit && isVisible(explicit) && !isSidebar(explicit)) return explicit;

  const candidates = [...panel.querySelectorAll<HTMLElement>('*')]
    .filter(isVisible)
    .filter((el) => !isSidebar(el) && !isChrome(el))
    .filter((el) => el.scrollHeight > el.clientHeight + 24)
    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));

  return candidates[0] ?? null;
}

function findSidebar(panel: HTMLElement): HTMLElement | null {
  return panel.querySelector<HTMLElement>('.lyrics-fs-sidebar, [class*="lyrics-fs-sidebar"], [class*="lyrics-sidebar"]');
}

function apply(panel: HTMLElement): void {
  const scrollHost = findScrollHost(panel);
  const sidebar = findSidebar(panel);

  panel.dataset.nomadLayoutReady = 'true';

  if (scrollHost) {
    scrollHost.dataset.lyricsScroll = 'true';
    scrollHost.style.setProperty('overflow-y', 'auto', 'important');
    scrollHost.style.setProperty('overflow-x', 'hidden', 'important');
    scrollHost.style.setProperty('min-height', '0', 'important');
    scrollHost.style.setProperty('min-width', '0', 'important');
    scrollHost.style.setProperty('height', '100%', 'important');
    scrollHost.style.setProperty('scrollbar-gutter', 'stable both-edges', 'important');
  }

  if (sidebar) {
    sidebar.dataset.lyricsRail = 'true';
    sidebar.style.setProperty('overflow-y', 'auto', 'important');
    sidebar.style.setProperty('overflow-x', 'hidden', 'important');
    sidebar.style.setProperty('min-height', '0', 'important');
    sidebar.style.setProperty('height', '100%', 'important');
  }
}

function install(): void {
  const root = document.documentElement;
  root.classList.add('nomad-lyrics-runtime');

  const sync = (): void => {
    const panel = document.querySelector<HTMLElement>(PANEL);
    if (!panel) return;
    apply(panel);
  };

  sync();
  window.addEventListener('resize', sync, { passive: true });
  window.visualViewport?.addEventListener('resize', sync, { passive: true });

  if ('ResizeObserver' in window) {
    const ro = new ResizeObserver(sync);
    const observe = (): void => {
      const panel = document.querySelector<HTMLElement>(PANEL);
      if (panel) ro.observe(panel);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', observe, { once: true });
    else observe();
  }

  const mo = new MutationObserver(() => requestAnimationFrame(sync));
  mo.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style'] });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install, { once: true });
else install();

export {};
