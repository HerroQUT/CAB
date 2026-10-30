from __future__ import annotations

from ..services.auth_service import login_user, register_user


def register_controller(payload: dict) -> tuple[int, dict]:
    """Handle auth register requests and return API response payload."""
    result = register_user(payload)
    return 201, {"message": "User registered successfully", **result}


def login_controller(payload: dict) -> tuple[int, dict]:
    """Handle auth login requests and return API response payload."""
    result = login_user(payload)
    return 200, {"message": "Login successful", **result}

