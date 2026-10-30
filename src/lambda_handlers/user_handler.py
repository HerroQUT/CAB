from __future__ import annotations

from ..controllers.follow_controller import (
    follow_user_controller,
    get_followers_controller,
    get_following_controller,
    unfollow_user_controller,
)
from ..controllers.user_controller import (
    delete_own_account_controller,
    get_user_profile_controller,
    list_users_controller,
)
from ..middleware.auth import authenticate, optional_authenticate
from ._router import Router, query_params


def _list_users(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/users"""
    current = optional_authenticate(event)
    q = query_params(event).get("q", "")
    limit = int(query_params(event).get("limit", 20))
    return list_users_controller(q, limit, current["userId"] if current else None)


def _get_profile(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/users/{id}"""
    current = optional_authenticate(event)
    return get_user_profile_controller(params["id"], current["userId"] if current else None)


def _delete_account(event: dict, params: dict) -> tuple[int, dict]:
    """DELETE /v1/users/{id}"""
    current = authenticate(event)
    return delete_own_account_controller(params["id"], current["userId"])


def _follow(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/users/{userId}/follow"""
    current = authenticate(event)
    return follow_user_controller(current["userId"], params["userId"])


def _unfollow(event: dict, params: dict) -> tuple[int, dict]:
    """DELETE /v1/users/{userId}/follow"""
    current = authenticate(event)
    return unfollow_user_controller(current["userId"], params["userId"])


def _followers(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/users/{userId}/followers"""
    current = optional_authenticate(event)
    return get_followers_controller(params["userId"], current["userId"] if current else None)


def _following(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/users/{userId}/following"""
    current = optional_authenticate(event)
    return get_following_controller(params["userId"], current["userId"] if current else None)


router = Router()
router.add("GET", "/v1/users", _list_users)
router.add("GET", "/v1/users/{id}", _get_profile)
router.add("DELETE", "/v1/users/{id}", _delete_account)
router.add("POST", "/v1/users/{userId}/follow", _follow)
router.add("DELETE", "/v1/users/{userId}/follow", _unfollow)
router.add("GET", "/v1/users/{userId}/followers", _followers)
router.add("GET", "/v1/users/{userId}/following", _following)


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for /v1/users/* (including nested follow routes)."""
    return router.dispatch(event)
