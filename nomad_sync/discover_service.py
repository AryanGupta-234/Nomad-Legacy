"""Normalized Discover service backed by the YouTube provider."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .youtube_provider import channel_catalogue


def discover(limit: int = 50) -> dict[str, Any]:
    catalogue = channel_catalogue(limit=limit)
    tracks = catalogue.get("tracks", [])
    artists = Counter(t.get("artist") or "Unknown artist" for t in tracks)
    recent = tracks[:12]
    return {
        "configured": catalogue.get("configured", False),
        "source": "youtube",
        "tracks": tracks,
        "recent": recent,
        "artists": [{"name": name, "count": count} for name, count in artists.most_common(20)],
        "recommendations": tracks[12:24],
    }
