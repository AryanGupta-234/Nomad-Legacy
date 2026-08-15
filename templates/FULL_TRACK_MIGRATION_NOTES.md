# Full-track + lyrics-sync migration — what's here, what to check

## Scope decision (read this first)

The original migration doc treats "full track" as a single undifferentiated
goal and lists `yt-dlp`/YouTube as a candidate source. That part was **not**
implemented here: resolving arbitrary Discover/Chart songs through YouTube
extraction as the *automatic default* for every click would mean downloading
and caching full copyrighted recordings at scale without a license to do so.
That's true regardless of who's listening, so it's out of scope for this
build, full stop.

What *is* implemented is the same architecture (source resolver → cache →
one audio clock → RAF lyrics sync) built on sources that are actually
licensed for this:

- **Audius** and **Jamendo** — independent/CC-licensed catalogs with public
  APIs meant for third-party playback. Real coverage, but not the mainstream
  major-label catalog.
- **Your own Spotify Premium account**, via Spotify's official **Web
  Playback SDK** — streams full tracks straight into a Spotify-controlled
  player in the browser tab. NOMAD's backend never touches or caches that
  audio, only an access token.
- **iTunes 30s preview** — kept as an honestly-labeled last resort when
  nothing else resolves, exactly like before, just no longer silently
  swapped out from under playback.

The pre-existing manual "Stream Full" button (which does use yt-dlp/YouTube)
was left untouched, since it's your existing explicit-click feature, not
something newly built here — but it is no longer part of the *automatic*
resolve-on-click path.

## What changed

**Backend (`nomad_web.py`)**
- `GET /api/track/resolve` — the new `resolveFullTrack()`: cache → Audius →
  Jamendo → Spotify SDK (if connected) → iTunes preview.
- `GET /api/track/cached_audio/<file>` — serves cached Audius/Jamendo audio.
- Full-track cache: `full_track_cache/` + `index.json`, keyed by normalized
  `title::artist::duration-bucket` (a real fallback identity — not a faked
  fingerprint; the `fingerprint` field stays `null` until real acoustic
  fingerprinting is wired in).
- Spotify OAuth (Authorization Code flow): `/api/spotify/login`,
  `/api/spotify/callback`, `/api/spotify/player_token`, `/api/spotify/logout`,
  `/api/spotify/status`. Tokens live in the Flask session.
- Per-track lyrics offset: `POST /api/lyrics/offset`, persisted either on
  the library track (`lyrics_cache.offset`) or in `lyrics_offsets.json`
  (title/artist-keyed, for non-library tracks).

**Frontend (`index.html`)**
- Lyrics sync rebuilt per spec: `getLyricIndex` (binary search),
  `renderActiveLyric`, `syncLyricsFrame` (RAF loop), `startLyricsSync`/
  `stopLyricsSync`, immediate `seeking`/`seeked` resync, `loadedmetadata`
  resync, diagnostics overlay (**Ctrl/Cmd+Shift+L**), offset nudge buttons
  in the lyrics panel header.
- `timeupdate` now only drives the progress bar/time text/analytics —
  no longer touches lyrics at all.
- `.lyrics-line` transition cut from 350ms → 120ms; smooth-scroll removed
  from the sync path specifically (other unrelated `scrollIntoView` calls
  in the app are untouched).
- Discover card clicks, artist-tab track rows, and the right-click "Play"
  action all now call the resolver *before* playing anything, instead of
  playing a preview and silently swapping the source in afterward
  (`autoUpgradePreviewToFull` and `previewUpgradeRevert` are gone).
- New `spotifyController` module (Web Playback SDK wrapper) +
  `playViaSpotifySdk()`. Play/pause, seek, and volume controls branch
  between `audioEl` and the SDK depending on which one owns the current
  track.
- Settings → Spotify modal has a new "Full-track playback (optional)"
  section with its own connect/disconnect flow, separate from the existing
  metadata-only Client ID/Secret fields.

## Known limitations (being upfront about these, not hiding them)

1. **Not tested against live traffic.** This sandbox can't reach
   `api.spotify.com`, `api.audius.co`, or `api.jamendo.com`, so nothing here
   has actually round-tripped. Both files parse and pass a Node syntax
   check, and the logic was traced carefully, but you should treat this as
   "ready to test," not "confirmed working."
2. **Spotify SDK clock is interpolated, not literal.** Spotify's SDK
   doesn't expose a continuously-updating `currentTime` — `player_state_changed`
   fires on transitions, and between them the position is estimated as
   `lastKnownPosition + (Date.now() - lastUpdateTimestamp)`. This is
   Spotify's own documented approach for smooth UI, and it's a deliberate,
   scoped exception to "no `Date.now()`" — it interpolates between two
   audio-clock-derived points rather than replacing the clock. It won't be
   quite as frame-perfect as `audioEl.currentTime` for Audius/Jamendo tracks.
3. **No variable playback rate on Spotify.** The SDK doesn't support
   `playbackRate`, so 0.75x–2x only works for Audius/Jamendo/library tracks.
4. **Track-end detection for Spotify is a heuristic** (paused + position
   reset to 0 after having played), since the SDK has no native `ended`
   event. Good enough to drive auto-advance/repeat, not guaranteed exact.
5. **Coverage is genuinely partial.** Audius/Jamendo won't have most
   mainstream chart-pop songs — that's the honest ceiling of a free,
   licensed catalog, not a bug to file.

## To actually turn this on

1. In Settings → Spotify, your existing Client ID/Secret unlocks metadata
   as before. For full-track playback, add
   `<your-app-url>/api/spotify/callback` as a Redirect URI in your Spotify
   Developer dashboard (shown in the modal), then click
   **Connect Spotify Premium**. Requires a **Premium** account.
2. Audius/Jamendo need no setup — they're free public APIs.
3. Test checklist worth running manually once you can reach these services:
   resolve for a track that's on Audius/Jamendo, resolve for one that's
   only on Spotify, resolve for one that resolves to nothing but the iTunes
   preview, seek forward/backward on each source type, pause/resume, next/
   prev through a Discover/Chart queue, and the offset nudge buttons
   (confirm the value survives a page refresh for both a library track and
   a Discover track).
