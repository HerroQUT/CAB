from __future__ import annotations

from ..services.feed_service import get_personal_feed


def get_personal_feed_controller(user_id: str, limit: int, offset: int, user_role: str | None) -> tuple[int, dict]:
    """Fetch personalized feed posts and shape the HTTP response body."""
    posts = get_personal_feed(user_id, limit=limit, offset=offset, user_role=user_role)
    return 200, {"message": "Feed retrieved successfully", "posts": posts}

