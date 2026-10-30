from __future__ import annotations

from ..controllers.moment_controller import (
    create_moment_controller,
    delete_moment_controller,
    get_moment_archive_controller,
    get_moment_feed_controller,
    hide_moment_controller,
)
from ..middleware.auth import authenticate, require_moderator
from ._router import Router, parse_body


def _create(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/moments"""
    current = authenticate(event)
    return create_moment_controller(current["userId"], parse_body(event))


def _feed(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/moments/feed"""
    current = authenticate(event)
    return get_moment_feed_controller(current["userId"], current["userRole"])


def _archive(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/moments/archive"""
    current = authenticate(event)
    return get_moment_archive_controller(current["userId"], current["userRole"])


def _hide(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/moments/{id}/hide (moderator-only)"""
    current = require_moderator(event)
    return hide_moment_controller(params["id"], current["userId"], current["userRole"])


def _delete(event: dict, params: dict) -> tuple[int, dict]:
    """DELETE /v1/moments/{id}"""
    current = authenticate(event)
    return delete_moment_controller(params["id"], current["userId"], current["userRole"])


router = Router()
router.add("POST", "/v1/moments", _create)
router.add("GET", "/v1/moments/feed", _feed)
router.add("GET", "/v1/moments/archive", _archive)
router.add("POST", "/v1/moments/{id}/hide", _hide)
router.add("DELETE", "/v1/moments/{id}", _delete)


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for /v1/moments/*."""
    return router.dispatch(event)
