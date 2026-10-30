from __future__ import annotations

from ..controllers.upload_controller import get_upload_url_controller
from ..middleware.auth import authenticate
from ._router import Router, parse_body


def _upload_url(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/uploads/url"""
    current = authenticate(event)
    return get_upload_url_controller(current["userId"], parse_body(event))


router = Router()
router.add("POST", "/v1/uploads/url", _upload_url)


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for /v1/uploads/url."""
    return router.dispatch(event)
