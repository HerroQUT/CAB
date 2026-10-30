from __future__ import annotations

from ..services.follow_service import follow_user, get_followers, get_following, unfollow_user


def follow_user_controller(current_user_id: str, target_user_id: str) -> tuple[int, dict]:
    """Create a follow relationship and shape the HTTP response payload."""
    follow = follow_user(current_user_id, target_user_id)
    return 201, {"message": "User followed successfully", "follow": follow}


def unfollow_user_controller(current_user_id: str, target_user_id: str) -> tuple[int, dict]:
    """Remove a follow relationship and return success response payload."""
    unfollow_user(current_user_id, target_user_id)
    return 200, {"message": "User unfollowed successfully"}


def get_followers_controller(user_id: str, current_user_id: str | None) -> tuple[int, dict]:
    """Fetch followers list and shape the HTTP response payload."""
    followers = get_followers(user_id, current_user_id)
    return 200, {"message": "Followers retrieved successfully", "followers": followers}


def get_following_controller(user_id: str, current_user_id: str | None) -> tuple[int, dict]:
    """Fetch following list and shape the HTTP response payload."""
    following = get_following(user_id, current_user_id)
    return 200, {"message": "Following retrieved successfully", "following": following}

