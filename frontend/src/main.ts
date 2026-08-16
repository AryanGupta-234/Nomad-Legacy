import './styles/foundation.css';
import './styles/production.css';
import './styles/lyrics-discover-overhaul.css';
import './styles/discover-modern-pass.css';
import './lyrics-layout-runtime';

/**
 * NOMAD frontend foundation.
 *
 * The legacy DOM remains the visual source of truth. Runtime measurements are
 * published as CSS variables so fixed/sticky chrome can coexist with
 * responsive content without hard-coded viewport guesses.
 */
const root = document.documentElement;
root.classList.add('nomad-modern-runtime');

const px = (value: number): string => `${Math.max(0, Math.round(value))}px`;

const updateViewport = (): void => {
  const viewport = window.visualViewport;
  const height = viewport?.height ?? window.innerHeight;
  const width = viewport?.width ?? window.innerWidth;

  root.style.setProperty('--nomad-vh', `${height * 0.01}px`);
  root.style.setProperty('--nomad-viewport-height', px(height));
  root.style.setProperty('--nomad-viewport-width', px(width));
  root.style.setProperty('--nomad-is-compact', width <= 900 ? '1' : '0');
};

const measureChrome = (): void => {
  const titlebar = document.querySelector<HTMLElement>('.app-titlebar');
  if (titlebar) {
    root.style.setProperty('--nomad-measured-header', px(titlebar.getBoundingClientRect().height));
  }

  const playerCandidates = [
    '[data-player]', '#player', '#music-player', '.music-player',
    '.bottom-player', '.player-bar', '.now-playing-bar',
  ];

  let playerHeight = 0;
  for (const selector of playerCandidates) {
    const element = document.querySelector<HTMLElement>(selector);
    if (!element) continue;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    if (style.display !== 'none' && style.visibility !== 'hidden' && rect.height > 0) {
      playerHeight = Math.max(playerHeight, rect.height);
    }
  }

  if (playerHeight > 0) {
    const safeBottom = Number.parseFloat(
      getComputedStyle(root).getPropertyValue('--nomad-safe-bottom') || '0',
    ) || 0;
    root.style.setProperty('--nomad-measured-player', px(playerHeight + safeBottom + 12));
  }
};

/** Upgrade the existing Discover DOM without replacing it. */
const enhanceDiscover = (): void => {
  const candidates = Array.from(document.querySelectorAll<HTMLElement>(
    '[id*="discover" i], [class*="discover" i], [data-tab="discover"], [data-page="discover"]',
  ));

  for (const element of candidates) {
    if (element.closest('.sidebar, .sidebar-nav, .nav-group')) continue;
    element.classList.add('nomad-discover-surface');

    const grids = element.querySelectorAll<HTMLElement>(
      '.grid, .cards, .card-grid, [class*="grid" i], [class*="cards" i], [class*="results" i]',
    );
    for (const grid of grids) {
      grid.classList.add('nomad-discover-grid');
    }

    const cards = element.querySelectorAll<HTMLElement>(
      '.card, [class*="card" i], article, [role="article"]',
    );
    for (const card of cards) {
      if (card.closest('.sidebar, .sidebar-nav')) continue;
      card.classList.add('nomad-discover-card');
    }
  }
};

const loadDiscoverData = async (): Promise<void> => {
  try {
    const response = await fetch('/api/discover', { headers: { Accept: 'application/json' } });
    if (!response.ok) return;
    const data = await response.json() as { configured?: boolean; tracks?: unknown[]; recommendations?: unknown[] };
    root.dataset.youtubeConfigured = data.configured ? 'true' : 'false';
    root.dataset.discoverTrackCount = String(data.tracks?.length ?? 0);
    root.dataset.discoverRecommendationCount = String(data.recommendations?.length ?? 0);
  } catch {
    root.dataset.youtubeConfigured = 'false';
  }
};

const updateLayout = (): void => {
  updateViewport();
  measureChrome();
  enhanceDiscover();
};

updateLayout();
window.addEventListener('resize', updateLayout, { passive: true });
window.addEventListener('orientationchange', updateLayout, { passive: true });
window.visualViewport?.addEventListener('resize', updateLayout, { passive: true });
window.visualViewport?.addEventListener('scroll', updateLayout, { passive: true });

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    enhanceDiscover();
    void loadDiscoverData();
  }, { once: true });
} else {
  enhanceDiscover();
  void loadDiscoverData();
}

if ('MutationObserver' in window) {
  const observer = new MutationObserver(() => enhanceDiscover());
  observer.observe(document.body, { childList: true, subtree: true });
}

if ('ResizeObserver' in window) {
  const observer = new ResizeObserver(() => measureChrome());
  const observe = (): void => {
    const titlebar = document.querySelector<HTMLElement>('.app-titlebar');
    if (titlebar) observer.observe(titlebar);
    for (const selector of ['[data-player]', '#player', '#music-player', '.music-player', '.bottom-player', '.player-bar', '.now-playing-bar']) {
      const element = document.querySelector<HTMLElement>(selector);
      if (element) observer.observe(element);
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', observe, { once: true });
  else observe();
}

export {};
