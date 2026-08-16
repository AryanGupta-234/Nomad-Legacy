# Discover long-pass implementation

The Discover migration is intentionally additive. The existing NOMAD page remains the presentation layer while the backend supplies a canonical music payload.

## Data flow

`.env` -> `youtube_provider.py` -> `discover_service.py` -> Discover Flask blueprint -> existing Discover widgets.

## Canonical track fields

`id`, `provider`, `provider_id`, `title`, `artist`, `channel_id`, `thumbnail`, `published_at`, `description`, `duration`, and `url`.

## Recommendation behavior

`recommendation_engine.py` scores the already-fetched catalogue using token similarity, artist frequency, recency and catalogue position. This is deterministic and network-free, so loading Discover does not trigger a second recommendation service call.

## Security

`YOUTUBE_API_KEY` is read by Python only. Do not expose it through frontend JavaScript, generated assets, or API responses. Keep real `.env` files outside Git.

## Next UI pass

Bind the normalized payload to the existing Discover sections, then improve their spacing, artwork treatment, responsive grids, loading/empty states and hover affordances without replacing the current page structure.
