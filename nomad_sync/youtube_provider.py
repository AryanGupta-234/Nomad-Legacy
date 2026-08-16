"""Server-side YouTube catalogue provider for NOMAD Discover.

Credentials are read from the process environment and are never returned to the
browser. The provider deliberately exposes normalized music records so Discover
is independent of the upstream API response shape.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

API_ROOT = "https://www.googleapis.com/youtube/v3"
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = int(os.getenv("NOMAD_YOUTUBE_CACHE_TTL", "900"))


def _api_key() -> str:
    return os.getenv("YOUTUBE_API_KEY", "").strip()


def _request(resource: str, params: dict[str, Any]) -> dict[str, Any]:
    key = _api_key()
    if not key:
        raise RuntimeError("YOUTUBE_API_KEY is not configured")
    params = {**params, "key": key}
    cache_key = resource + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params) if k != "key")
    cached = _CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]
    response = requests.get(f"{API_ROOT}/{resource}", params=params, timeout=12)
    response.raise_for_status()
    data = response.json()
    _CACHE[cache_key] = (time.time(), data)
    return data


def _duration(value: str | None) -> int | None:
    if not value or not value.startswith("PT"):
        return None
    import re
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not m:
        return None
    h, minute, second = (int(x or 0) for x in m.groups())
    return h * 3600 + minute * 60 + second


def channel_catalogue(channel_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    channel_id = (channel_id or os.getenv("YOUTUBE_CHANNEL_ID", "")).strip()
    if not channel_id:
        return {"configured": False, "tracks": [], "playlists": []}
    limit = max(1, min(limit, 50))
    channel = _request("channels", {"part": "snippet,contentDetails", "id": channel_id, "maxResults": 1})
    items = channel.get("items", [])
    if not items:
        return {"configured": True, "tracks": [], "playlists": []}
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    playlist = _request("playlistItems", {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": limit})
    video_ids = [x.get("contentDetails", {}).get("videoId") for x in playlist.get("items", [])]
    video_ids = [x for x in video_ids if x]
    details = _request("videos", {"part": "snippet,contentDetails", "id": ",".join(video_ids)}) if video_ids else {"items": []}
    by_id = {x["id"]: x for x in details.get("items", [])}
    tracks = []
    for item in playlist.get("items", []):
        video_id = item.get("contentDetails", {}).get("videoId")
        video = by_id.get(video_id, {})
        snippet = video.get("snippet", item.get("snippet", {}))
        tracks.append({
            "id": f"youtube:{video_id}",
            "provider": "youtube",
            "provider_id": video_id,
            "title": snippet.get("title", "Unknown track"),
            "artist": snippet.get("channelTitle", "Unknown artist"),
            "channel_id": snippet.get("channelId", channel_id),
            "thumbnail": (snippet.get("thumbnails", {}).get("high") or snippet.get("thumbnails", {}).get("default") or {}).get("url"),
            "published_at": snippet.get("publishedAt"),
            "description": snippet.get("description", ""),
            "duration": _duration(video.get("contentDetails", {}).get("duration")),
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    return {"configured": True, "channel_id": channel_id, "tracks": tracks}
