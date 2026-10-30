from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable

from ..errors import AppError

Handler = Callable[[dict, dict], tuple[int, dict]]


class Router:
    """Tiny regex-based router for API Gateway HTTP API v2 (payload format 2.0) events."""

    def __init__(self) -> None:
        """Initialise an empty route table."""
        self._routes: list[tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, path: str, handler: Handler) -> None:
        """Register a handler for a given HTTP method and path template."""
        pattern = re.sub(r"{(\w+)}", r"(?P<\1>[^/]+)", path)
        self._routes.append((method.upper(), re.compile(f"^{pattern}$"), handler))

    def dispatch(self, event: dict) -> dict:
        """Match the incoming event to a registered route and invoke its handler."""
        http = event.get("requestContext", {}).get("http", {})
        method = http.get("method", "GET").upper()
        raw_path = event.get("rawPath", "/")

        for route_method, pattern, handler in self._routes:
            if route_method != method:
                continue
            match = pattern.match(raw_path)
            if not match:
                continue
            try:
                status, body = handler(event, match.groupdict())
            except AppError as exc:
                return _response(exc.status_code, {"error": exc.error, "message": exc.message})
            except Exception as exc:  # pragma: no cover - safety net
                return _response(500, {"error": "InternalError", "message": str(exc)})
            return _response(status, body)

        return _response(404, {"error": "NotFound", "message": "Route not found"})


def _response(status: int, body: dict) -> dict:
    """Build an API Gateway HTTP API v2 JSON response."""
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(body),
    }


def parse_body(event: dict) -> dict[str, Any]:
    """Decode and parse the JSON request body from a Lambda event."""
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def query_params(event: dict) -> dict[str, str]:
    """Return the query string parameters from a Lambda event."""
    return event.get("queryStringParameters") or {}
