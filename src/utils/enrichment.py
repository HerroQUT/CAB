from __future__ import annotations

from ..models.user_model import find_user_by_id
from ..models.comment_model import get_comment_count_by_post_id
from ..models.follow_model import get_follower_count, get_following_count, is_following
from ..models.like_model import get_like_count_by_post_id, has_user_liked_post
from ..models.post_model import get_post_count_by_user_id


def enrich_post(post: dict, current_user_id: str | None = None) -> dict:
    """Attach derived metadata to a post for API responses."""
    from ..services.upload_service import presign_post_image  # local import avoids a cycle at module load

    enriched = dict(post)
    author = find_user_by_id(post["userId"])
    enriched["author"] = {"id": author["id"], "username": author["username"]} if author else {"id": post["userId"], "username": None}
    enriched["stats"] = {
        "likes": get_like_count_by_post_id(post["id"]),
        "comments": get_comment_count_by_post_id(post["id"]),
    }
    enriched["currentUserLiked"] = has_user_liked_post(post["id"], current_user_id) if current_user_id else False
    # Media is never exposed as a permanent public link (User Story 8/9): stored
    # object references are swapped for short-lived presigned S3 GET URLs on every read.
    enriched["images"] = [
        presign_post_image(post["userId"], post["id"], ref) for ref in post.get("images", [])
    ]
    return enriched


def enrich_posts(posts: list[dict], current_user_id: str | None = None) -> list[dict]:
    """Attach derived metadata to each post in a list response."""
    return [enrich_post(post, current_user_id) for post in posts]


def enrich_user(user: dict, current_user_id: str | None = None) -> dict:
    """Attach derived relationship flags to a user for API responses."""
    enriched = dict(user)
    enriched["stats"] = {
        "followers": get_follower_count(user["id"]),
        "following": get_following_count(user["id"]),
        "posts": get_post_count_by_user_id(user["id"]),
    }
    enriched["currentUserFollows"] = is_following(current_user_id, user["id"]) if current_user_id else False
    enriched.pop("password", None)
    return enriched


def enrich_users(users: list[dict], current_user_id: str | None = None) -> list[dict]:
    """Attach derived relationship flags to each user in a list response."""
    return [enrich_user(user, current_user_id) for user in users]

