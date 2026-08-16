"""Normalized Discover service backed by the YouTube provider."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .recommendation_engine import recommend
from .youtube_provider import channel_catalogue


def discover(limit: int = 50) -> dict[str, Any]:
    """Return a stable Discover payload for the existing NOMAD UI.

    The payload intentionally keeps presentation concerns out of the provider:
    callers receive canonical tracks, artist aggregates, recent items and a
    deterministic recommendation list.
    """
    catalogue = channel_catalogue(limit=limit)
    tracks = catalogue.get("tracks", [])
    artists = Counter(t.get("artist") or "Unknown artist" for t in tracks)
    recent = sorted(
        tracks,
        key=lambda t: t.get("published_at") or "",
        reverse=True,
    )[:12]
    return {
        "configured": catalogue.get("configured", False),
        "source": "youtube",
        "channel_id": catalogue.get("channel_id"),
        "count": len(tracks),
        "tracks": tracks,
        "recent": recent,
        "artists": [{"name": name, "count": count} for name, count in artists.most_common(20)],
        "recommendations": recommend(tracks, limit=12),
    }
