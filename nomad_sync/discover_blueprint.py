"""Flask blueprint for the new Discover data contract.

Kept separate from nomad_web.py so the legacy web controller does not need to
be rewritten just to add the modern Discover data layer.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from .discover_service import discover
from .recommendation_engine import recommend

bp = Blueprint("nomad_discover", __name__, url_prefix="/api/discover")


@bp.get("")
def discover_index():
    try:
        limit = min(50, max(1, int(request.args.get("limit", "36"))))
    except ValueError:
        limit = 36
    payload = discover(limit=limit)
    payload["recommendations"] = recommend(payload.get("tracks", []), limit=12)
    return jsonify(payload)


@bp.get("/health")
def discover_health():
    return jsonify({"service": "discover", "status": "ready"})
