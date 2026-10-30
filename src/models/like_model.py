from __future__ import annotations

from typing import Any

from ..config.settings import settings
from ..utils.id_generator import generate_id
from .dynamo_common import Key, copy_record, now_iso, table


def _table():
    """Return the likes DynamoDB table handle.

    Primary key: postId (partition) + userId (sort). Likes are an inherent
    many-to-many join between posts and users, so this composite key gives
    O(1) direct lookups for the two hottest access patterns (has-user-liked,
    like-count-for-post) instead of a synthetic id + Query/FilterExpression.
    A GSI on userId supports the one remaining pattern: "all likes by user"
    (used when cascading account deletion).
    """
    return table(settings.likes_table)


def find_like(post_id: str, user_id: str) -> dict[str, Any] | None:
    """Find like and return it when available via a direct key lookup."""
    response = _table().get_item(Key={"postId": post_id, "userId": user_id})
    return copy_record(response.get("Item"))


def has_user_liked_post(post_id: str, user_id: str | None) -> bool:
    """Return whether user liked post exists for the current context."""
    if not user_id:
        return False
    return find_like(post_id, user_id) is not None


def get_like_count_by_post_id(post_id: str) -> int:
    """Return like count by post id data for the requested context."""
    response = _table().query(
        KeyConditionExpression=Key("postId").eq(post_id),
        Select="COUNT",
    )
    return int(response.get("Count", 0))


def create_like(like_data: dict[str, Any]) -> dict[str, Any]:
    """Create like data and return the created payload."""
    like = {
        "id": like_data.get("id", generate_id()),
        "postId": like_data["postId"],
        "userId": like_data["userId"],
        "createdAt": like_data.get("createdAt", now_iso()),
    }
    _table().put_item(Item=like)
    return copy_record(like)


def remove_like(post_id: str, user_id: str) -> bool:
    """Remove like data for the requested context."""
    response = _table().delete_item(
        Key={"postId": post_id, "userId": user_id},
        ReturnValues="ALL_OLD",
    )
    return "Attributes" in response


def remove_likes_by_post_id(post_id: str) -> None:
    """Remove likes by post id data for the requested context."""
    response = _table().query(KeyConditionExpression=Key("postId").eq(post_id))
    with _table().batch_writer() as batch:
        for item in response.get("Items", []):
            batch.delete_item(Key={"postId": item["postId"], "userId": item["userId"]})


def remove_likes_by_user_id(user_id: str) -> None:
    """Remove likes by user id data for the requested context (via userId-index GSI)."""
    response = _table().query(
        IndexName="userId-index",
        KeyConditionExpression=Key("userId").eq(user_id),
    )
    with _table().batch_writer() as batch:
        for item in response.get("Items", []):
            batch.delete_item(Key={"postId": item["postId"], "userId": item["userId"]})
