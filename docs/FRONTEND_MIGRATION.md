# NOMAD Frontend Production Upgrade

## Goal

Modernize the frontend delivery and layout foundation without replacing the existing NOMAD visual design or legacy DOM in one risky rewrite.

## Current baseline

- Flask remains the application/backend server.
- `templates/index.html` remains the legacy UI shell and behavior source of truth.
- Existing API routes and Server-Sent Events remain unchanged.
- The modernization layer is additive and can be adopted component-by-component.

## New frontend foundation

- Vite production bundling
- TypeScript with strict compiler checks
- CSS cascade layers
- Centralized spacing/radius/layout tokens
- `dvh`/viewport-safe sizing
- safe-area insets
- `min-width: 0` / `min-height: 0` overflow contracts
- reusable scroll-region primitives
- container-query-ready component boundaries
- reduced-motion support
- production source maps
- runtime `ResizeObserver` measurement for fixed titlebars and player chrome
- `visualViewport` support for browser/window resizing and mobile viewport changes
- measured CSS variables for header height, player clearance, viewport width and viewport height
- isolation/containment primitives for complex panels

Vite's production build is used as an asset pipeline rather than forcing a framework migration. This preserves the existing Flask/Jinja application while giving future UI work a typed, modular frontend boundary.

## Runtime layout contract

The frontend foundation publishes layout measurements instead of requiring components to guess them:

```text
window / visualViewport
        │
        ├── viewport width/height
        ├── titlebar height
        └── player height + safe-area clearance
                │
                ▼
        CSS custom properties
                │
                ├── fullscreen surfaces
                ├── scroll regions
                └── responsive panels
```

The measurement layer is intentionally DOM-agnostic. It recognizes the existing titlebar and common player selectors, but does not rebuild or move those elements. This makes the migration safe while the legacy UI remains the source of truth.

## Migration rules

1. Do not replace the existing UI wholesale.
2. Do not duplicate backend state or API contracts.
3. Move one feature at a time from inline JavaScript/CSS into `frontend/src`.
4. Keep the legacy DOM stable until the migrated feature has parity.
5. Every migrated feature must preserve keyboard behavior, player behavior, SSE updates, and responsive behavior.
6. Avoid absolute positioning for primary layout. Grid/Flex should own page geometry; absolute positioning is reserved for overlays.
7. Every nested grid/flex scroll boundary must explicitly establish `min-width: 0` and `min-height: 0`.
8. Player and header clearance must be represented as layout space, not accidental z-index offsets.
9. Runtime measurement code must publish CSS state, not mutate the legacy component geometry directly.
10. Visual redesign is a separate phase from the engineering migration; the current NOMAD UI remains the visual reference.

## Planned module boundaries

```text
frontend/src/
  main.ts
  styles/
    foundation.css
    production.css
  modules/
    player/
    lyrics/
    navigation/
    media/
    storage/
    discover/
    tunnel/
```

The first passes intentionally contain only shared infrastructure. Feature modules should be extracted after DOM/API parity tests are established.

## Local development

```powershell
cd frontend
npm install
npm run typecheck
npm run dev
```

Production build:

```powershell
npm run build
```

The build emits browser assets under `static/nomad-ui/` for Flask to serve.

## Verification checklist

Before considering a migrated feature production-ready:

- [ ] TypeScript passes with no errors.
- [ ] Vite production build succeeds.
- [ ] Desktop resize does not introduce horizontal overflow.
- [ ] Narrow/mobile resize preserves usable controls and scroll boundaries.
- [ ] Fixed/sticky chrome does not cover primary content.
- [ ] Player clearance is preserved when player height changes.
- [ ] Keyboard focus remains visible.
- [ ] Reduced-motion preference is respected.
- [ ] Existing backend/API/SSE behavior remains unchanged.
