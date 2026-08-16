/**
 * Discover -> legacy player adapter.
 *
 * We intentionally do not create a second audio/player implementation here.
 * Cards expose a canonical `data-nomad-track` payload and dispatch a bubbling
 * event. Existing NOMAD playback code can opt into the event without having
 * its DOM rewritten.
 */
import type { DiscoverTrack } from './discover-data';

export const DISCOVER_PLAY_EVENT = 'nomad:discover-play';

export function attachDiscoverPlayerAdapter(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>('[data-nomad-discover-track]').forEach((card) => {
    if (card.dataset.nomadPlayerBound === '1') return;
    card.dataset.nomadPlayerBound = '1';
    card.addEventListener('click', (event) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest('button,a,input,textarea,select')) return;
      const raw = card.dataset.nomadTrack;
      if (!raw) return;
      try {
        const track = JSON.parse(raw) as DiscoverTrack;
        window.dispatchEvent(new CustomEvent(DISCOVER_PLAY_EVENT, { detail: track }));
      } catch {
        // Ignore malformed legacy card metadata rather than breaking Discover.
      }
    });
  });
}

export function markDiscoverTrack(card: HTMLElement, track: DiscoverTrack): void {
  card.dataset.nomadDiscoverTrack = '1';
  card.dataset.nomadTrack = JSON.stringify(track);
  card.setAttribute('role', 'button');
  card.setAttribute('tabindex', '0');
}
