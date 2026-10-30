from __future__ import annotations

from ..controllers.auth_controller import login_controller, register_controller
from ._router import Router, parse_body

router = Router()
router.add("POST", "/v1/auth/register", lambda event, params: register_controller(parse_body(event)))
router.add("POST", "/v1/auth/login", lambda event, params: login_controller(parse_body(event)))


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for /v1/auth/*."""
    return router.dispatch(event)
