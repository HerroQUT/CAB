from __future__ import annotations

from ..config.constants import ROLE_MODERATOR
from ..errors import conflict_error, forbidden_error, not_found_error, validation_error
from ..models.comment_model import delete_comments_by_post_id, get_comment_count_by_post_id
from ..models.like_model import create_like, find_like, get_like_count_by_post_id, remove_like, remove_likes_by_post_id
from ..models.common import now_iso
from ..models.post_model import (
    create_post as create_post_record,
    delete_post as delete_post_record,
    find_post_by_id,
    get_all_posts,
    get_posts_by_user_id,
    get_posts_from_users,
    update_post as update_post_record,
)
from ..models.user_model import find_user_by_id
from ..utils.enrichment import enrich_post, enrich_posts
from ..utils.validation import validate_comment_text, validate_images, validate_post_description, validate_post_title


def _can_view_post(post: dict, current_user_id: str | None, current_user_role: str | None) -> bool:
    """Return whether the current viewer can access a post, including hidden posts."""
    if post.get("status") != "hidden":
        return True
    if current_user_role == ROLE_MODERATOR:
        return True
    return current_user_id == post["userId"]


def create_post(user_id: str, post_data: dict) -> dict:
    """Create a validated post for a user and return enriched representation."""
    title = post_data.get("title")
    description = post_data.get("description", "")
    images = post_data.get("images", []) or []

    for validator in (validate_post_title(title), validate_post_description(description), validate_images(images)):
        if not validator["valid"]:
            raise validation_error(str(validator["error"]))

    if not find_user_by_id(user_id):
        raise not_found_error("User not found")

    post = create_post_record(
        {
            "userId": user_id,
            "title": title,
            "description": description,
            "images": images,
            "status": "active",
        }
    )
    return enrich_post(post, user_id)


def get_post(post_id: str, current_user_id: str | None = None, current_user_role: str | None = None) -> dict:
    """Return one enriched post if it exists and is visible to the viewer."""
    post = find_post_by_id(post_id)
    if not post:
        raise not_found_error("Post not found")
    if not _can_view_post(post, current_user_id, current_user_role):
        raise forbidden_error("You cannot view this post")
    return enrich_post(post, current_user_id)


def get_posts_by_user(user_id: str, limit: int = 20, offset: int = 0, current_user_id: str | None = None, current_user_role: str | None = None) -> list[dict]:
    """Return enriched posts authored by a user with visibility filtering."""
    if not find_user_by_id(user_id):
        raise not_found_error("User not found")
    bounded_limit = max(1, min(int(limit), 100))
    bounded_offset = max(0, int(offset))
    # Match Node behavior: fetch a larger window, then filter hidden posts.
    raw_posts = get_posts_by_user_id(user_id, limit=max(bounded_limit * 3, 20), offset=bounded_offset)
    visible_posts = [post for post in raw_posts if _can_view_post(post, current_user_id, current_user_role)]
    return enrich_posts(visible_posts[:bounded_limit], current_user_id)


def get_personal_feed(user_id: str, limit: int = 20, offset: int = 0, current_user_role: str | None = None) -> list[dict]:
    """Return enriched feed posts from followed users for the requester."""
    from ..models.follow_model import get_following

    following_ids = get_following(user_id)
    if not following_ids:
        return []

    raw_posts = get_posts_from_users(following_ids, limit=max(1, min(int(limit), 100)), offset=max(0, int(offset)))
    visible_posts = [post for post in raw_posts if post.get("status") != "hidden" or current_user_role == ROLE_MODERATOR]
    return enrich_posts(visible_posts, user_id)


def update_post(post_id: str, user_id: str, updates: dict) -> dict:
    """Update an owned post with validated mutable fields only."""
    post = find_post_by_id(post_id)
    if not post:
        raise not_found_error("Post not found")
    if post["userId"] != user_id:
        raise forbidden_error("You can only update your own post")

    payload = {}
    if "title" in updates:
        title_result = validate_post_title(updates.get("title"))
        if not title_result["valid"]:
            raise validation_error(str(title_result["error"]))
        payload["title"] = updates.get("title")
    if "description" in updates:
        description_result = validate_post_description(updates.get("description"))
        if not description_result["valid"]:
            raise validation_error(str(description_result["error"]))
        payload["description"] = updates.get("description")
    if "images" in updates:
        images_result = validate_images(updates.get("images") or [])
        if not images_result["valid"]:
            raise validation_error(str(images_result["error"]))
        payload["images"] = updates.get("images") or []
    if "status" in updates:
        payload.pop("status", None)

    updated = update_post_record(post_id, payload)
    return enrich_post(updated, user_id)


def delete_post(post_id: str, user_id: str) -> None:
    """Delete an owned post and cascade comments and likes."""
    post = find_post_by_id(post_id)
    if not post:
        raise not_found_error("Post not found")
    if post["userId"] != user_id:
        raise forbidden_error("You can only delete your own post")
    delete_comments_by_post_id(post_id)
    remove_likes_by_post_id(post_id)
    delete_post_record(post_id)

    from .upload_service import delete_post_media

    delete_post_media(post["userId"], post_id)


def like_post(post_id: str, user_id: str) -> dict:
    """Create a like record for a post by the given user."""
    if not find_post_by_id(post_id):
        raise not_found_error("Post not found")
    if find_like(post_id, user_id):
        raise conflict_error("Post already liked")
    return create_like({"postId": post_id, "userId": user_id})


def unlike_post(post_id: str, user_id: str) -> None:
    """Remove a user's like record from a post."""
    if not find_post_by_id(post_id):
        raise not_found_error("Post not found")
    if not remove_like(post_id, user_id):
        raise not_found_error("Like not found")


def hide_post(post_id: str, moderator_id: str, moderator_role: str) -> dict:
    """Hide a post as moderator and record moderation metadata."""
    if moderator_role != ROLE_MODERATOR:
        raise forbidden_error("Moderator access required")
    post = find_post_by_id(post_id)
    if not post:
        raise not_found_error("Post not found")
    from ..models.post_model import update_post as update_post_record

    updated = update_post_record(post_id, {"status": "hidden", "hiddenBy": moderator_id, "hiddenAt": now_iso()})
    return enrich_post(updated, moderator_id)

