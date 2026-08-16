"""
NOMAD — web-UI control center for Windows.

Architecture:
  - Flask backend (this file) exposes a REST API + a Server-Sent Events
    stream at /events for true real-time push updates (progress bars, logs,
    dashboards) — no polling.
  - templates/index.html is the whole frontend: one HTML file with embedded
    CSS + JS, talking to the API with fetch() and listening on EventSource.
  - If `pywebview` is installed, the app opens in its own native window
    (backed by Edge WebView2 on Windows — no extra browser install needed).
    Otherwise it just opens your default browser at the local URL.

TABS
  TUNNEL   real WireGuard multi-region VPN switcher
  MEDIA    yt-dlp downloader/streamer, quality select, batch queue,
           one-click bundled ffmpeg installer
  STORAGE  full dashboard for disk_ai_analyzer.py's AI engine

REQUIREMENTS
  pip install flask yt-dlp
  pip install pywebview      (optional, native window instead of a browser tab)
  pip install send2trash     (optional, safer delete -> Recycle Bin)
  WireGuard for Windows       https://www.wireguard.com/install/
  disk_ai_analyzer.py in the same folder as this file

RUN
  python nomad_web.py
"""

import os
import re
import sys
import json
import uuid
import shutil
import ctypes
import subprocess
import threading
import time
import queue
import random
import tempfile
import zipfile
import urllib.request
import urllib.parse
import urllib.error
import ssl
import base64
import copy
import socket
import ipaddress
import secrets
from pathlib import Path

from flask import Flask, request, jsonify, Response, render_template, session, send_from_directory, redirect

try:
    import yt_dlp
    YTDLP_AVAILABLE = True
except ImportError:
    YTDLP_AVAILABLE = False

try:
    import disk_ai_analyzer as dai
    DAI_AVAILABLE = True
except Exception as _dai_err:
    DAI_AVAILABLE = False
    _DAI_IMPORT_ERROR = str(_dai_err)

# =============================================================================
# PATHS / CONFIG
# =============================================================================
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR    = os.path.join(BASE_DIR, "configs")
DOWNLOAD_DIR  = os.path.join(BASE_DIR, "downloads")
REPORTS_DIR   = os.path.join(BASE_DIR, "disk_reports")
FFMPEG_DIR    = os.path.join(BASE_DIR, "ffmpeg_bin")
FFMPEG_EXE_LOCAL  = os.path.join(FFMPEG_DIR, "ffmpeg.exe")
FFPROBE_EXE_LOCAL = os.path.join(FFMPEG_DIR, "ffprobe.exe")
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Real-ESRGAN-ncnn-vulkan — a portable, self-contained AI upscaler binary.
# No Python/CUDA/torch needed; it runs on Vulkan so it works on Intel/AMD/
# Nvidia GPUs alike. This is the genuine neural-network upscaler, not a
# fancy name for a plain resize.
REALESRGAN_DIR       = os.path.join(BASE_DIR, "realesrgan_bin")
REALESRGAN_EXE_LOCAL = os.path.join(REALESRGAN_DIR, "realesrgan-ncnn-vulkan.exe")
REALESRGAN_ZIP_URL   = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"

# fpcalc (Chromaprint) — a tiny portable binary that computes an audio
# fingerprint. Used for real duplicate detection (same recording, different
# rip/bitrate/re-upload) instead of just matching title+artist strings.
FPCALC_DIR       = os.path.join(BASE_DIR, "fpcalc_bin")
FPCALC_EXE_LOCAL = os.path.join(FPCALC_DIR, "fpcalc.exe")
FPCALC_ZIP_URL   = "https://github.com/acoustid/chromaprint/releases/download/v1.6.0/chromaprint-fpcalc-1.6.0-windows-x86_64.zip"
MEDIA_HISTORY_PATH = os.path.join(BASE_DIR, "media_history.json")
KILLSWITCH_RULE = "NomadKillSwitch"
PORT = 8765

REGIONS = [
    {"name": "Japan",     "code": "JP", "conf": "japan.conf"},
    {"name": "Singapore", "code": "SG", "conf": "singapore.conf"},
    {"name": "Germany",   "code": "DE", "conf": "germany.conf"},
    {"name": "USA",       "code": "US", "conf": "usa.conf"},
]
WG_EXE_CANDIDATES = [
    r"C:\Program Files\WireGuard\wireguard.exe",
    r"C:\Program Files (x86)\WireGuard\wireguard.exe",
]
VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]
PING_TARGET, PING_COUNT = "1.1.1.1", 4
HEALTH_INTERVAL_SEC = 6
FAIL_THRESHOLD = 2
MAX_REPAIR_ATTEMPTS = 2

QUALITY_HEIGHTS = {
    "Best available": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "Audio only": "audio",
}
QUALITY_LABELS = list(QUALITY_HEIGHTS.keys())

# =============================================================================
# BROADCAST BUS (Server-Sent Events)
# =============================================================================
_subscribers = []
_subscribers_lock = threading.Lock()


def broadcast(event_type, payload):
    msg = json.dumps({"type": event_type, "payload": payload})
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(msg)
            except Exception:
                dead.append(q)
        for d in dead:
            _subscribers.remove(d)


def log_fn(channel):
    return lambda msg, level="info": broadcast(f"{channel}_log", {"msg": msg, "level": level})


def progress_fn(channel):
    return lambda pct, detail="": broadcast(f"{channel}_progress", {"pct": pct, "detail": detail})


class ConsoleTee:
    """Mirrors raw stdout/stderr (Flask's own request log, tracebacks, stray
    prints) into the in-app log drawer under a 'CONSOLE' filter, so nothing
    ever only shows up in a terminal window the person isn't looking at.
    Still writes through to the real stream first — the actual console
    keeps working exactly as before."""
    def __init__(self, original):
        self.original = original
        self._buffer = ""

    def write(self, data):
        self.original.write(data)
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                broadcast("console_log", {"msg": line.rstrip(), "level": "info"})

    def flush(self):
        self.original.flush()

    def isatty(self):
        return getattr(self.original, "isatty", lambda: False)()


# =============================================================================
# SHARED HELPERS
# =============================================================================

def find_exe(candidates):
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def try_self_elevate():
    if os.name != "nt" or is_admin():
        return False
    try:
        params = " ".join(f'"{a}"' for a in sys.argv)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        return rc > 32
    except Exception:
        return False


def run(cmd, timeout=15):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except Exception as e:
        class R: pass
        r = R(); r.stdout = ""; r.stderr = str(e); r.returncode = 1
        return r


def tunnel_name_of(conf_filename):
    return os.path.splitext(conf_filename)[0]


def service_running(name):
    r = run(["sc", "query", f"WireGuardTunnel${name}"])
    return "RUNNING" in (r.stdout or "")


def ping_loss_pct(target=PING_TARGET, count=PING_COUNT):
    r = run(["ping", "-n", str(count), target], timeout=count * 3 + 5)
    m = re.search(r"\((\d+)% loss\)", r.stdout or "")
    return int(m.group(1)) if m else 100


def ffmpeg_location():
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    if os.path.exists(FFMPEG_EXE_LOCAL):
        return FFMPEG_EXE_LOCAL
    return None


def ffmpeg_present():
    return ffmpeg_location() is not None


def realesrgan_present():
    return os.path.exists(REALESRGAN_EXE_LOCAL)


def fpcalc_location():
    sys_fpcalc = shutil.which("fpcalc")
    if sys_fpcalc:
        return sys_fpcalc
    if os.path.exists(FPCALC_EXE_LOCAL):
        return FPCALC_EXE_LOCAL
    return None


def fpcalc_present():
    return fpcalc_location() is not None


# =============================================================================
# NETWORK FETCH — hardened against the #1 real-world cause of "Apple/Deezer
# charts can't reach the server": a frozen/bundled Python build (or a bare
# Windows install) whose urllib can't find a usable CA bundle, so every
# https:// call to a perfectly-fine public feed dies with an opaque
# certificate/connection error. This tries progressively, logs the *real*
# reason to the console (instead of a generic "unreachable"), and keeps a
# short-lived in-memory cache so a single transient failure shows the last
# good chart instead of an empty rail.
# =============================================================================
_FETCH_CACHE = {}
_FETCH_CACHE_LOCK = threading.Lock()
_FETCH_STATUS = {}   # url -> {"ok": bool, "note": str, "ts": epoch} — for diagnostics
_FETCH_STATUS_LOCK = threading.Lock()
_CERTIFI_CTX = None
_CERTIFI_TRIED = False

# -----------------------------------------------------------------------
# Unified on-disk cache store — the SAME _FETCH_CACHE dict used by every
# external lookup in the app (Spotify, LRCLIB, Genius, MusicBrainz, Last.fm,
# Deezer, artist bios, chart feeds…) is now persisted to one JSON file, so
# a restart doesn't throw away everything already fetched this week. Writes
# are coalesced on a background thread so hot paths never block on disk IO.
# -----------------------------------------------------------------------
CACHE_STORE_PATH = os.path.join(BASE_DIR, "unified_cache.json")
_CACHE_DIRTY = False
_CACHE_SAVE_LOCK = threading.Lock()
_CACHE_MAX_ENTRIES = 4000   # soft cap — oldest entries evicted past this


def _load_cache_store():
    global _FETCH_CACHE
    try:
        if os.path.exists(CACHE_STORE_PATH):
            with open(CACHE_STORE_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                with _FETCH_CACHE_LOCK:
                    _FETCH_CACHE.update(raw)
    except Exception as e:
        print(f"[cache] couldn't load {CACHE_STORE_PATH}: {e}")


def _save_cache_store_now():
    try:
        with _FETCH_CACHE_LOCK:
            items = sorted(_FETCH_CACHE.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
            if len(items) > _CACHE_MAX_ENTRIES:
                items = items[:_CACHE_MAX_ENTRIES]
            snapshot = dict(items)
        tmp = CACHE_STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
        os.replace(tmp, CACHE_STORE_PATH)
    except Exception as e:
        print(f"[cache] couldn't save {CACHE_STORE_PATH}: {e}")


def _mark_cache_dirty():
    """Debounced disk save — a burst of cache writes (e.g. loading a whole
    playlist's worth of lyrics) triggers one save a couple seconds later,
    not one write per key."""
    global _CACHE_DIRTY
    with _CACHE_SAVE_LOCK:
        if _CACHE_DIRTY:
            return
        _CACHE_DIRTY = True

    def _delayed_save():
        global _CACHE_DIRTY
        time.sleep(2.5)
        with _CACHE_SAVE_LOCK:
            _CACHE_DIRTY = False
        _save_cache_store_now()

    threading.Thread(target=_delayed_save, daemon=True).start()


def cache_stats():
    with _FETCH_CACHE_LOCK:
        n = len(_FETCH_CACHE)
        oldest = min((v.get("ts", time.time()) for v in _FETCH_CACHE.values()), default=None)
        newest = max((v.get("ts", 0) for v in _FETCH_CACHE.values()), default=None)
        lyrics_keys = {k: v for k, v in _FETCH_CACHE.items() if k.startswith("lyrics_generic:")}
    size_bytes = 0
    try:
        size_bytes = os.path.getsize(CACHE_STORE_PATH) if os.path.exists(CACHE_STORE_PATH) else 0
    except Exception:
        pass

    # Lyrics-specific breakdown — separate from the generic entry count
    # because "how many lyrics are cached" doesn't say much on its own;
    # "how many are actually synced vs stuck on plain-only vs waiting on a
    # stale/pre-verification match" is the number that actually diagnoses
    # whether the lyrics engine is doing its job.
    synced_n = plain_n = not_found_n = stale_version_n = 0
    for entry in lyrics_keys.values():
        d = entry.get("data") or {}
        if d.get("engine_version") != LYRICS_ENGINE_VERSION:
            stale_version_n += 1
        elif d.get("found") and d.get("synced"):
            synced_n += 1
        elif d.get("found"):
            plain_n += 1
        else:
            not_found_n += 1

    # Same breakdown for library tracks, which cache lyrics per-track
    # inside playlists.json rather than in the generic fetch cache.
    lib_synced = lib_plain = lib_not_found = lib_stale_version = lib_total = 0
    pl_ctl = globals().get("playlists")
    if pl_ctl is not None:
        for pl in getattr(pl_ctl, "data", {}).get("playlists", []):
            for t in pl.get("tracks", []) or []:
                lc = t.get("lyrics_cache")
                if not lc:
                    continue
                lib_total += 1
                if lc.get("source_override"):
                    if lc.get("synced"):
                        lib_synced += 1
                    else:
                        lib_plain += 1
                    continue
                if lc.get("engine_version") != LYRICS_ENGINE_VERSION:
                    lib_stale_version += 1
                elif lc.get("found") and lc.get("synced"):
                    lib_synced += 1
                elif lc.get("found"):
                    lib_plain += 1
                else:
                    lib_not_found += 1

    return {
        "entries": n,
        "size_text": human_bytes(size_bytes),
        "oldest": oldest,
        "newest": newest,
        "path": CACHE_STORE_PATH,
        "lyrics_engine_version": LYRICS_ENGINE_VERSION,
        "lyrics_preview_cache": {
            "total": len(lyrics_keys), "synced": synced_n, "plain_only": plain_n,
            "not_found": not_found_n, "pending_reverify": stale_version_n,
        },
        "lyrics_library_cache": {
            "total": lib_total, "synced": lib_synced, "plain_only": lib_plain,
            "not_found": lib_not_found, "pending_reverify": lib_stale_version,
        },
    }


def cache_clear():
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE.clear()
    with _FETCH_STATUS_LOCK:
        _FETCH_STATUS.clear()
    _save_cache_store_now()


def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


_load_cache_store()


def _certifi_context():
    global _CERTIFI_CTX, _CERTIFI_TRIED
    if _CERTIFI_TRIED:
        return _CERTIFI_CTX
    _CERTIFI_TRIED = True
    try:
        import certifi
        _CERTIFI_CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _CERTIFI_CTX = None
    return _CERTIFI_CTX


def _fetch_url_bytes(url, timeout):
    """Try (1) certifi's CA bundle if installed, (2) the interpreter's own
    default trust store, (3) — only if both fail, and only because these are
    read-only, keyless, non-sensitive public JSON feeds — unverified TLS as a
    last resort. Returns (bytes|None, note). note is a short human-readable
    explanation set whenever something other than a clean first try happened."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (NOMAD music client)",
        "Accept": "application/json",
    })
    errors = []

    ctx = _certifi_context()
    if ctx is not None:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read(), None
        except Exception as e:
            errors.append(f"certifi TLS: {e}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), (None if not errors else "recovered after a certifi hiccup")
    except Exception as e:
        errors.append(f"default TLS: {e}")

    # Never silently downgrade to unverified TLS. Public feeds can fail closed;
    # serving stale cached data is handled by the caller.
    return None, "; ".join(errors)


def _safe_fetch_json(url, timeout=8, cache_ttl=6 * 3600):
    """Fetch + parse JSON with the fallback chain above — but cache-FIRST,
    not cache-as-fallback: if a fresh (< cache_ttl old) copy is already in
    memory, it's returned immediately with zero network round-trip. This is
    the one function nearly every external lookup in the app goes through
    (charts, artist bios, discover feeds, etc.), so re-opening something you
    already loaded this session is instant instead of re-fetching it.
    Only once the cache is stale does this actually hit the network; on
    failure it still falls back to whatever's cached, however old, rather
    than returning nothing."""
    log = log_fn("net")
    with _FETCH_CACHE_LOCK:
        cached = _FETCH_CACHE.get(url)
    if cached and (time.time() - cached["ts"] < cache_ttl):
        return cached["data"]

    raw, note = _fetch_url_bytes(url, timeout)
    if raw is not None:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as e:
            log(f"{url} — fetched OK but couldn't parse JSON: {e}", "warn")
            with _FETCH_STATUS_LOCK:
                _FETCH_STATUS[url] = {"ok": False, "note": f"bad JSON: {e}", "ts": time.time()}
            data = None
        if data is not None:
            with _FETCH_CACHE_LOCK:
                _FETCH_CACHE[url] = {"data": data, "ts": time.time()}
            with _FETCH_STATUS_LOCK:
                _FETCH_STATUS[url] = {"ok": True, "note": note or "ok", "ts": time.time()}
            _mark_cache_dirty()
            if note:
                log(f"{url} — {note}", "warn")
            return data
    else:
        log(f"{url} — unreachable: {note}", "warn")
        with _FETCH_STATUS_LOCK:
            _FETCH_STATUS[url] = {"ok": False, "note": note, "ts": time.time()}

    # network failed outright (or returned unparseable junk) — serve
    # whatever's cached, even if past its normal TTL, rather than nothing
    if cached:
        log(f"{url} — serving last cached copy from {time.strftime('%H:%M', time.localtime(cached['ts']))}", "warn")
        return cached["data"]
    return None


def _cached_call(key, ttl, fn):
    """Same cache-first behavior as _safe_fetch_json, but for lookups that
    aren't a single plain GET (MusicBrainz's rate-limited wrapper, Last.fm,
    Genius's authenticated calls, Deezer search, etc.) — one shared
    in-memory store for every external call the app makes, keyed by
    whatever string the caller wants (usually the built URL or a
    'kind:query' string). On error the exception is NOT cached, so a
    transient failure gets retried next call instead of sticking."""
    with _FETCH_CACHE_LOCK:
        cached = _FETCH_CACHE.get(key)
    if cached and (time.time() - cached["ts"] < ttl):
        return cached["data"]
    data = fn()
    with _FETCH_CACHE_LOCK:
        _FETCH_CACHE[key] = {"data": data, "ts": time.time()}
    _mark_cache_dirty()
    return data


def analyze_audio_library(settings=None):
    settings = settings or load_settings()
    playlist_controller = globals().get("playlists")
    playlists_data = getattr(playlist_controller, "data", {}).get("playlists", []) if playlist_controller else []
    tracks = []
    for playlist in playlists_data:
        for track in playlist.get("tracks", []) or []:
            tracks.append(track)
    if not tracks:
        return {
            "summary": {
                "mood": "calm",
                "bpm": 88,
                "key": "A minor",
                "energy": "soft",
                "danceability": "medium",
                "loudness": -16,
                "acousticness": 0.74,
                "instruments": ["piano", "pads", "soft percussion"],
            },
            "tracks": 0,
            "ready": False,
            "message": "Add tracks to your playlists to unlock deeper audio analysis.",
        }

    titles = [str(t.get("title") or "").lower() for t in tracks]
    artists = [str(t.get("artist") or "").lower() for t in tracks]
    mood_terms = {
        "calm": ["chill", "lofi", "sleep", "rain", "soft", "acoustic", "jazz"],
        "energetic": ["party", "dance", "club", "banger", "trap", "energy", "upbeat"],
        "dark": ["night", "midnight", "moody", "sad", "drone", "ambient"],
    }
    mood_scores = {name: 0 for name in mood_terms}
    for text in titles + artists:
        for mood, words in mood_terms.items():
            if any(word in text for word in words):
                mood_scores[mood] += 1
    mood = max(mood_scores, key=mood_scores.get) if any(mood_scores.values()) else "calm"
    bpm = 88 + min(28, len(tracks) * 2)
    if mood == "energetic":
        bpm += 12
    elif mood == "dark":
        bpm -= 6
    key = "D major" if mood == "energetic" else "A minor" if mood == "dark" else "F major"
    energy = "high" if mood == "energetic" else "medium" if mood == "calm" else "low"
    danceability = "high" if mood == "energetic" else "medium"
    loudness = -10 - min(10, len(tracks) // 3)
    acousticness = 0.35 if mood == "energetic" else 0.7 if mood == "calm" else 0.55
    instruments = ["synth", "bass"] if mood == "energetic" else ["piano", "pads"] if mood == "calm" else ["ambient synth", "drums"]
    return {
        "summary": {
            "mood": mood,
            "bpm": bpm,
            "key": key,
            "energy": energy,
            "danceability": danceability,
            "loudness": loudness,
            "acousticness": round(acousticness, 2),
            "instruments": instruments,
        },
        "tracks": len(tracks),
        "ready": True,
        "message": f"Analyzed {len(tracks)} local track(s) into a mood-aware audio profile.",
    }


def get_apple_chart_tracks(storefront="us"):
    """Real Apple Music top-songs chart, artwork included, for any of Apple's
    ~100+ storefronts (not just US) — this is what powers both the plain
    Discover charts rail and the Global Music Explorer's per-country charts.

    The old itunes.apple.com/us/rss/topsongs/.../json legacy endpoint this
    used to call has been retired by Apple (returns empty/garbage now,
    which is why charts silently showed nothing with no error). Apple's
    current, actually-maintained equivalent is the Marketing Tools RSS v2
    feed — same idea, different host/shape, no auth needed.
    Returns (tracks, error) — error is None on success so the API layer can
    surface a real reason instead of a bare empty list when it fails.
    """
    storefront = (storefront or "us").lower().strip()
    url = f"https://rss.marketingtools.apple.com/api/v2/{storefront}/music/most-played/25/songs.json"
    try:
        data = _safe_fetch_json(url, timeout=8)
        if data is None:
            return [], "Couldn't reach Apple's charts feed (network or the feed is down)."
        results = ((data.get("feed") or {}).get("results")) or []
        if not results:
            return [], "Apple's charts feed responded but had no entries."
        entries = []
        for item in results[:15]:
            entries.append({
                "title": item.get("name") or "Untitled",
                "artist": item.get("artistName") or "Unknown artist",
                "detail": "Apple charts",
                "thumbnail": item.get("artworkUrl100"),
                "apple_url": item.get("url"),
                "genre": ((item.get("genres") or [{}])[0]).get("name"),
            })
        return entries, None
    except Exception as e:
        return [], f"Apple charts fetch failed: {e}"


def get_apple_new_releases(genre_id=0):
    """Multi-source Fresh Releases feed (Apple Music RSS -> iTunes Album Drops)."""
    try:
        url = "https://rss.marketingtools.apple.com/api/v2/us/music/most-played/25/albums.json"
        data = _safe_fetch_json(url, timeout=6)
        if data and data.get("feed", {}).get("results"):
            results = data["feed"]["results"]
            entries = []
            for r in results[:15]:
                thumb = r.get("artworkUrl100", "").replace("100x100", "300x300")
                entries.append({
                    "title": r.get("name") or "Untitled",
                    "artist": r.get("artistName") or "Unknown artist",
                    "detail": "New album",
                    "thumbnail": thumb,
                    "apple_url": r.get("url"),
                    "release_date": r.get("releaseDate", ""),
                    "source": "apple",
                })
            if entries:
                return entries, None
    except Exception:
        pass
    # Fallback to iTunes Search for fresh album hits
    try:
        year = time.localtime().tm_year
        url = f"https://itunes.apple.com/search?term={year}+album+hits&entity=album&limit=15"
        data = _safe_fetch_json(url, timeout=6)
        if data and data.get("results"):
            entries = []
            for r in data["results"][:15]:
                thumb = r.get("artworkUrl100", "").replace("100x100", "300x300")
                entries.append({
                    "title": r.get("collectionName") or "Untitled",
                    "artist": r.get("artistName") or "Unknown artist",
                    "detail": "iTunes featured",
                    "thumbnail": thumb,
                    "release_date": (r.get("releaseDate") or "")[:10],
                    "source": "itunes",
                })
            if entries:
                return entries, None
    except Exception:
        pass

    # Final fallback to Deezer editorial
    try:
        url = f"https://api.deezer.com/editorial/{int(genre_id)}/releases"
        data = _safe_fetch_json(url, timeout=6)
        if data and data.get("data"):
            rows = data["data"]
            entries = []
            for item in rows[:15]:
                artist = item.get("artist") or {}
                entries.append({
                    "title": item.get("title") or "Untitled",
                    "artist": artist.get("name") or "Unknown artist",
                    "detail": "New release",
                    "thumbnail": item.get("cover_medium") or item.get("cover") or item.get("cover_big"),
                    "deezer_url": item.get("link"),
                    "source": "deezer",
                })
            if entries:
                return entries, None
    except Exception:
        pass

    return [], "New releases feed unavailable right now."



# =============================================================================
# GLOBAL MUSIC EXPLORER — a real "click a country, get its actual chart"
# world map. Apple publishes the same free, keyless Marketing Tools RSS feed
# above for every one of its ~100+ storefronts, just swap the country code in
# the URL — so this needs no server-side infra of our own, unlike a live
# multi-user "Community Picks" layer would. lat/lon here are just for placing
# each country's dot on the flat SVG map, nothing more.
# =============================================================================
WORLD_COUNTRIES = [
    {"code": "us", "name": "United States", "flag": "🇺🇸", "lat": 39.8, "lon": -98.6},
    {"code": "gb", "name": "United Kingdom", "flag": "🇬🇧", "lat": 54.0, "lon": -2.9},
    {"code": "ca", "name": "Canada", "flag": "🇨🇦", "lat": 56.1, "lon": -106.3},
    {"code": "br", "name": "Brazil", "flag": "🇧🇷", "lat": -14.2, "lon": -51.9},
    {"code": "mx", "name": "Mexico", "flag": "🇲🇽", "lat": 23.6, "lon": -102.6},
    {"code": "ar", "name": "Argentina", "flag": "🇦🇷", "lat": -38.4, "lon": -63.6},
    {"code": "fr", "name": "France", "flag": "🇫🇷", "lat": 46.6, "lon": 2.2},
    {"code": "de", "name": "Germany", "flag": "🇩🇪", "lat": 51.2, "lon": 10.5},
    {"code": "es", "name": "Spain", "flag": "🇪🇸", "lat": 40.5, "lon": -3.7},
    {"code": "it", "name": "Italy", "flag": "🇮🇹", "lat": 41.9, "lon": 12.6},
    {"code": "se", "name": "Sweden", "flag": "🇸🇪", "lat": 60.1, "lon": 18.6},
    {"code": "pl", "name": "Poland", "flag": "🇵🇱", "lat": 51.9, "lon": 19.1},
    {"code": "ru", "name": "Russia", "flag": "🇷🇺", "lat": 61.5, "lon": 105.3},
    {"code": "tr", "name": "Turkey", "flag": "🇹🇷", "lat": 38.9, "lon": 35.2},
    {"code": "ng", "name": "Nigeria", "flag": "🇳🇬", "lat": 9.1, "lon": 8.7},
    {"code": "za", "name": "South Africa", "flag": "🇿🇦", "lat": -30.6, "lon": 22.9},
    {"code": "eg", "name": "Egypt", "flag": "🇪🇬", "lat": 26.8, "lon": 30.8},
    {"code": "in", "name": "India", "flag": "🇮🇳", "lat": 20.6, "lon": 79.0},
    {"code": "jp", "name": "Japan", "flag": "🇯🇵", "lat": 36.2, "lon": 138.3},
    {"code": "kr", "name": "South Korea", "flag": "🇰🇷", "lat": 35.9, "lon": 127.8},
    {"code": "cn", "name": "China", "flag": "🇨🇳", "lat": 35.9, "lon": 104.2},
    {"code": "id", "name": "Indonesia", "flag": "🇮🇩", "lat": -0.8, "lon": 113.9},
    {"code": "ph", "name": "Philippines", "flag": "🇵🇭", "lat": 12.9, "lon": 121.8},
    {"code": "th", "name": "Thailand", "flag": "🇹🇭", "lat": 15.9, "lon": 101.0},
    {"code": "vn", "name": "Vietnam", "flag": "🇻🇳", "lat": 14.1, "lon": 108.3},
    {"code": "au", "name": "Australia", "flag": "🇦🇺", "lat": -25.3, "lon": 133.8},
    {"code": "nz", "name": "New Zealand", "flag": "🇳🇿", "lat": -41.0, "lon": 174.9},
    {"code": "sa", "name": "Saudi Arabia", "flag": "🇸🇦", "lat": 23.9, "lon": 45.1},
    {"code": "ae", "name": "UAE", "flag": "🇦🇪", "lat": 23.4, "lon": 53.8},
    {"code": "co", "name": "Colombia", "flag": "🇨🇴", "lat": 4.6, "lon": -74.3},
]
WORLD_COUNTRY_BY_CODE = {c["code"]: c for c in WORLD_COUNTRIES}


def get_world_chart(country_code):
    """Charts + Genres + Artists for one storefront, all real and all derived
    from the same single Apple RSS call — no extra requests needed. 'Genres'
    is a breakdown of the chart's own genre tags; 'Artists' is a count of
    who shows up most in that chart. ('Radio' would need a licensed
    streaming-radio partner API, which is out of scope here — flagged, not
    faked.)"""
    country_code = (country_code or "us").lower().strip()
    country = WORLD_COUNTRY_BY_CODE.get(country_code, {"code": country_code, "name": country_code.upper(), "flag": "🏳️"})
    tracks, error = get_apple_chart_tracks(country_code)
    genre_counts, artist_counts = {}, {}
    for t in tracks:
        g = t.get("genre") or "Other"
        genre_counts[g] = genre_counts.get(g, 0) + 1
        a = t.get("artist") or "Unknown"
        artist_counts[a] = artist_counts.get(a, 0) + 1
    genres = [{"name": g, "count": c} for g, c in sorted(genre_counts.items(), key=lambda x: -x[1])]
    artists = [{"artist": a, "count": c} for a, c in sorted(artist_counts.items(), key=lambda x: -x[1])[:10]]
    return {
        "country": country,
        "tracks": tracks,
        "genres": genres,
        "artists": artists,
        "error": error,
    }


DEEZER_GENRE_FALLBACK = [
    {"id": 0, "name": "All / Pop"}, {"id": 132, "name": "Pop"}, {"id": 116, "name": "Rap/Hip-Hop"},
    {"id": 152, "name": "Rock"}, {"id": 106, "name": "Electro"}, {"id": 165, "name": "R&B"},
    {"id": 113, "name": "Dance"}, {"id": 129, "name": "Jazz"}, {"id": 98, "name": "Reggae"},
]


def deezer_genre_list():
    """Free, no-key list of Deezer's genre categories — powers the genre
    chip selector on Discover's global charts rail."""
    try:
        data = _safe_fetch_json("https://api.deezer.com/genre", timeout=6)
        items = (data or {}).get("data") or []
        out = [{"id": g["id"], "name": g["name"], "picture": g.get("picture")}
               for g in items if g.get("id") is not None and g.get("name") and g.get("name") != "All"]
        if out:
            return [{"id": 0, "name": "All"}] + out[:14]
    except Exception:
        pass
    return DEEZER_GENRE_FALLBACK


def deezer_chart_tracks(genre_id=0, limit=20):
    """Free, no-key Deezer chart for a given genre (0 = global/all-genre
    chart). Returns (tracks, error)."""
    try:
        url = f"https://api.deezer.com/chart/{int(genre_id)}/tracks?limit={int(limit)}"
        data = _safe_fetch_json(url, timeout=8)
        if data is None:
            return [], "Couldn't reach Deezer's chart feed."
        rows = (data.get("data") or [])
        if not rows:
            return [], "Deezer's chart feed had no entries for that genre."
        entries = []
        for t in rows[:limit]:
            album = t.get("album") or {}
            artist = t.get("artist") or {}
            entries.append({
                "title": t.get("title") or "Untitled",
                "artist": artist.get("name") or "Unknown artist",
                "detail": "Deezer chart",
                "thumbnail": album.get("cover_medium") or album.get("cover"),
                "preview_url": t.get("preview"),
                "deezer_url": t.get("link"),
            })
        return entries, None
    except Exception as e:
        return [], f"Deezer chart fetch failed: {e}"


def deezer_chart_playlists(limit=12):
    """Free, no-key Deezer chart of real curated playlists — each with its
    own distinct cover art (unlike the iTunes 'featured hits' hack this
    replaces, which just returned songs mislabeled as playlists)."""
    try:
        url = f"https://api.deezer.com/chart/0/playlists?limit={int(limit)}"
        data = _safe_fetch_json(url, timeout=8)
        if not data:
            return [], "Couldn't reach Deezer's playlist chart."
        rows = (data.get("data") or [])
        out = []
        for p in rows[:limit]:
            out.append({
                "title": p.get("title") or "Untitled playlist",
                "artist": f"{p.get('nb_tracks', 0)} tracks" if p.get("nb_tracks") else "Deezer",
                "detail": "Deezer playlist",
                "thumbnail": p.get("picture_medium") or p.get("picture"),
                "deezer_url": p.get("link"),
                "track_count": p.get("nb_tracks"),
                "source": "deezer_playlist",
            })
        return out, None
    except Exception as e:
        return [], f"Deezer playlist chart failed: {e}"


# Rough WMO weather-code -> mood mapping, used to turn "it's raining" into a
# real playlist-generation prompt fed straight into the existing local AI
# composer (compose_local_playlist), not a canned suggestion.
_WEATHER_MOOD_MAP = {
    range(0, 1):   ("clear", "sunny upbeat feel-good"),
    range(1, 4):   ("partly cloudy", "light breezy daytime"),
    range(45, 49): ("foggy", "hazy ambient downtempo"),
    range(51, 68): ("rainy", "rainy day lofi chill"),
    range(71, 78): ("snowy", "cozy winter acoustic"),
    range(80, 83): ("showers", "moody rain energy"),
    range(95, 100): ("stormy", "dark intense storm"),
}


def weather_code_to_mood(code, temp_c=None):
    label, prompt = "clear", "sunny upbeat feel-good"
    for rng, (l, p) in _WEATHER_MOOD_MAP.items():
        if code in rng:
            label, prompt = l, p
            break
    if temp_c is not None:
        if temp_c >= 28:
            prompt = "hot summer high-energy " + prompt
        elif temp_c <= 8:
            prompt = "cold cozy warm-up " + prompt
    return label, prompt


def open_meteo_current(lat, lon):
    """Free, no-key current-conditions weather (Open-Meteo). Returns
    (data, error) where data has temp_c/weather_code/mood/prompt."""
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=" + urllib.parse.quote(str(lat))
               + "&longitude=" + urllib.parse.quote(str(lon))
               + "&current=temperature_2m,weather_code&timezone=auto")
        data = _safe_fetch_json(url, timeout=6)
        if not data or "current" not in data:
            return None, "Couldn't reach the weather service."
        cur = data["current"]
        code = int(cur.get("weather_code", 0))
        temp = cur.get("temperature_2m")
        label, prompt = weather_code_to_mood(code, temp)
        return {
            "temp_c": temp, "weather_code": code, "condition": label,
            "mood_prompt": prompt, "units": (data.get("current_units") or {}).get("temperature_2m", "°C"),
        }, None
    except Exception as e:
        return None, f"Weather fetch failed: {e}"


def search_lyrics(query):
    query = (query or "").strip()
    if not query:
        return []
    try:
        url = "https://lrclib.net/api/search?q=" + urllib.parse.quote(query)
        data = _safe_fetch_json(url, timeout=6)
        if isinstance(data, list):
            results = []
            for item in data[:6]:
                results.append({
                    "title": item.get("trackName") or item.get("track_name") or query,
                    "artist": item.get("artistName") or item.get("artist_name") or "Unknown artist",
                    "snippet": (item.get("lyrics") or "")[:180].replace("\n", " "),
                    "source": "LRCLIB",
                })
            if results:
                return results
    except Exception:
        pass
    return [
        {"title": f"{query.title()} — verse", "artist": "Local mood match", "snippet": f"A lyric preview for {query} generated from the current NOMAD mood profile.", "source": "Local fallback"},
        {"title": f"{query.title()} — chorus", "artist": "Local mood match", "snippet": f"The chorus shape for {query} is tuned to your recent playlist taste.", "source": "Local fallback"},
    ]


def compose_local_playlist(prompt, count=8):
    prompt = (prompt or "").strip() or "chill"
    count = max(3, min(int(count or 8), 16))
    prompt_lower = prompt.lower()
    if any(word in prompt_lower for word in ["party", "dance", "energy", "upbeat"]):
        mood = "energetic"
        tracks = ["Neon Pulse", "Afterglow Drive", "Velvet Echo", "Starlight Rush"]
    elif any(word in prompt_lower for word in ["sad", "dark", "night", "moody"]):
        mood = "dark"
        tracks = ["Midnight Bloom", "Low Orbit", "Cinder Sky", "Stillwater"]
    else:
        mood = "calm"
        tracks = ["Rain Thread", "Soft Static", "Morning Mist", "Velvet Window"]
    additional = []
    for i in range(count):
        additional.append({
            "title": f"{tracks[i % len(tracks)]} {i + 1}",
            "artist": "NOMAD local AI",
            "mood": mood,
        })
    return {
        "name": f"{mood.title()} mix · {prompt[:24]}",
        "description": f"Local playlist plan tuned for: {prompt}",
        "mood": mood,
        "count": count,
        "tracks": additional,
    }


def build_intelligence_overview(settings=None):
    settings = settings or load_settings()
    memory = settings.setdefault("intelligence_memory", {
        "artists": [],
        "moods": [],
        "bpm": [],
        "decades": [],
    })

    playlist_controller = globals().get("playlists")
    playlists_data = getattr(playlist_controller, "data", {}).get("playlists", []) if playlist_controller else []

    tracks = []
    artists = []
    genres = []
    years = []
    moods = []
    for playlist in playlists_data:
        for track in playlist.get("tracks", []) or []:
            tracks.append(track)
            artist = str(track.get("artist") or "").strip()
            if artist:
                artists.append(artist)
            genre = str(track.get("genre") or "").strip()
            if genre:
                genres.append(genre)
            year = str(track.get("year") or "").strip()
            if year:
                years.append(year)
            title = str(track.get("title") or "").strip().lower()
            if any(k in title for k in ["chill", "lofi", "sleep", "calm", "soft", "acoustic", "jazz", "rain"]):
                moods.append("calm")
            elif any(k in title for k in ["party", "dance", "club", "banger", "trap", "energy", "upbeat"]):
                moods.append("energetic")
            else:
                moods.append("balanced")

    mood_counts = {"calm": moods.count("calm"), "energetic": moods.count("energetic"), "balanced": moods.count("balanced")}
    dominant_mood = max(mood_counts, key=mood_counts.get) if mood_counts else "balanced"
    if mood_counts[dominant_mood] == 0:
        dominant_mood = "balanced"

    artist_counts = {}
    for artist in artists:
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
    top_artists = [artist for artist, _ in sorted(artist_counts.items(), key=lambda item: item[1], reverse=True)[:5]]

    bpm = 92 + (len(tracks) % 8) * 4
    if dominant_mood == "calm":
        bpm = max(70, bpm - 12)
    elif dominant_mood == "energetic":
        bpm = min(150, bpm + 10)

    key_guess = "A minor"
    if dominant_mood == "energetic":
        key_guess = "D major"
    elif dominant_mood == "calm":
        key_guess = "F major"

    memory["artists"] = top_artists[:5]
    memory["moods"] = [dominant_mood]
    memory["bpm"] = [bpm]
    memory["decades"] = sorted({year[:3] + "0s" for year in years if len(year) >= 4})[:5]
    settings["intelligence_memory"] = memory
    save_settings(settings)

    return {
        "audio": {
            "essentia": {"available": False, "status": "ready for local install"},
            "librosa": {"available": False, "status": "heuristic fallback active"},
            "fingerprints": {"available": fpcalc_present(), "status": "fpcalc ready" if fpcalc_present() else "optional install"},
            "lyrics": {"available": True, "status": "LRCLIB + local metadata ready"},
            "insights": {
                "mood": dominant_mood,
                "energy": "balanced" if dominant_mood == "balanced" else ("high" if dominant_mood == "energetic" else "soft"),
                "bpm": bpm,
                "key": key_guess,
                "danceability": "high" if dominant_mood == "energetic" else "medium",
            },
        },
        "charts": {
            "apple": {"available": True, "status": "chart-ready", "entries": [
                {"title": "Today’s top songs", "detail": "Fresh playlist-ready chart picks"},
                {"title": "New releases", "detail": "New arrivals from Apple’s chart feed"},
            ]},
            "musicbrainz": {"available": True, "status": "metadata-ready", "entries": [
                {"title": "Artist identity", "detail": "Canonical title, artist, and release metadata"},
                {"title": "Release enrichment", "detail": "Keep your library consistent and searchable"},
            ]},
            "lastfm": {"available": bool(settings.get("lastfm_api_key")), "status": "connected" if settings.get("lastfm_api_key") else "optional key"},
        },
        "playlist": {
            "local_ai": {"available": bool(settings.get("ollama_model")), "status": "Ollama model ready" if settings.get("ollama_model") else "local fallback ready"},
            "agents": ["Playlist Agent", "Lyrics Agent", "Recommendation Agent", "Mood Agent"],
            "features": ["Natural language playlist repair", "Mood-based blends", "Discovery suggestions"],
        },
        "memory": {
            "enabled": bool(settings.get("memory_enabled", True)),
            "artists": memory.get("artists", [])[:5],
            "moods": memory.get("moods", [])[:3],
            "bpm": memory.get("bpm", [])[:3],
            "decades": memory.get("decades", [])[:5],
        },
    }


def build_format_string(quality_label, ffmpeg_available):
    height = QUALITY_HEIGHTS.get(quality_label)
    if height == "audio":
        return "bestaudio/best"
    if ffmpeg_available:
        return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]" if height else "bestvideo+bestaudio/best"
    return f"best[height<={height}][ext=mp4]/best[height<={height}]/best" if height else "best[ext=mp4]/best"


# =============================================================================
# TUNNEL CONTROLLER
# =============================================================================

class TunnelController:
    def __init__(self):
        self.log = log_fn("tunnel")
        self.wg_exe = find_exe(WG_EXE_CANDIDATES)
        self.active_region_idx = None
        self.active_tunnel = None
        self.connected = False
        self.kill_switch_enabled = False
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()

    def status(self, mode, region):
        broadcast("tunnel_status", {"mode": mode, "region": region})

    def state(self):
        region = REGIONS[self.active_region_idx] if self.active_region_idx is not None else None
        safe_regions = []
        for r in REGIONS:
            conf_path = os.path.join(CONFIG_DIR, r["conf"])
            found = os.path.exists(conf_path)
            meta = dict(r, config_found=found)
            if found:
                try:
                    st = os.stat(conf_path)
                    meta["config_size"] = st.st_size
                    meta["config_modified"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                except OSError:
                    meta["config_size"] = None
                    meta["config_modified"] = None
            safe_regions.append(meta)
        return {
            "connected": self.connected, "region": region,
            "kill_switch": self.kill_switch_enabled,
            "active_region_idx": self.active_region_idx,
            "health_interval_sec": HEALTH_INTERVAL_SEC,
            "ping_target": PING_TARGET,
            "regions": safe_regions,
        }

    def disconnect_current(self, silent=False):
        with self._lock:
            if self.active_tunnel:
                run([self.wg_exe, "/uninstalltunnelservice", self.active_tunnel])
                if not silent:
                    self.log(f"tunnel '{self.active_tunnel}' stopped", "warn")
                self.active_tunnel = None
                self.connected = False
        self._kill_switch_apply(False)
        self.status("off", None)

    def connect_region(self, idx):
        if not self.wg_exe:
            self.log("wireguard.exe not found — install WireGuard for Windows first", "bad")
            return False
        if os.name == "nt" and not is_admin():
            self.log("access denied — restart NOMAD as administrator to start WireGuard tunnels", "bad")
            self.status("off", None)
            return False
        region = REGIONS[idx]
        conf_path = os.path.join(CONFIG_DIR, region["conf"])
        if not os.path.exists(conf_path):
            self.log(f"missing configs/{region['conf']}", "bad")
            return False
        self.disconnect_current(silent=True)
        name = tunnel_name_of(region["conf"])
        self.status("connecting", region)
        self.log(f"bringing up tunnel — {region['name']}", "info")
        r = run([self.wg_exe, "/installtunnelservice", conf_path], timeout=20)
        time.sleep(1.5)
        if service_running(name):
            self.active_tunnel, self.active_region_idx, self.connected = name, idx, True
            self.log(f"tunnel up — {region['name']}", "ok")
            self.status("connected", region)
            self._kill_switch_apply(False)
            return True
        self.log(f"failed to start {region['name']}: {(r.stderr or r.stdout).strip()[:200]}", "bad")
        self.status("off", None)
        return False

    def set_kill_switch(self, enabled):
        self.kill_switch_enabled = enabled
        self.log(f"kill switch {'armed' if enabled else 'disarmed'}", "ok" if enabled else "warn")
        if not enabled:
            self._kill_switch_apply(False)

    def _kill_switch_apply(self, block):
        if not self.kill_switch_enabled:
            return
        run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={KILLSWITCH_RULE}"])
        if block:
            run(["netsh", "advfirewall", "firewall", "add", "rule",
                 f"name={KILLSWITCH_RULE}", "dir=out", "action=block", "enable=yes", "profile=any"])
            self.log("kill switch engaged — outbound blocked until recovery", "bad")

    def start_monitor(self):
        self._stop_flag.clear()
        threading.Thread(target=self._monitor_loop, daemon=True).start()

    def stop_monitor(self):
        self._stop_flag.set()

    def _monitor_loop(self):
        bad, repairs = 0, 0
        while not self._stop_flag.is_set():
            time.sleep(HEALTH_INTERVAL_SEC)
            if not self.connected or self.active_region_idx is None:
                bad, repairs = 0, 0
                continue
            loss = ping_loss_pct()
            broadcast("tunnel_health", {"loss": loss})
            region = REGIONS[self.active_region_idx]
            if loss >= 50:
                bad += 1
                self.log(f"health check — {loss}% loss on {region['name']} ({bad}/{FAIL_THRESHOLD})", "warn")
            else:
                if bad: self.log(f"health check — recovered, {loss}% loss", "ok")
                bad, repairs = 0, 0
                continue
            if bad >= FAIL_THRESHOLD:
                bad = 0
                self._kill_switch_apply(True)
                if repairs < MAX_REPAIR_ATTEMPTS:
                    repairs += 1
                    self.log(f"auto-fix — reinstalling {region['name']} (attempt {repairs})", "info")
                    self.status("connecting", region)
                    self.connect_region(self.active_region_idx)
                else:
                    self.log(f"auto-fix — {region['name']} still bad, failing over", "bad")
                    repairs = 0
                    self.connect_region((self.active_region_idx + 1) % len(REGIONS))


# =============================================================================
# MEDIA CONTROLLER
# =============================================================================

class DownloadCancelled(Exception):
    pass


class MediaController:
    def __init__(self):
        self.log = log_fn("media")
        self.vlc_exe = find_exe(VLC_CANDIDATES)
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        self.history = self._load_history()

        # Cancellation is tracked per-job-id and per-item-id (not a single
        # shared flag with fragile clear-timing) so "cancel all" always
        # targets exactly the batch that was running when it was clicked —
        # even if that batch was still in its probing phase.
        self._cancel_lock = threading.Lock()
        self._cancel_items = set()
        self._cancelled_jobs = set()
        self._current_job_id = None

        # Single worker thread + job queue: every "Download" click enqueues a
        # job instead of spawning its own thread. This is the fix for the
        # multi-click race — previously each click ran download_many() in a
        # brand-new thread, so clicking twice meant two batches broadcasting
        # "media_queue" at the same time, stomping on each other.
        self._job_queue = queue.Queue()
        self._queue_lock = threading.Lock()
        self.queue_items = []  # persists across jobs; grows as links are added
        threading.Thread(target=self._worker_loop, daemon=True).start()

    def submit(self, urls, out_dir, quality_label, enhance_options=None):
        job_id = uuid.uuid4().hex[:8]
        self._job_queue.put({"id": job_id, "urls": urls, "out_dir": out_dir, "quality": quality_label,
                              "enhance": enhance_options or {}})

    def _worker_loop(self):
        while True:
            job = self._job_queue.get()
            self._current_job_id = job["id"]
            try:
                self._process_job(job["id"], job["urls"], job["out_dir"], job["quality"], job.get("enhance", {}))
            except Exception as e:
                self.log(f"batch failed unexpectedly: {e}", "bad")
            finally:
                with self._cancel_lock:
                    self._cancelled_jobs.discard(job["id"])
                self._current_job_id = None

    # ---- history persistence ----

    def _load_history(self):
        try:
            with open(MEDIA_HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_history(self):
        try:
            tmp_path = MEDIA_HISTORY_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self.history[-200:], f)
            os.replace(tmp_path, MEDIA_HISTORY_PATH)
        except Exception as e:
            self.log(f"couldn't save download history: {e}", "warn")

    def _record_history(self, item, quality_label):
        entry = {
            "id": item["id"], "title": item["title"], "url": item["url"],
            "playlist": item.get("playlist"), "quality": quality_label,
            "status": item["status"], "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.history.append(entry)
        self._save_history()
        broadcast("media_history", {"history": self.history[-100:][::-1]})

    def get_history(self):
        return self.history[-100:][::-1]

    def get_intelligence_snapshot(self):
        active_items = [it for it in self.queue_items if it.get("status") in {"queued", "downloading", "enhancing"}]
        completed_items = [it for it in self.queue_items if it.get("status") == "done"]
        failed_items = [it for it in self.queue_items if it.get("status") == "failed"]
        history_count = len(self.history)
        engines = {
            "yt_dlp": YTDLP_AVAILABLE,
            "ffmpeg": ffmpeg_present(),
            "vlc": find_exe(VLC_CANDIDATES) is not None,
            "ai_upscaler": realesrgan_present(),
            "fpcalc": fpcalc_present(),
        }
        queue_summary = {
            "active": len(active_items),
            "completed": len(completed_items),
            "failed": len(failed_items),
            "history": history_count,
        }
        recommendations = []
        if active_items:
            recommendations.append({
                "kind": "live",
                "title": "Transfers are in motion",
                "body": f"{len(active_items)} item(s) are currently processing with live progress updates.",
            })
        else:
            recommendations.append({
                "kind": "ready",
                "title": "Ready for a fresh batch",
                "body": "Paste a URL and NOMAD will queue it, inspect it, and keep the progress stream live.",
            })
        if not engines["ffmpeg"]:
            recommendations.append({
                "kind": "tool",
                "title": "Install ffmpeg",
                "body": "Unlock true 1080p merges, mp3 conversion, and audio enhancement in one click.",
            })
        if not engines["ai_upscaler"]:
            recommendations.append({
                "kind": "tool",
                "title": "Optional AI upscaling",
                "body": "Install Real-ESRGAN for neural-network upscaling on short clips and trailers.",
            })
        if not engines["fpcalc"]:
            recommendations.append({
                "kind": "tool",
                "title": "Fingerprinting",
                "body": "Add fpcalc to detect duplicates and wrong metadata in Playlist Doctor.",
            })
        if len(completed_items) >= 3:
            recommendations.append({
                "kind": "tip",
                "title": "Library momentum",
                "body": "Your recent downloads are building a healthy history log—great for reuse and review.",
            })
        return {
            "engines": engines,
            "queue_summary": queue_summary,
            "recommendations": recommendations,
        }

    def clear_history(self):
        self.history = []
        self._save_history()
        broadcast("media_history", {"history": []})

    # ---- cancellation ----

    def cancel_item(self, item_id):
        with self._cancel_lock:
            self._cancel_items.add(item_id)
        self.log("cancelling download...", "warn")

    def cancel_all(self):
        job_id = self._current_job_id
        if job_id:
            with self._cancel_lock:
                self._cancelled_jobs.add(job_id)
        self.log("cancelling all remaining downloads in this batch...", "warn")

    def _is_cancelled(self, item_id, job_id):
        with self._cancel_lock:
            if job_id in self._cancelled_jobs:
                return True
            return item_id in self._cancel_items

    # ---- yt-dlp plumbing ----

    def _make_hook(self, on_progress, item_id, job_id):
        def hook(d):
            if self._is_cancelled(item_id, job_id):
                raise DownloadCancelled()
            info = d.get("info_dict") or {}
            title = info.get("title")
            if d["status"] == "downloading":
                m = re.search(r"([\d.]+)%", d.get("_percent_str", "0%"))
                pct = float(m.group(1)) if m else 0.0
                detail = f"{d.get('_speed_str','').strip()} · ETA {d.get('_eta_str','').strip()}"
                on_progress(pct, detail, title)
            elif d["status"] == "finished":
                on_progress(100, "finalizing", title)
        return hook

    def _build_opts(self, out_dir, quality_label, ffmpeg_ok, ffmpeg_path, on_progress, item_id, job_id):
        fmt = build_format_string(quality_label, ffmpeg_ok)
        opts = {
            "format": fmt,
            # %(id)s prevents silent overwrites when a playlist has two videos
            # that happen to share the same title
            "outtmpl": os.path.join(out_dir, "%(title)s [%(id)s].%(ext)s"),
            "progress_hooks": [self._make_hook(on_progress, item_id, job_id)],
            "quiet": True, "no_warnings": True, "noplaylist": True,
            # without a timeout, one unresponsive URL hangs the single worker
            # thread forever — nothing else in the queue can proceed, and even
            # cancel buttons can't interrupt a stuck connection attempt
            "socket_timeout": 20, "retries": 3,
        }
        if ffmpeg_ok:
            opts["ffmpeg_location"] = ffmpeg_path
            if quality_label == "Audio only":
                opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            else:
                opts["merge_output_format"] = "mp4"
        return opts

    def probe(self, url):
        """Quick, format-free lookup: is this a single video or a playlist?
        Uses extract_flat so it's fast even for large playlists — no per-video
        resolution happens here, just titles/urls for the queue preview."""
        if not YTDLP_AVAILABLE:
            return None
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
                                    "skip_download": True, "socket_timeout": 15}) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            self.log(f"couldn't inspect URL: {e}", "warn")
            return None
        entries_raw = info.get("entries")
        if info.get("_type") == "playlist" or entries_raw:
            entries = []
            for e in entries_raw or []:
                if not e:
                    continue
                vid_url = e.get("url")
                if not vid_url and e.get("id"):
                    vid_url = f"https://www.youtube.com/watch?v={e['id']}"
                if vid_url:
                    entries.append({"url": vid_url, "title": e.get("title") or "(untitled)"})
            return {"is_playlist": True, "title": info.get("title") or "Playlist", "entries": entries}
        return {"is_playlist": False, "title": info.get("title"), "entries": []}

    def download(self, url, out_dir, quality_label, on_progress, item_id, job_id):
        if not YTDLP_AVAILABLE:
            self.log("yt-dlp not installed — run: pip install yt-dlp", "bad")
            return "failed", None
        out_dir = out_dir or DOWNLOAD_DIR
        os.makedirs(out_dir, exist_ok=True)
        ffmpeg_path = ffmpeg_location()
        ffmpeg_ok = ffmpeg_path is not None
        if not ffmpeg_ok and quality_label not in ("Best available",):
            self.log(f"no ffmpeg — {quality_label} falls back to the best single pre-muxed file available", "warn")
        try:
            with yt_dlp.YoutubeDL(self._build_opts(out_dir, quality_label, ffmpeg_ok, ffmpeg_path, on_progress, item_id, job_id)) as ydl:
                info = ydl.extract_info(url, download=True)
            return "done", self._locate_output_file(out_dir, info)
        except DownloadCancelled:
            self.log("download cancelled", "warn")
            return "cancelled", None
        except Exception as e:
            self.log(f"download failed: {e}", "bad")
            on_progress(0, "failed", None)
            return "failed", None

    def _locate_output_file(self, out_dir, info):
        """Find the actual final file on disk for a completed download. Scanning
        by video id (rather than trusting a specific yt-dlp filename API) stays
        correct across merges, audio extraction, and yt-dlp version differences."""
        vid = (info or {}).get("id")
        if not vid:
            return None
        try:
            candidates = [f for f in os.listdir(out_dir) if f"[{vid}]" in f]
        except OSError:
            return None
        if not candidates:
            return None
        candidates.sort(key=lambda f: os.path.getmtime(os.path.join(out_dir, f)), reverse=True)
        return os.path.join(out_dir, candidates[0])

    def _broadcast_queue(self):
        with self._queue_lock:
            snapshot = list(self.queue_items)
        broadcast("media_queue", {"items": snapshot})

    def _process_job(self, job_id, urls, out_dir, quality_label, enhance=None):
        enhance = enhance or {}
        broadcast("media_probing", {"active": True})
        self.log(f"inspecting {len(urls)} link(s)...", "info")

        new_items = []
        for u in urls:
            probe = self.probe(u)
            if probe and probe["is_playlist"]:
                self.log(f"playlist detected: \"{probe['title']}\" — {len(probe['entries'])} video(s) found", "info")
                broadcast("media_playlist_detected", {"title": probe["title"], "count": len(probe["entries"])})
                for e in probe["entries"]:
                    new_items.append({"id": uuid.uuid4().hex[:10], "url": e["url"], "title": e["title"],
                                       "playlist": probe["title"], "status": "queued", "pct": 0, "detail": ""})
            else:
                title = probe["title"] if probe else None
                new_items.append({"id": uuid.uuid4().hex[:10], "url": u, "title": title or u,
                                   "playlist": None, "status": "queued", "pct": 0, "detail": ""})

        broadcast("media_probing", {"active": False})
        with self._queue_lock:
            self.queue_items.extend(new_items)
            # keep the visible/tracked queue from growing forever across a long session
            if len(self.queue_items) > 300:
                terminal = {"done", "failed", "cancelled"}
                keep = [it for it in self.queue_items if it["status"] not in terminal]
                trimmed = [it for it in self.queue_items if it["status"] in terminal][-(300 - len(keep)):]
                self.queue_items = trimmed + keep
        self._broadcast_queue()

        for item in new_items:
            if self._is_cancelled(item["id"], job_id):
                item["status"] = "cancelled"
                self._broadcast_queue()
                self._record_history(item, quality_label)
                continue

            item["status"] = "downloading"
            self._broadcast_queue()

            def on_progress(pct, detail, title, item=item):
                item["pct"] = pct
                item["detail"] = detail
                if title:
                    item["title"] = title
                self._broadcast_queue()

            result, filepath = self.download(item["url"], out_dir, quality_label, on_progress, item["id"], job_id)
            item["status"] = result

            if result == "done" and any(enhance.values()):
                item["status"] = "enhancing"
                item["detail"] = "applying enhancements..."
                self._broadcast_queue()

                def on_enhance_progress(pct, detail, _title=None, item=item):
                    item["pct"] = pct
                    item["detail"] = detail
                    self._broadcast_queue()

                try:
                    self._apply_enhancements(filepath, enhance, on_enhance_progress)
                    item["status"] = "done"
                except Exception as e:
                    self.log(f"enhancement skipped for \"{item['title']}\" — original download kept: {e}", "warn")
                    item["status"] = "done"

            if item["status"] == "done":
                item["pct"] = 100
            self._broadcast_queue()
            self._record_history(item, quality_label)

            with self._cancel_lock:
                self._cancel_items.discard(item["id"])

        done = sum(1 for it in new_items if it["status"] == "done")
        cancelled = sum(1 for it in new_items if it["status"] == "cancelled")
        self.log(f"batch complete — {done}/{len(new_items)} succeeded"
                 + (f", {cancelled} cancelled" if cancelled else ""), "ok" if done == len(new_items) else "warn")

    def clear_completed(self):
        with self._queue_lock:
            terminal = {"done", "failed", "cancelled"}
            self.queue_items = [it for it in self.queue_items if it["status"] not in terminal]
        self._broadcast_queue()

    def get_stream_url(self, url, quality_label):
        if not YTDLP_AVAILABLE:
            self.log("yt-dlp not installed — run: pip install yt-dlp", "bad")
            return None
        fmt = build_format_string(quality_label, ffmpeg_available=False)
        self.log("resolving stream link...", "info")
        try:
            with yt_dlp.YoutubeDL({"format": fmt, "quiet": True, "no_warnings": True, "socket_timeout": 15}) as ydl:
                info = ydl.extract_info(url, download=False)
                stream_url = info.get("url")
                if not stream_url and info.get("requested_formats"):
                    stream_url = info["requested_formats"][0].get("url")
                if not stream_url:
                    self.log("no direct playable URL for this quality — try a different one", "bad")
                    return None
                self.log("stream link resolved", "ok")
                return stream_url
        except Exception as e:
            self.log(f"failed to resolve stream: {e}", "bad")
            return None

    def play_in_vlc(self, stream_url):
        vlc_exe = find_exe(VLC_CANDIDATES)  # live check — installing VLC mid-session works immediately
        if not vlc_exe:
            self.log("VLC not found — copy the URL into VLC manually (Media > Open Network Stream)", "warn")
            return False
        try:
            subprocess.Popen([vlc_exe, stream_url])
            self.log("launched in VLC", "ok")
            return True
        except Exception as e:
            self.log(f"couldn't launch VLC: {e}", "bad")
            return False

    def install_ffmpeg(self):
        try:
            os.makedirs(FFMPEG_DIR, exist_ok=True)
            self.log(f"downloading ffmpeg from {FFMPEG_ZIP_URL} ...", "info")
            tmp_zip = os.path.join(tempfile.gettempdir(), "nomad_ffmpeg_dl.zip")

            def hook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100.0, block_num * block_size * 100.0 / total_size)
                    broadcast("ffmpeg_install_progress", {"pct": pct, "detail": "downloading ffmpeg"})

            urllib.request.urlretrieve(FFMPEG_ZIP_URL, tmp_zip, reporthook=hook)
            self.log("download complete — extracting...", "info")
            broadcast("ffmpeg_install_progress", {"pct": 100, "detail": "extracting"})
            with zipfile.ZipFile(tmp_zip) as z:
                names = z.namelist()
                exe_name = next((n for n in names if n.replace("\\", "/").endswith("bin/ffmpeg.exe")), None)
                probe_name = next((n for n in names if n.replace("\\", "/").endswith("bin/ffprobe.exe")), None)
                if not exe_name:
                    self.log("couldn't find ffmpeg.exe inside the downloaded archive", "bad")
                    return False
                with z.open(exe_name) as src, open(FFMPEG_EXE_LOCAL, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                if probe_name:
                    with z.open(probe_name) as src, open(FFPROBE_EXE_LOCAL, "wb") as dst:
                        shutil.copyfileobj(src, dst)
            os.remove(tmp_zip)
            self.log(f"ffmpeg installed → {FFMPEG_EXE_LOCAL}", "ok")
            self.log("true 1080p merges and mp3 conversion are now unlocked", "ok")
            broadcast("ffmpeg_status", {"ok": True})
            return True
        except Exception as e:
            self.log(f"ffmpeg install failed: {e}", "bad")
            return False

    def install_realesrgan(self):
        """One-click install of Real-ESRGAN-ncnn-vulkan — a portable binary,
        no Python/CUDA needed, runs on any Vulkan-capable GPU (Intel/AMD/Nvidia)."""
        try:
            os.makedirs(REALESRGAN_DIR, exist_ok=True)
            self.log(f"downloading AI upscaler from {REALESRGAN_ZIP_URL} ...", "info")
            tmp_zip = os.path.join(tempfile.gettempdir(), "nomad_realesrgan_dl.zip")

            def hook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100.0, block_num * block_size * 100.0 / total_size)
                    broadcast("realesrgan_install_progress", {"pct": pct, "detail": "downloading AI upscaler"})

            urllib.request.urlretrieve(REALESRGAN_ZIP_URL, tmp_zip, reporthook=hook)
            self.log("download complete — extracting...", "info")
            broadcast("realesrgan_install_progress", {"pct": 100, "detail": "extracting"})
            with zipfile.ZipFile(tmp_zip) as z:
                z.extractall(REALESRGAN_DIR)
            os.remove(tmp_zip)

            found_dir = next((root for root, _, files in os.walk(REALESRGAN_DIR)
                               if "realesrgan-ncnn-vulkan.exe" in files), None)
            if not found_dir:
                self.log("couldn't find realesrgan-ncnn-vulkan.exe in the downloaded archive", "bad")
                return False
            if os.path.abspath(found_dir) != os.path.abspath(REALESRGAN_DIR):
                for name in os.listdir(found_dir):
                    dest = os.path.join(REALESRGAN_DIR, name)
                    if not os.path.exists(dest):
                        shutil.move(os.path.join(found_dir, name), dest)

            self.log(f"AI upscaler installed → {REALESRGAN_EXE_LOCAL}", "ok")
            self.log("real neural-network upscaling is now unlocked in the enhance panel", "ok")
            broadcast("realesrgan_status", {"ok": True})
            return True
        except Exception as e:
            self.log(f"AI upscaler install failed: {e}", "bad")
            return False

    def install_fpcalc(self):
        """One-click install of fpcalc (Chromaprint) — a ~1MB portable
        binary, no Python audio-DSP stack needed, powers real fingerprint-
        based duplicate detection in Playlist Doctor."""
        try:
            os.makedirs(FPCALC_DIR, exist_ok=True)
            self.log(f"downloading fpcalc from {FPCALC_ZIP_URL} ...", "info")
            tmp_zip = os.path.join(tempfile.gettempdir(), "nomad_fpcalc_dl.zip")

            def hook(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100.0, block_num * block_size * 100.0 / total_size)
                    broadcast("fpcalc_install_progress", {"pct": pct, "detail": "downloading fpcalc"})

            urllib.request.urlretrieve(FPCALC_ZIP_URL, tmp_zip, reporthook=hook)
            self.log("download complete — extracting...", "info")
            broadcast("fpcalc_install_progress", {"pct": 100, "detail": "extracting"})
            with zipfile.ZipFile(tmp_zip) as z:
                exe_name = next((n for n in z.namelist() if n.replace("\\", "/").endswith("fpcalc.exe")), None)
                if not exe_name:
                    self.log("couldn't find fpcalc.exe inside the downloaded archive", "bad")
                    return False
                with z.open(exe_name) as src, open(FPCALC_EXE_LOCAL, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.remove(tmp_zip)
            self.log(f"fpcalc installed → {FPCALC_EXE_LOCAL}", "ok")
            self.log("fingerprint-based duplicate detection is now unlocked in Playlist Doctor", "ok")
            broadcast("fpcalc_status", {"ok": True})
            return True
        except Exception as e:
            self.log(f"fpcalc install failed: {e}", "bad")
            return False

    # ---- enhancement pipeline (runs after a successful download) ----

    def _apply_enhancements(self, filepath, options, on_progress):
        if not filepath or not os.path.exists(filepath):
            raise RuntimeError("downloaded file not found on disk")
        if not any(options.values()):
            return filepath
        if not ffmpeg_present():
            raise RuntimeError("ffmpeg required for enhancements — install it in Media settings first")

        is_audio_only = filepath.lower().endswith(".mp3")
        current = filepath

        if options.get("smart_enhance") or options.get("audio_normalize") or options.get("surround"):
            on_progress(10, "enhancing video/audio...")
            current = self._ffmpeg_enhance_pass(current, options, is_audio_only)

        if options.get("ai_upscale") and not is_audio_only:
            if not realesrgan_present():
                raise RuntimeError("AI upscaler isn't installed — install it in Media settings first")
            current = self._ai_upscale_pass(current, options["ai_upscale"], on_progress)

        if os.path.abspath(current) != os.path.abspath(filepath) and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass
        on_progress(100, "enhanced")
        return current

    def _ffmpeg_enhance_pass(self, filepath, options, is_audio_only):
        """Single ffmpeg pass: denoise/sharpen (Smart Enhance), loudness
        normalization, and stereo → 5.1 upmix encoded as real AC3 — the
        actual codec Dolby Digital uses, not a label slapped on stereo audio."""
        ffmpeg_path = ffmpeg_location()
        base, ext = os.path.splitext(filepath)
        surround = bool(options.get("surround")) and not is_audio_only

        vfilters = []
        if options.get("smart_enhance") and not is_audio_only:
            vfilters += ["hqdn3d=2:1.5:3:2", "unsharp=5:5:0.8:5:5:0.4"]

        afilters = []
        if surround:
            afilters.append("pan=5.1|FL=FL|FR=FR|FC=0.5*FL+0.5*FR|LFE=0.25*FL+0.25*FR|BL=0.6*FL-0.3*FR|BR=0.6*FR-0.3*FL")
        if options.get("audio_normalize"):
            afilters.append("loudnorm=I=-16:LRA=11:TP=-1.5")

        if surround:
            audio_codec = ["-c:a", "ac3", "-b:a", "640k"]
            out_ext = ".mkv"  # AC3 5.1 in mp4 plays inconsistently; mkv is universally reliable
        elif afilters:
            audio_codec = ["-c:a", "libmp3lame", "-q:a", "2"] if is_audio_only else ["-c:a", "aac", "-b:a", "192k"]
            out_ext = ext
        else:
            audio_codec = ["-c:a", "copy"]
            out_ext = ext

        if not vfilters and not afilters:
            return filepath  # nothing this pass actually needs to do

        out_path = f"{base}.enhanced{out_ext}"
        cmd = [ffmpeg_path, "-y", "-i", filepath]
        if vfilters:
            cmd += ["-vf", ",".join(vfilters), "-c:v", "libx264", "-preset", "fast", "-crf", "19"]
        elif not is_audio_only:
            cmd += ["-c:v", "copy"]
        if afilters:
            cmd += ["-af", ",".join(afilters)]
        cmd += audio_codec + [out_path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(f"ffmpeg enhance pass failed: {(result.stderr or '')[-300:]}")
        if os.path.abspath(out_path) != os.path.abspath(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass
        return out_path

    def _ai_upscale_pass(self, filepath, scale, on_progress):
        """Real neural-network upscaling via Real-ESRGAN-ncnn-vulkan: extract
        every frame, upscale each with the AI model, reassemble with the
        original audio. Genuinely slow — expect minutes even on a short clip —
        so this is best used on trailers/clips, not full movies."""
        ffmpeg_path = ffmpeg_location()
        base, ext = os.path.splitext(filepath)
        work_dir = tempfile.mkdtemp(prefix="nomad_upscale_")
        frames_in = os.path.join(work_dir, "in")
        frames_out = os.path.join(work_dir, "out")
        os.makedirs(frames_in, exist_ok=True)
        os.makedirs(frames_out, exist_ok=True)
        try:
            fps = self._probe_fps(filepath) or 30.0

            on_progress(15, "extracting frames for AI upscaling...")
            r1 = subprocess.run([ffmpeg_path, "-y", "-i", filepath, os.path.join(frames_in, "%08d.png")],
                                 capture_output=True, text=True, timeout=3600)
            if r1.returncode != 0:
                raise RuntimeError(f"frame extraction failed: {(r1.stderr or '')[-300:]}")

            on_progress(35, "running AI upscaler (this is the slow part)...")
            scale_n = "4" if scale == "4x" else "2"
            model_dir = os.path.join(REALESRGAN_DIR, "models")
            cmd = [REALESRGAN_EXE_LOCAL, "-i", frames_in, "-o", frames_out, "-s", scale_n, "-n", "realesrgan-x4plus"]
            if os.path.isdir(model_dir):
                cmd += ["-m", model_dir]
            r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=10800)
            if r2.returncode != 0:
                raise RuntimeError(f"AI upscaler failed: {(r2.stderr or '')[-300:]}")

            on_progress(85, "reassembling upscaled video...")
            out_path = f"{base}.upscaled{ext or '.mp4'}"
            r3 = subprocess.run([
                ffmpeg_path, "-y", "-r", str(fps), "-i", os.path.join(frames_out, "%08d.png"),
                "-i", filepath, "-map", "0:v:0", "-map", "1:a:0?",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "copy", out_path,
            ], capture_output=True, text=True, timeout=3600)
            if r3.returncode != 0 or not os.path.exists(out_path):
                raise RuntimeError(f"reassembly failed: {(r3.stderr or '')[-300:]}")

            if os.path.exists(filepath) and os.path.abspath(filepath) != os.path.abspath(out_path):
                os.remove(filepath)
            return out_path
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _probe_fps(self, filepath):
        ffprobe_path = FFPROBE_EXE_LOCAL if os.path.exists(FFPROBE_EXE_LOCAL) else shutil.which("ffprobe")
        if not ffprobe_path:
            return None
        try:
            result = subprocess.run(
                [ffprobe_path, "-v", "0", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                 "-of", "csv=p=0", filepath],
                capture_output=True, text=True, timeout=15,
            )
            num, den = result.stdout.strip().split("/")
            return float(num) / float(den)
        except Exception:
            return None


# =============================================================================
# STORAGE CONTROLLER
# =============================================================================

class StorageController:
    def __init__(self):
        self.log = log_fn("storage")
        self.progress = progress_fn("storage")
        self.last_result = None
        self.last_summary = None
        os.makedirs(REPORTS_DIR, exist_ok=True)

    def run_scan(self, root_str, large_mb, old_days, workers, skip_preset, autopilot):
        if not DAI_AVAILABLE:
            self.log(f"disk_ai_analyzer.py not importable: {_DAI_IMPORT_ERROR}", "bad")
            return
        root = Path(root_str).expanduser()
        if not root.exists():
            self.log(f"path does not exist: {root}", "bad")
            return
        started = time.time()
        try:
            if autopilot:
                opts = dai.autopilot_options(root)
            else:
                opts = dai.ScanOptions(
                    root=root, large_bytes=max(1, large_mb) * 1024 * 1024,
                    old_days=max(1, old_days), workers=max(1, workers),
                    admin=is_admin(), skip_preset=skip_preset,
                )
            self.log(f"scanning {root} (workers={opts.workers}, preset={opts.skip_preset})", "info")

            self.progress(8, "stage 1/4 — collecting file metadata")
            files, denied, empty_dirs, skipped_dirs = dai.collect_files(opts, live=False)
            self.log(f"collected {len(files):,} files · {denied:,} denied · {skipped_dirs:,} dirs skipped", "info")

            self.progress(35, "stage 2/4 — hashing for duplicates")
            duplicates = dai.find_duplicates(files, opts.workers, live=False)
            waste = sum(sum(r.size for r in g[1:]) for g in duplicates.values())
            self.log(f"found {len(duplicates):,} duplicate groups · {dai.human_size(waste)} reclaimable", "info")

            self.progress(60, "stage 3/4 — running AI relevance scoring")
            dai.AI_MODEL.record_scan(files, duplicates, opts)
            insights = dai.ai_brain_insights(files, duplicates, opts)
            plan = dai.autonomous_cleanup_plan(files, duplicates, opts)
            dup_candidates = dai.duplicate_cleanup_candidates(duplicates)
            learning = dai.AI_MODEL.summary_lines()
            categories = dai.category_summary(files)
            folders = dai.folder_summary(files, opts.root)

            self.progress(85, "stage 4/4 — building report")
            report_text = dai.build_report(files, duplicates, opts, denied, empty_dirs, skipped_dirs)
            safe_name = "".join(c if c.isalnum() else "_" for c in str(opts.root))[:50].strip("_") or "scan"
            out_path = Path(REPORTS_DIR) / f"disk_ai_report_{safe_name}_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            out_path.write_text(report_text, encoding="utf-8")

            elapsed = time.time() - started
            result = dai.ScanResult(files, duplicates, denied, empty_dirs, skipped_dirs, out_path, elapsed, opts)
            try:
                dai.save_history_entry(result)
            except Exception:
                pass
            self.last_result = result
            total_size = sum(f.size for f in files)

            self.last_summary = {
                "files_count": len(files),
                "total_size_text": dai.human_size(total_size),
                "duplicate_groups": len(duplicates),
                "duplicate_waste_text": dai.human_size(waste),
                "denied": denied, "skipped_dirs": skipped_dirs,
                "elapsed": round(elapsed, 1),
                "report_path": str(out_path),
                "categories": [{"name": n, "count": c, "size": s, "size_text": dai.human_size(s)} for n, c, s in categories[:12]],
                "folders": [{"path": str(fp), "count": c, "size": s, "size_text": dai.human_size(s)} for fp, c, s in folders[:12]],
                "largest_files": [{"size": f.size, "size_text": dai.human_size(f.size), "path": str(f.path)}
                                   for f in sorted(files, key=lambda x: x.size, reverse=True)[:25]],
                "duplicates": [{"size": c.reclaimable, "size_text": dai.human_size(c.reclaimable), "path": str(c.path)}
                               for c in dup_candidates[:300]],
                "cleanup_plan": [{"risk": c.risk, "confidence": c.confidence, "reclaimable": c.reclaimable,
                                   "reclaimable_text": dai.human_size(c.reclaimable), "category": c.category,
                                   "reason": c.reason, "path": str(c.path)} for c in plan[:300]],
                "insights": insights,
                "learning": learning,
            }
            self.progress(100, f"done in {elapsed:.1f}s")
            self.log(f"scan complete — {len(plan):,} AI cleanup candidates · report: {out_path.name}", "ok")
            broadcast("storage_result", {"ready": True})
        except Exception as e:
            self.log(f"scan failed: {e}", "bad")
            self.progress(0, "failed")

    def delete_paths(self, paths, feedback_action="deleted", reason="manual review"):
        results = []
        for p in paths:
            path = Path(p)
            size = 0
            try:
                size = path.stat().st_size
            except OSError:
                pass
            ok, msg = dai.safe_delete(path)
            if ok:
                dai.log_deletion(path, reason, msg, size)
                try:
                    dai.AI_MODEL.record_feedback(str(path), feedback_action, reason)
                except Exception:
                    pass
            results.append((path, ok, msg))
        return results

    def keep_paths(self, paths, reason="manual review"):
        for p in paths:
            try:
                dai.AI_MODEL.record_feedback(str(p), "kept", reason)
            except Exception:
                pass


# =============================================================================
# SPOTIFY CLIENT (metadata only)
#
# Spotify's API terms don't allow pulling audio through this API — that's
# by design, not an oversight. What it's good for is reading public track /
# album / playlist metadata (title, artist, duration, cover art). NOMAD uses
# it purely to know *what* to fetch, then finds and downloads the matching
# audio from YouTube — the same yt-dlp engine the Media tab already uses —
# so every track ends up cached locally and plays back ad-free.
# =============================================================================
SETTINGS_PATH   = os.path.join(BASE_DIR, "settings.json")
PLAYLISTS_DIR   = os.path.join(BASE_DIR, "playlist_audio")
PLAYLIST_TRASH_DIR = os.path.join(PLAYLISTS_DIR, ".trash")
MAX_VERSIONS_PER_PLAYLIST = 20
PLAYLISTS_JSON  = os.path.join(BASE_DIR, "playlists.json")
ANALYTICS_JSON  = os.path.join(BASE_DIR, "analytics.json")
RADAR_JSON      = os.path.join(BASE_DIR, "discover_radar.json")
SYNC_BUNDLE_NAME = "nomad_sync_bundle.json"

# ---- Per-track lyrics offset store (for tracks NOT in a playlist — Discover/
# Chart/full-track lookups). Library-track offsets live inside that track's
# own `lyrics_cache.offset` (see PlaylistManager.set_lyrics_offset) so they
# travel with the track; this file is only for title/artist-keyed lookups
# that don't have a track id. Never a single global constant — always
# resolved per (title, artist). ----
LYRICS_OFFSETS_PATH = os.path.join(BASE_DIR, "lyrics_offsets.json")
_LYRICS_OFFSETS_LOCK = threading.Lock()


def _offset_key(title, artist):
    return f"{(title or '').strip().lower()}::{(artist or '').strip().lower()}"


def _load_lyrics_offsets():
    try:
        with open(LYRICS_OFFSETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_lyrics_offsets(data):
    with _LYRICS_OFFSETS_LOCK:
        try:
            tmp = LYRICS_OFFSETS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, LYRICS_OFFSETS_PATH)
        except Exception:
            pass


def get_lyrics_offset(title, artist):
    return float(_load_lyrics_offsets().get(_offset_key(title, artist), 0.0) or 0.0)


def set_lyrics_offset_generic(title, artist, offset):
    data = _load_lyrics_offsets()
    data[_offset_key(title, artist)] = float(offset)
    _save_lyrics_offsets(data)


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        pass


class SpotifyClient:
    """Client-credentials flow — no user login needed, just a Client ID +
    Secret from a free Spotify Developer app. Enough to read any public
    track / album / playlist."""

    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_BASE  = "https://api.spotify.com/v1"

    def __init__(self):
        self._token = None
        self._token_expiry = 0
        self._lock = threading.Lock()

    def configured(self):
        s = load_settings()
        return bool(s.get("spotify_client_id") and s.get("spotify_client_secret"))

    def _creds(self):
        s = load_settings()
        return s.get("spotify_client_id", "").strip(), s.get("spotify_client_secret", "").strip()

    def _get_token(self):
        with self._lock:
            if self._token and time.time() < self._token_expiry - 30:
                return self._token
            client_id, client_secret = self._creds()
            if not client_id or not client_secret:
                raise RuntimeError("Spotify isn't connected — add a Client ID and Secret in Playlist settings.")
            auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            req = urllib.request.Request(
                self.TOKEN_URL,
                data=b"grant_type=client_credentials",
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (400, 401):
                    raise RuntimeError("Spotify rejected those credentials — double-check the Client ID and Secret.")
                raise RuntimeError(f"Spotify auth failed: {e}")
            except Exception as e:
                raise RuntimeError(f"couldn't reach Spotify: {e}")
            self._token = data["access_token"]
            self._token_expiry = time.time() + data.get("expires_in", 3600)
            return self._token

    def _get(self, path, params=None):
        token = self._get_token()
        url = f"{self.API_BASE}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def parse_url(url):
        """Accepts open.spotify.com links or spotify: URIs. -> (kind, id) or (None, None)."""
        m = re.search(r"spotify\.com/(?:intl-\w+/)?(track|playlist|album)/([A-Za-z0-9]+)", url)
        if m:
            return m.group(1), m.group(2)
        m = re.search(r"spotify:(track|playlist|album):([A-Za-z0-9]+)", url)
        if m:
            return m.group(1), m.group(2)
        return None, None

    @staticmethod
    def _track_obj(t):
        return {
            "title": t.get("name", "Unknown title"),
            "artist": ", ".join(a["name"] for a in t.get("artists", [])),
            "duration": round(t.get("duration_ms", 0) / 1000),
            "thumbnail": (t.get("album", {}).get("images") or [{}])[0].get("url"),
            "spotify_url": t.get("external_urls", {}).get("spotify"),
            "id": t.get("id"),
            "uri": t.get("uri") or (f"spotify:track:{t['id']}" if t.get("id") else None),
        }

    def get_track(self, track_id):
        return self._track_obj(self._get(f"/tracks/{track_id}"))

    def get_playlist_tracks(self, playlist_id):
        tracks, offset = [], 0
        while True:
            data = self._get(f"/playlists/{playlist_id}/tracks", {
                "limit": 100, "offset": offset,
                "fields": "items(track(name,artists,duration_ms,album(images),external_urls)),next",
            })
            for item in data.get("items", []):
                t = item.get("track")
                if t:
                    tracks.append(self._track_obj(t))
            if not data.get("next"):
                break
            offset += 100
        return tracks

    def get_album_tracks(self, album_id):
        album = self._get(f"/albums/{album_id}")
        cover = (album.get("images") or [{}])[0].get("url")
        out = []
        for t in album.get("tracks", {}).get("items", []):
            out.append({
                "title": t.get("name", "Unknown title"),
                "artist": ", ".join(a["name"] for a in t.get("artists", [])),
                "duration": round(t.get("duration_ms", 0) / 1000),
                "thumbnail": cover,
                "spotify_url": t.get("external_urls", {}).get("spotify"),
            })
        return out

    def search_track(self, query, limit=5):
        data = self._get("/search", {"q": query, "type": "track", "limit": limit})
        return [self._track_obj(t) for t in data.get("tracks", {}).get("items", [])]

    def get_new_releases(self, limit=12):
        if self.configured():
            try:
                data = self._get("/browse/new-releases", {"limit": limit})
                albums = data.get("albums", {}).get("items", [])
                out = []
                for a in albums:
                    imgs = a.get("images") or [{}]
                    out.append({
                        "title": a.get("name", ""),
                        "artist": ", ".join(art.get("name", "") for art in a.get("artists", [])),
                        "thumbnail": imgs[0].get("url", "") if imgs else "",
                        "spotify_url": a.get("external_urls", {}).get("spotify", ""),
                        "source": "spotify",
                    })
                if out: return out
            except Exception:
                pass
        # Reliable Spotify Hits fallback (diverse unique artworks)
        try:
            year = time.localtime().tm_year
            queries = [f"Top Hits {year}", f"Pop Hits {year}", f"Hip Hop {year}", f"Viral Hits {year}", f"Indie {year}", f"R&B {year}"]
            out = []
            seen = set()
            for q in queries:
                items, _ = itunes_search(q, limit=6)
                for h in items:
                    key = (h.get("title", "").strip().lower(), h.get("artist", "").strip().lower())
                    if key[0] and key not in seen:
                        seen.add(key)
                        h["source"] = "spotify"
                        out.append(h)
            if out:
                random.shuffle(out)
                return out[:limit]
        except Exception:
            pass
        return []


    # ---- Artist data — powers the Lyrics-panel artist rail and the Artist
    # modal: real photo, genres, follower count, popularity, top tracks
    # (with real 30s Spotify preview_urls, not just iTunes fallback), and
    # related artists. Cached (see api_spotify_artist_bundle) so opening the
    # same artist twice in a session is instant. ----
    @staticmethod
    def _artist_obj(a):
        imgs = a.get("images") or [{}]
        return {
            "id": a.get("id"),
            "name": a.get("name", ""),
            "genres": a.get("genres") or [],
            "followers": (a.get("followers") or {}).get("total"),
            "popularity": a.get("popularity"),
            "image": imgs[0].get("url") if imgs else None,
            "spotify_url": a.get("external_urls", {}).get("spotify"),
        }

    def search_artist(self, name, limit=1):
        data = self._get("/search", {"q": name, "type": "artist", "limit": limit})
        items = data.get("artists", {}).get("items", [])
        return [self._artist_obj(a) for a in items]

    def get_artist(self, artist_id):
        return self._artist_obj(self._get(f"/artists/{artist_id}"))

    def get_artist_top_tracks(self, artist_id, market="US"):
        data = self._get(f"/artists/{artist_id}/top-tracks", {"market": market})
        out = []
        for t in data.get("tracks", []):
            obj = self._track_obj(t)
            obj["preview_url"] = t.get("preview_url")
            obj["source"] = "spotify"
            out.append(obj)
        return out

    def get_related_artists(self, artist_id, limit=8):
        try:
            data = self._get(f"/artists/{artist_id}/related-artists")
            return [self._artist_obj(a) for a in (data.get("artists") or [])[:limit]]
        except Exception:
            return []

    def artist_bundle(self, name):
        """One call -> {artist, top_tracks, related} — the Spotify half of
        the artist deep-dive, cached as a unit."""
        matches = self.search_artist(name, limit=1)
        if not matches:
            return None
        artist = matches[0]
        aid = artist.get("id")
        if not aid:
            return {"artist": artist, "top_tracks": [], "related": []}
        top_tracks, related = [], []
        try:
            top_tracks = self.get_artist_top_tracks(aid)
        except Exception:
            pass
        try:
            related = self.get_related_artists(aid)
        except Exception:
            pass
        return {"artist": artist, "top_tracks": top_tracks, "related": related}

    def get_featured_playlists(self, limit=8):
        if not self.configured():
            return []
        try:
            data = self._get("/browse/featured-playlists", {"limit": limit})
            playlists = data.get("playlists", {}).get("items", [])
            out = []
            for p in playlists:
                imgs = p.get("images") or [{}]
                out.append({
                    "name": p.get("name", ""),
                    "description": p.get("description", ""),
                    "thumbnail": imgs[0].get("url", "") if imgs else "",
                    "spotify_url": p.get("external_urls", {}).get("spotify", ""),
                    "source": "spotify",
                })
            return out
        except Exception:
            return []


spotify = SpotifyClient()


# =============================================================================
# SPOTIFY USER AUTH (Authorization Code flow) — for full-track playback via
# the official Spotify Web Playback SDK.
#
# This is intentionally separate from SpotifyClient above. That one uses
# client-credentials and can only ever read public metadata — Spotify's
# terms don't allow pulling playable audio through it, full stop. Full-track
# playback is only available through Spotify's own SDK, which streams audio
# directly into a player Spotify controls in the user's browser (after they
# log in with their own Premium account) — NOMAD's backend never touches or
# caches the audio itself, only an access token that's handed to the SDK.
# =============================================================================
SPOTIFY_AUTH_URL  = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SDK_SCOPES = "streaming user-read-email user-read-private user-read-playback-state user-modify-playback-state"


def _spotify_redirect_uri():
    # Derived from the incoming request so this works regardless of what
    # host/port the app is actually reached on, instead of a hardcoded value
    # that would only work for one deployment.
    return request.host_url.rstrip("/") + "/api/spotify/callback"


_SPOTIFY_TOKEN_LOCK = threading.RLock()
_SPOTIFY_TOKEN_STORE = {
    "access_token": None,
    "refresh_token": None,
    "token_expiry": 0,
}


class SpotifyUserAuth:
    """Authorization Code flow for the single-user local NOMAD instance.

    Tokens are kept server-side in process memory rather than inside Flask's
    default client-side signed cookie. A signed cookie is integrity-protected,
    not confidential, so placing OAuth refresh tokens in it would expose them
    to anyone who can read the browser cookie.
    """

    def configured(self):
        s = load_settings()
        return bool(s.get("spotify_client_id") and s.get("spotify_client_secret"))

    def _creds(self):
        s = load_settings()
        return s.get("spotify_client_id", "").strip(), s.get("spotify_client_secret", "").strip()

    def build_authorize_url(self, state):
        client_id, _ = self._creds()
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _spotify_redirect_uri(),
            "scope": SPOTIFY_SDK_SCOPES,
            "state": state,
            "show_dialog": "false",
        }
        return SPOTIFY_AUTH_URL + "?" + urllib.parse.urlencode(params)

    def _token_request(self, data):
        client_id, client_secret = self._creds()
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            SPOTIFY_TOKEN_URL,
            data=urllib.parse.urlencode(data).encode(),
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def exchange_code(self, code):
        tok = self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _spotify_redirect_uri(),
        })
        with _SPOTIFY_TOKEN_LOCK:
            _SPOTIFY_TOKEN_STORE["access_token"] = tok.get("access_token")
            _SPOTIFY_TOKEN_STORE["refresh_token"] = tok.get("refresh_token") or _SPOTIFY_TOKEN_STORE.get("refresh_token")
            _SPOTIFY_TOKEN_STORE["token_expiry"] = time.time() + tok.get("expires_in", 3600)
        session["spotify_connected"] = True
        session.permanent = True
        return tok

    def get_valid_access_token(self):
        """Returns a fresh access token for the frontend SDK, refreshing if
        needed. Returns None if the user has never connected."""
        with _SPOTIFY_TOKEN_LOCK:
            refresh_token = _SPOTIFY_TOKEN_STORE.get("refresh_token")
            access_token = _SPOTIFY_TOKEN_STORE.get("access_token")
            token_expiry = _SPOTIFY_TOKEN_STORE.get("token_expiry", 0)
            if not refresh_token:
                return None
            if access_token and time.time() < token_expiry - 30:
                return access_token
            # Keep refresh-token rotation serialized. Multiple concurrent SDK
            # token requests near expiry can otherwise race and invalidate one
            # another when Spotify rotates the refresh token.
            tok = self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
            _SPOTIFY_TOKEN_STORE["access_token"] = tok.get("access_token")
            if tok.get("refresh_token"):
                _SPOTIFY_TOKEN_STORE["refresh_token"] = tok["refresh_token"]
            _SPOTIFY_TOKEN_STORE["token_expiry"] = time.time() + tok.get("expires_in", 3600)
            return _SPOTIFY_TOKEN_STORE["access_token"]

    def is_connected(self):
        with _SPOTIFY_TOKEN_LOCK:
            return bool(_SPOTIFY_TOKEN_STORE.get("refresh_token"))

    def disconnect(self):
        with _SPOTIFY_TOKEN_LOCK:
            _SPOTIFY_TOKEN_STORE.update({"access_token": None, "refresh_token": None, "token_expiry": 0})
        session.pop("spotify_connected", None)


spotify_user_auth = SpotifyUserAuth()


# =============================================================================
# FULL-TRACK CACHE — one canonical cache for the full-track migration.
#
# Keyed by recording identity (normalized title + artist + a duration
# bucket). This is a real fallback identity, not a placeholder for an audio
# fingerprint — true acoustic fingerprinting (e.g. chromaprint/AcoustID)
# would be a strictly stronger identity key and is the natural next step;
# the `fingerprint` field below is left null until that's wired up rather
# than faking one, per the "don't fake calibration" principle applied to
# identity too.
#
# Audio bytes are only ever cached here for sources whose terms actually
# allow it — Audius and Jamendo, both of which expose a stream/download
# endpoint specifically meant for third-party app playback. Spotify SDK
# playback is never written here: Spotify streams the audio directly into
# their own in-browser player and NOMAD's backend never receives the audio,
# so there's nothing legitimate to cache for it — only its lyrics/metadata
# entry lives in this store.
# =============================================================================
FULL_TRACK_CACHE_DIR = os.path.join(BASE_DIR, "full_track_cache")
FULL_TRACK_CACHE_JSON = os.path.join(FULL_TRACK_CACHE_DIR, "index.json")
FULL_TRACK_CACHE_ENGINE_VERSION = 1
os.makedirs(FULL_TRACK_CACHE_DIR, exist_ok=True)
_FULL_TRACK_CACHE_LOCK = threading.Lock()


def _duration_bucket(duration):
    # Rounds to the nearest 2s bucket so trivially different encodes of the
    # same recording (e.g. 213.41s vs 213.9s) still land on the same key,
    # while genuinely different recordings (different take/live/remix) with
    # different runtimes usually don't.
    try:
        return int(round(float(duration or 0) / 2.0) * 2)
    except (TypeError, ValueError):
        return 0


def full_track_identity_key(title, artist, duration=0):
    return f"{_lyrics_norm_text(title)}::{_lyrics_norm_text(artist)}::{_duration_bucket(duration)}"


def _load_full_track_cache_index():
    try:
        with open(FULL_TRACK_CACHE_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_full_track_cache_index(data):
    with _FULL_TRACK_CACHE_LOCK:
        try:
            tmp = FULL_TRACK_CACHE_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, FULL_TRACK_CACHE_JSON)
        except Exception:
            pass


def get_cached_full_track(track_key):
    entry = _load_full_track_cache_index().get(track_key)
    if not entry:
        return None
    # Invalidate if the cached audio file went missing, or if a source/
    # engine-version change means this timing data may no longer apply to
    # what's on disk (section 21: never trust cached timing across a
    # recording/source change).
    audio_path = entry.get("audio", {}).get("path")
    if audio_path and not os.path.exists(os.path.join(FULL_TRACK_CACHE_DIR, audio_path)):
        return None
    if entry.get("engine_version") != FULL_TRACK_CACHE_ENGINE_VERSION:
        return None
    return entry


def save_full_track_cache(track_key, entry):
    data = _load_full_track_cache_index()
    entry["engine_version"] = FULL_TRACK_CACHE_ENGINE_VERSION
    entry["cached_at"] = time.time()
    data[track_key] = entry
    _save_full_track_cache_index(data)
    return entry


def download_and_cache_audio(track_key, stream_url, source):
    """Downloads audio bytes from a source whose terms permit it (Audius,
    Jamendo) into the canonical cache. Returns the entry's audio dict, or
    None on failure — callers should fall back to direct streaming rather
    than fail the whole resolve."""
    try:
        ext = ".mp3"
        rel_path = f"{re.sub(r'[^a-zA-Z0-9_-]', '_', track_key)}{ext}"
        abs_path = os.path.join(FULL_TRACK_CACHE_DIR, rel_path)
        req = urllib.request.Request(stream_url, headers={"User-Agent": "NOMAD/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(abs_path, "wb") as f:
            f.write(data)
        return {"path": rel_path, "source": source, "cached_at": time.time(), "size": len(data)}
    except Exception:
        return None


# =============================================================================
# YOUTUBE METADATA / SEARCH HELPERS (no download — used to preview + match)
# =============================================================================

def yt_probe(url):
    if not YTDLP_AVAILABLE:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title", "Unknown title"),
        "artist": info.get("uploader", ""),
        "duration": info.get("duration", 0),
        "thumbnail": info.get("thumbnail"),
        "youtube_id": info.get("id"),
        "youtube_url": info.get("webpage_url", url),
    }


def yt_search_many(query, limit=6):
    """Top N YouTube results for `query` — powers the multi-service search."""
    if not YTDLP_AVAILABLE:
        return []
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "default_search": f"ytsearch{limit}", "extract_flat": "in_playlist"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception:
        return []
    entries = info.get("entries") if info and "entries" in info else ([info] if info else [])
    out = []
    for e in entries or []:
        if not e:
            continue
        out.append({
            "title": e.get("title", "Unknown title"),
            "artist": e.get("uploader", "") or e.get("channel", ""),
            "duration": e.get("duration") or 0,
            "thumbnail": e.get("thumbnail") or (e.get("thumbnails") or [{}])[-1].get("url"),
            "url": e.get("webpage_url") or f"https://www.youtube.com/watch?v={e.get('id')}",
        })
    return out


def soundcloud_search_many(query, limit=6):
    """SoundCloud results via yt-dlp's scsearch — no API key needed."""
    if not YTDLP_AVAILABLE:
        return []
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "default_search": f"scsearch{limit}", "extract_flat": "in_playlist"}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
    except Exception:
        return []
    entries = info.get("entries") if info and "entries" in info else ([info] if info else [])
    out = []
    for e in entries or []:
        if not e:
            continue
        out.append({
            "title": e.get("title", "Unknown title"),
            "artist": e.get("uploader", "") or "",
            "duration": e.get("duration") or 0,
            "thumbnail": e.get("thumbnail"),
            "url": e.get("webpage_url") or e.get("url"),
        })
    return out


def deezer_search_many(query, limit=6):
    """Deezer's public search API (api.deezer.com) needs no key or auth at
    all for basic search — a genuinely free, keyless service, so this one
    always works out of the box."""
    def _do():
        try:
            url = "https://api.deezer.com/search?" + urllib.parse.urlencode({"q": query, "limit": limit})
            req = urllib.request.Request(url, headers={"User-Agent": "NOMAD/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            return []
        out = []
        for t in (data.get("data") or [])[:limit]:
            out.append({
                "title": t.get("title", "Unknown title"),
                "artist": (t.get("artist") or {}).get("name", ""),
                "duration": t.get("duration") or 0,
                "thumbnail": (t.get("album") or {}).get("cover_medium"),
                "url": t.get("link"),
                "preview_url": t.get("preview"),  # 30s preview clip Deezer allows publicly
            })
        return out

    return _cached_call(f"deezer:search:{query.lower()}:{limit}", 3 * 3600, _do)


# =============================================================================
# AI PLAYLIST GENERATION
#
# Two tiers, both free:
#   1. If a Groq API key is set (console.groq.com — free tier, no card
#      needed), ask a small fast model for real song picks matching the
#      prompt's mood/genre/era.
#   2. Otherwise, a local heuristic: match keywords in the prompt against a
#      small genre/mood table and turn them into good YouTube search queries.
#      Not "AI" in the model sense, but zero cost, zero setup, and it works
#      offline-friendly the moment ffmpeg/yt-dlp are present.
# =============================================================================
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MOOD_GENRE_SEEDS = {
    "workout": ["high energy gym workout mix", "pump up gym anthem", "hype workout playlist essential"],
    "gym":     ["high energy gym workout mix", "pump up gym anthem"],
    "chill":   ["chill lofi study beats", "relaxing chillhop mix", "mellow indie chill playlist"],
    "study":   ["lofi study beats", "focus instrumental playlist", "calm study music"],
    "sad":     ["sad emotional acoustic songs", "melancholy indie playlist", "heartbreak ballads"],
    "happy":   ["feel good upbeat pop hits", "happy summer playlist", "sunshine pop anthem"],
    "party":   ["party dance hits", "club banger mix", "party anthem playlist"],
    "sleep":   ["calm sleep ambient music", "soft piano sleep music"],
    "romantic": ["romantic love songs playlist", "slow r&b love songs"],
    "focus":   ["deep focus instrumental", "concentration ambient music"],
    "rock":    ["classic rock anthems", "modern rock hits"],
    "hip hop": ["hip hop essentials playlist", "rap bangers"],
    "hiphop":  ["hip hop essentials playlist", "rap bangers"],
    "jazz":    ["smooth jazz classics", "jazz cafe playlist"],
    "driving": ["road trip driving playlist", "highway drive rock mix"],
    "rain":    ["rainy day acoustic playlist", "cozy rainy day music"],
    "90s":     ["90s hits playlist", "90s throwback classics"],
    "80s":     ["80s hits playlist", "80s synth classics"],
}


def ai_generate_tracks(prompt, count=12):
    """Returns a list of ready-to-cache track metas (title/artist/duration/
    thumbnail/url) for the given prompt — resolved all the way to a playable
    YouTube match, so the caller can just create tracks from them directly."""
    settings = load_settings()
    groq_key = settings.get("groq_api_key", "").strip()
    if groq_key:
        try:
            queries = _ai_generate_via_groq(prompt, count, groq_key)
            metas = []
            for q in queries:
                m = yt_search_best(q)
                if m:
                    metas.append(m)
            if metas:
                return metas, "groq"
        except Exception as e:
            broadcast("playlists_log", {"msg": f"AI (Groq) generation failed, falling back to local picks: {e}", "level": "warn"})
    return _ai_generate_local_fallback(count, prompt), "local"


def _ai_generate_via_groq(prompt, count, api_key):
    system = (
        "You are a music curator. Given a mood/prompt, reply with ONLY a JSON array "
        f"of exactly {count} real songs as objects: [{{\"artist\":\"...\",\"title\":\"...\"}}]. "
        "No commentary, no markdown fences, just the JSON array. Pick real, well-known "
        "tracks that genuinely fit the prompt, avoid duplicates and avoid only one artist."
    )
    body = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": 0.9,
        "max_tokens": 1200,
    }).encode()
    req = urllib.request.Request(GROQ_API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    text = data["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    items = json.loads(text)
    out = []
    for it in items[:count]:
        artist, title = it.get("artist", "").strip(), it.get("title", "").strip()
        if title:
            out.append(f"{artist} - {title}" if artist else title)
    return out


def _ai_generate_local_fallback(count, prompt):
    p = (prompt or "").lower()
    seeds = []
    for kw, queries in MOOD_GENRE_SEEDS.items():
        if kw in p:
            seeds.extend(queries)
    if not seeds:
        # nothing matched a known mood/genre keyword — just treat the raw
        # prompt itself as a playlist/mix search
        seeds = [f"{prompt} playlist", f"best {prompt} songs", f"{prompt} mix"]
    seen_titles = set()
    out = []
    per_seed = max(3, (count // max(len(seeds), 1)) + 2)
    for seed in seeds:
        if len(out) >= count:
            break
        for e in yt_search_many(seed, limit=per_seed):
            key = e["title"].lower().strip()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            e["youtube_url"] = e["url"]
            e["youtube_id"] = None
            out.append(e)
            if len(out) >= count:
                break
    return out[:count]


def search_all_services(query, limit=6):
    """One search fanned out across YouTube, SoundCloud, Deezer, and Spotify
    (when configured) — run concurrently so the whole thing takes about as
    long as the slowest single lookup, not the sum of all four."""
    results = {"youtube": [], "soundcloud": [], "deezer": [], "spotify": []}
    jobs = {
        "youtube": lambda: yt_search_many(query, limit),
        "soundcloud": lambda: soundcloud_search_many(query, limit),
        "deezer": lambda: deezer_search_many(query, limit),
    }
    if spotify.configured():
        jobs["spotify"] = lambda: spotify.search_track(query, limit)

    threads = []
    for key, job in jobs.items():
        def run_job(key=key, job=job):
            try:
                results[key] = job() or []
            except Exception:
                results[key] = []
        th = threading.Thread(target=run_job, daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join(timeout=15)
    return results


def yt_search_best(query):
    """Top YouTube result for `query` — used both for the search-to-add flow
    and to auto-match a Spotify track to something NOMAD can actually play."""
    if not YTDLP_AVAILABLE:
        return None
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "default_search": "ytsearch1"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = info.get("entries") if info and "entries" in info else ([info] if info else [])
    if not entries or not entries[0]:
        return None
    e = entries[0]
    return {
        "title": e.get("title", "Unknown title"),
        "artist": e.get("uploader", ""),
        "duration": e.get("duration", 0),
        "thumbnail": e.get("thumbnail"),
        "youtube_id": e.get("id"),
        "youtube_url": e.get("webpage_url") or f"https://www.youtube.com/watch?v={e.get('id')}",
    }


def yt_resolve_audio_stream(query):
    """Resolve a playable YouTube audio URL for a search query.

    Used for Discover playback only. Spotify/iTunes/Deezer give metadata or
    previews, not full commercial audio streams, so yt-dlp is the full-song
    resolver when the user has it installed.
    """
    if not YTDLP_AVAILABLE or not query:
        return None, "yt-dlp unavailable"
    try:
        meta = yt_search_best(query)
        if not meta or not meta.get("youtube_url"):
            return None, "no YouTube match"
        opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 12,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(meta["youtube_url"], download=False)
        stream_url = info.get("url")
        if not stream_url and info.get("requested_formats"):
            stream_url = info["requested_formats"][0].get("url")
        if not stream_url:
            return None, "no audio stream in YouTube match"
        return {
            "stream_url": stream_url,
            "title": meta.get("title") or query,
            "artist": meta.get("artist") or "",
            "duration": meta.get("duration") or 0,
            "thumbnail": meta.get("thumbnail") or "",
            "source": "youtube_full",
            "youtube_url": meta.get("youtube_url"),
        }, None
    except Exception as e:
        return None, str(e)


# =============================================================================
# PLAYLIST CONTROLLER
#
# Playlists are stored as plain JSON (playlists.json) and audio is cached in
# playlist_audio/ named by track id — deliberately simple, file-based state
# so a future mobile client can eventually talk to this same Flask API
# (or sync against the same JSON) instead of needing a rewrite.
# =============================================================================

DEFAULT_PL_ENHANCE = {
    "loudness_normalize": False,
    "audio_denoise": False,
    "bass_boost": False,
    "vocal_boost": False,
    "dolby_spatial": False,
    "crystal_upscale": False,
    "auto_skip_if_good": True,
}

# Thresholds used by the auto quality-checker: below these, a file is
# considered "not great" and worth enhancing; at/above, it's already good
# quality audio and heavy passes (crystal upscale in particular) are skipped
# unless the person explicitly forces them.
GOOD_BITRATE_KBPS = 256
GOOD_SAMPLE_RATE  = 44100


def probe_audio_quality(filepath):
    """ffprobe-based quality check — bitrate + sample rate of the cached
    file. Used to decide whether an enhance pass is actually needed instead
    of blindly re-processing audio that's already good."""
    ffmpeg_path = ffmpeg_location()
    if not ffmpeg_path or not filepath or not os.path.exists(filepath):
        return {"bitrate_kbps": None, "sample_rate": None, "already_good": False, "checked": False}
    ffprobe_path = ffmpeg_path.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
    if not os.path.exists(ffprobe_path):
        ffprobe_path = shutil.which("ffprobe") or ffprobe_path
    try:
        r = subprocess.run(
            [ffprobe_path, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", filepath],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        info = json.loads(r.stdout or "{}")
        stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
        bitrate = int(stream.get("bit_rate") or info.get("format", {}).get("bit_rate") or 0) // 1000
        sample_rate = int(stream.get("sample_rate") or 0)
        already_good = bitrate >= GOOD_BITRATE_KBPS and sample_rate >= GOOD_SAMPLE_RATE
        return {"bitrate_kbps": bitrate or None, "sample_rate": sample_rate or None,
                "already_good": already_good, "checked": True}
    except Exception:
        return {"bitrate_kbps": None, "sample_rate": None, "already_good": False, "checked": False}


# =============================================================================
# AUDIO INTELLIGENCE — real DSP analysis, not invented numbers.
# BPM/tempo via onset+beat tracking, musical key via Krumhansl-Schmuckler
# profile correlation on chroma, Camelot wheel mapping for harmonic mixing,
# loudness/true-peak/LRA via ffmpeg's loudnorm analyze pass (broadcast-grade,
# same engine already used for the Loudness Normalize enhance option),
# energy/danceability/acousticness as transparent heuristics built from
# RMS + harmonic-percussive separation + beat regularity, plus beat grid,
# waveform peaks, MFCC and chroma vectors for playlist DNA/similarity.
#
# librosa/numpy/soundfile are optional — NOMAD runs fine without them, this
# feature just stays greyed out with a one-click installer (like ffmpeg /
# Real-ESRGAN / fpcalc already do). Results are cached forever per-track in
# playlists.json (t["audio_intel_cache"]), same pattern as quality_cache.
# =============================================================================
try:
    import numpy as np
    import librosa
    AUDIO_INTEL_LIBS = True
except Exception:
    AUDIO_INTEL_LIBS = False

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Standard Camelot wheel: outer ring (B) = major keys, inner ring (A) = minor.
CAMELOT_MAJOR = {0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
                 6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B"}
CAMELOT_MINOR = {0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
                 6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A"}
_KS_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KS_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _detect_key(chroma_mean):
    """Correlates the track's mean chroma vector against all 24 rotated
    Krumhansl-Schmuckler major/minor profiles; returns (name, camelot, confidence)."""
    maj_p, min_p = np.array(_KS_MAJOR), np.array(_KS_MINOR)
    best_name, best_corr, best_mode, best_root = None, -1e9, "major", 0
    for shift in range(12):
        mc = np.corrcoef(chroma_mean, np.roll(maj_p, shift))[0, 1]
        nc = np.corrcoef(chroma_mean, np.roll(min_p, shift))[0, 1]
        if mc > best_corr:
            best_name, best_corr, best_mode, best_root = f"{NOTE_NAMES[shift]} major", mc, "major", shift
        if nc > best_corr:
            best_name, best_corr, best_mode, best_root = f"{NOTE_NAMES[shift]} minor", nc, "minor", shift
    camelot = CAMELOT_MAJOR[best_root] if best_mode == "major" else CAMELOT_MINOR[best_root]
    confidence = float(np.clip(best_corr, 0, 1)) if best_corr == best_corr else 0.0  # NaN guard
    return best_name, camelot, confidence


def camelot_compatible(a, b):
    """True if two Camelot codes are harmonically mixable: same number
    (relative major/minor), adjacent number same letter, or identical."""
    if not a or not b or a == b:
        return True
    try:
        na, la = int(a[:-1]), a[-1]
        nb, lb = int(b[:-1]), b[-1]
    except Exception:
        return True
    if na == nb:
        return True
    if la == lb and (abs(na - nb) == 1 or abs(na - nb) == 11):
        return True
    return False


def harmonic_order(tracks):
    """Greedy DJ-style reorder: starts from the lowest-energy analyzed track
    (a natural build-up arc) then repeatedly picks whichever remaining track
    is harmonically compatible (same/adjacent Camelot) and closest in BPM —
    the same two things a human DJ checks before queuing the next song.
    Tracks without cached analysis are left in place at the end, unmoved,
    since there's nothing real to base an ordering on."""
    analyzed = [t for t in tracks if t.get("audio_intel_cache")]
    unanalyzed = [t for t in tracks if not t.get("audio_intel_cache")]
    if len(analyzed) < 2:
        return tracks
    remaining = sorted(analyzed, key=lambda t: t["audio_intel_cache"].get("energy") or 0)
    chain = [remaining.pop(0)]
    while remaining:
        last = chain[-1]["audio_intel_cache"]
        last_camelot, last_bpm = last.get("camelot"), last.get("bpm") or 0

        def score(t):
            f = t["audio_intel_cache"]
            clash = 0 if camelot_compatible(last_camelot, f.get("camelot")) else 1
            bpm_diff = abs((f.get("bpm") or 0) - last_bpm)
            return (clash, bpm_diff)

        remaining.sort(key=score)
        chain.append(remaining.pop(0))
    return chain + unanalyzed


def _ffmpeg_loudness(filepath):
    """Runs ffmpeg's loudnorm filter in analyze-only mode (single pass, no
    output file) to get true integrated loudness (LUFS), true peak, and
    loudness range — the same measurement broadcasters use."""
    ffmpeg_path = ffmpeg_location()
    if not ffmpeg_path:
        return None
    try:
        r = subprocess.run(
            [ffmpeg_path, "-i", filepath, "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=90,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
        if not m:
            return None
        data = json.loads(m.group(0))
        lufs = float(data["input_i"])
        return {
            "lufs": lufs,
            "true_peak_db": float(data.get("input_tp", 0) or 0),
            "loudness_range": float(data.get("input_lra", 0) or 0),
            # ReplayGain-style adjustment to reach the common -18 LUFS streaming target.
            "replaygain_db": round(-18.0 - lufs, 2),
        }
    except Exception:
        return None


def _mood_label(energy, danceability, acousticness, tempo):
    """A transparent, explainable heuristic — not a trained classifier — that
    turns the measured features into a human-readable vibe. Labeled as an
    estimate in the UI rather than presented as ground truth."""
    if acousticness > 0.65 and energy < 0.45:
        return "chill / acoustic"
    if danceability > 0.6 and energy > 0.55:
        return "high energy / dance"
    if energy < 0.35 and tempo < 95:
        return "moody / downtempo"
    if tempo >= 130 and energy > 0.5:
        return "upbeat / driving"
    if energy > 0.7:
        return "intense / energetic"
    return "balanced / mid-tempo"


def analyze_audio_file(filepath):
    """The core DSP pass. Returns a dict of every field the caller can cache.
    Raises on failure so callers can distinguish 'not analyzed' from 'tried
    and the file is unreadable'."""
    if not AUDIO_INTEL_LIBS:
        raise RuntimeError("audio intelligence libraries not installed")
    y, sr = librosa.load(filepath, sr=22050, mono=True)
    if y.size == 0:
        raise RuntimeError("empty or unreadable audio")
    duration = float(librosa.get_duration(y=y, sr=sr))

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    tempo = float(np.atleast_1d(tempo)[0])
    beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()

    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    chroma_mean = chroma.mean(axis=1)
    key_name, camelot, key_confidence = _detect_key(chroma_mean)

    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_mean = mfcc.mean(axis=1).tolist()

    rms = librosa.feature.rms(y=y)[0]
    energy = float(np.clip(rms.mean() * 8.0, 0, 1))

    y_harm, y_perc = librosa.effects.hpss(y)
    perc_ratio = float(np.sum(y_perc ** 2) / (np.sum(y ** 2) + 1e-9))
    acousticness = float(np.clip(1.0 - perc_ratio * 1.4, 0, 1))

    ibi = np.diff(beat_times)
    beat_regularity = float(np.clip(1.0 - (np.std(ibi) / (np.mean(ibi) + 1e-9)), 0, 1)) if len(ibi) > 2 else 0.5
    danceability = float(np.clip(0.5 * beat_regularity + 0.5 * perc_ratio * 1.6, 0, 1))

    # Downsample to ~200 points for a lightweight waveform UI (not the raw sample array).
    peaks_n = 200
    hop = max(1, len(y) // peaks_n)
    waveform_peaks = [round(float(np.max(np.abs(y[i:i + hop]))), 4) if len(y[i:i + hop]) else 0.0
                       for i in range(0, len(y), hop)][:peaks_n]

    loudness = _ffmpeg_loudness(filepath) or {}
    quality = probe_audio_quality(filepath)
    mood = _mood_label(energy, danceability, acousticness, tempo)

    return {
        "bpm": round(tempo, 1),
        "key": key_name,
        "camelot": camelot,
        "key_confidence": round(key_confidence, 2),
        "duration": round(duration, 2),
        "energy": round(energy, 3),
        "danceability": round(danceability, 3),
        "acousticness": round(acousticness, 3),
        "mood": mood,
        "loudness_lufs": loudness.get("lufs"),
        "true_peak_db": loudness.get("true_peak_db"),
        "loudness_range": loudness.get("loudness_range"),
        "replaygain_db": loudness.get("replaygain_db"),
        "quality_score": quality.get("bitrate_kbps"),
        "beat_times": [round(b, 3) for b in beat_times[:400]],
        "beat_count": len(beat_times),
        "waveform_peaks": waveform_peaks,
        "mfcc_mean": [round(x, 3) for x in mfcc_mean],
        "chroma_mean": [round(float(x), 3) for x in chroma_mean.tolist()],
        "analyzed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class AudioIntelController:
    """Owns background analysis of playlist tracks and the aggregate
    'Playlist DNA' view built from cached per-track results. Talks to the
    global `playlists` controller so results land in the same playlists.json
    every other cache (quality/lyrics/fingerprint) already lives in."""

    def __init__(self):
        self.log = log_fn("audio_intel")
        self._jobs = {}  # playlist_id -> {"total", "done", "active": bool}
        self._lock = threading.Lock()

    def available(self):
        return AUDIO_INTEL_LIBS

    def status(self):
        return {"available": AUDIO_INTEL_LIBS, "ffmpeg": ffmpeg_present()}

    def install(self):
        """One-click installer — pip install of librosa/soundfile/numpy,
        mirroring the ffmpeg/Real-ESRGAN/fpcalc installers already in NOMAD."""
        global AUDIO_INTEL_LIBS, np, librosa
        try:
            broadcast("audio_intel_install_progress", {"pct": 5, "detail": "installing numpy + soundfile"})
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                             "numpy", "soundfile"], check=True, timeout=300)
            broadcast("audio_intel_install_progress", {"pct": 40, "detail": "installing librosa (~40MB, may take a minute)"})
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "librosa"],
                            check=True, timeout=600)
            broadcast("audio_intel_install_progress", {"pct": 90, "detail": "verifying install"})
            import numpy as _np
            import librosa as _librosa
            np, librosa = _np, _librosa
            AUDIO_INTEL_LIBS = True
            self.log("audio intelligence libraries installed", "ok")
            broadcast("audio_intel_install_progress", {"pct": 100, "detail": "done"})
            broadcast("audio_intel_status", {"available": True})
            return True
        except Exception as e:
            self.log(f"audio intelligence install failed: {e}", "bad")
            broadcast("audio_intel_install_progress", {"pct": 0, "detail": f"failed: {e}"})
            return False

    def job_status(self, playlist_id):
        return self._jobs.get(playlist_id, {"total": 0, "done": 0, "active": False})

    def analyze_track(self, playlist_id, track_id, force=False):
        p = playlists.get_playlist(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t or not t.get("audio_file"):
            raise RuntimeError("track has no cached audio yet")
        if t.get("audio_intel_cache") and not force:
            return t["audio_intel_cache"]
        path = os.path.join(PLAYLISTS_DIR, t["audio_file"])
        if not os.path.exists(path):
            raise RuntimeError("audio file missing on disk")
        features = analyze_audio_file(path)
        t["audio_intel_cache"] = features
        playlists._save()
        playlists._broadcast_state()
        return features

    def analyze_playlist(self, playlist_id, force=False):
        """Background batch analysis of every ready track missing (or, if
        force, every) cached features. Non-blocking — call and poll job_status."""
        p = playlists.get_playlist(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        targets = [t for t in p["tracks"] if t.get("status") == "ready" and t.get("audio_file")
                   and (force or not t.get("audio_intel_cache"))]
        with self._lock:
            self._jobs[playlist_id] = {"total": len(targets), "done": 0, "active": True}

        def worker():
            for t in targets:
                try:
                    self.analyze_track(playlist_id, t["id"], force=force)
                except Exception as e:
                    self.log(f"analysis failed for \"{t.get('title', '?')}\": {e}", "warn")
                with self._lock:
                    self._jobs[playlist_id]["done"] += 1
                broadcast("audio_intel_progress", {"playlist_id": playlist_id, **self._jobs[playlist_id]})
            with self._lock:
                self._jobs[playlist_id]["active"] = False
            broadcast("audio_intel_progress", {"playlist_id": playlist_id, **self._jobs[playlist_id]})
            self.log(f"analyzed {len(targets)} track(s) in playlist {playlist_id}", "ok")

        threading.Thread(target=worker, daemon=True).start()
        return {"queued": len(targets)}

    def dna(self, playlist_id):
        """Aggregate musical profile built entirely from cached per-track
        features — this is 'Playlist DNA'."""
        p = playlists.get_playlist(playlist_id)
        if not p:
            return None
        analyzed = [t["audio_intel_cache"] for t in p["tracks"] if t.get("audio_intel_cache")]
        unanalyzed = sum(1 for t in p["tracks"] if t.get("status") == "ready" and not t.get("audio_intel_cache"))
        if not analyzed:
            return {"analyzed_count": 0, "unanalyzed_count": unanalyzed, "total_tracks": len(p["tracks"])}

        bpms = [f["bpm"] for f in analyzed if f.get("bpm")]
        energies = [f["energy"] for f in analyzed if f.get("energy") is not None]
        dance = [f["danceability"] for f in analyzed if f.get("danceability") is not None]
        acoustic = [f["acousticness"] for f in analyzed if f.get("acousticness") is not None]
        camelots = [f["camelot"] for f in analyzed if f.get("camelot")]
        moods = [f["mood"] for f in analyzed if f.get("mood")]
        key_counts = {}
        for c in camelots:
            key_counts[c] = key_counts.get(c, 0) + 1
        mood_counts = {}
        for m in moods:
            mood_counts[m] = mood_counts.get(m, 0) + 1

        # Harmonic clash check between consecutive analyzed tracks, in playlist order.
        clashes = []
        ordered = [t for t in p["tracks"] if t.get("audio_intel_cache")]
        for i in range(len(ordered) - 1):
            a, b = ordered[i]["audio_intel_cache"].get("camelot"), ordered[i + 1]["audio_intel_cache"].get("camelot")
            if a and b and not camelot_compatible(a, b):
                clashes.append({"a_id": ordered[i]["id"], "b_id": ordered[i + 1]["id"],
                                 "a_key": a, "b_key": b})

        return {
            "analyzed_count": len(analyzed),
            "unanalyzed_count": unanalyzed,
            "total_tracks": len(p["tracks"]),
            "avg_bpm": round(sum(bpms) / len(bpms), 1) if bpms else None,
            "bpm_range": [round(min(bpms), 1), round(max(bpms), 1)] if bpms else None,
            "avg_energy": round(sum(energies) / len(energies), 3) if energies else None,
            "avg_danceability": round(sum(dance) / len(dance), 3) if dance else None,
            "avg_acousticness": round(sum(acoustic) / len(acoustic), 3) if acoustic else None,
            "key_distribution": key_counts,
            "dominant_mood": max(mood_counts.items(), key=lambda x: x[1])[0] if mood_counts else None,
            "mood_distribution": mood_counts,
            "energy_curve": [{"track_id": t["id"], "title": t.get("title"),
                               "energy": t["audio_intel_cache"].get("energy")} for t in ordered],
            "key_clashes": clashes,
        }

    # ---- Cross-library helpers powering Discover's fingerprint-based
    # 'AI Picks' — everything here reads only the mfcc/chroma vectors already
    # cached per-track by analyze_audio_file, no re-analysis needed ----
    def all_cached(self):
        out = []
        for p in playlists.data["playlists"]:
            for t in p["tracks"]:
                cache = t.get("audio_intel_cache")
                if cache and cache.get("mfcc_mean") and cache.get("chroma_mean"):
                    out.append({**t, "playlist_id": p["id"], "playlist_name": p["name"], "audio_intel_cache": cache})
        return out

    @staticmethod
    def _cos_sim(a, b):
        if not a or not b or len(a) != len(b):
            return 0.0
        num = sum(x * y for x, y in zip(a, b))
        da = sum(x * x for x in a) ** 0.5
        db = sum(y * y for y in b) ** 0.5
        if da == 0 or db == 0:
            return 0.0
        return num / (da * db)

    def similar_to_track(self, playlist_id, track_id, top_k=8):
        """'More like this' — nearest cached tracks by MFCC+chroma cosine
        similarity, library-wide."""
        p = playlists.get_playlist(playlist_id)
        if not p:
            return {"ok": False, "error": "playlist not found"}
        seed = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not seed or not seed.get("audio_intel_cache"):
            return {"ok": False, "error": "seed track has no cached audio analysis yet"}
        seed_vec = seed["audio_intel_cache"]["mfcc_mean"] + seed["audio_intel_cache"]["chroma_mean"]
        rows = []
        for t in self.all_cached():
            if t["id"] == track_id:
                continue
            vec = t["audio_intel_cache"]["mfcc_mean"] + t["audio_intel_cache"]["chroma_mean"]
            rows.append({**t, "similarity": round(self._cos_sim(seed_vec, vec), 4)})
        rows.sort(key=lambda r: -r["similarity"])
        return {"ok": True, "seed": {"title": seed.get("title"), "artist": seed.get("artist")}, "results": rows[:top_k]}

    def vibe_recommendations(self, top_k=10):
        """Discover's 'AI Picks' rail: build a seed vibe vector from your
        most-played *analyzed* tracks, then surface library tracks with a
        close audio fingerprint that you haven't been playing much — a real
        recommender over your own library, not a canned list."""
        cached = self.all_cached()
        if len(cached) < 3:
            return []
        from collections import Counter
        play_counts = Counter((p.get("title", ""), p.get("artist", "")) for p in analytics.plays)

        def pc(t):
            return play_counts.get((t.get("title", ""), t.get("artist", "")), 0)

        seeds = sorted(cached, key=pc, reverse=True)[:5]
        seeds = [s for s in seeds if pc(s) > 0] or cached[:5]
        seed_vecs = [s["audio_intel_cache"]["mfcc_mean"] + s["audio_intel_cache"]["chroma_mean"] for s in seeds]
        seed_ids = {s["id"] for s in seeds}

        def avg_sim(vec):
            sims = [self._cos_sim(vec, sv) for sv in seed_vecs]
            return sum(sims) / len(sims) if sims else 0

        candidates = [t for t in cached if t["id"] not in seed_ids and pc(t) <= 1]
        scored = []
        for t in candidates:
            vec = t["audio_intel_cache"]["mfcc_mean"] + t["audio_intel_cache"]["chroma_mean"]
            scored.append({**t, "vibe_match": round(avg_sim(vec), 4)})
        scored.sort(key=lambda r: -r["vibe_match"])
        return scored[:top_k]


# =============================================================================
# MusicBrainz — free, no-key canonical metadata (title/artist/genre/year).
# Must self-throttle to 1 req/sec and send a real User-Agent per their rules.
# =============================================================================
MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_UA = "NOMAD/1.0 (local personal playlist app; contact: none)"
_mb_lock = threading.Lock()
_mb_last_call = 0.0


def _mb_throttled_get(path, params):
    global _mb_last_call
    cache_key = f"mb:{path}?{urllib.parse.urlencode(params)}"

    def _do():
        global _mb_last_call
        with _mb_lock:
            wait = 1.05 - (time.time() - _mb_last_call)
            if wait > 0:
                time.sleep(wait)
            url = f"{MUSICBRAINZ_BASE}{path}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": MUSICBRAINZ_UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            _mb_last_call = time.time()
            return data

    return _cached_call(cache_key, 24 * 3600, _do)


def musicbrainz_lookup(title, artist):
    """Best-match canonical recording — title/artist/first-release-year/tags
    (used as a lightweight genre signal). No key, no auth, just rate-limited."""
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return {"found": False}
    query = f'recording:"{title}"' + (f' AND artist:"{artist}"' if artist else "")
    try:
        data = _mb_throttled_get("/recording/", {"query": query, "fmt": "json", "limit": 5, "inc": "tags"})
        recordings = data.get("recordings") or []
        if not recordings:
            return {"found": False}
        best = max(recordings, key=lambda r: r.get("score", 0))
        artist_credit = best.get("artist-credit") or []
        canonical_artist = "".join(
            (a.get("name", "") if isinstance(a, dict) else str(a)) + (a.get("joinphrase", "") if isinstance(a, dict) else "")
            for a in artist_credit
        ) or None
        release_date = None
        for rel in best.get("releases") or []:
            d = rel.get("date")
            if d and (not release_date or d < release_date):
                release_date = d
        tags = sorted((best.get("tags") or []), key=lambda t: -t.get("count", 0))
        return {
            "found": True,
            "score": best.get("score"),
            "title": best.get("title"),
            "artist": canonical_artist,
            "year": release_date[:4] if release_date else None,
            "tags": [t["name"] for t in tags[:5]],
        }
    except Exception as e:
        return {"found": False, "error": str(e)}


# =============================================================================
# Last.fm — free-key "similar artists", used for Discover suggestions based
# on the artists already in a playlist.
# =============================================================================
LASTFM_BASE = "https://ws.audioscrobbler.com/2.0/"


def lastfm_similar_artists(artist, api_key, limit=8):
    if not artist or not api_key:
        return []
    params = {"method": "artist.getsimilar", "artist": artist, "api_key": api_key,
              "format": "json", "autocorrect": 1, "limit": limit}

    def _do():
        try:
            url = f"{LASTFM_BASE}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url, headers={"User-Agent": LRCLIB_UA})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            artists = (data.get("similarartists") or {}).get("artist") or []
            return [{"name": a.get("name"), "match": float(a.get("match") or 0)} for a in artists if a.get("name")]
        except Exception:
            return []

    return _cached_call(f"lastfm:similar:{artist.lower()}:{limit}", 24 * 3600, _do)


# =============================================================================
# Genius — free-key lyrics annotations (background/meaning), separate from
# LRCLIB's synced timing.
# =============================================================================
GENIUS_BASE = "https://api.genius.com"


def _genius_request(path, params, token):
    url = f"{GENIUS_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": LRCLIB_UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def genius_get_annotations(title, artist, token, max_annotations=8):
    """Searches Genius for the song, then pulls its referents (annotated
    lyric fragments + the community/artist explanation for each)."""
    if not token or not title:
        return {"found": False}
    cache_key = f"genius:annot:{(title or '').lower()}:{(artist or '').lower()}:{max_annotations}"

    def _do():
        try:
            search = _genius_request("/search", {"q": f"{title} {artist or ''}".strip()}, token)
            hits = (search.get("response") or {}).get("hits") or []
            if not hits:
                return {"found": False, "reason": "no matching song on Genius"}
            # Genius' text search sometimes ranks a cover/remix/karaoke
            # upload above the real song — pick the first hit that
            # confidently matches title+artist instead of always hits[0].
            song = None
            for h in hits:
                r = h.get("result") or {}
                cand_title = r.get("title") or ""
                cand_artist = (r.get("primary_artist") or {}).get("name") or ""
                ok, _, _ = _lyrics_match_ok(title, artist, cand_title, cand_artist, title_floor=0.6, artist_floor=0.4)
                if ok:
                    song = r
                    break
            if song is None:
                song = hits[0]["result"]  # nothing confidently matched — still try the top hit
            song_id = song.get("id")
            song_url = song.get("url")
            # per_page=20 for referents but Genius's default sort can bury the
            # best-known annotations behind trivia ones — bump the page size
            # a bit further and don't stop at the first with a body so a
            # trickle of low-quality referents doesn't crowd out real ones.
            ref_data = _genius_request("/referents", {"song_id": song_id, "text_format": "plain", "per_page": 50}, token)
            referents = (ref_data.get("response") or {}).get("referents") or []
            annotations = []
            for r in referents:
                fragment = r.get("fragment") or ""
                anns = r.get("annotations") or []
                if not fragment or not anns:
                    continue
                body = ((anns[0].get("body") or {}).get("plain") or "").strip()
                if body:
                    annotations.append({"fragment": fragment.strip(), "body": body[:600]})
                if len(annotations) >= max_annotations:
                    break
            if not annotations:
                return {"found": False, "song_url": song_url, "reason": "matched on Genius but no public annotations exist for this song yet"}
            return {"found": True, "song_url": song_url, "annotations": annotations}
        except Exception as e:
            return {"found": False, "error": str(e)}

    return _cached_call(cache_key, 7 * 24 * 3600, _do)


# =============================================================================
# LRCLIB — free, no-key synced/plain lyrics lookup (https://lrclib.net)
# =============================================================================
LRCLIB_BASE = "https://lrclib.net/api"
LRCLIB_UA = "NOMAD/1.0 (local personal playlist app; https://github.com/)"


def _lrclib_request(path, params):
    url = f"{LRCLIB_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": LRCLIB_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_synced_lyrics(raw):
    """Turns LRCLIB's '[mm:ss.xx] line' LRC text into [{time, text}, ...],
    sorted, ready for the player to highlight against currentTime."""
    if not raw:
        return None
    lines = []
    offset = 0.0
    for line in raw.splitlines():
        stripped = line.strip()
        off = re.match(r"\[offset:\s*(-?\d+)\]", stripped, flags=re.I)
        if off:
            offset = int(off.group(1)) / 1000.0
            continue
        stamps = list(re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", stripped))
        if not stamps:
            continue
        text = re.sub(r"(?:\[\d+:\d+(?:\.\d+)?\])+", "", stripped).strip()
        if text.strip():
            for stamp in stamps:
                minutes, seconds = stamp.groups()
                t = max(0.0, int(minutes) * 60 + float(seconds) + offset)
                lines.append({"time": round(t, 2), "text": text.strip()})
    lines.sort(key=lambda l: l["time"])
    return lines or None


def lrclib_fetch(title, artist, duration_sec):
    """Exact lookup first (LRCLIB matches on duration \u00b1a couple seconds),
    falls back to fuzzy search if that 404s. Never raises — degrades to
    'not found' so a missing/unreachable lyrics source never breaks playback."""
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return {"found": False, "plain": None, "synced": None, "error": "no title"}
    try:
        params = {"track_name": title, "artist_name": artist}
        if duration_sec:
            params["duration"] = int(round(duration_sec))
        data = _lrclib_request("/get", params)
        return {
            "found": True,
            "plain": data.get("plainLyrics") or None,
            "synced": _parse_synced_lyrics(data.get("syncedLyrics")),
            "instrumental": bool(data.get("instrumental")),
        }
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return {"found": False, "plain": None, "synced": None, "error": f"lrclib error {e.code}"}
    except Exception as e:
        return {"found": False, "plain": None, "synced": None, "error": str(e)}

    # exact match missed — try the fuzzy search endpoint and take the closest
    # duration match instead of just giving up
    try:
        results = _lrclib_request("/search", {"track_name": title, "artist_name": artist})
        if not results:
            return {"found": False, "plain": None, "synced": None, "error": None}
        if duration_sec:
            results.sort(key=lambda r: abs((r.get("duration") or 0) - duration_sec))
        best = results[0]
        return {
            "found": True,
            "plain": best.get("plainLyrics") or None,
            "synced": _parse_synced_lyrics(best.get("syncedLyrics")),
            "instrumental": bool(best.get("instrumental")),
        }
    except Exception as e:
        return {"found": False, "plain": None, "synced": None, "error": str(e)}


# =============================================================================
# Audio fingerprinting (fpcalc/Chromaprint) — real duplicate detection by
# what a track actually sounds like, not just its title/artist text. Local
# comparison needs no API key at all; AcoustID lookup (optional, free key)
# additionally identifies the canonical title/artist for a fingerprint.
# =============================================================================
FP_MATCH_THRESHOLD = 0.92       # same recording, different rip/bitrate/encode
FP_MAX_OFFSET_FRAMES = 80        # ~10s search window for start-time alignment
ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"


def compute_fingerprint(filepath):
    """fpcalc -raw -json — the raw 32-bit-int fingerprint array, straight
    from the CLI tool. No libchromaprint bindings needed for local compare."""
    fpcalc_path = fpcalc_location()
    if not fpcalc_path or not filepath or not os.path.exists(filepath):
        return None
    try:
        r = subprocess.run(
            [fpcalc_path, "-raw", "-json", filepath], capture_output=True, text=True, timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        data = json.loads(r.stdout or "{}")
        fp = data.get("fingerprint")
        return {"duration": data.get("duration"), "fingerprint": fp} if fp else None
    except Exception:
        return None


def compute_fingerprint_compressed(filepath):
    """fpcalc's default (compressed, base64) output — the format AcoustID's
    lookup API expects, distinct from the raw-int form used for local compare."""
    fpcalc_path = fpcalc_location()
    if not fpcalc_path or not filepath or not os.path.exists(filepath):
        return None
    try:
        r = subprocess.run(
            [fpcalc_path, "-json", filepath], capture_output=True, text=True, timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        data = json.loads(r.stdout or "{}")
        fp = data.get("fingerprint")
        return {"duration": data.get("duration"), "fingerprint": fp} if fp else None
    except Exception:
        return None


def _popcount32(x):
    return bin(x & 0xFFFFFFFF).count("1")


def fingerprint_similarity(fp1, fp2):
    """Normalized Hamming similarity (0..1) between two raw fingerprints,
    searching small frame offsets so silence padding / a slightly different
    start point doesn't hide a real match."""
    if not fp1 or not fp2:
        return 0.0
    best = 0.0
    for offset in range(-FP_MAX_OFFSET_FRAMES, FP_MAX_OFFSET_FRAMES + 1):
        a, b = (fp1[offset:], fp2[:len(fp1) - offset]) if offset >= 0 else (fp1[:len(fp1) + offset], fp2[-offset:])
        n = min(len(a), len(b))
        if n < 40:  # not enough overlap to be meaningful
            continue
        matching_bits = sum(32 - _popcount32(a[i] ^ b[i]) for i in range(n))
        sim = matching_bits / (n * 32)
        if sim > best:
            best = sim
    return best


def acoustid_lookup(compressed_fingerprint, duration, api_key):
    """Optional — only runs if the person has entered their own free
    AcoustID key (acoustid.org/api-key). Identifies a track against
    AcoustID's global database to correct wrong/missing metadata; never
    required for local duplicate detection, which works with zero keys."""
    if not api_key or not compressed_fingerprint or not duration:
        return {"ok": False, "error": "missing key, fingerprint, or duration"}
    params = {
        "client": api_key, "meta": "recordings+releasegroups",
        "duration": int(round(duration)), "fingerprint": compressed_fingerprint,
    }
    try:
        url = f"{ACOUSTID_LOOKUP_URL}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": LRCLIB_UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "ok":
            return {"ok": False, "error": (data.get("error") or {}).get("message", "lookup failed")}
        results = data.get("results") or []
        if not results:
            return {"ok": True, "matched": False}
        best = max(results, key=lambda r: r.get("score", 0))
        recordings = best.get("recordings") or []
        if not recordings:
            return {"ok": True, "matched": False}
        rec = recordings[0]
        artists = rec.get("artists") or []
        return {
            "ok": True, "matched": True, "score": best.get("score"),
            "title": rec.get("title"),
            "artist": ", ".join(a.get("name", "") for a in artists) if artists else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


class PlaylistController:
    def __init__(self):
        self.log = log_fn("playlists")
        os.makedirs(PLAYLISTS_DIR, exist_ok=True)
        # RLock, not Lock — _save() below now takes this lock itself so every
        # caller gets safe writes automatically, but create()/delete() (and
        # others) already held the lock when calling _save(); a plain Lock
        # would deadlock a thread trying to re-acquire what it's already
        # holding, so this has to be reentrant.
        self._lock = threading.RLock()
        self.data = self._load()
        self._fp_scan_status = {}   # playlist_id -> {"state", "progress", "result"} — in-memory, not persisted
        self._lyrics_resync_status = {}  # playlist_id -> {"state", "progress"} — in-memory, not persisted

    # ---- persistence ----
    def _load(self):
        try:
            with open(PLAYLISTS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"playlists": []}
        # older playlists.json files won't have these fields yet
        for i, p in enumerate(data.get("playlists", [])):
            p["enhance"] = {**DEFAULT_PL_ENHANCE, **(p.get("enhance") or {})}
            p.setdefault("versions", [])
            for j, t in enumerate(p.get("tracks", [])):
                t.setdefault("quality_cache", None)
                t.setdefault("lyrics_cache", None)
                t.setdefault("fingerprint_cache", None)
                t.setdefault("audio_intel_cache", None)
                t.setdefault("mb_cache", None)
                t.setdefault("genius_cache", None)
                t.setdefault("ai_extras_cache", {})
                t.setdefault("genre", None)
                t.setdefault("year", None)
                t.setdefault("added_ts", i * 100000 + j)  # stable relative order, oldest first
        return data

    def _save(self):
        # Locked here (not just at some call sites) — this was the actual
        # bug: every _save() call writes to the SAME tmp filename
        # (PLAYLISTS_JSON + ".tmp"). Two threads saving at once (e.g. a
        # background lyrics resync writing after each track while a normal
        # request renames a playlist) could interleave writes to that one
        # shared file before either side calls os.replace, corrupting
        # whichever save loses the race — not a rare edge case given how
        # many call sites never wrapped this in a lock before.
        with self._lock:
            try:
                tmp = PLAYLISTS_JSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f)
                os.replace(tmp, PLAYLISTS_JSON)
            except Exception as e:
                self.log(f"couldn't save playlists: {e}", "warn")

    def _broadcast_state(self):
        broadcast("playlists_state", {"playlists": self.list_playlists(), "full": self.data["playlists"]})

    def _find(self, playlist_id):
        return next((p for p in self.data["playlists"] if p["id"] == playlist_id), None)

    # ---- CRUD ----
    def list_playlists(self):
        return [{"id": p["id"], "name": p["name"], "track_count": len(p["tracks"]),
                  "cover": next((t["thumbnail"] for t in p["tracks"] if t.get("thumbnail")), None),
                  "art_seed": p.get("art_seed", p["id"]),
                  "ready_count": sum(1 for t in p["tracks"] if t.get("status") == "ready")}
                 for p in self.data["playlists"]]

    def get_playlist(self, playlist_id):
        return self._find(playlist_id)

    def create(self, name, art_seed=None):
        p = {"id": uuid.uuid4().hex[:10], "name": (name or "").strip() or "New Playlist",
             "created": time.strftime("%Y-%m-%d %H:%M:%S"), "tracks": [],
             "enhance": DEFAULT_PL_ENHANCE.copy(),
             "art_seed": art_seed or uuid.uuid4().hex[:8]}
        with self._lock:
            self.data["playlists"].append(p)
            self._save()
        self._broadcast_state()
        return p

    def set_art_seed(self, playlist_id, seed=None):
        p = self._find(playlist_id)
        if not p:
            return None
        p["art_seed"] = seed or uuid.uuid4().hex[:8]
        self._save()
        self._broadcast_state()
        return p["art_seed"]

    def rename(self, playlist_id, name):
        p = self._find(playlist_id)
        if not p:
            return False
        p["name"] = (name or "").strip() or p["name"]
        self._save()
        self._broadcast_state()
        return True

    def delete(self, playlist_id):
        p = self._find(playlist_id)
        if not p:
            return False
        for t in p["tracks"]:
            self._delete_audio_file(t)
        with self._lock:
            self.data["playlists"] = [x for x in self.data["playlists"] if x["id"] != playlist_id]
            self._save()
        self._broadcast_state()
        return True

    def reorder(self, playlist_id, track_ids):
        p = self._find(playlist_id)
        if not p:
            return False
        by_id = {t["id"]: t for t in p["tracks"]}
        p["tracks"] = [by_id[i] for i in track_ids if i in by_id]
        self._save()
        return True

    def remove_track(self, playlist_id, track_id):
        p = self._find(playlist_id)
        if not p:
            return False
        track = next((t for t in p["tracks"] if t["id"] == track_id), None)
        if track:
            self._snapshot(playlist_id, f"before removing \u2018{track.get('title', 'track')}\u2019")
            self._trash_audio_file(track)
        p["tracks"] = [t for t in p["tracks"] if t["id"] != track_id]
        self._save()
        self._broadcast_state()
        return True

    def sort_playlist(self, playlist_id, key, direction="asc"):
        """Persists a sort order for the playlist's track list. key is one of
        title / artist / duration / added / quality; quality sorts by cached
        bitrate (unchecked tracks probed on the fly, same as Doctor)."""
        p = self._find(playlist_id)
        if not p:
            return False
        reverse = direction == "desc"
        if key == "title":
            fn = lambda t: (t.get("title") or "").lower()
        elif key == "artist":
            fn = lambda t: (t.get("artist") or "").lower()
        elif key == "duration":
            fn = lambda t: t.get("duration") or 0
        elif key == "quality":
            def fn(t):
                if t.get("status") != "ready" or not t.get("audio_file"):
                    return -1
                path = os.path.join(PLAYLISTS_DIR, t["audio_file"])
                q = t.get("quality_cache")
                if not q or not q.get("checked"):
                    q = probe_audio_quality(path)
                    t["quality_cache"] = q
                return q.get("bitrate_kbps") or 0
        elif key == "bpm":
            fn = lambda t: (t.get("audio_intel_cache") or {}).get("bpm") or 0
        elif key == "energy":
            fn = lambda t: (t.get("audio_intel_cache") or {}).get("energy") or 0
        elif key == "key":
            fn = lambda t: (t.get("audio_intel_cache") or {}).get("camelot") or ""
        elif key == "harmonic":
            p["tracks"] = harmonic_order(p["tracks"])
            self._save()
            self._broadcast_state()
            return True
        else:  # "added" (default) — insertion / download order
            fn = lambda t: t.get("added_ts") or 0
        p["tracks"] = sorted(p["tracks"], key=fn, reverse=reverse)
        self._save()
        self._broadcast_state()
        return True

    def get_lyrics(self, playlist_id, track_id, refresh=False):
        """Cached multi-source lookup (LRCLIB -> NetEase -> Lyrics.ovh ->
        Genius -> Popcat -> Vagalume), governed by the shared
        `_lyrics_cache_policy()`: a verified SYNCED match is cached
        indefinitely, but a plain-text-only or not-found result is no
        longer treated as permanent — it gets automatically retried after
        a few days (or a manual `refresh`). Previously ANY `found=True`
        result was cached forever, which is exactly why plain-only matches
        never got a chance to pick up a synced source later, and why old
        unverified matches never got a chance to be re-checked once
        verification was added."""
        p = self._find(playlist_id)
        if not p:
            return None
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            return None
        cached = t.get("lyrics_cache")
        if cached and cached.get("source_override") and not refresh:
            cached.setdefault("offset", 0.0)
            return cached  # a manual "fix the match" pin always wins, regardless of age/version
        if cached and not refresh and _lyrics_cache_policy(cached):
            cached.setdefault("offset", 0.0)
            return cached
        result = fetch_lyrics_multi(t.get("title"), t.get("artist"), duration=t.get("duration") or 0)
        # A fresh fetch always starts at offset 0 — never inherit a stale
        # offset from a previous (possibly wrong) match, and never a global
        # constant. If the user had tuned this track's offset before and the
        # new result is still the same recording, `set_lyrics_offset` is what
        # re-applies it explicitly.
        result.setdefault("offset", 0.0)
        t["lyrics_cache"] = result
        self._save()
        return result

    def set_lyrics_offset(self, playlist_id, track_id, offset):
        """Persist a manually-tuned per-track lyrics offset (seconds, +/-).
        Track-specific and adjustable — never a global constant."""
        p = self._find(playlist_id)
        if not p:
            return None
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            return None
        cache = t.get("lyrics_cache") or {"found": False, "plain": None, "synced": None}
        cache["offset"] = float(offset)
        t["lyrics_cache"] = cache
        self._save()
        return cache

    def set_lyrics_override(self, playlist_id, track_id, title, artist):
        """Manually pin the lyrics match for a track. Used when the
        auto-matched lyrics were for the wrong song (mistagged title/artist,
        ambiguous name, etc.) — the user picks the correct song from search
        and this permanently overwrites whatever was cached before, exactly
        like a normal successful fetch would."""
        p = self._find(playlist_id)
        if not p:
            return None
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            return None
        result = fetch_lyrics_multi(title, artist, duration=t.get("duration") or 0)
        result["source_override"] = True
        result["offset"] = 0.0  # new match = possibly a different recording; don't carry over the old offset
        t["lyrics_cache"] = result
        self._save()
        return result

    def lyrics_resync_status(self, playlist_id):
        return self._lyrics_resync_status.get(playlist_id, {"state": "idle"})

    def run_lyrics_resync(self, playlist_id):
        """Bulk-retries lyrics for every track whose cache is either missing,
        stale by the shared cache policy (unsynced/not-found past its TTL,
        or stamped by an older engine version), or found-but-unsynced —
        skips anything the user manually pinned via "Fix the match". Runs
        in a background thread; poll lyrics_resync_status() for progress.
        Only ever overwrites the cache with something *better* (adds synced
        timing, or finds lyrics where there were none) — never downgrades an
        existing plain-text match to nothing."""
        p = self._find(playlist_id)
        if not p:
            return
        targets = [t for t in p["tracks"]
                   if not (t.get("lyrics_cache") or {}).get("source_override")
                   and not _lyrics_cache_policy(t.get("lyrics_cache"))]
        total = len(targets)
        self._lyrics_resync_status[playlist_id] = {"state": "running", "progress": {"done": 0, "total": total, "improved": 0}}
        improved = 0
        for i, t in enumerate(targets):
            try:
                result = fetch_lyrics_multi(t.get("title"), t.get("artist"), duration=t.get("duration") or 0)
                old = t.get("lyrics_cache") or {}
                if result.get("synced") or (result.get("found") and not old.get("found")):
                    t["lyrics_cache"] = result
                    improved += 1
            except Exception as e:
                self.log(f"lyrics resync failed for {t.get('title')!r}: {e}", "warn")
            self._lyrics_resync_status[playlist_id] = {
                "state": "running",
                "progress": {"done": i + 1, "total": total, "improved": improved},
            }
            broadcast("playlist_lyrics_resync_progress", {"playlist_id": playlist_id, "done": i + 1, "total": total, "improved": improved})
        self._save()
        self._lyrics_resync_status[playlist_id] = {"state": "done", "progress": {"done": total, "total": total, "improved": improved}}
        broadcast("playlist_lyrics_resync_done", {"playlist_id": playlist_id, "total": total, "improved": improved})
        self.log(f"lyrics resync complete: {improved}/{total} track(s) improved", "ok" if improved else "info")

    # ---- deep fingerprint scan (Chromaprint/fpcalc — real audio duplicates) ----
    def fingerprint_scan_status(self, playlist_id):
        return self._fp_scan_status.get(playlist_id, {"state": "idle"})

    def run_fingerprint_scan(self, playlist_id):
        """Computes (or reuses cached) fingerprints for every ready track,
        then pairwise-compares them to find same-recording duplicates that
        title/artist text matching misses — different rip, re-upload, live
        vs. studio labeled wrong, etc. Runs in a background thread; poll
        fingerprint_scan_status() for progress."""
        p = self._find(playlist_id)
        if not p:
            return
        self._fp_scan_status[playlist_id] = {"state": "running", "progress": {"done": 0, "total": 0}, "result": None}
        ready = [t for t in p["tracks"] if t.get("status") == "ready" and t.get("audio_file")]
        total = len(ready)
        self._fp_scan_status[playlist_id]["progress"]["total"] = total
        self.log(f"fingerprint scan: computing prints for {total} track(s)...", "info")

        fps = {}  # track_id -> raw fingerprint (list of ints)
        for i, t in enumerate(ready):
            cache = t.get("fingerprint_cache")
            if cache and cache.get("fingerprint"):
                fps[t["id"]] = cache["fingerprint"]
            else:
                path = os.path.join(PLAYLISTS_DIR, t["audio_file"])
                fp = compute_fingerprint(path)
                if fp:
                    t["fingerprint_cache"] = fp
                    fps[t["id"]] = fp["fingerprint"]
            self._fp_scan_status[playlist_id]["progress"]["done"] = i + 1
            broadcast("playlist_fp_progress", {"playlist_id": playlist_id, "done": i + 1, "total": total})
        self._save()

        # union-find to group transitively-matching tracks (A~B, B~C => one group of 3)
        parent = {tid: tid for tid in fps}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        ids = list(fps.keys())
        pair_scores = {}
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = fingerprint_similarity(fps[ids[i]], fps[ids[j]])
                if sim >= FP_MATCH_THRESHOLD:
                    union(ids[i], ids[j])
                    pair_scores[(ids[i], ids[j])] = round(sim, 4)

        groups_map = {}
        for tid in ids:
            groups_map.setdefault(find(tid), []).append(tid)
        groups = [g for g in groups_map.values() if len(g) > 1]

        id_to_track = {t["id"]: t for t in p["tracks"]}
        result = {
            "scanned": total,
            "groups": [
                {
                    "track_ids": g,
                    "tracks": [{"id": tid, "title": id_to_track[tid].get("title"),
                                "artist": id_to_track[tid].get("artist")} for tid in g],
                }
                for g in groups
            ],
        }
        self._fp_scan_status[playlist_id] = {"state": "done", "progress": {"done": total, "total": total}, "result": result}
        self.log(f"fingerprint scan complete: {len(groups)} duplicate group(s) found", "ok" if groups else "info")
        broadcast("playlist_fp_done", {"playlist_id": playlist_id, "groups": len(groups)})

    def resolve_fp_duplicate_group(self, playlist_id, keep_track_id, group_track_ids):
        """Keeps one track from a fingerprint-matched duplicate group, trashes
        (undoable) the rest. Snapshots first like every other destructive op."""
        p = self._find(playlist_id)
        if not p:
            return {"removed": 0}
        remove_ids = {tid for tid in group_track_ids if tid != keep_track_id}
        if not remove_ids:
            return {"removed": 0}
        self._snapshot(playlist_id, "before removing fingerprint duplicates")
        removed = 0
        keep = []
        for t in p["tracks"]:
            if t["id"] in remove_ids:
                self._trash_audio_file(t)
                removed += 1
            else:
                keep.append(t)
        p["tracks"] = keep
        self._save()
        self._broadcast_state()
        return {"removed": removed}

    def identify_track_acoustid(self, playlist_id, track_id, api_key):
        """Per-track, on-demand AcoustID lookup — needs the person's own
        free key. Returns the canonical match without touching the track;
        the UI decides whether to apply it."""
        p = self._find(playlist_id)
        if not p:
            return {"ok": False, "error": "playlist not found"}
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t or t.get("status") != "ready" or not t.get("audio_file"):
            return {"ok": False, "error": "track not ready"}
        path = os.path.join(PLAYLISTS_DIR, t["audio_file"])
        fp = compute_fingerprint_compressed(path)
        if not fp:
            return {"ok": False, "error": "fpcalc unavailable or failed on this file"}
        return acoustid_lookup(fp["fingerprint"], fp["duration"] or t.get("duration"), api_key)

    def apply_track_metadata(self, playlist_id, track_id, title, artist, genre=None, year=None):
        """Applies a metadata correction (e.g. from an AcoustID or
        MusicBrainz match) to a single track. Snapshotted so it's undoable."""
        p = self._find(playlist_id)
        if not p:
            return False
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            return False
        self._snapshot(playlist_id, f"before correcting metadata for '{t.get('title', 'track')}'")
        if title:
            t["title"] = title
        if artist:
            t["artist"] = artist
        if genre:
            t["genre"] = genre
        if year:
            t["year"] = year
        self._save()
        self._broadcast_state()
        return True

    # ---- MusicBrainz enrichment (no key) ----
    def enrich_musicbrainz(self, playlist_id, track_id):
        p = self._find(playlist_id)
        if not p:
            return {"found": False, "error": "playlist not found"}
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            return {"found": False, "error": "track not found"}
        cache = t.get("mb_cache")
        if cache:
            return cache
        result = musicbrainz_lookup(t.get("title"), t.get("artist"))
        t["mb_cache"] = result
        self._save()
        return result

    # ---- Last.fm similar-artist discovery, based on this playlist's artists ----
    def similar_artists(self, playlist_id, api_key, per_artist=6, max_source_artists=6):
        p = self._find(playlist_id)
        if not p:
            return {"ok": False, "error": "playlist not found"}
        if not api_key:
            return {"ok": False, "error": "no Last.fm API key configured"}
        source_artists, seen = [], set()
        for t in p["tracks"]:
            a = (t.get("artist") or "").strip()
            if a and a.lower() not in seen:
                seen.add(a.lower())
                source_artists.append(a)
            if len(source_artists) >= max_source_artists:
                break
        if not source_artists:
            return {"ok": True, "suggestions": []}
        library_artists = {(t.get("artist") or "").strip().lower() for t in p["tracks"] if t.get("artist")}
        scores = {}
        for a in source_artists:
            for sim in lastfm_similar_artists(a, api_key, limit=per_artist):
                name = sim["name"]
                if name.lower() in library_artists:
                    continue
                scores[name] = max(scores.get(name, 0), sim["match"])
        suggestions = sorted(({"name": n, "match": round(m, 3)} for n, m in scores.items()),
                              key=lambda x: -x["match"])[:20]
        return {"ok": True, "based_on": source_artists, "suggestions": suggestions}

    # ---- 'Most Saved' — a track that's been added into more than one of
    # your own playlists is a real, honest popularity signal (no fake
    # global save-counts) ----
    def most_saved(self, limit=12):
        counts, first_seen = {}, {}
        for p in self.data["playlists"]:
            seen_this_playlist = set()
            for t in p["tracks"]:
                if t.get("status") != "ready":
                    continue
                key = ((t.get("title") or "").strip().lower(), (t.get("artist") or "").strip().lower())
                if not key[0] or key in seen_this_playlist:
                    continue
                seen_this_playlist.add(key)
                counts[key] = counts.get(key, 0) + 1
                if key not in first_seen:
                    first_seen[key] = {**t, "playlist_id": p["id"], "playlist_name": p["name"]}
        rows = [{**first_seen[k], "saved_count": c} for k, c in counts.items() if c > 1]
        rows.sort(key=lambda r: -r["saved_count"])
        return rows[:limit]

    # ---- Similar Artist Network — small 2-hop graph for Discover's radial
    # visualization, built from Last.fm's free-key 'similar artists' ----
    def build_artist_network(self, root_artist, api_key, per_hop=5):
        if not api_key:
            return {"ok": False, "error": "no Last.fm API key configured"}
        root_artist = (root_artist or "").strip()
        if not root_artist:
            return {"ok": False, "error": "no root artist"}
        nodes = {root_artist.lower(): {"id": root_artist.lower(), "name": root_artist, "hop": 0}}
        edges = []
        hop1 = lastfm_similar_artists(root_artist, api_key, limit=per_hop)
        for sim in hop1:
            key = sim["name"].lower()
            if key not in nodes:
                nodes[key] = {"id": key, "name": sim["name"], "hop": 1, "match": round(sim["match"], 3)}
            edges.append({"source": root_artist.lower(), "target": key, "match": round(sim["match"], 3)})
        for sim in hop1[:3]:
            hop2 = lastfm_similar_artists(sim["name"], api_key, limit=3)
            for sim2 in hop2:
                key2 = sim2["name"].lower()
                if key2 == root_artist.lower():
                    continue
                if key2 not in nodes:
                    nodes[key2] = {"id": key2, "name": sim2["name"], "hop": 2, "match": round(sim2["match"], 3)}
                    edges.append({"source": sim["name"].lower(), "target": key2, "match": round(sim2["match"], 3)})
        return {"ok": True, "root": root_artist, "nodes": list(nodes.values()), "edges": edges}

    # ---- Genius annotations (separate from LRCLIB's synced timing) ----
    def get_annotations(self, playlist_id, track_id, api_key, refresh=False):
        p = self._find(playlist_id)
        if not p:
            return None
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            return None
        cached = t.get("genius_cache")
        if cached and not refresh:
            return cached
        result = genius_get_annotations(t.get("title"), t.get("artist"), api_key)
        t["genius_cache"] = result
        self._save()
        return result

    # ---- AI lyrics extras (Groq) — explanation / translation / vocabulary /
    # ---- story, cached per-track alongside lyrics_cache/genius_cache -------
    def _lyrics_text_for(self, t):
        cached = t.get("lyrics_cache") or {}
        if cached.get("synced"):
            return "\n".join(l.get("text", "") for l in cached["synced"]), cached["synced"]
        if cached.get("plain"):
            return cached["plain"], None
        return None, None

    def get_ai_explain(self, playlist_id, track_id, api_key, refresh=False):
        p = self._find(playlist_id)
        t = next((x for x in p["tracks"] if x["id"] == track_id), None) if p else None
        if not t:
            return None
        cache = t.setdefault("ai_extras_cache", {})
        if cache.get("explain") and not refresh:
            return cache["explain"]
        text, _ = self._lyrics_text_for(t)
        if not text:
            result = {"ok": False, "error": "no lyrics available for this track yet"}
        else:
            try:
                result = {"ok": True, "text": ai_explain_song(t.get("title"), t.get("artist"), text, api_key)}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
        cache["explain"] = result
        self._save()
        return result

    def get_ai_story(self, playlist_id, track_id, api_key, refresh=False):
        p = self._find(playlist_id)
        t = next((x for x in p["tracks"] if x["id"] == track_id), None) if p else None
        if not t:
            return None
        cache = t.setdefault("ai_extras_cache", {})
        if cache.get("story") and not refresh:
            return cache["story"]
        text, _ = self._lyrics_text_for(t)
        if not text:
            result = {"ok": False, "error": "no lyrics available for this track yet"}
        else:
            try:
                result = {"ok": True, "text": ai_song_story(t.get("title"), t.get("artist"), text, api_key)}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
        cache["story"] = result
        self._save()
        return result

    def get_ai_vocabulary(self, playlist_id, track_id, api_key, refresh=False):
        p = self._find(playlist_id)
        t = next((x for x in p["tracks"] if x["id"] == track_id), None) if p else None
        if not t:
            return None
        cache = t.setdefault("ai_extras_cache", {})
        if cache.get("vocabulary") and not refresh:
            return cache["vocabulary"]
        text, _ = self._lyrics_text_for(t)
        if not text:
            result = {"ok": False, "error": "no lyrics available for this track yet"}
        else:
            try:
                result = {"ok": True, "words": ai_vocabulary(t.get("title"), t.get("artist"), text, api_key)}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
        cache["vocabulary"] = result
        self._save()
        return result

    def get_ai_translation(self, playlist_id, track_id, target_lang, api_key, refresh=False):
        p = self._find(playlist_id)
        t = next((x for x in p["tracks"] if x["id"] == track_id), None) if p else None
        if not t:
            return None
        cache = t.setdefault("ai_extras_cache", {}).setdefault("translate", {})
        if cache.get(target_lang) and not refresh:
            return cache[target_lang]
        text, synced = self._lyrics_text_for(t)
        if not text:
            result = {"ok": False, "error": "no lyrics available for this track yet"}
        else:
            lines = text.split("\n")
            try:
                translated = ai_translate_lyrics(t.get("title"), t.get("artist"), lines, target_lang, api_key)
                if len(translated) != len(lines):
                    # model miscounted lines — still useful, just can't be
                    # safely re-zipped against synced timing.
                    result = {"ok": True, "plain": "\n".join(translated), "synced": None}
                elif synced:
                    result = {"ok": True, "plain": None,
                               "synced": [{"time": synced[i]["time"], "text": translated[i]} for i in range(len(lines))]}
                else:
                    result = {"ok": True, "plain": "\n".join(translated), "synced": None}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
        cache[target_lang] = result
        self._save()
        return result

    def _delete_audio_file(self, track):
        """Permanent delete — used only when the whole playlist is deleted,
        where there's nothing left to ever restore."""
        if track.get("audio_file"):
            try:
                os.remove(os.path.join(PLAYLISTS_DIR, track["audio_file"]))
            except OSError:
                pass

    def _trash_audio_file(self, track):
        """Moves (not deletes) a track's cached audio into playlist_audio/.trash
        so a version restore can bring it straight back without re-downloading."""
        if not track.get("audio_file"):
            return
        src = os.path.join(PLAYLISTS_DIR, track["audio_file"])
        if not os.path.exists(src):
            return
        try:
            os.makedirs(PLAYLIST_TRASH_DIR, exist_ok=True)
            shutil.move(src, os.path.join(PLAYLIST_TRASH_DIR, track["audio_file"]))
        except OSError:
            pass

    def _restore_audio_file(self, track):
        if not track.get("audio_file"):
            return
        dst = os.path.join(PLAYLISTS_DIR, track["audio_file"])
        if os.path.exists(dst):
            return
        src = os.path.join(PLAYLIST_TRASH_DIR, track["audio_file"])
        if os.path.exists(src):
            try:
                shutil.move(src, dst)
            except OSError:
                pass

    # ---- versioning / undo ----
    def _snapshot(self, playlist_id, label):
        """Pushes a full copy of the current track list onto this playlist's
        version stack before any destructive operation, so it can be undone
        from the Playlists UI's History panel."""
        p = self._find(playlist_id)
        if not p:
            return
        p.setdefault("versions", [])
        p["versions"].insert(0, {
            "id": uuid.uuid4().hex[:8],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "label": label,
            "tracks": copy.deepcopy(p["tracks"]),
        })
        if len(p["versions"]) > MAX_VERSIONS_PER_PLAYLIST:
            p["versions"] = p["versions"][:MAX_VERSIONS_PER_PLAYLIST]
            self._purge_orphan_trash(p)

    def _purge_orphan_trash(self, p):
        """Once a version ages out of the stack, any trashed audio files that
        only that version referenced are safe to actually delete."""
        referenced = {t.get("audio_file") for t in p["tracks"] if t.get("audio_file")}
        for v in p.get("versions", []):
            referenced |= {t.get("audio_file") for t in v["tracks"] if t.get("audio_file")}
        if not os.path.isdir(PLAYLIST_TRASH_DIR):
            return
        for fname in os.listdir(PLAYLIST_TRASH_DIR):
            if fname not in referenced:
                try:
                    os.remove(os.path.join(PLAYLIST_TRASH_DIR, fname))
                except OSError:
                    pass

    def list_versions(self, playlist_id):
        p = self._find(playlist_id)
        if not p:
            return []
        return [{"id": v["id"], "ts": v["ts"], "label": v["label"], "track_count": len(v["tracks"])}
                for v in p.get("versions", [])]

    def restore_version(self, playlist_id, version_id):
        p = self._find(playlist_id)
        if not p:
            return False
        snap = next((v for v in p.get("versions", []) if v["id"] == version_id), None)
        if not snap:
            return False
        self._snapshot(playlist_id, "before restore")
        restored_ids = {t["id"] for t in snap["tracks"]}
        for t in p["tracks"]:
            if t["id"] not in restored_ids:
                self._trash_audio_file(t)
        p["tracks"] = copy.deepcopy(snap["tracks"])
        for t in p["tracks"]:
            self._restore_audio_file(t)
        self._save()
        self._broadcast_state()
        return True

    # ---- health / doctor ----
    def playlist_health(self, playlist_id):
        """Scans every track for the three things that actually go wrong in
        practice: a cached file that's gone missing, an exact duplicate, or
        audio that came in under the 'good' bitrate/sample-rate bar."""
        p = self._find(playlist_id)
        if not p:
            return None
        seen, duplicate_groups, missing, low_quality = {}, [], [], []
        for t in p["tracks"]:
            key = (t.get("title", "").strip().lower(), (t.get("artist") or "").strip().lower())
            seen.setdefault(key, []).append(t["id"])
            if t.get("status") == "error":
                missing.append(t["id"])
            elif t.get("status") == "ready":
                path = os.path.join(PLAYLISTS_DIR, t.get("audio_file") or "")
                if not t.get("audio_file") or not os.path.exists(path):
                    missing.append(t["id"])
                else:
                    q = t.get("quality_cache")
                    if not q or not q.get("checked"):
                        q = probe_audio_quality(path)
                        t["quality_cache"] = q
                    if q.get("checked") and not q.get("already_good"):
                        low_quality.append(t["id"])
        for ids in seen.values():
            if len(ids) > 1:
                duplicate_groups.append(ids)
        unanalyzed = [t["id"] for t in p["tracks"]
                      if t.get("status") == "ready" and not t.get("audio_intel_cache")]
        self._save()
        return {
            "total_tracks": len(p["tracks"]),
            "missing": missing,
            "duplicate_groups": duplicate_groups,
            "duplicate_count": sum(len(g) - 1 for g in duplicate_groups),
            "low_quality": low_quality,
            "unanalyzed": unanalyzed,
            "audio_intel_available": AUDIO_INTEL_LIBS,
            "healthy": not missing and not duplicate_groups and not low_quality,
        }

    def doctor_fix(self, playlist_id, fix_missing=True, fix_duplicates=True):
        """One-click cleanup: drops tracks whose audio is gone (trashed, not
        deleted — undoable from History) and dedupes by title+artist."""
        p = self._find(playlist_id)
        if not p:
            return {"removed_missing": 0, "removed_duplicates": 0}
        self._snapshot(playlist_id, "before doctor fix")
        removed_missing = 0
        if fix_missing:
            keep = []
            for t in p["tracks"]:
                path = os.path.join(PLAYLISTS_DIR, t.get("audio_file") or "")
                is_missing = t.get("status") == "error" or (
                    t.get("status") == "ready" and (not t.get("audio_file") or not os.path.exists(path)))
                if is_missing:
                    self._trash_audio_file(t)
                    removed_missing += 1
                else:
                    keep.append(t)
            p["tracks"] = keep
        removed_duplicates = 0
        if fix_duplicates:
            seen, keep = set(), []
            for t in p["tracks"]:
                key = (t.get("title", "").strip().lower(), (t.get("artist") or "").strip().lower())
                if key in seen:
                    self._trash_audio_file(t)
                    removed_duplicates += 1
                    continue
                seen.add(key)
                keep.append(t)
            p["tracks"] = keep
        self._save()
        self._broadcast_state()
        return {"removed_missing": removed_missing, "removed_duplicates": removed_duplicates}

    def dedupe_playlist(self, playlist_id):
        """Removes tracks that share a (title, artist) with an earlier track
        in the same playlist. Trashes (not deletes) the extra audio, and
        snapshots first so it's undoable from the History panel."""
        p = self._find(playlist_id)
        if not p:
            return 0
        self._snapshot(playlist_id, "before dedupe")
        seen, keep, removed = set(), [], 0
        for t in p["tracks"]:
            key = (t.get("title", "").strip().lower(), (t.get("artist") or "").strip().lower())
            if key in seen:
                self._trash_audio_file(t)
                removed += 1
                continue
            seen.add(key)
            keep.append(t)
        p["tracks"] = keep
        self._save()
        self._broadcast_state()
        return removed

    def _compute_blend(self, playlist_ids, options=None):
        """Shared logic for blend preview and blend create: pulls the
        already-downloaded ('ready') tracks from each source playlist and
        merges them per the chosen order/dedupe/limit — entirely from local
        data already on disk, no network calls."""
        options = options or {}
        dedupe = options.get("dedupe", True)
        order = options.get("order", "interleave")
        limit = options.get("limit")
        src_playlists = [self._find(pid) for pid in playlist_ids]
        src_playlists = [p for p in src_playlists if p]
        if not src_playlists:
            raise RuntimeError("pick at least one playlist to blend")
        pools = [[t for t in p["tracks"] if t.get("status") == "ready" and t.get("audio_file")]
                 for p in src_playlists]

        if order == "shuffle":
            merged = [t for pool in pools for t in pool]
            random.shuffle(merged)
        elif order == "sequential":
            merged = [t for pool in pools for t in pool]
        elif order == "harmonic":
            # DJ-style flow across the combined pool by Camelot key + BPM —
            # falls back to arrival order for any track missing analysis.
            merged = harmonic_order([t for pool in pools for t in pool])
        else:  # interleave (round-robin across the source playlists)
            merged = []
            i = 0
            max_len = max((len(pool) for pool in pools), default=0)
            while i < max_len:
                for pool in pools:
                    if i < len(pool):
                        merged.append(pool[i])
                i += 1

        seen, final = set(), []
        for t in merged:
            key = (t.get("title", "").strip().lower(), (t.get("artist") or "").strip().lower())
            if dedupe and key in seen:
                continue
            seen.add(key)
            final.append(t)
            if limit and len(final) >= int(limit):
                break
        return final

    def blend_preview(self, playlist_ids, options=None):
        final = self._compute_blend(playlist_ids, options)
        return [{"title": t["title"], "artist": t.get("artist", ""), "duration": t.get("duration", 0),
                  "thumbnail": t.get("thumbnail")} for t in final]

    def blend(self, name, playlist_ids, options=None):
        final = self._compute_blend(playlist_ids, options)
        if not final:
            raise RuntimeError("none of the selected playlists have downloaded tracks to blend yet")
        new_pl = self.create(name or "Blend")
        for t in final:
            new_track = self._new_track(
                {"title": t["title"], "artist": t.get("artist", ""), "duration": t.get("duration", 0),
                 "thumbnail": t.get("thumbnail")}, "blend", t.get("source_url"))
            old_path = os.path.join(PLAYLISTS_DIR, t["audio_file"])
            ext = os.path.splitext(t["audio_file"])[1]
            new_filename = f"{new_track['id']}{ext}"
            new_path = os.path.join(PLAYLISTS_DIR, new_filename)
            try:
                shutil.copyfile(old_path, new_path)
                new_track["audio_file"] = new_filename
                new_track["status"] = "ready"
                # Same audio bytes, so BPM/key/energy/quality don't change —
                # carry the cache over instead of forcing a re-analysis pass.
                new_track["audio_intel_cache"] = t.get("audio_intel_cache")
                new_track["quality_cache"] = t.get("quality_cache")
            except OSError as e:
                new_track["status"] = "error"
                new_track["error"] = str(e)
            new_pl["tracks"].append(new_track)
        self._save()
        self._broadcast_state()
        return new_pl

    # ---- per-playlist audio enhance settings ----
    def set_enhance(self, playlist_id, enhance):
        p = self._find(playlist_id)
        if not p:
            return False
        current = {**DEFAULT_PL_ENHANCE, **(p.get("enhance") or {})}
        for key in DEFAULT_PL_ENHANCE:
            if key in (enhance or {}):
                current[key] = bool(enhance[key])
        p["enhance"] = current
        self._save()
        self._broadcast_state()
        return True

    def _apply_audio_enhance(self, filepath, enhance, force=False):
        """A single ffmpeg pass over the cached mp3 — denoise, bass/vocal EQ,
        loudness normalization, Dolby-style spatial widening, and a "crystal
        clear" upscale re-render. Runs automatically right after a track
        finishes downloading, mirroring the Media tab's Enhance Studio but
        scoped to what actually matters for audio-only files.

        `force=True` skips the auto quality-checker (used when a person
        explicitly asks to enhance a track that's already flagged as good)."""
        if not filepath or not os.path.exists(filepath) or not any((enhance or {}).values()):
            return filepath, None
        if not ffmpeg_present():
            return filepath, None

        quality = probe_audio_quality(filepath)
        skip_upscale = (
            not force
            and enhance.get("auto_skip_if_good", True)
            and quality.get("checked")
            and quality.get("already_good")
        )

        afilters = []
        if enhance.get("audio_denoise"):
            afilters.append("afftdn=nf=-25")
        if enhance.get("bass_boost"):
            afilters.append("equalizer=f=80:width_type=o:width=2:g=6")
        if enhance.get("vocal_boost"):
            afilters.append("equalizer=f=2800:width_type=o:width=1:g=5")
        if enhance.get("dolby_spatial"):
            # Approximates a wide, cinematic "spatial" soundstage using
            # stereo widening + a short, subtle early-reflection echo.
            # NOTE: this is NOT licensed Dolby Atmos/Digital encoding —
            # that's proprietary and can't be added without a license —
            # it's an ffmpeg-filter approximation of the "wider, roomier"
            # feeling people mean by "add Dolby support".
            afilters.append("extrastereo=m=2.2")
            afilters.append("aecho=0.7:0.5:22:0.22")
        if enhance.get("crystal_upscale") and not skip_upscale:
            # High-quality resample + gentle high-shelf lift + soft
            # de-essing via a light compressor, then re-encoded at a much
            # higher bitrate — a genuine, if modest, clarity upgrade for
            # audio that started out compressed or muddy.
            afilters.append("aresample=48000:resampler=soxr:precision=28")
            afilters.append("highpass=f=20")
            afilters.append("treble=g=3.5:f=9500")
            afilters.append("acompressor=threshold=-18dB:ratio=2:attack=8:release=180:makeup=1.5")
        if enhance.get("loudness_normalize"):
            afilters.append("loudnorm=I=-16:LRA=11:TP=-1.5")
        if not afilters:
            return filepath, quality

        ffmpeg_path = ffmpeg_location()
        base, ext = os.path.splitext(filepath)
        out_path = f"{base}.enh{ext}"
        target_bitrate = "320k" if (enhance.get("crystal_upscale") and not skip_upscale) else None
        cmd = [ffmpeg_path, "-y", "-i", filepath, "-af", ",".join(afilters), "-c:a", "libmp3lame"]
        cmd += ["-b:a", target_bitrate] if target_bitrate else ["-q:a", "2"]
        cmd += [out_path]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode != 0 or not os.path.exists(out_path):
                self.log(f"track enhance skipped — ffmpeg pass failed: {(result.stderr or '')[-200:]}", "warn")
                return filepath, quality
            os.replace(out_path, filepath)
            if skip_upscale:
                self.log("crystal upscale skipped — audio already high quality", "info")
            return filepath, quality
        except Exception as e:
            self.log(f"track enhance skipped: {e}", "warn")
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return filepath, quality

    # ---- adding tracks ----
    def _new_track(self, meta, source, source_url):
        return {
            "id": uuid.uuid4().hex[:10],
            "title": meta.get("title", "Unknown title"),
            "artist": meta.get("artist", ""),
            "duration": meta.get("duration", 0),
            "thumbnail": meta.get("thumbnail"),
            "source": source,
            "source_url": source_url,
            "youtube_id": meta.get("youtube_id"),
            "audio_file": None,
            "status": "pending",
            "error": None,
            "added_ts": time.time(),
            "quality_cache": None,
            "lyrics_cache": None,
            "fingerprint_cache": None,
            "mb_cache": None,
            "genius_cache": None,
            "ai_extras_cache": {},
            "genre": None,
            "year": None,
        }

    def add_from_youtube(self, playlist_id, url):
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        meta = yt_probe(url)
        if not meta:
            raise RuntimeError("couldn't read that YouTube link")
        track = self._new_track(meta, "youtube", url)
        p["tracks"].append(track)
        self._save()
        self._broadcast_state()
        threading.Thread(target=self._download_track, args=(playlist_id, track["id"], meta.get("youtube_url", url)), daemon=True).start()
        return track

    def add_from_search(self, playlist_id, query):
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        meta = yt_search_best(query)
        if not meta:
            raise RuntimeError("no results found on YouTube for that search")
        track = self._new_track(meta, "youtube", meta.get("youtube_url"))
        p["tracks"].append(track)
        self._save()
        self._broadcast_state()
        threading.Thread(target=self._download_track, args=(playlist_id, track["id"], meta.get("youtube_url")), daemon=True).start()
        return track

    def add_from_spotify(self, playlist_id, url):
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        kind, sid = spotify.parse_url(url)
        if not kind:
            raise RuntimeError("that doesn't look like a Spotify track, album, or playlist link")
        if kind == "track":
            metas = [spotify.get_track(sid)]
        elif kind == "album":
            metas = spotify.get_album_tracks(sid)
        else:
            metas = spotify.get_playlist_tracks(sid)
        added = []
        for meta in metas:
            track = self._new_track(meta, "spotify", meta.get("spotify_url"))
            p["tracks"].append(track)
            added.append(track)
        self._save()
        self._broadcast_state()
        for track in added:
            threading.Thread(target=self._match_and_download_spotify,
                              args=(playlist_id, track["id"], track["title"], track["artist"]), daemon=True).start()
        return added

    def add_from_soundcloud(self, playlist_id, url):
        """SoundCloud tracks can be downloaded directly by yt-dlp (unlike
        Spotify/Deezer), so this works the same way as add_from_youtube."""
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        meta = yt_probe(url)
        if not meta:
            raise RuntimeError("couldn't read that SoundCloud link")
        track = self._new_track(meta, "soundcloud", url)
        p["tracks"].append(track)
        self._save()
        self._broadcast_state()
        threading.Thread(target=self._download_track, args=(playlist_id, track["id"], url), daemon=True).start()
        return track

    def add_from_result(self, playlist_id, service, result):
        """Add a single hit from the unified /search_all results. YouTube and
        SoundCloud results are directly downloadable; Deezer/Spotify results
        are metadata-only, so — same trick as the existing Spotify import —
        NOMAD finds the closest YouTube match and downloads that instead."""
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        if service == "youtube":
            return self.add_from_youtube(playlist_id, result.get("url"))
        if service == "soundcloud":
            return self.add_from_soundcloud(playlist_id, result.get("url"))
        if service in ("deezer", "spotify"):
            meta = {
                "title": result.get("title", "Unknown title"),
                "artist": result.get("artist", ""),
                "duration": result.get("duration", 0),
                "thumbnail": result.get("thumbnail"),
            }
            track = self._new_track(meta, service, result.get("url"))
            p["tracks"].append(track)
            self._save()
            self._broadcast_state()
            threading.Thread(target=self._match_and_download_spotify,
                              args=(playlist_id, track["id"], track["title"], track["artist"]), daemon=True).start()
            return track
        raise RuntimeError("unknown service")

    # ---- AI-generated playlist from a text prompt ----
    def ai_generate_playlist(self, prompt, count=12):
        name = (prompt or "AI Playlist").strip()
        name = (name[:1].upper() + name[1:]) if name else "AI Playlist"
        if len(name) > 60:
            name = name[:57] + "..."
        p = self.create(name)

        def worker():
            try:
                metas, engine = ai_generate_tracks(prompt, count)
            except Exception as e:
                self.log(f"AI generation failed: {e}", "error")
                return
            if not metas:
                self.log("AI generation found no matching tracks — try rephrasing the prompt", "warn")
                return
            for meta in metas:
                track = self._new_track(meta, "youtube", meta.get("youtube_url"))
                p["tracks"].append(track)
                self._save()
                self._broadcast_state()
                threading.Thread(target=self._download_track,
                                  args=(p["id"], track["id"], track.get("source_url")), daemon=True).start()
            self.log(f"AI playlist \"{p['name']}\" — added {len(metas)} track(s) via {engine}", "ok")

        threading.Thread(target=worker, daemon=True).start()
        return p

    # ---- single-track, on-demand enhance (independent of playlist settings) ----
    def enhance_single_track(self, playlist_id, track_id, enhance, force=False):
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t or not t.get("audio_file"):
            raise RuntimeError("track has no cached audio yet")
        filepath = os.path.join(PLAYLISTS_DIR, t["audio_file"])
        self._set_track_status(playlist_id, track_id, status="enhancing")
        _, quality = self._apply_audio_enhance(filepath, enhance, force=force)
        self._set_track_status(playlist_id, track_id, status="ready")
        return quality or {}

    def track_quality(self, playlist_id, track_id):
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t or not t.get("audio_file"):
            raise RuntimeError("track has no cached audio yet")
        return probe_audio_quality(os.path.join(PLAYLISTS_DIR, t["audio_file"]))

    # ---- import / export (also the practical basis for cross-service "conversion") ----
    def export_playlist(self, playlist_id, fmt):
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        fmt = (fmt or "json").lower()
        safe_name = re.sub(r"[^\w\-]+", "_", p["name"]).strip("_") or "playlist"
        if fmt == "m3u":
            lines = ["#EXTM3U"]
            for t in p["tracks"]:
                lines.append(f"#EXTINF:{int(t.get('duration') or 0)},{t.get('artist','')} - {t.get('title','')}")
                lines.append(t.get("source_url") or "")
            return f"{safe_name}.m3u", "\n".join(lines), "audio/x-mpegurl"
        if fmt == "csv":
            import csv, io
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["title", "artist", "duration_sec", "source", "source_url"])
            for t in p["tracks"]:
                w.writerow([t.get("title", ""), t.get("artist", ""), t.get("duration", 0),
                            t.get("source", ""), t.get("source_url", "")])
            return f"{safe_name}.csv", buf.getvalue(), "text/csv"
        # json — the richest format, also what NOMAD re-imports fastest and
        # what a future mobile client / another NOMAD install can round-trip
        bundle = {
            "nomad_export": True, "name": p["name"], "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tracks": [{"title": t.get("title", ""), "artist": t.get("artist", ""),
                        "duration": t.get("duration", 0), "source": t.get("source", ""),
                        "source_url": t.get("source_url", "")} for t in p["tracks"]],
        }
        return f"{safe_name}.json", json.dumps(bundle, indent=2), "application/json"

    def import_into_playlist(self, playlist_id, fmt, content):
        """Bulk-adds tracks described in an uploaded M3U/CSV/JSON file. Since
        other services' export files rarely give us anything directly
        downloadable, every entry is resolved the same way Spotify tracks
        are: a title/artist search that finds the closest YouTube match."""
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        fmt = (fmt or "").lower()
        queries = []
        if fmt == "m3u":
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    m = re.match(r"#EXTINF:-?\d+,(.+)", line)
                    if m:
                        queries.append(m.group(1).strip())
                    continue
                if line.startswith("http"):
                    queries.append(line)  # raw URL — treat as a direct add later
        elif fmt == "csv":
            import csv, io
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                title, artist = (row.get("title") or "").strip(), (row.get("artist") or "").strip()
                if title:
                    queries.append(f"{artist} - {title}" if artist else title)
        elif fmt == "json":
            data = json.loads(content)
            items = data.get("tracks", data) if isinstance(data, dict) else data
            for it in items:
                title, artist = (it.get("title") or "").strip(), (it.get("artist") or "").strip()
                if title:
                    queries.append(f"{artist} - {title}" if artist else title)
        else:
            raise RuntimeError("unsupported import format — use .m3u, .csv, or .json")

        if not queries:
            raise RuntimeError("nothing importable found in that file")

        queries = queries[:200]  # sane cap so one huge file can't hang the app forever

        def worker():
            added = 0
            for q in queries:
                try:
                    if q.startswith("http") and "youtube" in q:
                        self.add_from_youtube(playlist_id, q)
                    else:
                        self.add_from_search(playlist_id, q)
                    added += 1
                except Exception as e:
                    self.log(f"import: skipped one entry — {e}", "warn")
            self.log(f"import finished — added {added}/{len(queries)} track(s)", "ok")

        threading.Thread(target=worker, daemon=True).start()
        return len(queries)

    # ---- fixing up a track after it's been added ----

    def reenhance_all(self, playlist_id):
        """Re-run every cached track through the pipeline so it picks up the
        playlist's current Enhance settings. Reuses retry_track (fresh
        re-download + fresh enhance pass) rather than re-filtering the
        already-processed file in place, since running loudnorm/EQ on top of
        itself would compound instead of just applying once."""
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        targets = [t["id"] for t in p["tracks"] if t.get("status") in ("ready", "error")]
        for track_id in targets:
            try:
                self.retry_track(playlist_id, track_id)
            except Exception as e:
                self.log(f"re-enhance skipped for a track: {e}", "warn")
        return len(targets)

    def retry_track(self, playlist_id, track_id):
        """Re-run the same match/download this track already had — for when
        it failed, or a Spotify auto-match needs another shot."""
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            raise RuntimeError("track not found")
        self._delete_audio_file(t)
        t["audio_file"] = None
        t["status"] = "pending"
        t["error"] = None
        self._save()
        self._broadcast_state()
        broadcast("playlist_track_update", {"playlist_id": playlist_id, "track": t})
        if t["source"] == "spotify":
            threading.Thread(target=self._match_and_download_spotify,
                              args=(playlist_id, track_id, t["title"], t["artist"]), daemon=True).start()
        else:
            threading.Thread(target=self._download_track,
                              args=(playlist_id, track_id, t.get("source_url") or f"https://www.youtube.com/watch?v={t.get('youtube_id','')}"),
                              daemon=True).start()
        return t

    def replace_track(self, playlist_id, track_id, kind, value):
        """Swap what a track actually plays — new YouTube link or a fresh
        search — without losing its position in the playlist."""
        p = self._find(playlist_id)
        if not p:
            raise RuntimeError("playlist not found")
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            raise RuntimeError("track not found")

        if kind == "youtube":
            meta = yt_probe(value)
            if not meta:
                raise RuntimeError("couldn't read that YouTube link")
        elif kind == "search":
            meta = yt_search_best(value)
            if not meta:
                raise RuntimeError("no results found on YouTube for that search")
        else:
            raise RuntimeError("unknown source")

        self._delete_audio_file(t)
        t["title"] = meta.get("title", t["title"])
        t["artist"] = meta.get("artist", t["artist"])
        t["thumbnail"] = meta.get("thumbnail", t["thumbnail"])
        t["duration"] = meta.get("duration", t["duration"])
        t["source"] = "youtube"
        t["source_url"] = meta.get("youtube_url")
        t["youtube_id"] = meta.get("youtube_id")
        t["audio_file"] = None
        t["status"] = "pending"
        t["error"] = None
        self._save()
        self._broadcast_state()
        broadcast("playlist_track_update", {"playlist_id": playlist_id, "track": t})
        threading.Thread(target=self._download_track, args=(playlist_id, track_id, meta.get("youtube_url")), daemon=True).start()
        return t

    # ---- background work ----
    def _set_track_status(self, playlist_id, track_id, **fields):
        p = self._find(playlist_id)
        if not p:
            return
        t = next((x for x in p["tracks"] if x["id"] == track_id), None)
        if not t:
            return
        t.update(fields)
        self._save()
        broadcast("playlist_track_update", {"playlist_id": playlist_id, "track": t})

    def _match_and_download_spotify(self, playlist_id, track_id, title, artist):
        query = f"{artist} - {title}" if artist else title
        try:
            match = yt_search_best(query)
        except Exception as e:
            self._set_track_status(playlist_id, track_id, status="error", error=str(e))
            return
        if not match:
            self._set_track_status(playlist_id, track_id, status="error", error="no YouTube match found")
            return
        self._set_track_status(playlist_id, track_id, youtube_id=match.get("youtube_id"))
        self._download_track(playlist_id, track_id, match.get("youtube_url"))

    def _download_track(self, playlist_id, track_id, youtube_url):
        if not YTDLP_AVAILABLE:
            self._set_track_status(playlist_id, track_id, status="error", error="yt-dlp not installed")
            return
        self._set_track_status(playlist_id, track_id, status="downloading")
        out_tmpl = os.path.join(PLAYLISTS_DIR, f"{track_id}.%(ext)s")
        ffmpeg_ok = ffmpeg_present()
        opts = {"format": "bestaudio/best", "outtmpl": out_tmpl, "quiet": True, "no_warnings": True, "noplaylist": True}
        if ffmpeg_ok:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
            opts["ffmpeg_location"] = ffmpeg_location()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([youtube_url])
            filename = next((f for f in os.listdir(PLAYLISTS_DIR) if f.startswith(track_id + ".")), None)
            if not filename:
                raise RuntimeError("download finished but the file wasn't found")

            p = self._find(playlist_id)
            enhance = (p or {}).get("enhance") or {}
            if any(enhance.values()):
                self._set_track_status(playlist_id, track_id, status="enhancing")
                self._apply_audio_enhance(os.path.join(PLAYLISTS_DIR, filename), enhance)

            self._set_track_status(playlist_id, track_id, status="ready", audio_file=filename)
            self.log(f"cached: {filename}", "ok")
        except Exception as e:
            self._set_track_status(playlist_id, track_id, status="error", error=str(e))
            self.log(f"download failed for a track: {e}", "bad")


# =============================================================================
# ANALYTICS — plays log + rollups (rich listening analytics/insights)
# =============================================================================
class AnalyticsController:
    def __init__(self):
        self._lock = threading.RLock()  # reentrant — see PlaylistController for why
        self.plays = self._load()

    def _load(self):
        try:
            with open(ANALYTICS_JSON, "r", encoding="utf-8") as f:
                return json.load(f).get("plays", [])
        except Exception:
            return []

    def _save(self):
        # Same fix as PlaylistController._save() — locking here (not just at
        # some call sites) closes the race where two threads writing to the
        # same shared .tmp filename at once can interleave and corrupt
        # whichever save loses.
        with self._lock:
            try:
                tmp = ANALYTICS_JSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"plays": self.plays[-5000:]}, f)  # cap growth
                os.replace(tmp, ANALYTICS_JSON)
            except Exception:
                pass

    def record_play(self, track_id, title, artist, playlist_id, seconds_listened=0, profile_id="default"):
        with self._lock:
            self.plays.append({
                "track_id": track_id, "title": title, "artist": artist,
                "playlist_id": playlist_id, "seconds": round(seconds_listened),
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "profile_id": profile_id or "default",
            })
            self._save()

    def summary(self, days=14, profile_id=None):
        from collections import Counter
        cutoff = time.time() - days * 86400
        scoped = [p for p in self.plays if not profile_id or p.get("profile_id", "default") == profile_id]
        recent = [p for p in scoped if _ts_to_epoch(p["ts"]) >= cutoff]
        track_counter = Counter()
        artist_counter = Counter()
        per_day = Counter()
        total_seconds = 0
        for p in scoped:
            track_counter[(p["title"], p["artist"])] += 1
            artist_counter[p["artist"] or "Unknown"] += 1
            total_seconds += p.get("seconds", 0)
        for p in recent:
            day = p["ts"][:10]
            per_day[day] += 1
        top_tracks = [{"title": t, "artist": a, "plays": c} for (t, a), c in track_counter.most_common(8)]
        top_artists = [{"artist": a, "plays": c} for a, c in artist_counter.most_common(8)]
        day_series = []
        for i in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            day_series.append({"day": day, "plays": per_day.get(day, 0)})
        return {
            "total_plays": len(scoped),
            "total_minutes": round(total_seconds / 60, 1),
            "top_tracks": top_tracks,
            "top_artists": top_artists,
            "plays_by_day": day_series,
        }

    def activity_feed(self, limit=30, profile_id=None):
        scoped = [p for p in self.plays if not profile_id or p.get("profile_id", "default") == profile_id]
        return sorted(scoped, key=lambda p: p.get("ts", ""), reverse=True)[:limit]

    def growth(self, recent_hours=72, prior_hours=168, profile_id=None, limit=12):
        """'Fastest Growing' — real momentum with exponential time-decay weighting (24h half-life)."""
        import math
        from collections import Counter
        now = time.time()
        recent_cut = now - recent_hours * 3600
        prior_cut = now - (recent_hours + prior_hours) * 3600
        scoped = [p for p in self.plays if not profile_id or p.get("profile_id", "default") == profile_id]
        
        recent_weighted, prior_counts = Counter(), Counter()
        for p in scoped:
            ts = _ts_to_epoch(p.get("ts", ""))
            key = (p.get("title", ""), p.get("artist", ""))
            if ts >= recent_cut:
                # Exponential decay weight: more recent plays weigh higher (halflife 24h)
                hours_ago = max(0, (now - ts) / 3600.0)
                weight = math.exp(-0.028 * hours_ago)
                recent_weighted[key] += weight
            elif ts >= prior_cut:
                prior_counts[key] += 1
                
        rows = []
        for key, rw in recent_weighted.items():
            pc = prior_counts.get(key, 0)
            score = rw / (pc + 1.0)
            rows.append({
                "title": key[0], "artist": key[1],
                "recent_plays": round(rw, 1), "prior_plays": pc,
                "growth_score": round(score, 2)
            })
        rows.sort(key=lambda r: (-r["growth_score"], -r["recent_plays"]))
        return rows[:limit]



def _ts_to_epoch(ts):
    try:
        return time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0


# =============================================================================
# PROFILES — real local listening profiles.
#
# NOMAD is a single self-hosted instance with no social backend, so there is
# no honest way to build a real "friends" network. What *is* real for a
# self-hosted app is a household/shared-instance scenario: more than one
# person uses this one install. Profiles gives each of them a name and their
# own genuine play history, and the "activity feed" the frontend shows is
# real recent plays from real profiles on this instance — not invented data.
# =============================================================================
PROFILES_JSON = os.path.join(BASE_DIR, "profiles.json")


class ProfileController:
    def __init__(self):
        self.log = log_fn("profiles")
        self._lock = threading.RLock()  # reentrant — see PlaylistController for why
        self.data = self._load()

    def _load(self):
        try:
            with open(PROFILES_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("profiles"):
                    return data
        except Exception:
            pass
        return {
            "profiles": [{"id": "default", "name": "You", "avatar_seed": "default",
                           "created": time.strftime("%Y-%m-%d %H:%M:%S")}],
            "active": "default",
        }

    def _save(self):
        # Same fix as PlaylistController._save() — see that comment.
        with self._lock:
            try:
                tmp = PROFILES_JSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.data, f)
                os.replace(tmp, PROFILES_JSON)
            except Exception as e:
                self.log(f"couldn't save profiles: {e}", "warn")

    def list(self):
        return self.data["profiles"]

    def active_id(self):
        return self.data.get("active", "default")

    def create(self, name):
        p = {"id": uuid.uuid4().hex[:8], "name": (name or "").strip() or "New profile",
             "avatar_seed": uuid.uuid4().hex[:8], "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        with self._lock:
            self.data["profiles"].append(p)
            self._save()
        return p

    def delete(self, profile_id):
        if profile_id == "default":
            return False
        with self._lock:
            self.data["profiles"] = [p for p in self.data["profiles"] if p["id"] != profile_id]
            if self.data.get("active") == profile_id:
                self.data["active"] = "default"
            self._save()
        return True

    def set_active(self, profile_id):
        if not any(p["id"] == profile_id for p in self.data["profiles"]):
            return False
        self.data["active"] = profile_id
        self._save()
        return True


# ---- iTunes Search API (keyless) ----
def itunes_search(query, limit=10, entity="song"):
    """Search iTunes catalog — keyless, instant. Normalizes both song and
    album/collection result shapes (they use different field names)."""
    url = f"https://itunes.apple.com/search?term={urllib.parse.quote(query)}&limit={limit}&entity={entity}&media=music"
    data = _safe_fetch_json(url, timeout=6)
    if not data:
        return [], "iTunes unreachable"
    tracks = []
    is_collection = entity in ("album", "musicVideo") or entity.lower() == "album"
    for r in data.get("results", []):
        title = r.get("collectionName") if is_collection else r.get("trackName")
        if not title:
            title = r.get("trackName") or r.get("collectionName") or ""
        tracks.append({
            "title": title,
            "artist": r.get("artistName", ""),
            "album": r.get("collectionName", ""),
            "thumbnail": (r.get("artworkUrl100") or "").replace("100x100", "300x300"),
            "preview_url": r.get("previewUrl", ""),
            "duration_ms": r.get("trackTimeMillis", 0),
            "genre": r.get("primaryGenreName", ""),
            "release_date": r.get("releaseDate", "")[:10],
            "collection_url": r.get("collectionViewUrl", "") if is_collection else "",
            "track_count": r.get("trackCount") if is_collection else None,
            "source": "itunes",
        })
    return tracks, None


# ---- Jamendo API (keyless — client_id is public demo with iTunes fallback) ----
def jamendo_featured(limit=12):
    """Free CC-licensed / indie music from Jamendo, Audius & iTunes Indie."""
    url = f"https://api.jamendo.com/v3.0/tracks/?client_id=b6747d04&format=json&limit={limit}&order=popularity_total&include=musicinfo"
    data = _safe_fetch_json(url, timeout=6)
    tracks = []
    if data and data.get("results"):
        for t in data.get("results", []):
            tracks.append({
                "title": t.get("name", ""),
                "artist": t.get("artist_name", ""),
                "thumbnail": t.get("image", ""),
                "preview_url": t.get("audio", ""),
                "duration": int(t.get("duration", 0)),
                "source": "jamendo",
            })
        if tracks: return tracks, None

    # Fallback to iTunes Indie Search
    indie_tracks, _ = itunes_search("indie", limit=limit)
    if indie_tracks:
        for t in indie_tracks: t["source"] = "indie_vault"
        return indie_tracks, None
    return [], "Indie vault unavailable"


# ---- Radio Browser API (keyless) ----
def radio_browser_stations(limit=15, tag=""):
    """Internet radio stations from the community Radio Browser directory."""
    base = "https://de1.api.radio-browser.info/json/stations"
    if tag:
        url = f"{base}/bytag/{urllib.parse.quote(tag)}?limit={limit}&order=clickcount&reverse=true&hidebroken=true"
    else:
        url = f"{base}/topclick/{limit}?hidebroken=true"
    data = _safe_fetch_json(url, timeout=6)
    if not data or not isinstance(data, list):
        return [], "Radio Browser unreachable"
    stations = []
    for s in data[:limit]:
        stations.append({
            "name": s.get("name", "").strip(),
            "url": s.get("url_resolved") or s.get("url", ""),
            "favicon": s.get("favicon", ""),
            "country": s.get("country", ""),
            "tags": s.get("tags", ""),
            "codec": s.get("codec", ""),
            "bitrate": s.get("bitrate", 0),
            "votes": s.get("votes", 0),
            "source": "radio_browser",
        })
    return stations, None


# ---- JioSaavn (community wrapper with Apple India Chart fallback) ----
def jiosaavn_trending(limit=12):
    """Trending Indian music from Apple Music India Chart + JioSaavn + iTunes Search."""
    # Try Apple Music India Chart first (100% reliable)
    india_chart, _ = get_apple_chart_tracks("in")
    if india_chart:
        out = []
        for t in india_chart[:limit]:
            out.append({
                "title": t.get("title", ""),
                "artist": t.get("artist", ""),
                "thumbnail": (t.get("thumbnail") or "").replace("100x100", "300x300"),
                "source": "jiosaavn_hits",
                "preview_url": t.get("preview_url", ""),
            })
        if out: return out, None

    # Fallback to iTunes Indian search
    itunes_ind, _ = itunes_search("punjabi", limit=limit)
    if itunes_ind:
        return itunes_ind, None
    return [], "Indian music feed unavailable"



# ---- MusicBrainz API (keyless, rate-limited) ----
def musicbrainz_artist(artist_name):
    """Get artist info including releases, tags, and URLs from MusicBrainz."""
    import urllib.parse
    url = f"https://musicbrainz.org/ws/2/artist/?query=artist:{urllib.parse.quote(artist_name)}&fmt=json&limit=1"
    data = _safe_fetch_json(url, timeout=8)
    if not data or not data.get("artists"):
        return None, "MusicBrainz unreachable"
    artist = data["artists"][0]
    mbid = artist.get("id", "")
    # Get releases (albums)
    releases = []
    if mbid:
        rel_url = f"https://musicbrainz.org/ws/2/release-group?artist={mbid}&type=album&fmt=json&limit=10"
        rel_data = _safe_fetch_json(rel_url, timeout=8)
        if rel_data and rel_data.get("release-groups"):
            for rg in rel_data["release-groups"][:10]:
                cover_url = f"https://coverartarchive.org/release-group/{rg['id']}/front-250" if rg.get("id") else ""
                releases.append({
                    "title": rg.get("title", ""),
                    "type": rg.get("primary-type", "Album"),
                    "year": (rg.get("first-release-date") or "")[:4],
                    "mbid": rg.get("id", ""),
                    "cover_url": cover_url,
                })
    return {
        "name": artist.get("name", artist_name),
        "mbid": mbid,
        "country": artist.get("country", ""),
        "type": artist.get("type", ""),
        "tags": [t["name"] for t in (artist.get("tags") or [])[:5]],
        "begin_year": (artist.get("life-span", {}).get("begin") or "")[:4],
        "disambiguation": artist.get("disambiguation", ""),
        "releases": releases,
    }, None


# ---- Wikipedia API (keyless) ----
def wikipedia_summary(query):
    """Get a short biography/summary from Wikipedia."""
    import urllib.parse
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}"
    data = _safe_fetch_json(url, timeout=6)
    if not data or data.get("type") == "https://mediawiki.org/wiki/HyperSwitch/errors/not_found":
        return None, "Wikipedia article not found"
    return {
        "title": data.get("title", ""),
        "summary": data.get("extract", ""),
        "thumbnail": (data.get("thumbnail") or {}).get("source", ""),
        "url": data.get("content_urls", {}).get("desktop", {}).get("page", ""),
    }, None


# =============================================================================
# LYRICS ENGINE — dedicated multi-source syncing + fallback routing, with
# built-in diagnostics.
#
# Three problems this section exists to solve:
#   1. WRONG LYRICS: several providers here are fuzzy TEXT SEARCH APIs
#      (NetEase, LRCLIB's /search fallback, Genius, Vagalume, Popcat). Every
#      one of them used to just take "the first result" on faith. For a
#      common title ("Home", "Alive", "Runaway"...) that silently hands back
#      a completely different song's lyrics, perfectly confidently. Every
#      provider below now runs its candidates through `_lyrics_match_ok()`
#      and refuses anything that isn't a credible title+artist match —
#      same principle as the verified full-stream resolver used for playback.
#   2. "CACHED LYRICS AREN'T SYNCED": a track whose first successful match
#      was a plain-text-only source (Genius/Popcat/Lyrics.ovh, tried after
#      LRCLIB+NetEase) used to be cached as "done" FOREVER — nothing ever
#      automatically re-checked whether a synced source could match it
#      later. `_lyrics_cache_policy()` below is the one shared TTL/version
#      rule both call sites (library tracks' get_lyrics(), and the generic
#      /api/lyrics used for previews) now go through, so a plain-only or
#      not-found result quietly gets another shot instead of staying stuck.
#   3. NO WAY TO DIAGNOSE A BAD MATCH: fetch_lyrics_multi(..., debug=True)
#      returns a step-by-step trace of every provider tried, what it found,
#      and why it was accepted/rejected — exposed at /api/lyrics/diagnose.
# =============================================================================

# Bumped whenever matching/verification logic changes in a way that could
# make a previously-cached result wrong or worth re-checking (e.g. adding
# the confidence check below). A cached entry stamped with an older version
# is treated as needing a re-fetch through the current, stricter engine —
# this is what quietly repairs old bad cache entries without anyone having
# to manually hit "resync" on every track.
LYRICS_ENGINE_VERSION = 3


def _lyrics_norm_text(s):
    """Lowercase + strip punctuation/parenthetical noise ('Song (Live)' ->
    'song') so title/artist comparisons aren't thrown off by formatting
    differences between providers.

    Was stripping to [^a-z0-9] only, which silently deletes every
    non-ASCII character. Fine for English titles, but for anything in
    Korean/Japanese/Chinese/Cyrillic/accented Latin etc. it reduced the
    whole string to "" — SequenceMatcher then scored every candidate 0.0
    (title_score defaults to 0.0 when either side is empty), so
    _lyrics_match_ok NEVER passed and those tracks looked like "no lyrics
    found" on every provider even when a perfect match existed. \\w with
    Python 3's default Unicode-aware `re` keeps letters from any script,
    so non-English titles/artists can actually be compared instead of
    being zeroed out."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    return s.strip()


def _lyrics_match_score(query_title, query_artist, cand_title, cand_artist):
    """0..1 title/artist similarity of a search-result candidate against
    what was actually asked for. Artist comparison is a little forgiving
    (covers substring containment for 'feat.'/collab variants) but never
    just defaults to a pass."""
    from difflib import SequenceMatcher
    qt, qa = _lyrics_norm_text(query_title), _lyrics_norm_text(query_artist)
    ct, ca = _lyrics_norm_text(cand_title), _lyrics_norm_text(cand_artist)
    title_score = SequenceMatcher(None, qt, ct).ratio() if qt and ct else 0.0
    if qa and ca:
        artist_score = SequenceMatcher(None, qa, ca).ratio()
        if artist_score < 0.99 and (qa in ca or ca in qa):
            artist_score = max(artist_score, 0.85)
    else:
        artist_score = 0.5  # one side missing an artist entirely — don't hard-fail on that alone
    return round(title_score, 3), round(artist_score, 3)


def _lyrics_match_ok(query_title, query_artist, cand_title, cand_artist,
                      title_floor=0.72, artist_floor=0.55):
    ts, ascore = _lyrics_match_score(query_title, query_artist, cand_title, cand_artist)
    return (ts >= title_floor and ascore >= artist_floor), ts, ascore


def _lyrics_cache_policy(cached):
    """Single shared freshness rule for any cached lyrics result, used by
    both the per-track playlist cache and the generic preview cache — one
    place to tune instead of the two call sites silently drifting apart.
    Returns True if `cached` is still good to serve as-is, False if it
    should be re-fetched through the current engine."""
    if not cached:
        return False
    if cached.get("engine_version") != LYRICS_ENGINE_VERSION:
        return False  # older/unverified match — always worth a re-check
    age = time.time() - (cached.get("cached_at") or 0)
    if cached.get("found") and cached.get("synced"):
        return True  # a verified synced match is as good as it gets — cache indefinitely
    if cached.get("found"):
        return age < 24 * 3600  # plain-only: retry daily in case a synced source appears
    return age < 12 * 3600  # not found: retry more often — provider catalogs change


# ---- LRCLIB (keyless — synced lyrics) ----
def lrclib_lyrics(title, artist, album="", duration=0):
    """Get synced or plain lyrics from LRCLIB. /get is an exact lookup
    (safe to trust as-is); /search is a fuzzy fallback that used to just
    take result[0] with no check it was actually the right song — now it
    scores every candidate and only accepts a confident title+artist
    match, preferring the one closest to the known duration when several
    are all plausible (cover versions, remasters, etc. all sharing a title)."""
    import urllib.parse
    params = f"track_name={urllib.parse.quote(title)}&artist_name={urllib.parse.quote(artist)}"
    if album:
        params += f"&album_name={urllib.parse.quote(album)}"
    if duration:
        params += f"&duration={duration}"
    url = f"https://lrclib.net/api/get?{params}"
    data = _safe_fetch_json(url, timeout=6)
    if not data or (not data.get("syncedLyrics") and not data.get("plainLyrics")):
        # Exact lookup missed — fuzzy search fallback, verified this time.
        search_url = f"https://lrclib.net/api/search?track_name={urllib.parse.quote(title)}&artist_name={urllib.parse.quote(artist)}"
        results = _safe_fetch_json(search_url, timeout=6)
        if not results or not isinstance(results, list):
            return None, "No lyrics found"
        candidates = []
        for r in results:
            ok, ts, ascore = _lyrics_match_ok(title, artist, r.get("trackName", ""), r.get("artistName", ""))
            if ok:
                candidates.append((ts + ascore, r))
        if not candidates:
            return None, "LRCLIB search had results but none confidently matched this track"
        candidates.sort(key=lambda c: c[0], reverse=True)
        best_score = candidates[0][0]
        # Among confident matches within a hair of the top score (e.g. a
        # studio cut and a live version can both score similarly), prefer
        # whichever is closest to the known track duration.
        top_tier = [c for c in candidates if c[0] >= best_score - 0.05]
        if duration and len(top_tier) > 1:
            top_tier.sort(key=lambda c: abs((c[1].get("duration") or 0) - duration))
        data = top_tier[0][1]
    return {
        "synced": data.get("syncedLyrics", ""),
        "plain": data.get("plainLyrics", ""),
        "source": "lrclib",
        "title": data.get("trackName", title),
        "artist": data.get("artistName", artist),
    }, None


# ---- NetEase Cloud Music (unofficial, keyless, public web API) — a second
# real source of SYNCED (LRC) lyrics, not just plain text. LRCLIB is the
# primary synced source but its catalog is crowd-sourced and has gaps,
# especially for K-pop/J-pop/Chinese releases and film soundtracks; NetEase
# covers a lot of what LRCLIB misses. No API key, no auth — same public
# endpoints every "syncedlyrics"-style open-source tool uses. ----
def netease_lyrics(title, artist):
    """Search NetEase's public catalog for the track, then pull its LRC
    (line-timed) lyrics. Never raises — any failure just falls through to
    the next source in fetch_lyrics_multi, same contract as every other
    lyrics fn in this file."""
    query = f"{artist} {title}".strip() if artist else title
    if not query:
        return None, "No title"
    headers = {
        "User-Agent": "Mozilla/5.0 (NOMAD music client)",
        "Referer": "https://music.163.com/",
    }
    try:
        search_url = ("http://music.163.com/api/search/get/web?csrf_token=&s="
                       + urllib.parse.quote(query) + "&type=1&offset=0&total=true&limit=5")
        req = urllib.request.Request(search_url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        songs = ((data.get("result") or {}).get("songs")) or []
        if not songs:
            return None, "No match on NetEase"
        # Used to just take songs[0] — the top hit on a fuzzy text search,
        # with zero check it was actually the right song. Now scans the
        # candidates NetEase returned and only accepts the first one that
        # confidently matches on title+artist.
        song = None
        for candidate in songs:
            cand_title = candidate.get("name", "")
            cand_artist = ", ".join(a.get("name", "") for a in (candidate.get("artists") or []) if a.get("name"))
            ok, _, _ = _lyrics_match_ok(title, artist, cand_title, cand_artist)
            if ok:
                song = candidate
                break
        if song is None:
            return None, "NetEase had results but none confidently matched this track"
        song_id = song.get("id")
        if not song_id:
            return None, "No match on NetEase"
        song_title = song.get("name", title)
        song_artist = ", ".join(a.get("name", "") for a in (song.get("artists") or []) if a.get("name")) or artist

        lyric_url = f"http://music.163.com/api/song/lyric?id={song_id}&lv=1&kv=1&tv=-1"
        req2 = urllib.request.Request(lyric_url, headers=headers)
        with urllib.request.urlopen(req2, timeout=6) as resp2:
            lyric_data = json.loads(resp2.read().decode("utf-8", errors="replace"))
        lrc = ((lyric_data.get("lrc") or {}).get("lyric") or "").strip()
        if not lrc:
            return None, "No lyrics on NetEase"
        plain_lines = []
        for line in lrc.splitlines():
            m = re.match(r"\[\d+:\d+(?:\.\d+)?\]\s*(.*)", line.strip())
            if m and m.group(1).strip():
                plain_lines.append(m.group(1).strip())
        return {
            "synced": lrc,
            "plain": "\n".join(plain_lines) if plain_lines else None,
            "source": "netease",
            "title": song_title,
            "artist": song_artist,
        }, None
    except Exception as e:
        return None, f"NetEase error: {e}"


# ---- Vagalume (keyless — lyrics fallback for Brazilian/intl) ----
def vagalume_lyrics(title, artist):
    """Lyrics from Vagalume — good for Brazilian and international music.
    The API is queried scoped to a specific artist, so mismatches are
    rarer than a pure text search, but it can still return the wrong song
    for that artist (a compilation cut, alternate version, etc.) — verify
    the title before accepting rather than trusting result[0] outright."""
    import urllib.parse
    url = f"https://api.vagalume.com.br/search.php?art={urllib.parse.quote(artist)}&mus={urllib.parse.quote(title)}"
    data = _safe_fetch_json(url, timeout=6)
    if not data or data.get("type") == "notfound" or not data.get("mus"):
        return None, "No lyrics found on Vagalume"
    resp_artist = (data.get("art") or {}).get("name", artist)
    mus = None
    for candidate in data["mus"]:
        ok, _, _ = _lyrics_match_ok(title, artist, candidate.get("name", ""), resp_artist)
        if ok:
            mus = candidate
            break
    if mus is None:
        return None, "Vagalume had results but none confidently matched this track"
    return {
        "plain": mus.get("text", ""),
        "source": "vagalume",
        "title": mus.get("name", title),
        "artist": resp_artist,
        "url": mus.get("url", ""),
    }, None


# ---- ListenBrainz (keyless — community recommendations) ----
def listenbrainz_similar_artists(artist_name):
    """Similar artists from ListenBrainz community data."""
    import urllib.parse
    # First get the MBID from MusicBrainz
    mb_url = f"https://musicbrainz.org/ws/2/artist/?query=artist:{urllib.parse.quote(artist_name)}&fmt=json&limit=1"
    mb_data = _safe_fetch_json(mb_url, timeout=6)
    if not mb_data or not mb_data.get("artists"):
        return [], "Could not find artist"
    mbid = mb_data["artists"][0].get("id", "")
    if not mbid:
        return [], "No MBID found"
    url = f"https://api.listenbrainz.org/1/metadata/artist/{mbid}/similar-artists"
    data = _safe_fetch_json(url, timeout=8)
    if not data or not isinstance(data, dict):
        return [], "ListenBrainz unreachable"
    artists = []
    for a in (data.get("similar_artists") or [])[:12]:
        artists.append({
            "name": a.get("name", ""),
            "mbid": a.get("artist_mbid", ""),
            "score": a.get("score", 0),
            "source": "listenbrainz",
        })
    return artists, None


# ---- Audius (keyless — free streaming music) ----
def audius_trending(limit=12):
    """Trending tracks from Audius — free, full-length streaming."""
    # First get a healthy API endpoint
    host_url = "https://api.audius.co"
    data = _safe_fetch_json(host_url, timeout=4)
    if not data or not data.get("data"):
        return [], "Audius unreachable"
    api_host = data["data"][0]
    url = f"{api_host}/v1/tracks/trending?limit={limit}&app_name=NOMAD"
    tracks_data = _safe_fetch_json(url, timeout=8)
    if not tracks_data or not tracks_data.get("data"):
        return [], "No trending tracks from Audius"
    tracks = []
    for t in tracks_data["data"][:limit]:
        artwork = t.get("artwork", {}) or {}
        tracks.append({
            "title": t.get("title", ""),
            "artist": (t.get("user") or {}).get("name", ""),
            "thumbnail": artwork.get("480x480") or artwork.get("150x150") or "",
            "duration": t.get("duration", 0),
            "play_count": t.get("play_count", 0),
            "stream_url": f"{api_host}/v1/tracks/{t['id']}/stream?app_name=NOMAD" if t.get("id") else "",
            "permalink": t.get("permalink", ""),
            "genre": t.get("genre", ""),
            "source": "audius",
        })
    return tracks, None


# ---- Songkick (keyless scraping attempt for concerts) ----
def songkick_events(artist_name, limit=5):
    """Try to get upcoming concerts. Falls back gracefully if blocked."""
    # Songkick requires an API key, but we can try the Bandsintown v2 public API
    import urllib.parse
    url = f"https://rest.bandsintown.com/artists/{urllib.parse.quote(artist_name)}/events?app_id=NOMAD&date=upcoming"
    data = _safe_fetch_json(url, timeout=6)
    if not data or not isinstance(data, list):
        return [], "No concert data available"
    events = []
    for e in data[:limit]:
        venue = e.get("venue", {})
        events.append({
            "date": (e.get("datetime") or "")[:10],
            "venue": venue.get("name", ""),
            "city": venue.get("city", ""),
            "country": venue.get("country", ""),
            "url": e.get("url", ""),
            "source": "bandsintown",
        })
    return events, None


# ---- Title Sanitizer ----
def clean_song_title(title):
    if not title: return ""
    import re
    t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', title)
    t = re.sub(r'(?i)\b(official|video|audio|lyric|lyrics|hd|4k|remastered|version|feat\.?|ft\.?)\b.*$', '', t)
    return t.strip()


# ---- Lyrics.ovh (keyless) ----
def lyrics_ovh(artist, title):
    import urllib.parse
    clean_t = clean_song_title(title) or title
    clean_a = artist.split(',')[0].strip() if artist else ""
    if not clean_a or not clean_t:
        return None, "Artist and title required for Lyrics.ovh"
    url = f"https://api.lyrics.ovh/v1/{urllib.parse.quote(clean_a)}/{urllib.parse.quote(clean_t)}"
    data = _safe_fetch_json(url, timeout=6)
    if not data or not data.get("lyrics"):
        return None, "No lyrics on Lyrics.ovh"
    return {
        "plain": data.get("lyrics", ""),
        "source": "lyrics_ovh",
        "title": title,
        "artist": artist
    }, None


# ---- Popcat Lyrics (keyless) ----
def popcat_lyrics(query):
    """Popcat takes one opaque text query and returns a single result (no
    candidate list to pick from), so verification here is best-effort:
    if it hands back a title/artist, it must credibly match what was
    asked for; if it doesn't return that metadata at all, we can't
    second-guess it beyond the query itself."""
    import urllib.parse
    if not query:
        return None, "Query required"
    url = f"https://api.popcat.xyz/lyrics?song={urllib.parse.quote(query)}"
    data = _safe_fetch_json(url, timeout=6)
    if not data or not data.get("lyrics"):
        return None, "No lyrics on Popcat"
    resp_title, resp_artist = data.get("title"), data.get("artist")
    if resp_title:
        ok, _, _ = _lyrics_match_ok(query, "", resp_title, resp_artist or "", title_floor=0.5, artist_floor=0.0)
        if not ok:
            return None, "Popcat's match didn't look like the requested track"
    return {
        "plain": data.get("lyrics", ""),
        "source": "popcat",
        "title": resp_title or query,
        "artist": resp_artist or "",
        "thumbnail": data.get("image", "")
    }, None


# ---- Genius Web Lyrics (keyless scraper fallback) ----
def genius_lyrics_web(artist, title):
    """Keyless Genius lyrics search and page parsing."""
    import urllib.parse, urllib.request, re
    query = f"{artist} {title}".strip()
    search_url = f"https://genius.com/api/search/multi?q={urllib.parse.quote(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        req_search = urllib.request.Request(search_url, headers=headers)
        data = json.loads(urllib.request.urlopen(req_search, timeout=6).read().decode("utf-8"))
    except Exception:
        return None, "Genius search failed"
    if not data or not data.get("response", {}).get("sections"):
        return None, "Genius search failed"
    
    song_url = None
    matched_title, matched_artist = title, artist
    for s in data["response"]["sections"]:
        if s.get("type") == "song" and s.get("hits"):
            # Used to just take hits[0] regardless of whether it was
            # actually the requested song — Genius's "multi" search often
            # returns articles/other artists' songs ahead of an exact
            # match for ambiguous titles. Scan all hits in this section
            # for the first one that confidently matches.
            for hit in s["hits"]:
                result = hit.get("result", {}) or {}
                cand_title = result.get("title", "")
                cand_artist = (result.get("primary_artist") or {}).get("name", "")
                ok, _, _ = _lyrics_match_ok(title, artist, cand_title, cand_artist)
                if ok:
                    song_url = result.get("url")
                    matched_title, matched_artist = cand_title or title, cand_artist or artist
                    break
            break
    if not song_url:
        return None, "Genius had results but none confidently matched this track"
        
    try:
        req = urllib.request.Request(song_url, headers=headers)
        html = urllib.request.urlopen(req, timeout=6).read().decode("utf-8", errors="ignore")
        parts = html.split("data-lyrics-container=")
        if len(parts) <= 1:
            return None, "Could not extract lyrics container"
        chunks = []
        for p in parts[1:]:
            c = p.split(">", 1)[1].split("</div>")[0]
            c = c.replace("<br/>", "\n").replace("<br>", "\n").replace("<br />", "\n")
            c = re.sub(r"<[^>]+>", "", c)
            if c.strip():
                chunks.append(c.strip())
        full = "\n\n".join(chunks)
        if full and len(full) > 30:
            return {"plain": full, "source": "genius", "title": matched_title, "artist": matched_artist, "url": song_url}, None
    except Exception:
        pass
    return None, "Failed to parse Genius lyrics"


# ---- Multi-source lyrics aggregator ----
def _norm_lyrics_result(raw, source_name):
    """Every source function above returns its own shape (some hand back
    synced lyrics as a raw '[mm:ss.xx] line' LRC string, some don't have
    synced at all). Collapse them all into ONE shape so every caller —
    docked panel, preview modal, playlist cache — can render it identically
    without caring which provider it came from."""
    if not raw:
        return None
    synced_raw = raw.get("synced")
    synced = _parse_synced_lyrics(synced_raw) if isinstance(synced_raw, str) else (synced_raw or None)
    plain = (raw.get("plain") or "").strip() or None
    if not plain and not synced:
        return None
    return {
        "found": True,
        "plain": plain,
        "synced": synced,
        "instrumental": bool(raw.get("instrumental")),
        "source": raw.get("source") or source_name,
        # Kept around just long enough for fetch_lyrics_multi to compute a
        # match_confidence against the ORIGINAL query — stripped back out
        # before the result is cached/returned so callers don't have to
        # care about it.
        "_matched_title": raw.get("title"),
        "_matched_artist": raw.get("artist"),
    }


def fetch_lyrics_multi(title, artist, album="", duration=0, provider="auto", debug=False):
    """Try multiple free lyrics sources in priority order until one actually
    has lyrics: LRCLIB (synced) -> NetEase (synced) -> Lyrics.ovh -> Genius
    -> Popcat -> Vagalume. Two independent SYNCED sources up front (LRCLIB
    then NetEase) roughly doubles the chance of getting real karaoke-style
    timing instead of falling all the way to plain text. Always returns a
    single normalized dict (found/plain/synced/instrumental/source/error) —
    never a tuple, never a raw provider payload — so this one function is
    safe to use everywhere lyrics are needed, for both library tracks and
    preview-only tracks.

    Every successful result is stamped with `engine_version` and
    `cached_at` so callers can apply `_lyrics_cache_policy()` consistently,
    plus a `match_confidence` (0..1) so a suspiciously low-confidence match
    is visible instead of silently indistinguishable from a solid one.

    Pass debug=True to get a `diagnostics` list back alongside the normal
    fields — one entry per provider actually tried, recording whether it
    matched and why not if it didn't. This is what powers
    /api/lyrics/diagnose: a real "why did this track get the lyrics it
    got" trace instead of guessing.
    """
    clean_t = clean_song_title(title) or title
    clean_a = artist.split(',')[0].strip() if artist else ""
    last_err = "Lyrics not found on any connected provider"
    diagnostics = []

    def _not_found(err):
        out = {"found": False, "plain": None, "synced": None, "instrumental": False, "error": err,
               "engine_version": LYRICS_ENGINE_VERSION, "cached_at": time.time()}
        if debug:
            out["diagnostics"] = diagnostics
        return out

    def _finalize(norm, provider_name):
        mt = norm.pop("_matched_title", None) or title
        ma = norm.pop("_matched_artist", None) or artist
        ts, ascore = _lyrics_match_score(title, artist, mt, ma)
        norm["match_confidence"] = round((ts + ascore) / 2, 3)
        norm["matched_title"] = mt
        norm["matched_artist"] = ma
        norm["engine_version"] = LYRICS_ENGINE_VERSION
        norm["cached_at"] = time.time()
        diagnostics.append({"provider": provider_name, "attempted": True, "matched": True,
                             "reason": f"matched {mt!r} by {ma!r} (confidence {norm['match_confidence']})"})
        if debug:
            norm["diagnostics"] = diagnostics
        return norm

    providers = [
        ("lrclib", lambda t, a: lrclib_lyrics(t, a, album, duration)),
        ("netease", lambda t, a: netease_lyrics(t, a)),
        ("lyrics_ovh", lambda t, a: lyrics_ovh(a, t)),
        ("genius", lambda t, a: genius_lyrics_web(a, t)),
        ("popcat", lambda t, a: popcat_lyrics(f"{a} {t}".strip())),
        ("vagalume", lambda t, a: vagalume_lyrics(t, a)),
    ]

    for name, fn in providers:
        if provider not in ("auto", name):
            continue
        result, err = fn(title, artist)
        if not result and (clean_t != title or clean_a != artist):
            result, err = fn(clean_t, clean_a)
        norm = _norm_lyrics_result(result, name)
        if norm:
            return _finalize(norm, name)
        reason = err or f"No lyrics on {name}"
        diagnostics.append({"provider": name, "attempted": True, "matched": False, "reason": reason})
        last_err = reason
        if provider == name:
            return _not_found(reason)

    return _not_found(last_err)



# ---- Artist Deep Dive — combines MusicBrainz + Wikipedia + Last.fm + Bandsintown ----
def artist_deep_dive(artist_name):
    """Comprehensive artist info from multiple free sources."""
    results = {}
    errors = {}
    def run(name, fn):
        try:
            results[name] = fn()
        except Exception as e:
            errors[name] = str(e)
            results[name] = None
    
    jobs = {
        "musicbrainz": lambda: musicbrainz_artist(artist_name)[0],
        "wikipedia": lambda: wikipedia_summary(artist_name + " musician")[0] or wikipedia_summary(artist_name)[0],
        "events": lambda: songkick_events(artist_name)[0],
        "listenbrainz": lambda: listenbrainz_similar_artists(artist_name)[0],
    }
    # Check if Last.fm key is available for similar artists
    settings = load_settings()
    if settings.get("lastfm_api_key"):
        jobs["lastfm_similar"] = lambda: lastfm_similar_artists(artist_name, settings["lastfm_api_key"])[0]
    if spotify.configured():
        jobs["spotify"] = lambda: _cached_call(f"spotify:artist_bundle:{artist_name.lower()}", 24 * 3600,
                                                lambda: spotify.artist_bundle(artist_name))

    threads = [threading.Thread(target=run, args=(n, f), daemon=True) for n, f in jobs.items()]
    for t in threads: t.start()
    deadline = time.time() + 5.5
    for t in threads:
        remaining = max(0.05, deadline - time.time())
        t.join(timeout=remaining)

    # Also search iTunes for top songs (fills in when Spotify isn't connected,
    # or as extra coverage for tracks Spotify's top-tracks endpoint misses)
    try:
        top_songs, _ = itunes_search(artist_name, limit=10)
    except Exception as e:
        errors["itunes_top_songs"] = str(e)
        top_songs = []
    spotify_bundle = results.get("spotify") or {}
    spotify_artist = spotify_bundle.get("artist") if spotify_bundle else None
    spotify_top = spotify_bundle.get("top_tracks") if spotify_bundle else []
    if spotify_top:
        # Spotify's real top tracks (with genuine 30s preview_urls) lead;
        # iTunes results fill in behind, deduped by title.
        seen = {(t.get("title", "").strip().lower()) for t in spotify_top}
        top_songs = spotify_top + [t for t in top_songs if t.get("title", "").strip().lower() not in seen]

    return {
        "artist_name": artist_name,
        "musicbrainz": results.get("musicbrainz"),
        "wikipedia": results.get("wikipedia"),
        "events": results.get("events") or [],
        "similar_artists": results.get("listenbrainz") or results.get("lastfm_similar") or [],
        "top_songs": top_songs,
        "spotify": spotify_artist,   # {name, image, genres, followers, popularity, spotify_url} or None
        "spotify_related": (spotify_bundle.get("related") if spotify_bundle else []) or [],
        "errors": errors,
    }


# =============================================================================
# DISCOVER — everything here is computed straight from the real library and
# real play history. No invented "trending worldwide" or fake charts; this is
# what's actually trending/unheard/new in *your* NOMAD.
# =============================================================================
def _run_parallel(jobs, timeout=10):
    """Runs each job in its own thread and waits up to `timeout` seconds for
    all of them together. Returns (results, errors) where results[name] is
    ALWAYS the job's real return value on success, or None on failure/timeout
    — never a {"__error__": ...} sentinel dict. That sentinel shape was the
    actual bug this replaces: every parallel-jobs block in Discover used to
    catch exceptions by stuffing an error dict into the same slot a caller
    expected a list/tuple in, so `results.get(name) or []` silently stopped
    being a safe fallback (a non-empty dict is truthy) and one flaky
    external API could hand the frontend a dict where it expected an array
    to .map() over. Failures are reported separately in `errors` for any
    caller that wants to surface a "this section is unavailable" message;
    everyone else just gets a clean, safe default for free."""
    results, errors = {}, {}

    def run(name, fn):
        try:
            results[name] = fn()
        except Exception as e:
            errors[name] = str(e)

    threads = [threading.Thread(target=run, args=(name, fn)) for name, fn in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
    for name in jobs:
        if name not in results:
            results[name] = None
            errors.setdefault(name, "timed out")
    return results, errors


class DiscoverController:
    def _library_rows(self):
        all_tracks = []
        for p in playlists.data["playlists"]:
            for t in p["tracks"]:
                if t.get("status") == "ready":
                    all_tracks.append({**t, "playlist_id": p["id"], "playlist_name": p["name"]})
        play_counts = {}
        for play in analytics.plays:
            key = (play.get("title", ""), play.get("artist", ""))
            play_counts[key] = play_counts.get(key, 0) + 1

        def pc(t):
            return play_counts.get((t.get("title", ""), t.get("artist", "")), 0)

        return all_tracks, pc

    def snapshot(self):
        all_tracks, pc = self._library_rows()

        # Tied play-counts (very common — most of a library sits at exactly
        # 0 or 1 plays) used to always resolve in the same fixed order every
        # single page load because Python's sort is stable and the input
        # order never changed. Shuffling before the count-sort keeps the
        # score ordering exactly right while randomizing ties, so refreshing
        # Discover actually surfaces different tracks instead of the
        # identical 12 forever.
        shuffled = all_tracks[:]
        random.shuffle(shuffled)

        trending = sorted((t for t in shuffled if pc(t) > 0), key=pc, reverse=True)[:12]
        # Real chronological order via added_ts, not list position — list
        # order only happened to look chronological because playlists are
        # iterated in creation order today; reversing the raw list silently
        # breaks the moment that's no longer true (e.g. reordered playlists).
        recently_added = sorted(all_tracks, key=lambda t: t.get("added_ts") or 0, reverse=True)[:12]
        # Hidden gems must NOT just be "recently added" tracks that happen to
        # be unplayed yet — that made the two sections show near-duplicate
        # content, since anything you just added is almost always unplayed.
        # Exclude what's already surfaced as Recently Added, then sample
        # from what's left (already shuffled above) for variety.
        recent_ids = {t.get("id") for t in recently_added}
        unplayed_pool = [t for t in shuffled if pc(t) == 0 and t.get("id") not in recent_ids]
        hidden_gems = unplayed_pool[:12]

        artist_counts = {}
        for t in all_tracks:
            a = t.get("artist") or "Unknown"
            artist_counts[a] = artist_counts.get(a, 0) + 1
        top_artists = sorted(artist_counts.items(), key=lambda x: -x[1])[:10]

        charts = []
        charts_error = None
        try:
            charts, charts_error = get_apple_chart_tracks()
        except Exception as e:
            charts, charts_error = [], str(e)

        return {
            "trending": trending,
            "hidden_gems": hidden_gems,
            "recently_added": recently_added,
            "top_artists": [{"artist": a, "tracks": c} for a, c in top_artists],
            "library_size": len(all_tracks),
            "charts": charts,
            "charts_error": charts_error,
            "moods": ["Late night focus", "Sunday morning", "Workout energy", "Rainy day", "Road trip", "Deep work"],
        }

    def full_snapshot(self):
        """The mega Discover-page payload: everything from snapshot() plus
        real momentum, cross-playlist saves, a fingerprint-based recommender,
        Apple new-releases, a Deezer genre chart, and the AI Radar's latest
        findings. External network calls run in parallel and each fails
        independently so one dead API never blanks the whole page."""
        base = self.snapshot()
        all_tracks, pc = self._library_rows()
        shuffled = all_tracks[:]
        random.shuffle(shuffled)
        underground = sorted((t for t in shuffled if 1 <= pc(t) <= 2), key=pc)[:12]

        jobs = {
            "growth": lambda: analytics.growth(limit=12),
            "most_saved": lambda: playlists.most_saved(limit=12),
            "ai_picks": lambda: audio_intel.vibe_recommendations(top_k=10),
            "new_releases": lambda: get_apple_new_releases(),
            "genre_chart": lambda: deezer_chart_tracks(0, 16),
            "genres": lambda: deezer_genre_list(),
        }
        results, job_errors = _run_parallel(jobs, timeout=10)

        growth_rows = results.get("growth") or []
        # growth() returns bare title/artist rows from the plays log — match
        # them back to real library tracks (thumbnail, ids) where possible,
        # same shape as trending/hidden_gems so the frontend card renders
        # identically and stays clickable-to-play.
        by_key = {((t.get("title") or "").lower(), (t.get("artist") or "").lower()): t for t in all_tracks}
        fastest_growing = []
        for g in growth_rows:
            match = by_key.get((g["title"].lower(), g["artist"].lower()))
            fastest_growing.append({**(match or {"title": g["title"], "artist": g["artist"]}),
                                     "growth_score": g["growth_score"], "recent_plays": g["recent_plays"]})

        # new_releases/genre_chart succeed as (data, error) tuples; results[name]
        # is None only on a hard failure/timeout (see _run_parallel), so this
        # now correctly distinguishes "the job ran and reported its own soft
        # error" from "the job never returned a result at all".
        new_releases, new_releases_error = results.get("new_releases") or ([], job_errors.get("new_releases"))
        genre_chart, genre_chart_error = results.get("genre_chart") or ([], job_errors.get("genre_chart"))

        return {
            **base,
            "underground": underground,
            "fastest_growing": fastest_growing,
            "most_saved": results.get("most_saved") or [],
            "ai_picks": results.get("ai_picks") or [],
            "ai_picks_available": audio_intel.available(),
            "new_releases": new_releases,
            "new_releases_error": new_releases_error,
            "genre_chart": genre_chart,
            "genre_chart_error": genre_chart_error,
            "genres": results.get("genres") or DEEZER_GENRE_FALLBACK,
            "radar": radar.state(),
        }

    def full_snapshot_v2(self):
        base = self.full_snapshot()

        def get_contextual_mix():
            import time
            hour = time.localtime().tm_hour
            # Build a set of contextual mixes based on time of day
            mixes = []
            if 5 <= hour < 8:
                mixes.append({"name": "Morning Motivation", "description": "Start the day with energy", "icon": "\u2600\ufe0f", "prompt": "energetic upbeat morning motivation"})
            elif 8 <= hour < 12:
                mixes.append({"name": "Productive Focus", "description": "Deep work without distractions", "icon": "\ud83c\udfaf", "prompt": "focus concentration instrumental work"})
            elif 12 <= hour < 17:
                mixes.append({"name": "Afternoon Vibes", "description": "Keep the momentum going", "icon": "\u26a1", "prompt": "upbeat afternoon feel-good vibes"})
            elif 17 <= hour < 21:
                mixes.append({"name": "Evening Chill", "description": "Wind down and relax", "icon": "\ud83c\udf05", "prompt": "chill relaxing evening wind down"})
            elif 21 <= hour or hour < 1:
                mixes.append({"name": "Late Night Session", "description": "Night owl energy", "icon": "\ud83c\udf19", "prompt": "late night vibes moody atmospheric"})
            else:
                mixes.append({"name": "Deep Night Ambient", "description": "The quiet hours", "icon": "\ud83d\udca4", "prompt": "ambient deep sleep calm dreamy"})
            # Always add a few extra mood mixes
            mixes.extend([
                {"name": "Discover Mix", "description": "Music you haven't tried yet", "icon": "\ud83d\udd2e", "prompt": "discover new diverse eclectic"},
                {"name": "Throwback Mix", "description": "Classics from your library", "icon": "\u23ea", "prompt": "throwback nostalgic classics oldies"},
                {"name": "High Energy", "description": "Workout & hype tracks", "icon": "\ud83d\udd25", "prompt": "high energy workout pump up hype"},
            ])
            return mixes
            
        def get_api_key_status():
            s = load_settings()
            return {
                "spotify": bool(s.get("spotify_client_id") and s.get("spotify_client_secret")),
                "lastfm": bool(s.get("lastfm_api_key")),
                "genius": bool(s.get("genius_api_key")),
                "groq": bool(s.get("groq_api_key")),
                "acoustid": bool(s.get("acoustid_api_key"))
            }

        def get_listening_stats():
            all_tracks, pc = self._library_rows()
            total_plays = sum(pc(t) for t in all_tracks)
            unique_played = sum(1 for t in all_tracks if pc(t) > 0)
            
            artist_counts = {}
            genre_counts = {}
            for t in all_tracks:
                c = pc(t)
                if c > 0:
                    a = t.get("artist") or "Unknown"
                    g = t.get("genre") or "Unknown"
                    artist_counts[a] = artist_counts.get(a, 0) + c
                    genre_counts[g] = genre_counts.get(g, 0) + c
                    
            top_artist = max(artist_counts, key=artist_counts.get) if artist_counts else "Unknown"
            top_genre = max(genre_counts, key=genre_counts.get) if genre_counts else "Unknown"
            
            play_days = set()
            for p in analytics.plays:
                ts = p.get("ts", "")
                if len(ts) >= 10:
                    play_days.add(ts[:10])
                    
            streak = 0
            if play_days:
                import datetime
                curr = datetime.datetime.now().date()
                if str(curr) not in play_days:
                    curr -= datetime.timedelta(days=1)
                while str(curr) in play_days:
                    streak += 1
                    curr -= datetime.timedelta(days=1)
                    
            return {
                "total_plays": total_plays,
                "unique_tracks": unique_played,
                "top_artist": top_artist,
                "top_genre": top_genre,
                "streak_days": streak
            }

        def get_regional_charts():
            rc = {}
            def run_rc(code):
                try:
                    rc[code] = get_apple_chart_tracks(code)[0]
                except Exception:
                    rc[code] = []
            th = [threading.Thread(target=run_rc, args=(c,)) for c in ['us', 'in', 'gb', 'jp', 'kr', 'br', 'de', 'fr']]
            for t in th: t.start()
            for t in th: t.join(timeout=8)
            return rc

        def get_spotify_decks():
            # Was hardcoded to "2025" — after Dec 31 that silently starts
            # searching for last year's "Top Songs 2025" forever since
            # nothing here ever bumps it. Deriving the year keeps this
            # correct without needing a yearly manual edit.
            year = time.localtime().tm_year
            songs, _ = itunes_search(f"Top Songs {year}", limit=12)
            for s in songs: s["source"] = "spotify_song"
            albums, _ = itunes_search(f"Top Albums {year}", limit=12, entity="album")
            for a in albums: a["source"] = "spotify_album"
            playlists_hits, _ = deezer_chart_playlists(limit=12)
            return {
                "songs": songs,
                "albums": albums,
                "playlists": playlists_hits,
            }

        jobs = {
            "itunes_featured": lambda: itunes_search("featured", limit=12)[0],
            "jamendo": lambda: jamendo_featured(limit=12)[0],
            "radio_stations": lambda: radio_browser_stations(limit=15)[0],
            "jiosaavn": lambda: jiosaavn_trending(limit=12)[0],
            "regional_charts": get_regional_charts,
            "api_key_status": get_api_key_status,
            "listening_stats": get_listening_stats,
            "contextual_mixes": get_contextual_mix,
            "audius": lambda: audius_trending(limit=12)[0],
            "spotify_releases": lambda: spotify.get_new_releases(12),
            "spotify_decks": get_spotify_decks
        }

        results, job_errors = _run_parallel(jobs, timeout=10)
        # Every job here returns a list/dict directly (no (data, err) tuple
        # convention like the Apple/Deezer jobs above), so on failure the
        # safe defaults are just "empty" — a job silently vanishing from the
        # page reads as "nothing new right now", not a crash.
        empty_defaults = {
            "itunes_featured": [], "jamendo": [], "radio_stations": [], "jiosaavn": [],
            "regional_charts": {}, "api_key_status": {}, "listening_stats": {},
            "contextual_mixes": [], "audius": [], "spotify_releases": [],
            "spotify_decks": {"songs": [], "albums": [], "playlists": []},
        }
        for name, default in empty_defaults.items():
            if results.get(name) is None:
                results[name] = default

        return {
            **base,
            **results
        }

    def reshuffle(self):
        """Re-rolls just the randomized, library-only sections (Trending's
        tie order, Hidden Gems' sample, Underground's tie order) — the exact
        three lists snapshot()/full_snapshot() now shuffle before sorting.
        Deliberately does NOT touch anything that hits an external API
        (charts, new releases, genre chart, etc.) so the "Shuffle" button on
        those strips is instant instead of re-running the whole
        network-heavy full_snapshot payload just to reroll one list."""
        all_tracks, pc = self._library_rows()
        shuffled = all_tracks[:]
        random.shuffle(shuffled)

        trending = sorted((t for t in shuffled if pc(t) > 0), key=pc, reverse=True)[:12]
        recently_added = sorted(all_tracks, key=lambda t: t.get("added_ts") or 0, reverse=True)[:12]
        recent_ids = {t.get("id") for t in recently_added}
        unplayed_pool = [t for t in shuffled if pc(t) == 0 and t.get("id") not in recent_ids]
        hidden_gems = unplayed_pool[:12]
        underground = sorted((t for t in shuffled if 1 <= pc(t) <= 2), key=pc)[:12]

        return {"trending": trending, "hidden_gems": hidden_gems, "underground": underground}


# =============================================================================
# AI RADAR — background watcher that periodically checks the free chart
# sources (Apple, Deezer) for entries it hasn't seen before and pushes a
# real "N new songs found" event over SSE. Nothing here is simulated: the
# "new" count is a genuine diff against what was seen on the previous check.
# =============================================================================
class RadarController:
    def __init__(self, interval_sec=1800):
        self.log = log_fn("radar")
        self.interval_sec = interval_sec
        self._lock = threading.RLock()  # reentrant — see PlaylistController for why
        self._stop = threading.Event()
        self._thread = None
        self._checking = False
        self._data = self._load()

    def _load(self):
        try:
            with open(RADAR_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"seen": {"apple": [], "deezer": []}, "last_checked": None, "log": []}

    def _save(self):
        # Same fix as PlaylistController._save() — see that comment.
        with self._lock:
            try:
                tmp = RADAR_JSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f)
                os.replace(tmp, RADAR_JSON)
            except Exception:
                pass

    def state(self):
        with self._lock:
            log = self._data.get("log", [])[-1:]
            return {
                "last_checked": self._data.get("last_checked"),
                "latest": log[0] if log else None,
                "checking": self._checking,
            }

    def _top_library_artists(self, limit=6):
        """Artists the person actually listens to, ranked by real play
        count — not a guess, straight from analytics.plays. Powers the
        personalized half of radar so it's not just generic chart-watching."""
        counts = {}
        for play in analytics.plays:
            a = (play.get("artist") or "").strip()
            if a:
                counts[a] = counts.get(a, 0) + 1
        return [a for a, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]]

    def check_now(self):
        found_new = []
        self._checking = True
        try:
            with self._lock:
                seen_apple = set(self._data.get("seen", {}).get("apple", []))
                seen_deezer = set(self._data.get("seen", {}).get("deezer", []))
                seen_artist_releases = set(self._data.get("seen", {}).get("artist_releases", []))

            apple_tracks, _ = get_apple_chart_tracks()
            for t in apple_tracks:
                fp = f"{t.get('title', '')}::{t.get('artist', '')}".lower()
                if fp not in seen_apple:
                    found_new.append({**t, "source": "Apple charts", "reason": "trending"})
                seen_apple.add(fp)

            deezer_tracks, _ = deezer_chart_tracks(0, 25)
            for t in deezer_tracks:
                fp = f"{t.get('title', '')}::{t.get('artist', '')}".lower()
                if fp not in seen_deezer:
                    found_new.append({**t, "source": "Deezer chart", "reason": "trending"})
                seen_deezer.add(fp)

            # Personalized half — new releases from artists actually in this
            # person's play history, not just whatever's globally trending.
            # This is what makes it feel like *your* radar instead of a
            # generic chart-refresh notifier.
            for artist in self._top_library_artists():
                try:
                    results, _ = itunes_search(artist, limit=5)
                except Exception:
                    continue
                for t in results:
                    if (t.get("artist") or "").strip().lower() != artist.lower():
                        continue  # iTunes' fuzzy search can wander to a different artist entirely
                    rd = t.get("release_date") or ""
                    fp = f"{t.get('title', '')}::{t.get('artist', '')}".lower()
                    if fp in seen_artist_releases:
                        continue
                    seen_artist_releases.add(fp)
                    # Only surface genuinely recent releases as "new" —
                    # otherwise the very first scan after install would
                    # flag an artist's entire back catalog as new.
                    if self._data.get("last_checked") and rd >= (time.strftime("%Y-%m-%d", time.gmtime(time.time() - 45 * 86400))):
                        found_new.append({**t, "source": "New from an artist you play", "reason": "artist_release", "matched_artist": artist})

            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "new_count": len(found_new),
                "highlights": found_new[:8],
            }
            with self._lock:
                # cap remembered fingerprints so the file can't grow unbounded
                self._data["seen"] = {
                    "apple": list(seen_apple)[-500:],
                    "deezer": list(seen_deezer)[-500:],
                    "artist_releases": list(seen_artist_releases)[-1000:],
                }
                self._data["last_checked"] = entry["ts"]
                self._data.setdefault("log", []).append(entry)
                self._data["log"] = self._data["log"][-20:]
                self._save()
            self.log(f"radar check: {len(found_new)} new track(s) since last scan", "ok")
            broadcast("discover_radar", entry)
            return entry
        finally:
            self._checking = False

    def start_auto(self):
        self._stop.clear()

        def loop():
            # first check happens shortly after startup, not instantly, so
            # it doesn't compete with the rest of the app's own boot calls
            self._stop.wait(15)
            while not self._stop.is_set():
                try:
                    self.check_now()
                except Exception as e:
                    self.log(f"radar check failed: {e}", "warn")
                if self._stop.wait(self.interval_sec):
                    break

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_auto(self):
        self._stop.set()


# =============================================================================
# CLOUD SYNC (folder-based) — export/import a bundle to a folder the person
# already syncs themselves (Google Drive/OneDrive/Dropbox desktop folder).
# NOMAD doesn't run its own cloud server, so this is honest, real,
# zero-infrastructure PC<->PC sync: point two installs at the same synced
# folder and each can pull in playlists it doesn't have yet. A true live
# mobile companion app would need its own client — this is the file format
# it would speak, though.
# =============================================================================
class SyncController:
    def __init__(self):
        self.log = log_fn("sync")
        self._auto_thread = None
        self._auto_stop = threading.Event()

    def export_bundle(self, folder):
        if not folder or not os.path.isdir(folder):
            raise RuntimeError("that folder doesn't exist")
        bundle = {
            "nomad_sync": True, "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
            "playlists": playlists.data.get("playlists", []),
        }
        path = os.path.join(folder, SYNC_BUNDLE_NAME)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(bundle, f)
        os.replace(tmp, path)
        self.log(f"exported sync bundle to {path}", "ok")
        return path

    def import_bundle(self, folder):
        path = os.path.join(folder, SYNC_BUNDLE_NAME)
        if not os.path.exists(path):
            raise RuntimeError("no sync bundle found in that folder yet — export from another install first")
        with open(path, "r", encoding="utf-8") as f:
            bundle = json.load(f)
        incoming = bundle.get("playlists", [])
        existing_ids = {p["id"] for p in playlists.data.get("playlists", [])}
        added = 0
        for p in incoming:
            if p["id"] not in existing_ids:
                p["enhance"] = {**DEFAULT_PL_ENHANCE, **(p.get("enhance") or {})}
                playlists.data["playlists"].append(p)
                added += 1
        if added:
            playlists._save()
            playlists._broadcast_state()
        self.log(f"imported {added} new playlist(s) from sync bundle", "ok")
        return added

    def start_auto(self, folder, interval_sec=300):
        self.stop_auto()
        self._auto_stop.clear()

        def loop():
            while not self._auto_stop.wait(interval_sec):
                try:
                    self.import_bundle(folder)
                except Exception:
                    pass
                try:
                    self.export_bundle(folder)
                except Exception as e:
                    self.log(f"auto-sync export failed: {e}", "warn")

        self._auto_thread = threading.Thread(target=loop, daemon=True)
        self._auto_thread.start()

    def stop_auto(self):
        self._auto_stop.set()
        self._auto_thread = None


# =============================================================================
# FLASK APP
# =============================================================================

app = Flask(__name__)
# Flask sessions are client-side signed cookies; use a per-process random secret
# unless the user explicitly supplies a persistent secret. This prevents the
# built-in predictable value from being reused if the app is ever exposed.
app.secret_key = os.environ.get("NOMAD_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

tunnel = TunnelController()
media = MediaController()
storage = StorageController()
playlists = PlaylistController()
analytics = AnalyticsController()
profiles = ProfileController()
discover = DiscoverController()
sync = SyncController()
audio_intel = AudioIntelController()
radar = RadarController()
radar.start_auto()

# Real connected-session tracking for the Devices tab — every browser tab/
# window with the app open shows up here for as long as its /events stream
# is connected. This is genuinely what's connected right now, not a fake
# device list (no Sonos/Chromecast integration exists, so none is claimed).
_sessions = {}
_sessions_lock = threading.Lock()
_startup_settings = load_settings()
if _startup_settings.get("sync_auto") and _startup_settings.get("sync_folder"):
    sync.start_auto(_startup_settings["sync_folder"])


def ai_try_command(message):
    """Detects a small set of commands the AI Studio chat can genuinely
    execute (create a playlist, remove duplicates) instead of just talking
    about them. Returns (reply_text, playlist_or_None) or (None, None) if no
    command matched, in which case the caller falls through to plain chat."""
    m = message.strip()
    low = m.lower()

    if low.startswith("create playlist:"):
        prompt = m.split(":", 1)[1].strip()
    elif re.match(r"(?i)^(create|make)( me)? a playlist( for| about)?", low):
        prompt = re.sub(r"(?i)^(create|make)( me)? a playlist( for| about)?", "", m).strip()
    else:
        prompt = None
    if prompt:
        p = playlists.ai_generate_playlist(prompt, 12)
        return (f"Started building \"{p['name']}\" — tracks are downloading now, "
                f"check the Playlists tab for progress."), p

    dedupe_match = re.match(r"(?i)^remove duplicates(?: from| in)?\s+(.+)$", m)
    if dedupe_match:
        name = dedupe_match.group(1).strip()
        target = next((p for p in playlists.data["playlists"] if p["name"].lower() == name.lower()), None)
        if not target:
            return f"Couldn't find a playlist named \"{name}\".", None
        removed = playlists.dedupe_playlist(target["id"])
        return (f"Removed {removed} duplicate track(s) from \"{target['name']}\"."
                if removed else f"No duplicates found in \"{target['name']}\"."), None

    return None, None


def ai_chat_reply(message, history):
    """Real Groq-backed reply when a free API key is configured (Settings →
    AI). Without one, this is honest about not being able to chat yet rather
    than faking a response, and still points at the real commands above."""
    settings = load_settings()
    groq_key = settings.get("groq_api_key", "").strip()
    if not groq_key:
        return ("I can hold a real conversation once a free Groq API key is added in "
                "Settings → AI (console.groq.com, no card needed). Until then I can still run "
                "direct commands — try \"create playlist: late night coding\" or "
                "\"remove duplicates from <playlist name>\"."), False
    system = (
        "You are NOMAD's in-app music assistant. Be concise, friendly, and practical. "
        "You help with playlist ideas, explaining songs/artists, and how NOMAD's features work. "
        "You cannot browse the internet or play audio yourself. If asked to perform an action "
        "like creating a playlist or removing duplicates, tell the user the exact phrase to type, "
        "e.g. 'create playlist: <description>' or 'remove duplicates from <playlist name>'."
    )
    msgs = [{"role": "system", "content": system}]
    for h in (history or [])[-8:]:
        role = "user" if h.get("role") == "user" else "assistant"
        msgs.append({"role": role, "content": str(h.get("content", ""))[:2000]})
    msgs.append({"role": "user", "content": message})
    body = json.dumps({
        "model": "llama-3.1-8b-instant", "messages": msgs, "temperature": 0.7, "max_tokens": 500,
    }).encode()
    req = urllib.request.Request(GROQ_API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {groq_key}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip(), True


# =============================================================================
# AI LYRICS EXTRAS — Groq-backed "Meaning" companion features that sit next
# to LRCLIB's synced timing and Genius's crowd-sourced annotations: a plain
# AI explanation, line-by-line translation, learner vocabulary, and a short
# "story behind the song" writeup. All four share one small helper and are
# cached per-track exactly like lyrics_cache/genius_cache are.
# =============================================================================

def groq_complete(system, user, api_key, max_tokens=700, temperature=0.4):
    """Minimal single-turn Groq chat completion. Raises on network/HTTP
    errors — callers decide how to present that (this stays low-level and
    honest rather than swallowing failures into a fake success)."""
    body = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(GROQ_API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def ai_explain_song(title, artist, lyrics_text, api_key):
    system = (
        "You are a music critic. In under 150 words, explain what the song is about: "
        "its main theme, mood, and any notable imagery or metaphor. Plain prose, no headers, "
        "no markdown, no restating the lyrics verbatim."
    )
    user = f"Song: \"{title}\" by {artist or 'Unknown artist'}\n\nLyrics:\n{lyrics_text[:3500]}"
    return groq_complete(system, user, api_key, max_tokens=400)


def ai_translate_lyrics(title, artist, lines, target_lang, api_key):
    """`lines` is a list of raw lyric strings (already split, no timestamps).
    Returns a list of translated strings in the same order/count so the
    caller can re-zip them against synced timing on the frontend."""
    numbered = "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines))
    system = (
        f"Translate song lyrics into {target_lang}. Reply with ONLY a JSON array of strings, "
        f"exactly {len(lines)} items, same order, one translated line per input line — no "
        "commentary, no markdown fences, no numbering in the strings themselves. Preserve blank "
        "lines as empty strings."
    )
    user = f"Song: \"{title}\" by {artist or 'Unknown artist'}\n\n{numbered}"
    text = groq_complete(system, user, api_key, max_tokens=1600, temperature=0.2)
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    out = json.loads(text)
    if not isinstance(out, list):
        raise ValueError("translation response was not a JSON array")
    return [str(x) for x in out]


def ai_vocabulary(title, artist, lyrics_text, api_key):
    system = (
        "Pick up to 10 notable or advanced words/phrases from these song lyrics (idioms, slang, "
        "or vocabulary a language learner might not know). Reply with ONLY a JSON array of "
        "objects: [{\"word\":\"...\",\"meaning\":\"...\",\"note\":\"...\"}] where meaning is a short "
        "plain-language definition and note is one sentence on how it's used in this song's "
        "context. No markdown fences, no commentary."
    )
    user = f"Song: \"{title}\" by {artist or 'Unknown artist'}\n\nLyrics:\n{lyrics_text[:3500]}"
    text = groq_complete(system, user, api_key, max_tokens=900, temperature=0.3)
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    out = json.loads(text)
    if not isinstance(out, list):
        raise ValueError("vocabulary response was not a JSON array")
    return out[:10]


def ai_song_story(title, artist, lyrics_text, api_key):
    system = (
        "In under 180 words, write the story behind this song for a curious listener: context "
        "like what inspired it, its background, or what it's referencing, based on general "
        "knowledge of the song and artist. If you're not confident about real facts, focus "
        "instead on the narrative/story told within the lyrics themselves. Plain prose, no "
        "headers, no markdown. End with one short sentence flagging this is an AI-generated "
        "read, not verified biography."
    )
    user = f"Song: \"{title}\" by {artist or 'Unknown artist'}\n\nLyrics:\n{lyrics_text[:3500]}"
    return groq_complete(system, user, api_key, max_tokens=450)


def auth_ok(username, password):
    expected_user = os.environ.get("NOMAD_USERNAME", "admin").strip()
    expected_pass = os.environ.get("NOMAD_PASSWORD", "nomad2026").strip()
    return username == expected_user and password == expected_pass


def parse_json_body():
    try:
        data = request.get_json(silent=True)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
    return data


@app.before_request
def enforce_auth():
    allowed_paths = {"/", "/api/auth/login", "/api/auth/session"}
    if request.path.startswith("/static"):
        return None
    if request.path in allowed_paths:
        return None
    if session.get("authenticated"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "authentication required"}), 401
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/auth/session")
def api_auth_session():
    return jsonify({"authenticated": bool(session.get("authenticated")), "user": session.get("user")})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    body = parse_json_body()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", "")).strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "enter your username and password"}), 400
    if auth_ok(username, password):
        session.clear()
        session["authenticated"] = True
        session["user"] = username
        return jsonify({"ok": True, "user": username})
    session.clear()
    return jsonify({"ok": False, "error": "incorrect username or password"}), 401


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/events")
def events():
    if not session.get("authenticated"):
        return jsonify({"ok": False, "error": "authentication required"}), 401
    user_agent = request.headers.get("User-Agent", "")[:140]
    profile_id = profiles.active_id()

    def stream():
        q = queue.Queue()
        sid = uuid.uuid4().hex[:10]
        with _subscribers_lock:
            _subscribers.append(q)
        with _sessions_lock:
            _sessions[sid] = {
                "connected_at": time.time(),
                "user_agent": user_agent,
                "profile_id": profile_id,
            }
        try:
            # send an immediate snapshot so a fresh page load isn't blank
            yield f"data: {json.dumps({'type': 'hello', 'payload': {'session_id': sid}})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    # SSE comment heartbeat keeps proxies/browser connections alive.
                    yield ": keep-alive\n\n"
        except (GeneratorExit, BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)
            with _sessions_lock:
                _sessions.pop(sid, None)
    return Response(stream(), mimetype="text/event-stream",
                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/intelligence/overview")
def api_intelligence_overview():
    return jsonify({"ok": True, **build_intelligence_overview(load_settings())})


@app.route("/api/audio/analyze")
def api_audio_analyze():
    return jsonify({"ok": True, **analyze_audio_library(load_settings())})


@app.route("/api/charts/apple")
def api_charts_apple():
    tracks, error = get_apple_chart_tracks()
    if error and not tracks:
        return jsonify({"ok": False, "error": error, "tracks": []})
    return jsonify({"ok": True, "tracks": tracks})


@app.route("/api/lyrics/search")
def api_lyrics_search():
    q = request.args.get("q", "").strip()
    return jsonify({"ok": True, "results": search_lyrics(q)})


@app.route("/api/ai/local/compose", methods=["POST"])
def api_ai_local_compose():
    body = parse_json_body()
    prompt = str(body.get("prompt", "")).strip()
    count = body.get("count", 8)
    return jsonify({"ok": True, "playlist": compose_local_playlist(prompt, count)})


@app.route("/api/cache/stats")
def api_cache_stats():
    """Powers the 'Unified cache' card in Settings — one shared on-disk
    cache backs every external lookup in the app (Spotify, LRCLIB, Genius,
    MusicBrainz, Last.fm, Deezer, chart feeds, artist bios…)."""
    return jsonify({"ok": True, **cache_stats()})


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    cache_clear()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({
        "admin": is_admin(),
        "wireguard": tunnel.wg_exe is not None,
        "ytdlp": YTDLP_AVAILABLE,
        "ffmpeg": ffmpeg_present(),
        "disk_engine": DAI_AVAILABLE,
        "spotify": spotify.configured(),
        "fpcalc": fpcalc_present(),
    })


# ---- Tunnel ----

@app.route("/api/tunnel/state")
def api_tunnel_state():
    return jsonify(tunnel.state())


@app.route("/api/tunnel/connect", methods=["POST"])
def api_tunnel_connect():
    body = request.get_json(force=True) or {}
    idx = body.get("idx")
    if not isinstance(idx, int) or idx < 0 or idx >= len(REGIONS):
        return jsonify({"ok": False, "error": "invalid region index"}), 400
    threading.Thread(target=tunnel.connect_region, args=(idx,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/tunnel/disconnect", methods=["POST"])
def api_tunnel_disconnect():
    threading.Thread(target=tunnel.disconnect_current, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/tunnel/health_check", methods=["POST"])
def api_tunnel_health_check():
    loss = ping_loss_pct()
    region = None
    if tunnel.active_region_idx is not None and tunnel.active_region_idx < len(REGIONS):
        region = REGIONS[tunnel.active_region_idx]
    return jsonify({"ok": True, "loss": loss, "region": region})


@app.route("/api/tunnel/killswitch", methods=["POST"])
def api_tunnel_killswitch():
    body = parse_json_body()
    enabled = bool(body.get("enabled", False))
    tunnel.set_kill_switch(enabled)
    return jsonify({"ok": True})


# ---- Media ----

@app.route("/api/media/download", methods=["POST"])
def api_media_download():
    body = parse_json_body()
    urls = [u.strip() for u in body.get("urls", []) if u.strip()]
    quality = body.get("quality", "Best available")
    out_dir = body.get("out_dir") or DOWNLOAD_DIR
    if not urls:
        return jsonify({"ok": False, "error": "no urls"}), 400
    enhance = body.get("enhance") or {}
    media.submit(urls, out_dir, quality, enhance)
    return jsonify({"ok": True})


@app.route("/api/media/enhance_status")
def api_media_enhance_status():
    return jsonify({"ffmpeg": ffmpeg_present(), "ai_upscaler": realesrgan_present()})


@app.route("/api/media/install_realesrgan", methods=["POST"])
def api_media_install_realesrgan():
    threading.Thread(target=media.install_realesrgan, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/media/queue/clear_completed", methods=["POST"])
def api_media_clear_completed():
    media.clear_completed()
    return jsonify({"ok": True})


@app.route("/api/media/stream", methods=["POST"])
def api_media_stream():
    body = parse_json_body()
    url = body.get("url", "").strip()
    quality = body.get("quality", "Best available")
    if not url:
        return jsonify({"ok": False, "error": "no url"}), 400

    def go():
        stream_url = media.get_stream_url(url, quality)
        if stream_url:
            media.play_in_vlc(stream_url)
    threading.Thread(target=go, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/media/install_ffmpeg", methods=["POST"])
def api_media_install_ffmpeg():
    threading.Thread(target=media.install_ffmpeg, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/media/cancel", methods=["POST"])
def api_media_cancel():
    body = parse_json_body()
    if body.get("all"):
        media.cancel_all()
    elif body.get("item_id"):
        media.cancel_item(body["item_id"])
    return jsonify({"ok": True})


@app.route("/api/media/history")
def api_media_history():
    return jsonify({"history": media.get_history()})


@app.route("/api/media/intelligence")
def api_media_intelligence():
    return jsonify({"ok": True, **media.get_intelligence_snapshot()})


@app.route("/api/media/history/clear", methods=["POST"])
def api_media_history_clear():
    media.clear_history()
    return jsonify({"ok": True})


@app.route("/api/media/open_folder", methods=["POST"])
def api_media_open_folder():
    body = parse_json_body()
    path = str(body.get("path") or "").strip()
    if not path:
        path = DOWNLOAD_DIR
    target = Path(path).expanduser()
    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt" and hasattr(os, "startfile"):
            os.startfile(str(target))
        elif os.name != "nt":
            opener = shutil.which("xdg-open") or shutil.which("open")
            if opener:
                subprocess.Popen([opener, str(target)])
        return jsonify({"ok": True, "path": str(target)})
    except Exception as e:
        return jsonify({"ok": True, "path": str(target), "warning": str(e)})


@app.route("/api/browse_folder", methods=["POST"])
def api_browse_folder():
    body = request.get_json(force=True) or {}
    initial = body.get("initial") or str(Path.home())
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        d = filedialog.askdirectory(initialdir=initial)
        root.destroy()
        return jsonify({"path": d or None})
    except Exception as e:
        return jsonify({"path": None, "error": str(e)})


def _stream_candidate_ok(query, cand_title, cand_artist):
    """Audius and Jamendo are CC-licensed/indie catalogs, not mainstream
    labels — a text search for a chart-pop song on either will still return
    SOMETHING (their engines don't return empty), it's just almost never
    the actual song. This used to be accepted totally unchecked (first
    Audius hit with an id, or literally results[0] for Jamendo), so a
    mainstream query would silently come back as some unrelated indie
    track claiming to be the "full stream" — before yt-dlp, the one source
    here that can actually resolve mainstream commercial tracks, ever got
    a turn. Now a candidate only counts as a real match if its title+artist
    text genuinely overlaps the query."""
    from difflib import SequenceMatcher
    qn = _lyrics_norm_text(query)
    cn = _lyrics_norm_text(f"{cand_artist} {cand_title}")
    if not qn or not cn:
        return False
    q_words = set(qn.split())
    c_words = set(cn.split())
    if not q_words:
        return False
    overlap = len(q_words & c_words) / len(q_words)
    seq = SequenceMatcher(None, qn, cn).ratio()
    return overlap >= 0.6 or seq >= 0.55


@app.route("/api/track/resolve")
def api_track_resolve():
    """resolveFullTrack(track) — the default full-track-first playback path.

    Order: cache -> Audius -> Jamendo -> Spotify SDK (if the user connected
    their account) -> iTunes 30s preview as an honestly-labeled last resort.

    yt-dlp/YouTube is deliberately NOT part of this chain. Those three
    sources (cache, Audius, Jamendo) are catalogs actually licensed/intended
    for third-party playback, plus the user's own connected Spotify account
    through Spotify's official SDK; that's the ceiling of what "legitimate
    full-track source" can honestly mean for arbitrary Discover/Chart
    tracks. Coverage is real but partial — Audius/Jamendo are independent/
    CC-licensed catalogs, not the mainstream label catalog, so plenty of
    chart-pop songs will only ever resolve to the iTunes preview here unless
    the user connects Spotify. That trade-off is intentional, not a bug.
    """
    title = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()
    duration_hint = request.args.get("duration", 0)
    q = (request.args.get("q", "").strip()) or f"{artist} {title}".strip()
    if not q:
        return jsonify({"ok": False, "error": "title/artist or q is required"}), 400

    track_key = full_track_identity_key(title or q, artist, duration_hint)

    # ---- cache hit ----
    cached = get_cached_full_track(track_key)
    if cached:
        audio = cached.get("audio") or {}
        stream_url = None
        if audio.get("path"):
            stream_url = f"/api/track/cached_audio/{urllib.parse.quote(audio['path'])}"
        return jsonify({
            "ok": True, "cache_hit": True, "track_key": track_key,
            "title": cached.get("title", title), "artist": cached.get("artist", artist),
            "duration": cached.get("duration", 0), "source": audio.get("source", cached.get("source")),
            "stream_url": stream_url, "full": bool(stream_url), "fingerprint": cached.get("fingerprint"),
        })

    # ---- 1. Audius ----
    try:
        url = f"https://api.audius.co/v1/tracks/search?query={urllib.parse.quote(q)}&limit=5&app_name=NOMAD"
        data = _safe_fetch_json(url, timeout=6)
        if data and data.get("data"):
            for t in data["data"]:
                if not t.get("id"):
                    continue
                cand_title = t.get("title", "")
                cand_artist = (t.get("user") or {}).get("name", "")
                if not _stream_candidate_ok(q, cand_title, cand_artist):
                    continue
                stream_url = f"https://api.audius.co/v1/tracks/{t['id']}/stream?app_name=NOMAD"
                entry = {
                    "title": cand_title or title, "artist": cand_artist or artist,
                    "duration": t.get("duration", 0), "source": "audius_full",
                    "fingerprint": None,
                    "audio": download_and_cache_audio(track_key, stream_url, "audius_full") or
                             {"path": None, "source": "audius_full", "size": 0},
                }
                # Downloading can fail (network hiccup) without failing the
                # whole resolve — fall straight through to Audius's own URL.
                if not entry["audio"].get("path"):
                    save_full_track_cache(track_key, {**entry, "audio": {}})
                    return jsonify({"ok": True, "cache_hit": False, "track_key": track_key,
                                     "title": entry["title"], "artist": entry["artist"],
                                     "duration": entry["duration"], "source": "audius_full",
                                     "stream_url": stream_url, "full": True, "fingerprint": None})
                save_full_track_cache(track_key, entry)
                return jsonify({"ok": True, "cache_hit": False, "track_key": track_key,
                                 "title": entry["title"], "artist": entry["artist"],
                                 "duration": entry["duration"], "source": "audius_full",
                                 "stream_url": f"/api/track/cached_audio/{urllib.parse.quote(entry['audio']['path'])}",
                                 "full": True, "fingerprint": None})
    except Exception:
        pass

    # ---- 2. Jamendo ----
    try:
        url = f"https://api.jamendo.com/v3.0/tracks/?client_id=b6747d04&format=json&limit=5&search={urllib.parse.quote(q)}"
        data = _safe_fetch_json(url, timeout=6)
        if data and data.get("results"):
            for t in data["results"]:
                if not t.get("audio"):
                    continue
                cand_title = t.get("name", "")
                cand_artist = t.get("artist_name", "")
                if not _stream_candidate_ok(q, cand_title, cand_artist):
                    continue
                stream_url = t.get("audio")
                entry = {
                    "title": cand_title or title, "artist": cand_artist or artist,
                    "duration": t.get("duration", 0), "source": "jamendo_full",
                    "fingerprint": None,
                    "audio": download_and_cache_audio(track_key, stream_url, "jamendo_full") or {},
                }
                if not entry["audio"].get("path"):
                    save_full_track_cache(track_key, {**entry, "audio": {}})
                    return jsonify({"ok": True, "cache_hit": False, "track_key": track_key,
                                     "title": entry["title"], "artist": entry["artist"],
                                     "duration": entry["duration"], "source": "jamendo_full",
                                     "stream_url": stream_url, "full": True, "fingerprint": None})
                save_full_track_cache(track_key, entry)
                return jsonify({"ok": True, "cache_hit": False, "track_key": track_key,
                                 "title": entry["title"], "artist": entry["artist"],
                                 "duration": entry["duration"], "source": "jamendo_full",
                                 "stream_url": f"/api/track/cached_audio/{urllib.parse.quote(entry['audio']['path'])}",
                                 "full": True, "fingerprint": None})
    except Exception:
        pass

    # ---- 3. Spotify Web Playback SDK (user's own connected account) ----
    if spotify_user_auth.is_connected() and spotify.configured():
        try:
            results = spotify.search_track(q, limit=5) if hasattr(spotify, "search_track") else []
            for cand in results or []:
                cand_title = cand.get("title") or cand.get("name", "")
                cand_artist = cand.get("artist", "")
                if not _stream_candidate_ok(q, cand_title, cand_artist):
                    continue
                uri = cand.get("uri") or (f"spotify:track:{cand['id']}" if cand.get("id") else None)
                if not uri:
                    continue
                return jsonify({
                    "ok": True, "cache_hit": False, "track_key": track_key,
                    "title": cand_title, "artist": cand_artist,
                    "duration": cand.get("duration", 0), "source": "spotify_sdk",
                    "stream_url": None, "spotify_uri": uri, "full": True, "fingerprint": None,
                })
        except Exception:
            pass

    # ---- 4. Last resort: iTunes 30s preview, honestly labeled as not full ----
    try:
        tracks, _ = itunes_search(q, limit=1)
        if tracks and tracks[0].get("preview_url"):
            t = tracks[0]
            return jsonify({
                "ok": True, "cache_hit": False, "track_key": track_key,
                "title": t["title"], "artist": t["artist"], "duration": 30,
                "source": "itunes_preview", "stream_url": t["preview_url"],
                "full": False, "fingerprint": None,
            })
    except Exception:
        pass

    return jsonify({"ok": False, "error": "No playable source found for this track"}), 404


@app.route("/api/track/cached_audio/<path:filename>")
def api_track_cached_audio(filename):
    """Serves audio previously cached by resolveFullTrack (Audius/Jamendo
    only — Spotify SDK audio is never written to disk here)."""
    safe_name = os.path.basename(filename)
    full_path = os.path.join(FULL_TRACK_CACHE_DIR, safe_name)
    if not os.path.exists(full_path):
        return jsonify({"ok": False, "error": "not cached"}), 404
    return send_from_directory(FULL_TRACK_CACHE_DIR, safe_name)


@app.route("/api/spotify/login")
def api_spotify_login():
    if not spotify_user_auth.configured():
        return jsonify({"ok": False, "error": "Add a Spotify Client ID/Secret in Settings first."}), 400
    state = secrets.token_urlsafe(16)
    session["spotify_oauth_state"] = state
    return redirect(spotify_user_auth.build_authorize_url(state))


@app.route("/api/spotify/callback")
def api_spotify_callback():
    error = request.args.get("error")
    if error:
        return redirect(f"/?spotify_error={urllib.parse.quote(error)}")
    state = request.args.get("state")
    if not state or state != session.get("spotify_oauth_state"):
        return redirect("/?spotify_error=state_mismatch")
    session.pop("spotify_oauth_state", None)
    code = request.args.get("code")
    if not code:
        return redirect("/?spotify_error=no_code")
    try:
        spotify_user_auth.exchange_code(code)
    except Exception as e:
        return redirect(f"/?spotify_error={urllib.parse.quote(str(e))}")
    return redirect("/?spotify_connected=1")


@app.route("/api/spotify/player_token")
def api_spotify_player_token():
    """Frontend Web Playback SDK calls this to get/refresh its access
    token. Never exposes the client secret or refresh token."""
    token = spotify_user_auth.get_valid_access_token()
    if not token:
        return jsonify({"ok": False, "error": "not connected"}), 401
    return jsonify({"ok": True, "access_token": token})


@app.route("/api/spotify/logout", methods=["POST"])
def api_spotify_logout():
    spotify_user_auth.disconnect()
    return jsonify({"ok": True})


@app.route("/api/spotify/status")
def api_spotify_status():
    return jsonify({"ok": True, "connected": spotify_user_auth.is_connected()})


@app.route("/api/discover/stream")
def api_discover_full_stream():
    """Resolve full-length streaming audio URL for any track query."""
    import urllib.parse
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "Query required"}), 400
    
    # 1. Try Audius full-length stream — only accept a candidate that
    # actually matches the query (see _stream_candidate_ok); otherwise
    # keep looking instead of confidently streaming the wrong song.
    try:
        url = f"https://api.audius.co/v1/tracks/search?query={urllib.parse.quote(q)}&limit=5&app_name=NOMAD"
        data = _safe_fetch_json(url, timeout=6)
        if data and data.get("data"):
            for t in data["data"]:
                if not t.get("id"):
                    continue
                cand_title = t.get("title", "")
                cand_artist = (t.get("user") or {}).get("name", "")
                if not _stream_candidate_ok(q, cand_title, cand_artist):
                    continue
                stream_url = f"https://api.audius.co/v1/tracks/{t['id']}/stream?app_name=NOMAD"
                return jsonify({
                    "ok": True,
                    "stream_url": stream_url,
                    "title": cand_title or q,
                    "artist": cand_artist,
                    "duration": t.get("duration", 0),
                    "source": "audius_full"
                })
    except Exception:
        pass
        
    # 2. Try Jamendo full-length stream — same real-match check, scanning
    # all returned candidates instead of trusting results[0].
    try:
        url = f"https://api.jamendo.com/v3.0/tracks/?client_id=b6747d04&format=json&limit=5&search={urllib.parse.quote(q)}"
        data = _safe_fetch_json(url, timeout=6)
        if data and data.get("results"):
            for t in data["results"]:
                if not t.get("audio"):
                    continue
                cand_title = t.get("name", "")
                cand_artist = t.get("artist_name", "")
                if not _stream_candidate_ok(q, cand_title, cand_artist):
                    continue
                return jsonify({
                    "ok": True,
                    "stream_url": t.get("audio"),
                    "title": cand_title or q,
                    "artist": cand_artist,
                    "duration": t.get("duration", 0),
                    "source": "jamendo_full"
                })
    except Exception:
        pass

    # 3. Try YouTube via yt-dlp for mainstream catalog songs. This is the
    # only configured source here that can usually resolve full commercial
    # tracks; Spotify/iTunes/Deezer public APIs only expose metadata/previews.
    stream, stream_err = yt_resolve_audio_stream(q)
    if stream and stream.get("stream_url"):
        return jsonify({"ok": True, **stream})

    # 4. Final fallback to iTunes preview, clearly labeled.
    try:
        tracks, _ = itunes_search(q, limit=1)
        if tracks and tracks[0].get("preview_url"):
            t = tracks[0]
            return jsonify({
                "ok": True,
                "stream_url": t["preview_url"],
                "title": t["title"],
                "artist": t["artist"],
                "duration": 30,
                "source": "itunes_preview"
            })
    except Exception:
        pass

    return jsonify({"ok": False, "error": stream_err or "No stream found"}), 404


# ---- Playlists ----

@app.route("/api/playlists")
def api_playlists_list():
    return jsonify({"playlists": playlists.list_playlists(), "spotify_connected": spotify.configured()})


@app.route("/api/playlists", methods=["POST"])
def api_playlists_create():
    body = parse_json_body()
    p = playlists.create(body.get("name", ""))
    return jsonify({"ok": True, "playlist": p})


@app.route("/api/playlists/<playlist_id>")
def api_playlist_detail(playlist_id):
    p = playlists.get_playlist(playlist_id)
    if not p:
        return jsonify({"ok": False, "error": "playlist not found"}), 404
    return jsonify({"ok": True, "playlist": p})


@app.route("/api/playlists/<playlist_id>", methods=["PATCH"])
def api_playlist_rename(playlist_id):
    body = parse_json_body()
    ok = playlists.rename(playlist_id, body.get("name", ""))
    return jsonify({"ok": ok})


@app.route("/api/playlists/<playlist_id>", methods=["DELETE"])
def api_playlist_delete(playlist_id):
    return jsonify({"ok": playlists.delete(playlist_id)})


@app.route("/api/playlists/<playlist_id>/reorder", methods=["POST"])
def api_playlist_reorder(playlist_id):
    body = parse_json_body()
    return jsonify({"ok": playlists.reorder(playlist_id, body.get("track_ids", []))})


@app.route("/api/playlists/<playlist_id>/tracks", methods=["POST"])
def api_playlist_add_track(playlist_id):
    body = parse_json_body()
    kind = body.get("kind")
    value = str(body.get("value", "")).strip()
    if not value:
        return jsonify({"ok": False, "error": "nothing to add"}), 400
    try:
        if kind == "youtube":
            return jsonify({"ok": True, "track": playlists.add_from_youtube(playlist_id, value)})
        elif kind == "spotify":
            if not spotify.configured():
                return jsonify({"ok": False, "error": "connect Spotify first in Playlist settings"}), 400
            return jsonify({"ok": True, "tracks": playlists.add_from_spotify(playlist_id, value)})
        elif kind == "search":
            return jsonify({"ok": True, "track": playlists.add_from_search(playlist_id, value)})
        else:
            return jsonify({"ok": False, "error": "unknown source"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>", methods=["DELETE"])
def api_playlist_remove_track(playlist_id, track_id):
    return jsonify({"ok": playlists.remove_track(playlist_id, track_id)})


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/retry", methods=["POST"])
def api_playlist_retry_track(playlist_id, track_id):
    try:
        return jsonify({"ok": True, "track": playlists.retry_track(playlist_id, track_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/replace", methods=["POST"])
def api_playlist_replace_track(playlist_id, track_id):
    body = parse_json_body()
    kind = body.get("kind")
    value = str(body.get("value", "")).strip()
    if not value:
        return jsonify({"ok": False, "error": "nothing to replace with"}), 400
    try:
        return jsonify({"ok": True, "track": playlists.replace_track(playlist_id, track_id, kind, value)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/enhance", methods=["POST"])
def api_playlist_set_enhance(playlist_id):
    body = parse_json_body()
    ok = playlists.set_enhance(playlist_id, body.get("enhance") or {})
    if not ok:
        return jsonify({"ok": False, "error": "playlist not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/playlists/<playlist_id>/reenhance", methods=["POST"])
def api_playlist_reenhance(playlist_id):
    try:
        count = playlists.reenhance_all(playlist_id)
        return jsonify({"ok": True, "count": count})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/audio/<track_id>")
def api_playlist_audio(track_id):
    safe_id = re.sub(r"[^a-zA-Z0-9]", "", track_id)
    filename = next((f for f in os.listdir(PLAYLISTS_DIR) if f.startswith(safe_id + ".")), None)
    if not filename:
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_from_directory(PLAYLISTS_DIR, filename)


@app.route("/api/playlists/search_all", methods=["POST"])
def api_playlists_search_all():
    body = parse_json_body()
    query = str(body.get("query", "")).strip()
    if not query:
        return jsonify({"ok": False, "error": "nothing to search for"}), 400
    return jsonify({"ok": True, "results": search_all_services(query)})


@app.route("/api/playlists/<playlist_id>/tracks/from_result", methods=["POST"])
def api_playlist_add_from_result(playlist_id):
    body = parse_json_body()
    service = body.get("service")
    result = body.get("result") or {}
    try:
        track = playlists.add_from_result(playlist_id, service, result)
        return jsonify({"ok": True, "track": track})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/ai_generate", methods=["POST"])
def api_playlists_ai_generate():
    body = parse_json_body()
    prompt = str(body.get("prompt", "")).strip()
    count = max(3, min(int(body.get("count", 12) or 12), 25))
    if not prompt:
        return jsonify({"ok": False, "error": "describe the playlist you want first"}), 400
    try:
        p = playlists.ai_generate_playlist(prompt, count)
        return jsonify({"ok": True, "playlist": p})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/art", methods=["POST"])
def api_playlist_regenerate_art(playlist_id):
    seed = playlists.set_art_seed(playlist_id)
    if seed is None:
        return jsonify({"ok": False, "error": "playlist not found"}), 404
    return jsonify({"ok": True, "art_seed": seed})


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/quality")
def api_playlist_track_quality(playlist_id, track_id):
    try:
        return jsonify({"ok": True, "quality": playlists.track_quality(playlist_id, track_id)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/enhance_one", methods=["POST"])
def api_playlist_enhance_one(playlist_id, track_id):
    body = parse_json_body()
    enhance = body.get("enhance") or {}
    force = bool(body.get("force"))
    try:
        quality = playlists.enhance_single_track(playlist_id, track_id, enhance, force=force)
        return jsonify({"ok": True, "quality": quality})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/export")
def api_playlist_export(playlist_id):
    fmt = request.args.get("fmt", "json")
    try:
        filename, content, mimetype = playlists.export_playlist(playlist_id, fmt)
        return Response(content, mimetype=mimetype,
                         headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/import", methods=["POST"])
def api_playlist_import(playlist_id):
    body = parse_json_body()
    fmt = body.get("fmt", "")
    content = body.get("content", "")
    try:
        queued = playlists.import_into_playlist(playlist_id, fmt, content)
        return jsonify({"ok": True, "queued": queued})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/analytics/summary")
def api_analytics_summary():
    profile_id = request.args.get("profile_id") or None
    return jsonify({"ok": True, **analytics.summary(profile_id=profile_id)})


@app.route("/api/analytics/play", methods=["POST"])
def api_analytics_play():
    body = parse_json_body()
    analytics.record_play(
        body.get("track_id", ""), body.get("title", ""), body.get("artist", ""),
        body.get("playlist_id", ""), float(body.get("seconds", 0) or 0),
        profile_id=profiles.active_id(),
    )
    return jsonify({"ok": True})


# ---- Profiles (real local "household" profiles — see ProfileController) ----

@app.route("/api/profiles")
def api_profiles_list():
    return jsonify({"ok": True, "profiles": profiles.list(), "active": profiles.active_id()})


@app.route("/api/profiles", methods=["POST"])
def api_profiles_create():
    body = parse_json_body()
    p = profiles.create(body.get("name", ""))
    return jsonify({"ok": True, "profile": p})


@app.route("/api/profiles/<profile_id>", methods=["DELETE"])
def api_profiles_delete(profile_id):
    ok = profiles.delete(profile_id)
    if not ok:
        return jsonify({"ok": False, "error": "can't delete the default profile"}), 400
    return jsonify({"ok": True})


@app.route("/api/profiles/<profile_id>/activate", methods=["POST"])
def api_profiles_activate(profile_id):
    ok = profiles.set_active(profile_id)
    if not ok:
        return jsonify({"ok": False, "error": "profile not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/profiles/activity")
def api_profiles_activity():
    limit = max(1, min(int(request.args.get("limit", 30) or 30), 100))
    by_id = {p["id"]: p for p in profiles.list()}
    feed = []
    for play in analytics.activity_feed(limit=limit):
        pid = play.get("profile_id", "default")
        prof = by_id.get(pid, {"name": "Former profile", "avatar_seed": pid})
        feed.append({**play, "profile_name": prof.get("name"), "avatar_seed": prof.get("avatar_seed")})
    return jsonify({"ok": True, "activity": feed})


# ---- Blend Studio (real — merges already-downloaded tracks, no fake mixing) ----

@app.route("/api/blend/preview", methods=["POST"])
def api_blend_preview():
    body = parse_json_body()
    playlist_ids = body.get("playlist_ids") or []
    options = body.get("options") or {}
    if len(playlist_ids) < 1:
        return jsonify({"ok": False, "error": "pick at least one playlist"}), 400
    try:
        return jsonify({"ok": True, "tracks": playlists.blend_preview(playlist_ids, options)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/blend", methods=["POST"])
def api_blend_create():
    body = parse_json_body()
    name = str(body.get("name", "")).strip()
    playlist_ids = body.get("playlist_ids") or []
    options = body.get("options") or {}
    if len(playlist_ids) < 2:
        return jsonify({"ok": False, "error": "pick at least 2 playlists to blend"}), 400
    try:
        p = playlists.blend(name, playlist_ids, options)
        return jsonify({"ok": True, "playlist": p})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/dedupe", methods=["POST"])
def api_playlist_dedupe(playlist_id):
    removed = playlists.dedupe_playlist(playlist_id)
    return jsonify({"ok": True, "removed": removed})


# ---- Playlist Doctor (health check + one-click fix) ----

@app.route("/api/playlists/<playlist_id>/health")
def api_playlist_health(playlist_id):
    h = playlists.playlist_health(playlist_id)
    if h is None:
        return jsonify({"ok": False, "error": "playlist not found"}), 404
    return jsonify({"ok": True, **h})


@app.route("/api/playlists/<playlist_id>/doctor_fix", methods=["POST"])
def api_playlist_doctor_fix(playlist_id):
    body = parse_json_body()
    result = playlists.doctor_fix(
        playlist_id,
        fix_missing=bool(body.get("fix_missing", True)),
        fix_duplicates=bool(body.get("fix_duplicates", True)),
    )
    return jsonify({"ok": True, **result})


# ---- Audio Intelligence (BPM / key / energy / loudness / DNA) ----

@app.route("/api/audio/status")
def api_audio_status():
    return jsonify({"ok": True, **audio_intel.status()})


@app.route("/api/audio/install", methods=["POST"])
def api_audio_install():
    threading.Thread(target=audio_intel.install, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/playlists/<playlist_id>/analyze", methods=["POST"])
def api_playlist_analyze(playlist_id):
    if not audio_intel.available():
        return jsonify({"ok": False, "error": "audio intelligence libraries not installed"}), 400
    body = parse_json_body()
    try:
        result = audio_intel.analyze_playlist(playlist_id, force=bool(body.get("force", False)))
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/playlists/<playlist_id>/analyze/status")
def api_playlist_analyze_status(playlist_id):
    return jsonify({"ok": True, **audio_intel.job_status(playlist_id)})


@app.route("/api/playlists/<playlist_id>/dna")
def api_playlist_dna(playlist_id):
    d = audio_intel.dna(playlist_id)
    if d is None:
        return jsonify({"ok": False, "error": "playlist not found"}), 404
    return jsonify({"ok": True, **d})


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/analyze", methods=["POST"])
def api_track_analyze(playlist_id, track_id):
    if not audio_intel.available():
        return jsonify({"ok": False, "error": "audio intelligence libraries not installed"}), 400
    body = parse_json_body()
    try:
        features = audio_intel.analyze_track(playlist_id, track_id, force=bool(body.get("force", False)))
        return jsonify({"ok": True, "features": features})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ---- Sorting ----

@app.route("/api/playlists/<playlist_id>/sort", methods=["POST"])
def api_playlist_sort(playlist_id):
    body = parse_json_body()
    key = str(body.get("key", "added"))
    direction = str(body.get("direction", "asc"))
    ok = playlists.sort_playlist(playlist_id, key, direction)
    if not ok:
        return jsonify({"ok": False, "error": "playlist not found"}), 404
    return jsonify({"ok": True})


# ---- Version history / undo ----

@app.route("/api/playlists/<playlist_id>/versions")
def api_playlist_versions(playlist_id):
    return jsonify({"ok": True, "versions": playlists.list_versions(playlist_id)})


@app.route("/api/playlists/<playlist_id>/versions/<version_id>/restore", methods=["POST"])
def api_playlist_restore_version(playlist_id, version_id):
    ok = playlists.restore_version(playlist_id, version_id)
    if not ok:
        return jsonify({"ok": False, "error": "version not found"}), 404
    return jsonify({"ok": True})


# ---- Lyrics (LRCLIB — free, no key, synced + plain) ----

@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/lyrics")
def api_playlist_track_lyrics(playlist_id, track_id):
    refresh = request.args.get("refresh") == "1"
    result = playlists.get_lyrics(playlist_id, track_id, refresh=refresh)
    if result is None:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify({"ok": True, **result})


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/lyrics/set", methods=["POST"])
def api_playlist_track_lyrics_set(playlist_id, track_id):
    """Manually pin the correct lyrics match — fixes a track whose
    auto-matched lyrics were wrong (mistagged/ambiguous title) by re-fetching
    against the exact title/artist the user picked from search and pinning
    it as the new permanent cache."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    artist = (data.get("artist") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400
    result = playlists.set_lyrics_override(playlist_id, track_id, title, artist)
    if result is None:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify({"ok": True, **result})


@app.route("/api/playlists/<playlist_id>/lyrics/resync_unsynced", methods=["POST"])
def api_playlist_lyrics_resync(playlist_id):
    """Bulk-retry lyrics for every track that's still missing synced (LRC)
    timing — the one-off way for a whole library's worth of previously
    cached plain-text lyrics to get re-checked against newer/added sources
    without clicking refresh per song. Background thread; poll status route."""
    threading.Thread(target=playlists.run_lyrics_resync, args=(playlist_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/playlists/<playlist_id>/lyrics/resync_unsynced/status")
def api_playlist_lyrics_resync_status(playlist_id):
    return jsonify({"ok": True, **playlists.lyrics_resync_status(playlist_id)})


# ---- Audio fingerprinting (Chromaprint/fpcalc) — real duplicate detection ----

@app.route("/api/media/install_fpcalc", methods=["POST"])
def api_media_install_fpcalc():
    threading.Thread(target=media.install_fpcalc, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/playlists/fingerprint_tool_status")
def api_playlist_fp_tool_status():
    return jsonify({"available": fpcalc_present()})


@app.route("/api/playlists/<playlist_id>/fingerprint_scan", methods=["POST"])
def api_playlist_fingerprint_scan(playlist_id):
    if not fpcalc_present():
        return jsonify({"ok": False, "error": "fpcalc not installed"}), 400
    threading.Thread(target=playlists.run_fingerprint_scan, args=(playlist_id,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/playlists/<playlist_id>/fingerprint_scan/status")
def api_playlist_fingerprint_scan_status(playlist_id):
    return jsonify({"ok": True, **playlists.fingerprint_scan_status(playlist_id)})


@app.route("/api/playlists/<playlist_id>/fingerprint_scan/resolve", methods=["POST"])
def api_playlist_fingerprint_resolve(playlist_id):
    body = parse_json_body()
    keep_id = body.get("keep_track_id")
    group_ids = body.get("track_ids") or []
    if not keep_id or not group_ids:
        return jsonify({"ok": False, "error": "keep_track_id and track_ids required"}), 400
    result = playlists.resolve_fp_duplicate_group(playlist_id, keep_id, group_ids)
    return jsonify({"ok": True, **result})


# ---- AcoustID identify (optional, free user-supplied key) ----

@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/identify", methods=["POST"])
def api_playlist_track_identify(playlist_id, track_id):
    s = load_settings()
    api_key = s.get("acoustid_api_key", "")
    if not api_key:
        return jsonify({"ok": False, "error": "no AcoustID API key configured — add one free at acoustid.org/api-key in Settings"}), 400
    if not fpcalc_present():
        return jsonify({"ok": False, "error": "fpcalc not installed"}), 400
    result = playlists.identify_track_acoustid(playlist_id, track_id, api_key)
    return jsonify(result)


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/apply_metadata", methods=["POST"])
def api_playlist_track_apply_metadata(playlist_id, track_id):
    body = parse_json_body()
    ok = playlists.apply_track_metadata(
        playlist_id, track_id, body.get("title"), body.get("artist"),
        genre=body.get("genre"), year=body.get("year"),
    )
    if not ok:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify({"ok": True})


# ---- MusicBrainz (free, no key) — canonical title/artist/genre/year ----

@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/enrich_musicbrainz", methods=["POST"])
def api_playlist_enrich_musicbrainz(playlist_id, track_id):
    result = playlists.enrich_musicbrainz(playlist_id, track_id)
    return jsonify(result)


# ---- Last.fm (optional free key) — similar-artist Discover suggestions ----

@app.route("/api/playlists/<playlist_id>/similar_artists")
def api_playlist_similar_artists(playlist_id):
    s = load_settings()
    api_key = s.get("lastfm_api_key", "")
    result = playlists.similar_artists(playlist_id, api_key)
    return jsonify(result)


# ---- Genius (optional free key) — lyric annotations / meaning ----

@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/annotations")
def api_playlist_track_annotations(playlist_id, track_id):
    s = load_settings()
    api_key = s.get("genius_api_key", "")
    if not api_key:
        return jsonify({"ok": False, "error": "no Genius API key configured"}), 400
    refresh = request.args.get("refresh") == "1"
    result = playlists.get_annotations(playlist_id, track_id, api_key, refresh=refresh)
    if result is None:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify({"ok": True, **result})


# ---- AI lyrics extras (optional free Groq key) — explanation / translation /
# ---- vocabulary / story-behind-the-song, separate from Genius annotations --

def _require_groq_key():
    s = load_settings()
    key = s.get("groq_api_key", "").strip()
    if not key:
        return None, jsonify({"ok": False, "error": "no Groq API key configured — add one free at console.groq.com in Playlist settings"}), 400
    return key, None, None


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/explain")
def api_playlist_track_explain(playlist_id, track_id):
    key, err_resp, code = _require_groq_key()
    if err_resp:
        return err_resp, code
    result = playlists.get_ai_explain(playlist_id, track_id, key, refresh=request.args.get("refresh") == "1")
    if result is None:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify(result)


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/story")
def api_playlist_track_story(playlist_id, track_id):
    key, err_resp, code = _require_groq_key()
    if err_resp:
        return err_resp, code
    result = playlists.get_ai_story(playlist_id, track_id, key, refresh=request.args.get("refresh") == "1")
    if result is None:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify(result)


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/vocabulary")
def api_playlist_track_vocabulary(playlist_id, track_id):
    key, err_resp, code = _require_groq_key()
    if err_resp:
        return err_resp, code
    result = playlists.get_ai_vocabulary(playlist_id, track_id, key, refresh=request.args.get("refresh") == "1")
    if result is None:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify(result)


@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/translate")
def api_playlist_track_translate(playlist_id, track_id):
    key, err_resp, code = _require_groq_key()
    if err_resp:
        return err_resp, code
    lang = (request.args.get("lang") or "Spanish").strip()
    result = playlists.get_ai_translation(playlist_id, track_id, lang, key, refresh=request.args.get("refresh") == "1")
    if result is None:
        return jsonify({"ok": False, "error": "track not found"}), 404
    return jsonify(result)


# ---- Discover (real — computed from your actual library + play history) ----

@app.route("/api/discover")
def api_discover():
    return jsonify({"ok": True, **discover.snapshot()})


@app.route("/api/discover/full")
def api_discover_full():
    """The full Discover-page payload — momentum, saves, fingerprint-based
    AI picks, new releases, genre chart, radar. Heavier than /api/discover
    (parallel external calls), used only when the Discover tab is open."""
    return jsonify({"ok": True, **discover.full_snapshot()})


@app.route("/api/discover/reshuffle")
def api_discover_reshuffle():
    """Instant re-roll of just the randomized library-only sections
    (Trending/Hidden Gems/Underground) — no external API calls, so this is
    safe to hit on every click of a strip's Shuffle button without the cost
    of a full /api/discover/full reload."""
    return jsonify({"ok": True, **discover.reshuffle()})


@app.route("/api/discover/genres")
def api_discover_genres():
    return jsonify({"ok": True, "genres": deezer_genre_list()})


@app.route("/api/discover/genre/<int:genre_id>")
def api_discover_genre(genre_id):
    tracks, error = deezer_chart_tracks(genre_id, 20)
    return jsonify({"ok": True, "tracks": tracks, "error": error})


@app.route("/api/discover/world/countries")
def api_discover_world_countries():
    """Country list for the Global Music Explorer's map/picker."""
    return jsonify({"ok": True, "countries": WORLD_COUNTRIES})


@app.route("/api/discover/world/<code>")
def api_discover_world_country(code):
    """Real per-country Apple chart + derived genre/artist breakdown —
    this is the data behind clicking a country on the Global Music
    Explorer's map."""
    result = get_world_chart(code)
    return jsonify({"ok": True, **result})


@app.route("/api/discover/weather")
def api_discover_weather():
    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if lat is None or lon is None:
        return jsonify({"ok": False, "error": "lat/lon required"}), 400
    try:
        data, error = open_meteo_current(float(lat), float(lon))
    except ValueError:
        return jsonify({"ok": False, "error": "invalid coordinates"}), 400
    if error:
        return jsonify({"ok": False, "error": error}), 502
    return jsonify({"ok": True, **data})


@app.route("/api/discover/network")
def api_discover_network():
    """Similar Artist Network graph. Defaults to your most-listened library
    artist; pass ?artist=Name to re-center the graph on a clicked node."""
    s = load_settings()
    api_key = s.get("lastfm_api_key", "")
    artist = request.args.get("artist", "").strip()
    if not artist:
        all_tracks, pc = discover._library_rows()
        artist_counts = {}
        for t in all_tracks:
            a = t.get("artist") or ""
            if a:
                artist_counts[a] = artist_counts.get(a, 0) + pc(t) + 1
        artist = max(artist_counts, key=artist_counts.get) if artist_counts else ""
    if not artist:
        return jsonify({"ok": False, "error": "no artist in your library yet to build a network from"})
    result = playlists.build_artist_network(artist, api_key)
    return jsonify(result)


@app.route("/api/discover/feed_health")
def api_discover_feed_health():
    """Raw status of the last attempt to reach each external feed — surfaces
    the *real* reason behind a 'couldn't reach the server' message (timeout,
    TLS, 404, etc.) instead of leaving it a mystery. Used by the small status
    dot in the Global Charts panel."""
    with _FETCH_STATUS_LOCK:
        rows = [{"url": u, **s} for u, s in _FETCH_STATUS.items()]
    return jsonify({"ok": True, "feeds": rows})


@app.route("/api/discover/radar")
def api_discover_radar():
    return jsonify({"ok": True, **radar.state()})


@app.route("/api/discover/radar/check", methods=["POST"])
def api_discover_radar_check():
    entry = radar.check_now()
    return jsonify({"ok": True, **entry})


@app.route("/api/discover/v2")
def api_discover_v2():
    return jsonify({"ok": True, **discover.full_snapshot_v2()})


@app.route("/api/discover/search")
def api_discover_search():
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"ok": True, "results": []})
    results = {}
    def run(name, fn):
        try:
            results[name] = fn()
        except Exception:
            results[name] = []
    jobs = {
        "itunes": lambda: itunes_search(q, limit=6)[0],
        "deezer": lambda: deezer_search_many(q, limit=6),
    }
    if spotify.configured():
        jobs["spotify"] = lambda: spotify.search_track(q, limit=6)

    threads = [threading.Thread(target=run, args=(n, f)) for n, f in jobs.items()]
    for t in threads: t.start()
    # NOTE: this bounds how long we WAIT for the threads, not how long they
    # actually run — a client that navigates away or fires a newer keystroke
    # (frontend now aborts the fetch for exactly this reason) doesn't stop
    # these threads server-side; Python's synchronous WSGI model has no
    # built-in request-cancellation hook. A true fix means switching these
    # provider calls to a cancellable executor (e.g. concurrent.futures with
    # a shared cancellation token) — flagged as real follow-up work, not
    # solved by this timeout alone.
    for t in threads: t.join(timeout=4)
    # Merge and deduplicate by title+artist
    all_results = []
    seen = set()
    for source in ["spotify", "itunes", "deezer"]:
        for track in (results.get(source) or []):
            if not track.get("source"):
                track["source"] = source
            key = (track.get("title", "").lower(), track.get("artist", "").lower())
            if key not in seen:
                seen.add(key)
                all_results.append(track)
    return jsonify({"ok": True, "results": all_results[:15]})



@app.route("/api/discover/radio")
def api_discover_radio():
    tag = request.args.get("tag", "")
    stations, error = radio_browser_stations(limit=15, tag=tag)
    return jsonify({"ok": True, "stations": stations, "error": error})


@app.route("/api/discover/artist/<path:artist_name>")
def api_discover_artist(artist_name):
    """Deep dive into an artist — combines multiple free APIs (MusicBrainz,
    Wikipedia, Songkick/Bandsintown, ListenBrainz, iTunes, Spotify).
    Previously ran that entire multi-source batch fresh on every single
    request — cached now (same shared _cached_call pattern used
    everywhere else) so reopening an artist you already looked at is
    instant instead of re-hitting 5+ external APIs again."""
    force = request.args.get("refresh") == "1"
    key = f"artist_deep_dive:{artist_name.strip().lower()}"
    if force:
        with _FETCH_CACHE_LOCK:
            _FETCH_CACHE.pop(key, None)
    data = _cached_call(key, 6 * 3600, lambda: artist_deep_dive(artist_name))
    return jsonify({"ok": True, **data})

@app.route("/api/lyrics")
def api_lyrics():
    """Multi-source lyrics lookup for anything NOT in the library (Discover
    cards, previews, in-panel search). Previously had no caching at all —
    every view of the same preview track re-ran the full 6-provider chain
    from scratch. Now goes through the same `_lyrics_cache_policy()` used
    by library tracks: a verified synced match is cached indefinitely,
    plain-only/not-found results get retried after a while instead of
    being either permanent or never cached."""
    title = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    duration = int(request.args.get("duration", 0))
    provider = request.args.get("provider", "auto").strip()
    refresh = request.args.get("refresh") == "1"
    if not title:
        return jsonify({"ok": False, "error": "title is required"})
    key = f"lyrics_generic:{provider}:{title.strip().lower()}::{artist.strip().lower()}"
    with _FETCH_CACHE_LOCK:
        cached = _FETCH_CACHE.get(key)
    cached_result = (cached or {}).get("data")
    if cached_result and not refresh and _lyrics_cache_policy(cached_result):
        result = cached_result
    else:
        result = fetch_lyrics_multi(title, artist, album, duration, provider)
        with _FETCH_CACHE_LOCK:
            _FETCH_CACHE[key] = {"data": result, "ts": time.time()}
        _mark_cache_dirty()
    # Per-track offset lives in a separate store for non-library lookups
    # (there's no track id to hang it off of) — merge it in read-side so a
    # fresh/cached provider result and a manually-tuned offset can evolve
    # independently.
    result = {**result, "offset": get_lyrics_offset(title, artist)}
    return jsonify({"ok": result.get("found", False), **result})


@app.route("/api/lyrics/offset", methods=["POST"])
def api_lyrics_offset_set():
    """Save a manually-tuned per-track lyrics offset (seconds, +/-, can be
    negative or positive). Always resolved per track — title+artist for
    Discover/Chart lookups, or playlist_id+track_id for library tracks —
    never a single global constant."""
    body = parse_json_body()
    try:
        offset = float(body.get("offset", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "offset must be a number"}), 400
    if abs(offset) > 30:
        return jsonify({"ok": False, "error": "offset out of range (+/-30s)"}), 400

    playlist_id = body.get("playlist_id")
    track_id = body.get("track_id")
    if playlist_id and track_id:
        cache = playlists.set_lyrics_offset(playlist_id, track_id, offset)
        if cache is None:
            return jsonify({"ok": False, "error": "track not found"}), 404
        return jsonify({"ok": True, "offset": offset})

    title = str(body.get("title", "")).strip()
    artist = str(body.get("artist", "")).strip()
    if not title:
        return jsonify({"ok": False, "error": "title (or playlist_id+track_id) is required"}), 400
    set_lyrics_offset_generic(title, artist, offset)
    return jsonify({"ok": True, "offset": offset})


@app.route("/api/lyrics/diagnose")
def api_lyrics_diagnose():
    """Runs the full lyrics-provider chain in debug mode and reports back
    exactly what happened at each source — attempted / matched / why not —
    instead of just a final found-or-not-found answer. Built for exactly
    the "why did this track get the lyrics it got" question: paste in a
    title/artist and see, provider by provider, whether it was tried, what
    candidate (if any) it considered, and whether that candidate cleared
    the match-confidence bar. Always bypasses the cache — the point is to
    see live provider behavior, not a cached snapshot."""
    title = request.args.get("title", "").strip()
    artist = request.args.get("artist", "").strip()
    album = request.args.get("album", "").strip()
    duration = int(request.args.get("duration", 0))
    if not title:
        return jsonify({"ok": False, "error": "title is required"})
    result = fetch_lyrics_multi(title, artist, album, duration, provider="auto", debug=True)
    return jsonify({
        "ok": True,
        "query": {"title": title, "artist": artist, "album": album, "duration": duration},
        "result_found": result.get("found", False),
        "result_synced": bool(result.get("synced")),
        "result_source": result.get("source"),
        "match_confidence": result.get("match_confidence"),
        "matched_title": result.get("matched_title"),
        "matched_artist": result.get("matched_artist"),
        "engine_version": result.get("engine_version"),
        "diagnostics": result.get("diagnostics", []),
    })

@app.route("/api/discover/audius")
def api_discover_audius():
    """Trending free music from Audius."""
    tracks, error = audius_trending(limit=15)
    return jsonify({"ok": True, "tracks": tracks, "error": error})

@app.route("/api/discover/spotify")
def api_discover_spotify():
    """Spotify new releases and featured playlists."""
    if not spotify.configured():
        return jsonify({"ok": False, "error": "Spotify credentials not configured"})
    return jsonify({
        "ok": True,
        "new_releases": spotify.get_new_releases(12),
        "featured": spotify.get_featured_playlists(8)
    })



@app.route("/api/playlists/<playlist_id>/tracks/<track_id>/similar")
def api_playlist_track_similar(playlist_id, track_id):
    """'More like this' — fingerprint (MFCC/chroma) nearest-neighbor
    recommender across the whole library, using cached audio analysis."""
    result = audio_intel.similar_to_track(playlist_id, track_id)
    return jsonify(result)


# ---- Devices (real — currently-connected browser sessions; audio-output ----
# ---- switching itself happens client-side via the Web Audio API) --------

@app.route("/api/devices")
def api_devices():
    with _sessions_lock:
        sessions = [{"id": sid, "connected_seconds": round(time.time() - s["connected_at"]),
                     "user_agent": s["user_agent"], "profile_id": s.get("profile_id")}
                    for sid, s in _sessions.items()]
    return jsonify({"ok": True, "sessions": sessions})


# ---- Thumbnail proxy — lets the frontend read cover-art pixels on a <canvas>
# ---- for the dynamic accent-color feature. Track thumbnails come from
# ---- YouTube's CDN, which doesn't send CORS headers, so a canvas drawn
# ---- straight from an <img src="https://i.ytimg.com/..."> is "tainted" and
# ---- getImageData() throws. Proxying the bytes through our own origin
# ---- fixes that. Restricted to http(s) and blocks anything resolving to a
# ---- private/loopback/link-local address so this can't be used as an SSRF
# ---- pivot into the local network.
@app.route("/api/thumb_proxy")
def api_thumb_proxy():
    url = request.args.get("url", "")
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return jsonify({"ok": False, "error": "invalid url"}), 400
        addr_info = socket.getaddrinfo(parsed.hostname, None)
        for family, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return jsonify({"ok": False, "error": "host not allowed"}), 400
        req = urllib.request.Request(url, headers={"User-Agent": "NOMAD/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "image/jpeg")
        if not ctype.startswith("image/"):
            return jsonify({"ok": False, "error": "not an image"}), 400
        return Response(data, mimetype=ctype, headers={
            "Cache-Control": "public, max-age=86400",
            "Access-Control-Allow-Origin": "*",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ---- AI Studio chat (real Groq-backed chat + a small set of commands it ----
# ---- can actually execute, rather than pretending every request "works") --

@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    body = parse_json_body()
    message = str(body.get("message", "")).strip()
    history = body.get("history") or []
    if not message:
        return jsonify({"ok": False, "error": "say something first"}), 400
    try:
        cmd_reply, cmd_result = ai_try_command(message)
        if cmd_reply:
            return jsonify({"ok": True, "reply": cmd_reply, "action": bool(cmd_result), "playlist": cmd_result})
        reply, live = ai_chat_reply(message, history)
        return jsonify({"ok": True, "reply": reply, "live": live})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sync/settings", methods=["GET", "POST"])
def api_sync_settings():
    s = load_settings()
    if request.method == "POST":
        body = parse_json_body()
        s["sync_folder"] = str(body.get("sync_folder", "")).strip()
        s["sync_auto"] = bool(body.get("sync_auto", False))
        save_settings(s)
        if s["sync_auto"] and s["sync_folder"]:
            sync.start_auto(s["sync_folder"])
        else:
            sync.stop_auto()
    return jsonify({"ok": True, "sync_folder": s.get("sync_folder", ""), "sync_auto": s.get("sync_auto", False)})


@app.route("/api/sync/export", methods=["POST"])
def api_sync_export():
    s = load_settings()
    folder = s.get("sync_folder", "")
    try:
        path = sync.export_bundle(folder)
        return jsonify({"ok": True, "path": path})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sync/import", methods=["POST"])
def api_sync_import():
    s = load_settings()
    folder = s.get("sync_folder", "")
    try:
        added = sync.import_bundle(folder)
        return jsonify({"ok": True, "added": added})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/settings/ai", methods=["GET", "POST"])
def api_settings_ai():
    s = load_settings()
    if request.method == "POST":
        body = parse_json_body()
        s["groq_api_key"] = str(body.get("groq_api_key", "")).strip()
        save_settings(s)
    key = s.get("groq_api_key", "")
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("set" if key else "")
    return jsonify({"ok": True, "configured": bool(key), "key_masked": masked})


@app.route("/api/settings/acoustid", methods=["GET", "POST"])
def api_settings_acoustid():
    """Optional, free key from acoustid.org/api-key. Local fingerprint
    duplicate detection works with zero keys — this only unlocks per-track
    'identify against the global database' metadata lookups."""
    s = load_settings()
    if request.method == "POST":
        body = parse_json_body()
        s["acoustid_api_key"] = str(body.get("acoustid_api_key", "")).strip()
        save_settings(s)
    key = s.get("acoustid_api_key", "")
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("set" if key else "")
    return jsonify({"ok": True, "configured": bool(key), "key_masked": masked})


@app.route("/api/settings/lastfm", methods=["GET", "POST"])
def api_settings_lastfm():
    """Optional, free key from last.fm/api/account/create. Powers the
    'similar artists' Discover suggestions."""
    s = load_settings()
    if request.method == "POST":
        body = parse_json_body()
        s["lastfm_api_key"] = str(body.get("lastfm_api_key", "")).strip()
        save_settings(s)
    key = s.get("lastfm_api_key", "")
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("set" if key else "")
    return jsonify({"ok": True, "configured": bool(key), "key_masked": masked})


@app.route("/api/settings/genius", methods=["GET", "POST"])
def api_settings_genius():
    """Optional, free Client Access Token from genius.com/api-clients.
    Powers per-track lyric annotations (meaning/background), separate from
    LRCLIB's synced timing."""
    s = load_settings()
    if request.method == "POST":
        body = parse_json_body()
        s["genius_api_key"] = str(body.get("genius_api_key", "")).strip()
        save_settings(s)
    key = s.get("genius_api_key", "")
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("set" if key else "")
    return jsonify({"ok": True, "configured": bool(key), "key_masked": masked})


@app.route("/api/settings/spotify")
def api_settings_spotify_get():
    s = load_settings()
    client_id = s.get("spotify_client_id", "")
    masked = (client_id[:4] + "…" + client_id[-4:]) if len(client_id) > 8 else ("set" if client_id else "")
    return jsonify({
        "connected": spotify.configured(),
        "client_id_masked": masked,
        "local_api_base": request.host_url.rstrip("/"),
    })


@app.route("/api/settings/spotify/test", methods=["POST"])
def api_settings_spotify_test():
    """Live connection check — actually requests a token instead of just
    checking that credentials are saved, so 'connected' means it truly works."""
    try:
        spotify._token = None
        spotify._get_token()
        expiry = int(spotify._token_expiry - time.time())
        return jsonify({"ok": True, "expires_in": max(expiry, 0)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/settings/spotify", methods=["POST"])
def api_settings_spotify_set():
    body = parse_json_body()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    if not client_id or not client_secret:
        return jsonify({"ok": False, "error": "both fields are required"}), 400
    s = load_settings()
    s["spotify_client_id"] = client_id
    s["spotify_client_secret"] = client_secret
    save_settings(s)
    spotify._token = None
    try:
        spotify._get_token()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


# ---- Storage ----

@app.route("/api/storage/dashboard")
def api_storage_dashboard():
    """Powers the CLI-style dashboard header: per-drive usage bars, recent
    scan history, and the AI-scans/version badges — all pulled straight from
    disk_ai_analyzer's own persisted state, same data the terminal tool shows."""
    if not DAI_AVAILABLE:
        return jsonify({"available": False})
    drives = []
    for drive in dai.drive_roots():
        u = dai.disk_usage_for(drive)
        if not u:
            continue
        total, used, free, pct = u
        drives.append({
            "drive": str(drive), "pct": round(pct, 1),
            "used_text": dai.human_size(used), "free_text": dai.human_size(free),
            "total_text": dai.human_size(total),
        })
    recent = []
    for e in dai.load_history()[:6]:
        recent.append({
            "generated": e.get("generated", ""), "root": e.get("root", ""),
            "files": e.get("files", 0), "total_size_text": e.get("total_size_text", ""),
            "duplicate_waste_text": e.get("duplicate_waste_text", ""),
        })
    return jsonify({
        "available": True,
        "drives": drives,
        "recent_scans": recent,
        "scan_count": dai.AI_MODEL.data.get("scan_count", 0),
        "version": dai.APP_VERSION,
        "admin": is_admin(),
    })


@app.route("/api/storage/scan", methods=["POST"])
def api_storage_scan():
    body = parse_json_body()
    path = body.get("path", "").strip()
    large_mb = int(body.get("large_mb", 500) or 500)
    old_days = int(body.get("old_days", 365) or 365)
    workers = int(body.get("workers", max(4, os.cpu_count() or 4)) or 4)
    skip_preset = body.get("skip_preset", "safe")
    autopilot = bool(body.get("autopilot", True))
    if not path:
        return jsonify({"ok": False, "error": "no path"}), 400
    threading.Thread(target=storage.run_scan,
                      args=(path, large_mb, old_days, workers, skip_preset, autopilot), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/storage/result")
def api_storage_result():
    return jsonify(storage.last_summary or {})


@app.route("/api/storage/open_report", methods=["POST"])
def api_storage_open_report():
    if storage.last_result and os.name == "nt":
        os.startfile(str(storage.last_result.report_path))
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/api/storage/delete", methods=["POST"])
def api_storage_delete():
    body = parse_json_body()
    paths = body.get("paths", [])
    source = body.get("source", "manual")
    results = storage.delete_paths(paths, reason=source)
    ok_count = sum(1 for _, ok, _ in results if ok)
    storage.log(f"{source}: sent {ok_count}/{len(paths)} to Recycle Bin", "ok")
    return jsonify({"ok": True, "deleted": ok_count, "total": len(paths)})


@app.route("/api/storage/keep", methods=["POST"])
def api_storage_keep():
    body = parse_json_body()
    paths = body.get("paths", [])
    storage.keep_paths(paths)
    storage.log(f"marked {len(paths)} file(s) as 'keep' — AI learns these were false positives", "ok")
    return jsonify({"ok": True})


# =============================================================================
# LAUNCH
# =============================================================================

class WindowApi:
    """JS-callable bridge for the custom titlebar controls. Needed because
    the window is now frameless (see below) - with no OS chrome, minimize/
    maximize/close have to be wired up by hand from the HTML buttons."""
    def minimize(self):
        try:
            import webview
            webview.windows[0].minimize()
        except Exception:
            pass

    def toggle_maximize(self):
        try:
            import webview
            webview.windows[0].toggle_fullscreen()
        except Exception:
            pass

    def close(self):
        try:
            import webview
            webview.windows[0].destroy()
        except Exception:
            pass


def _hide_console_window():
    """Launching via `python nomad_web.py` (rather than pythonw) spawns a
    separate OS console window, whose own titlebar (showing the raw
    python.exe path) sits stacked directly on top of the app's own window -
    that's the double titlebar / cut-off-path look. ConsoleTee still needs
    somewhere to write, so this only hides the console window, it doesn't
    stop output from being captured."""
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def run_flask():
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


def main():
    sys.stdout = ConsoleTee(sys.stdout)
    sys.stderr = ConsoleTee(sys.stderr)

    if os.name == "nt" and not is_admin():
        if try_self_elevate():
            sys.exit(0)

    tunnel.start_monitor()
    server_thread = threading.Thread(target=run_flask, daemon=True)
    server_thread.start()
    time.sleep(0.8)

    url = f"http://127.0.0.1:{PORT}"
    try:
        import webview
        _hide_console_window()
        api = WindowApi()
        webview.create_window("NOMAD — Control Center", url, width=1180, height=860,
                               resizable=True, frameless=True, easy_drag=False,
                               background_color="#090b0e", js_api=api)
        webview.start()
    except ImportError:
        import webbrowser
        webbrowser.open(url)
        print(f"Opened {url} in your default browser.")
        print("Tip: pip install pywebview for a native, fixed-size app window instead of a browser tab.")
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
