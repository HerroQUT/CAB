from __future__ import annotations

from ..config.constants import ROLE_MODERATOR
from ..models.follow_model import get_following
from ..models.post_model import get_posts_from_users
from ..utils.enrichment import enrich_posts


def get_personal_feed(user_id: str, limit: int = 20, offset: int = 0, user_role: str | None = None) -> list[dict]:
    """Return feed posts from followed users, filtered for hidden content visibility."""
    following_ids = get_following(user_id)
    if not following_ids:
        return []
    posts = get_posts_from_users(following_ids, limit=max(1, min(int(limit), 100)), offset=max(0, int(offset)))
    posts = [post for post in posts if post.get("status") != "hidden" or user_role == ROLE_MODERATOR]
    return enrich_posts(posts, user_id)

