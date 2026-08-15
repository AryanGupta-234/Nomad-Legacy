# Nomad Redesign & Feature Overhaul — Complete Changelog

**Date:** August 2026  
**Session:** Comprehensive UI/UX + Feature Pass + Apple Charts Fix

---

## 🎨 **DESIGN SYSTEM** (NEW)

### Named Color Palette
Added semantic naming for the core palette — all new UI components pull from this instead of one-off hex values:
- `--c-black` / `--c-black-2`: Deep black, RGB foundation
- `--c-graphite` / `--c-graphite-2`: Dark grey for secondary surfaces
- `--c-glass`: Glass morphism base (`rgba(255,255,255,0.045)`)
- `--c-purple`, `--c-blue`, `--c-emerald`: Primary accent colors (with `-rgb` variants for rgba)
- `--spring`: Spring easing function (`cubic-bezier(.34,1.56,.64,1)`) used throughout for snappy, playful micro-interactions

### Dynamic Accent Color (From Artwork)
New `applyAccentFromArtwork()` function samples the currently-playing album cover and extracts the most vibrant pixel, then drives:
- Player bar colors
- Seek fill gradient  
- Lyrics panel text highlights
- Focus ring colors on interactive elements

Falls back silently to the default pink if artwork can't be read (CORS, network, etc.) — pure visual enhancement, never blocks playback.

**Backend support:** New `/api/thumb_proxy` route that proxies thumbnail requests through our origin (fixes YouTube CORS-taint on `<canvas>.getImageData()`), with SSRF protection to prevent access to private/loopback IPs.

---

## 🎯 **BUTTON & PROGRESS SYSTEM** (OVERHAULED)

### `setButtonLoading()` Redesign
Every button loading state now includes a springy **"settle" pop** on completion — a tiny overshoot animation that reads as "done" without needing a toast.
- Uses `--spring` easing curve
- Fires a `.just-done` class for ~0.42s on completion
- Applied globally (69 existing call sites get this for free, no per-button changes needed)

### `setProgressFill()` Progress Bars
New helper function for all progress indicators across the app:
- Animates width with spring easing (`--spring`) instead of linear
- **Glow pulse at 100%**: fires a `progressComplete` keyframe (radiant highlight + brightness spike) when a bar reaches completion
- Wired into: media queue, storage scan, ffmpeg/upscaler installs, per-row downloads, fingerprint scan

---

## 🎤 **LYRICS PANEL REDESIGN** (MAJOR)

### Tab Strip (6 Tabs)
Replaced the single "Annotations" toggle with a full tab system:

1. **Lyrics** — LRCLIB synced karaoke highlight (existing, still works)
2. **Meaning** — Genius crowd-sourced annotations (existing, now exposed as tab)
3. **Translation** — NEW: Groq-backed, 6 languages (Spanish/French/German/Japanese/Hindi/Portuguese), re-synced to LRCLIB timing when available
4. **AI Explanation** — NEW: Groq-generated "what is this song about" in 150 words
5. **Vocabulary** — NEW: 10 standout words/phrases with plain-language definitions and context notes
6. **Story** — NEW: Groq-generated "story behind the song" (inspired by / context / references)

### Tabs UI
- Glass pill-style buttons (gradient background on active)
- Spring animations + micro-interactions
- Per-track caching (all 4 new tabs cache results alongside existing lyrics/genius caches)

### Backend
New Groq integration methods on `Playlists` class:
- `get_ai_explain()` / `get_ai_story()` / `get_ai_vocabulary()` / `get_ai_translation()`
- New routes: `/api/playlists/<id>/tracks/<id>/explain`, `/story`, `/vocabulary`, `/translate?lang=`
- Each shares a common `groq_complete()` helper (Llama 3.1 8B instant model, tuned prompts, timeout=25s)
- Error handling: surfaces real reasons (network, API key missing, etc.) instead of silent failures

**AI Key:** Uses the existing `groq_api_key` from Playlist settings. If missing, the UI shows a hint linking to console.groq.com with no-card-needed note.

---

## 📱 **GLASS DROPDOWN COMPONENT** (NEW)

### NOMAD SELECT
Custom replacement for native `<select>` across the entire app with:
- **Glass morphism design**: backdrop blur (22px), saturated, layered shadows
- **Spring animations**: backdrop opens with scale + transform spring easing
- **Progressive enhancement**: original `<select>` stays in DOM (hidden but value-bearing), so existing `addEventListener("change", ...)` code works untouched
- **Auto-detection**: MutationObserver picks up any new `<select>` elements added to the DOM later (modals, dynamic rows, etc.)

### Features
- Hover states (text lift, border glow)
- Keyboard-ready (click to open, clickable options)
- Checkmark on selected item
- Disabled state support
- Custom property interceptor so both native `.value = x` assignments AND menu clicks keep the label in sync

Applied to: media quality, AI upscale scale, skip preset, blend order, playlist sort, discover add modal.

---

## ✨ **STAGGERED ENTRANCE ANIMATIONS** (NEW)

### `staggerIn()` Helper
Card grids and strips now animate in as a sequence instead of all popping in at once:
- Applied to: Discover trending/gems/recent/charts/artists strips
- 40ms stagger between each item (capped at 480ms total)
- Uses `staggerRise` keyframe (translateY + scale rise from bottom)

Makes the Home dashboard and Discover page feel alive and intentional.

---

## 🎵 **APPLE CHARTS FIX** (CRITICAL)

### Endpoint Migration
The old `https://itunes.apple.com/us/rss/topsongs/limit=15/json` endpoint Apple maintained has been retired and now returns empty/garbage data. Nomad was silently showing nothing.

**New endpoint:** `https://rss.marketingtools.apple.com/api/v2/us/music/most-played/25/songs.json`
- Same data, new host/schema (Apple's current, actively maintained RSS feed)
- Includes: title, artist, artwork (100px), URL, genres

### Error Handling
Charts now return a tuple `(tracks, error)` instead of silent empty lists:
- Backend surfaces real reasons: network down, feed unreachable, no entries
- Frontend shows the error message + a "Retry" link on the Discover page
- Dashboard (home-charts) shows the error in a readable way

**Endpoints:**
- `GET /api/charts/apple` — returns `{ok, tracks, error}`
- `GET /api/discover` — includes `charts` and `charts_error` fields

---

## 🎨 **UI POLISH & MICRO-INTERACTIONS** (THROUGHOUT)

### Focus Rings
Updated all `:focus-visible` states to use the new `--c-blue` palette var (rgba-aware so it stays consistent with dynamic accent recoloring)

### Player Bar
- Thumbnail now has accent-color glow shadow
- Seek bar transitions animated (background color follows dynamic accent)
- Play button has hover lift + spring scale
- Volume slider `accent-color` synced to dynamic accent

### Lyrics Panel
- Active lyric line includes `text-shadow` glow (uses dynamic accent)
- Tab active state fires spring easing lift animation
- All AI text boxes use consistent `lyrics-ai-text` styling

### Progress Bars
- Tunnel/Media/Storage fill gradients use their tab colors (no change)
- New `.complete` glow animation fires at 100%
- Optional `.shimmer` class for passive animations during long operations

---

## 🖱️ **DASHBOARD CARD MICRO-INTERACTIONS** (NEW, this pass)

- `.dash-card` now has a real hover lift (`translateY(-4px)`, spring easing) instead of just a border/shadow change.
- Added a cursor-following **spotlight** effect (soft radial light tracking the pointer) on dashboard cards — Linear/Arc-style — via a single delegated `pointermove` listener + `--mx`/`--my` CSS vars, no per-card listeners.
- Home dashboard's `.dash-grid` cards now animate in with `staggerIn()` on load, matching the Discover page.

---



✅ Python backend: `ast.parse()` clean  
✅ JavaScript: `node --check` clean  
✅ No duplicate function definitions  
✅ All new routes registered (5 new lyrics AI endpoints, 1 thumb proxy)  
✅ CSS variables properly balanced (braces, quotes)  
✅ No stale references to removed old code  
✅ MutationObserver for dynamic select enhancement  
✅ Progressive enhancement pattern verified

---

## ⚠️ **WHAT STILL NEEDS TESTING (On Your Machine)**

1. **Groq API integration** — You'll need a free Groq API key (console.groq.com, add to Playlist settings). Test with a real track to ensure:
   - Explanation loads and is coherent
   - Translation picks right language and re-syncs to timing
   - Vocabulary extraction works on various genres
   - Story generation doesn't timeout (25s limit)

2. **Apple Charts endpoint** — Test that `/api/charts/apple` fetches real data and renders on Discover page. If network is down, verify the error message displays and "Retry" works.

3. **Dynamic accent color extraction** — Test with several album artworks:
   - Mostly-dark cover with one bright accent → should extract that accent
   - All-black or all-white cover → should fall back to pink
   - Missing/404 artwork → should fall back silently
   - Verify player bar / seek / lyrics panel all recolor together

4. **NOMAD SELECT dropdown** — Test on desktop/mobile:
   - Click to open/close
   - Select an option → value updates + label reflects change
   - Repopulating options via `.innerHTML` (discover-add modal) → label stays in sync
   - Keyboard: should be clickable
   - Modal backdrop dismiss when clicking outside

5. **Stagger animations** — Open Discover tab, watch cards enter in sequence (not all at once)

6. **Full end-to-end**: Play a track with synced lyrics, switch to Translation tab, pick a language, see translation + synced timing, switch to AI Explanation, verify load + read time.

---

## 📁 **File Structure (Unchanged)**

```
nomad_web.py         ← Flask backend (228 KB)
templates/
  index.html         ← Single-file frontend (383 KB)
```

Drop the new files in the same location as before. No new dependencies — all backend uses standard library + existing Groq calls (you already have that wired up).

---

## 🚀 **Next Steps**

1. Swap the files locally
2. Restart Flask
3. Test each feature above
4. If any of the Groq/Apple/dynamic color features don't work, check browser console for errors + network tab for API response codes
5. Tweak Groq prompts (in `ai_explain_song()`, etc.) if the AI generation feels too verbose or off-tone

---

**Questions?** All new code is pretty well-commented. Main entry points:
- `applyAccentFromArtwork()` — color extraction + dynamic vars
- `enhanceSelect()` + `enhanceAllSelects()` — dropdown enhancement
- `loadDiscover()` — chart rendering with error handling
- Lyrics tabs logic around `switchLyricsTab()` and the 6 load functions

Good luck! 🎵
