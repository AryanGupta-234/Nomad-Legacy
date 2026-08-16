import './styles/foundation.css';
import './styles/production.css';
import './styles/lyrics-discover-overhaul.css';

/**
 * NOMAD frontend foundation.
 *
 * This module deliberately does not rebuild the legacy DOM. It establishes
 * stable layout primitives that the existing UI can adopt incrementally.
 * Runtime measurements are published as CSS variables so fixed/sticky chrome
 * can coexist with responsive content without hard-coded viewport guesses.
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
    '[data-player]',
    '#player',
    '#music-player',
    '.music-player',
    '.bottom-player',
    '.player-bar',
    '.now-playing-bar',
  ];

  let playerHeight = 0;
  for (const selector of playerCandidates) {
    const element = document.querySelector<HTMLElement>(selector);
    if (!element) continue;
    const rect = element.getBoundingClientRect();
    const style = window.getComputedStyle(element);
    const isVisible = style.display !== 'none' && style.visibility !== 'hidden' && rect.height > 0;
    if (isVisible) {
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

const updateLayout = (): void => {
  updateViewport();
  measureChrome();
};

updateLayout();

window.addEventListener('resize', updateLayout, { passive: true });
window.addEventListener('orientationchange', updateLayout, { passive: true });
window.visualViewport?.addEventListener('resize', updateLayout, { passive: true });
window.visualViewport?.addEventListener('scroll', updateLayout, { passive: true });

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

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', observe, { once: true });
  } else {
    observe();
  }
}

export {};
