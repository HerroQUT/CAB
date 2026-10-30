from __future__ import annotations

from ..controllers.feed_controller import get_personal_feed_controller
from ..middleware.auth import authenticate
from ._router import Router, query_params


def _get_feed(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/feed"""
    current = authenticate(event)
    q = query_params(event)
    limit = int(q.get("limit", 20))
    offset = int(q.get("offset", 0))
    return get_personal_feed_controller(current["userId"], limit, offset, current["userRole"])


router = Router()
router.add("GET", "/v1/feed", _get_feed)


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for /v1/feed."""
    return router.dispatch(event)
