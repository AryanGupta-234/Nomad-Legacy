# NOMAD Discover — YouTube data layer

The Discover upgrade keeps the existing NOMAD UI structure but moves catalogue
and recommendation data behind a server-side provider boundary.

## Environment

Set these in the local `.env` (never commit the values):

```env
YOUTUBE_API_KEY=...
YOUTUBE_CHANNEL_ID=...
NOMAD_YOUTUBE_CACHE_TTL=900
```

`YOUTUBE_API_KEY` is only read by the Python provider. It is never sent to the
browser.

## Data flow

```text
.env
  -> nomad_sync.youtube_provider
  -> normalized Track records
  -> nomad_sync.discover_service
  -> recommendation_engine
  -> /api/discover
  -> existing Discover widgets
```

## API contract

`GET /api/discover?limit=36`

Returns `configured`, `source`, `tracks`, `recent`, `artists`, and
`recommendations`.

`GET /api/discover/health`

Returns a lightweight service health response without contacting YouTube.

## Flask registration

The blueprint lives in `nomad_sync/discover_blueprint.py` and is intentionally
isolated from the legacy controller. Register it once on the existing Flask
application:

```python
from nomad_sync.discover_blueprint import bp as discover_bp
app.register_blueprint(discover_bp)
```

This keeps the monolithic `nomad_web.py` stable while allowing the Discover
surface to migrate incrementally.
