/** Small accessibility pass for legacy Discover cards. */
export function enhanceDiscoverKeyboard(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>('[data-nomad-discover-track][role="button"]').forEach((card) => {
    if (card.dataset.nomadKeyboardBound === '1') return;
    card.dataset.nomadKeyboardBound = '1';
    card.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      card.click();
    });
  });
}
