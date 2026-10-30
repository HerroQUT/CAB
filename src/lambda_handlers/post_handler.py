from __future__ import annotations

from ..controllers.comment_controller import (
    create_comment_controller,
    delete_comment_controller,
    get_post_comments_controller,
)
from ..controllers.post_controller import (
    create_post_controller,
    delete_post_controller,
    get_post_controller,
    get_user_posts_controller,
    hide_post_controller,
    like_post_controller,
    unlike_post_controller,
    update_post_controller,
)
from ..middleware.auth import authenticate, optional_authenticate, require_moderator
from ._router import Router, parse_body, query_params


def _create_post(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/posts"""
    current = authenticate(event)
    return create_post_controller(current["userId"], parse_body(event))


def _user_posts(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/posts/user/{userId}"""
    current = optional_authenticate(event)
    q = query_params(event)
    limit = int(q.get("limit", 20))
    offset = int(q.get("offset", 0))
    return get_user_posts_controller(
        params["userId"], limit, offset,
        current["userId"] if current else None,
        current["userRole"] if current else None,
    )


def _get_post(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/posts/{id}"""
    current = optional_authenticate(event)
    return get_post_controller(params["id"], current["userId"] if current else None, current["userRole"] if current else None)


def _update_post(event: dict, params: dict) -> tuple[int, dict]:
    """PUT /v1/posts/{id}"""
    current = authenticate(event)
    return update_post_controller(params["id"], current["userId"], parse_body(event))


def _delete_post(event: dict, params: dict) -> tuple[int, dict]:
    """DELETE /v1/posts/{id}"""
    current = authenticate(event)
    return delete_post_controller(params["id"], current["userId"])


def _like_post(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/posts/{id}/like"""
    current = authenticate(event)
    return like_post_controller(params["id"], current["userId"])


def _unlike_post(event: dict, params: dict) -> tuple[int, dict]:
    """DELETE /v1/posts/{id}/like"""
    current = authenticate(event)
    return unlike_post_controller(params["id"], current["userId"])


def _hide_post(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/posts/{id}/hide (moderator-only)"""
    current = require_moderator(event)
    return hide_post_controller(params["id"], current["userId"], current["userRole"])


def _list_comments(event: dict, params: dict) -> tuple[int, dict]:
    """GET /v1/posts/{postId}/comments"""
    return get_post_comments_controller(params["postId"])


def _create_comment(event: dict, params: dict) -> tuple[int, dict]:
    """POST /v1/posts/{postId}/comments"""
    current = authenticate(event)
    return create_comment_controller(params["postId"], current["userId"], parse_body(event))


def _delete_comment(event: dict, params: dict) -> tuple[int, dict]:
    """DELETE /v1/posts/{postId}/comments/{commentId}"""
    current = authenticate(event)
    return delete_comment_controller(params["commentId"], current["userId"])


router = Router()
router.add("POST", "/v1/posts", _create_post)
router.add("GET", "/v1/posts/user/{userId}", _user_posts)
router.add("GET", "/v1/posts/{id}", _get_post)
router.add("PUT", "/v1/posts/{id}", _update_post)
router.add("DELETE", "/v1/posts/{id}", _delete_post)
router.add("POST", "/v1/posts/{id}/like", _like_post)
router.add("DELETE", "/v1/posts/{id}/like", _unlike_post)
router.add("POST", "/v1/posts/{id}/hide", _hide_post)
router.add("GET", "/v1/posts/{postId}/comments", _list_comments)
router.add("POST", "/v1/posts/{postId}/comments", _create_comment)
router.add("DELETE", "/v1/posts/{postId}/comments/{commentId}", _delete_comment)


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for /v1/posts/* (including nested comment routes)."""
    return router.dispatch(event)
