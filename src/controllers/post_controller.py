from __future__ import annotations

from ..services.post_service import (
    create_post,
    delete_post,
    get_post,
    get_posts_by_user,
    hide_post,
    like_post,
    unlike_post,
    update_post,
)


def create_post_controller(user_id: str, payload: dict) -> tuple[int, dict]:
    """Create a post and shape the HTTP response payload."""
    post = create_post(user_id, payload)
    return 201, {"message": "Post created successfully", "post": post}


def get_post_controller(post_id: str, current_user_id: str | None, current_user_role: str | None) -> tuple[int, dict]:
    """Fetch one post and shape the HTTP response payload."""
    post = get_post(post_id, current_user_id=current_user_id, current_user_role=current_user_role)
    return 200, {"message": "Post retrieved successfully", "post": post}


def get_user_posts_controller(user_id: str, limit: int, offset: int, current_user_id: str | None, current_user_role: str | None) -> tuple[int, dict]:
    """Fetch posts by user and shape the HTTP response payload."""
    posts = get_posts_by_user(
        user_id,
        limit=limit,
        offset=offset,
        current_user_id=current_user_id,
        current_user_role=current_user_role,
    )
    return 200, {"message": "Posts retrieved successfully", "posts": posts}


def update_post_controller(post_id: str, user_id: str, payload: dict) -> tuple[int, dict]:
    """Update a post and shape the HTTP response payload."""
    post = update_post(post_id, user_id, payload)
    return 200, {"message": "Post updated successfully", "post": post}


def delete_post_controller(post_id: str, user_id: str) -> tuple[int, dict]:
    """Delete a post and return success response payload."""
    delete_post(post_id, user_id)
    return 200, {"message": "Post deleted successfully"}


def like_post_controller(post_id: str, user_id: str) -> tuple[int, dict]:
    """Like a post and shape the HTTP response payload."""
    like = like_post(post_id, user_id)
    return 201, {"message": "Post liked successfully", "like": like}


def unlike_post_controller(post_id: str, user_id: str) -> tuple[int, dict]:
    """Unlike a post and return success response payload."""
    unlike_post(post_id, user_id)
    return 200, {"message": "Post unliked successfully"}


def hide_post_controller(post_id: str, moderator_id: str, moderator_role: str) -> tuple[int, dict]:
    """Hide a post as moderator and shape the HTTP response payload."""
    post = hide_post(post_id, moderator_id, moderator_role)
    return 200, {"message": "Post hidden successfully", "post": post}

