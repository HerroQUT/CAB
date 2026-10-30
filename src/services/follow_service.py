from __future__ import annotations

from ..errors import conflict_error, forbidden_error, not_found_error
from ..models.follow_model import (
    create_follow as create_follow_record,
    find_follow,
    get_followers as get_follower_ids,
    get_following as get_following_ids,
    is_following,
    remove_follow,
)
from ..models.user_model import find_user_by_id
from ..utils.enrichment import enrich_users


def follow_user(follower_id: str, followee_id: str) -> dict:
    """Create a follow relationship with self-follow and duplicate guards."""
    if follower_id == followee_id:
        raise forbidden_error("You cannot follow yourself")
    if not find_user_by_id(followee_id):
        raise not_found_error("User not found")
    if find_follow(follower_id, followee_id):
        raise conflict_error("Already following this user")
    return create_follow_record({"followerId": follower_id, "followeeId": followee_id})


def unfollow_user(follower_id: str, followee_id: str) -> None:
    """Remove a follow relationship between follower and followee."""
    if not find_user_by_id(followee_id):
        raise not_found_error("User not found")
    if not remove_follow(follower_id, followee_id):
        raise not_found_error("Follow relationship not found")


def get_followers(user_id: str, current_user_id: str | None = None) -> list[dict]:
    """Return enriched user records for followers of the target user."""
    if not find_user_by_id(user_id):
        raise not_found_error("User not found")
    followers = [find_user_by_id(follower_id) for follower_id in get_follower_ids(user_id)]
    followers = [user for user in followers if user]
    return enrich_users(followers, current_user_id)


def get_following(user_id: str, current_user_id: str | None = None) -> list[dict]:
    """Return enriched user records for accounts the target user follows."""
    if not find_user_by_id(user_id):
        raise not_found_error("User not found")
    following = [find_user_by_id(followee_id) for followee_id in get_following_ids(user_id)]
    following = [user for user in following if user]
    return enrich_users(following, current_user_id)


def is_user_following(follower_id: str, followee_id: str) -> bool:
    """Return whether follower currently follows followee."""
    return is_following(follower_id, followee_id)

