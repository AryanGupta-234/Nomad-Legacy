import './styles/foundation.css';

/**
 * NOMAD frontend foundation.
 *
 * This module deliberately does not rebuild the legacy DOM. It establishes
 * stable layout primitives that the existing UI can adopt incrementally.
 */
const root = document.documentElement;
root.classList.add('nomad-modern-runtime');

const updateViewportUnit = (): void => {
  root.style.setProperty('--nomad-vh', `${window.innerHeight * 0.01}px`);
};

updateViewportUnit();
window.addEventListener('resize', updateViewportUnit, { passive: true });
window.visualViewport?.addEventListener('resize', updateViewportUnit, { passive: true });

export {};
