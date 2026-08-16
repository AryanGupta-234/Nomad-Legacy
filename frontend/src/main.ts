import './styles/foundation.css';
import './styles/production.css';
import './styles/lyrics-discover-overhaul.css';
import './styles/discover-modern-pass.css';
import './styles/discover-modern.css';
import './lyrics-layout-runtime';
import { loadDiscover, formatTrackDuration, type DiscoverTrack } from './discover-data';

/** NOMAD frontend foundation. The legacy DOM remains the visual source of truth. */
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
  if (titlebar) root.style.setProperty('--nomad-measured-header', px(titlebar.getBoundingClientRect().height));
  const selectors = ['[data-player]', '#player', '#music-player', '.music-player', '.bottom-player', '.player-bar', '.now-playing-bar'];
  let playerHeight = 0;
  for (const selector of selectors) {
    const element = document.querySelector<HTMLElement>(selector);
    if (!element) continue;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    if (style.display !== 'none' && style.visibility !== 'hidden' && rect.height > 0) playerHeight = Math.max(playerHeight, rect.height);
  }
  if (playerHeight > 0) root.style.setProperty('--nomad-measured-player', px(playerHeight + 12));
};

const discoverCandidates = (): HTMLElement[] => Array.from(document.querySelectorAll<HTMLElement>(
  '[id*="discover" i], [class*="discover" i], [data-tab="discover"], [data-page="discover"]',
)).filter((element) => !element.closest('.sidebar, .sidebar-nav, .nav-group'));

const enhanceDiscover = (): void => {
  for (const element of discoverCandidates()) {
    element.classList.add('nomad-discover-surface');
    element.dataset.discoverModern = 'true';
    element.querySelectorAll<HTMLElement>('.grid, .cards, .card-grid, [class*="grid" i], [class*="cards" i], [class*="results" i]')
      .forEach((grid) => grid.classList.add('nomad-discover-grid'));
    element.querySelectorAll<HTMLElement>('.card, [class*="card" i], article, [role="article"]')
      .forEach((card) => { if (!card.closest('.sidebar, .sidebar-nav')) card.classList.add('nomad-discover-card'); });
  }
};

/** Attach the server catalogue to the existing Discover surface without replacing its DOM. */
const bindDiscoverCatalogue = async (): Promise<void> => {
  const data = await loadDiscover();
  if (!data) return;
  root.dataset.youtubeConfigured = String(data.configured);
  root.dataset.discoverTrackCount = String(data.count ?? data.tracks.length);
  root.dataset.discoverRecommendationCount = String(data.recommendations.length);

  for (const surface of discoverCandidates()) {
    surface.dataset.discoverSource = data.source ?? 'unknown';
    surface.dataset.discoverTrackCount = String(data.tracks.length);

    // Existing cards become data-aware; no legacy markup is removed.
    const cards = Array.from(surface.querySelectorAll<HTMLElement>('.card, [class*="card" i], article, [role="article"]'));
    const tracks = [...data.recent, ...data.recommendations];
    cards.slice(0, tracks.length).forEach((card, index) => {
      const track = tracks[index];
      if (!track) return;
      card.dataset.nomadTrackId = track.id;
      card.dataset.nomadProvider = track.provider;
      if (track.thumbnail && !card.querySelector('img')) {
        const image = document.createElement('img');
        image.src = track.thumbnail;
        image.alt = track.title;
        image.loading = 'lazy';
        image.className = 'discover-modern-artwork';
        card.prepend(image);
      }
      card.classList.add('discover-modern-card');
      if (!card.querySelector('[data-discover-meta]')) {
        const meta = document.createElement('div');
        meta.dataset.discoverMeta = 'true';
        meta.className = 'discover-modern-meta';
        meta.textContent = [track.artist, formatTrackDuration(track.duration)].filter(Boolean).join(' · ');
        card.append(meta);
      }
    });
  }
};

const updateLayout = (): void => { updateViewport(); measureChrome(); enhanceDiscover(); };
updateLayout();
window.addEventListener('resize', updateLayout, { passive: true });
window.addEventListener('orientationchange', updateLayout, { passive: true });
window.visualViewport?.addEventListener('resize', updateLayout, { passive: true });

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => { enhanceDiscover(); void bindDiscoverCatalogue(); }, { once: true });
} else {
  enhanceDiscover();
  void bindDiscoverCatalogue();
}

if ('MutationObserver' in window) {
  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; enhanceDiscover(); });
  });
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
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', observe, { once: true }); else observe();
}

export {};
