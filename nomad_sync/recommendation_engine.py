"""Small, deterministic recommendation scorer for NOMAD Discover.

This intentionally has no network dependency. It scores the normalized catalogue
locally so the UI remains fast and YouTube is only used as a metadata provider.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

_STOP = {"official", "video", "audio", "music", "lyrics", "hd", "hq", "topic"}


def _tokens(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOP}


def _recency(published_at: str | None) -> float:
    if not published_at:
        return 0.0
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)
        return max(0.0, 1.0 - age_days / 365.0)
    except (TypeError, ValueError):
        return 0.0


def recommend(tracks: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    if not tracks:
        return []
    artist_counts = Counter(t.get("artist") or "" for t in tracks)
    catalogue_tokens = Counter()
    for track in tracks:
        catalogue_tokens.update(_tokens(f"{track.get('title', '')} {track.get('description', '')}"))

    scored = []
    for index, track in enumerate(tracks):
        title_tokens = _tokens(f"{track.get('title', '')} {track.get('description', '')}")
        token_signal = sum(catalogue_tokens[token] for token in title_tokens)
        artist_signal = artist_counts.get(track.get("artist") or "", 0)
        score = (
            min(token_signal, 20) * 0.05
            + min(artist_signal, 10) * 0.08
            + _recency(track.get("published_at")) * 0.35
            + max(0.0, 1.0 - index / max(len(tracks), 1)) * 0.15
        )
        scored.append((score, track))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [track for _, track in scored[:max(1, limit)]]
