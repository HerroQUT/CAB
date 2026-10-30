from __future__ import annotations

from typing import Any

from ..config.settings import settings
from ..utils.id_generator import generate_id
from .dynamo_common import Key, copy_record, now_iso, table


def _table():
    """Return the posts DynamoDB table handle."""
    return table(settings.posts_table)


def find_post_by_id(post_id: str) -> dict[str, Any] | None:
    """Find post by id and return it when available."""
    response = _table().get_item(Key={"id": post_id})
    return copy_record(response.get("Item"))


def get_posts_by_user_id(user_id: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Return a user's posts, newest first, via the userId-index GSI."""
    response = _table().query(
        IndexName="userId-index",
        KeyConditionExpression=Key("userId").eq(user_id),
        ScanIndexForward=False,
    )
    items = [copy_record(item) for item in response.get("Items", [])]
    return items[offset:offset + limit]


def get_all_posts(limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Return all posts, newest first (scan; acceptable at assignment scale)."""
    response = _table().scan()
    items = [copy_record(item) for item in response.get("Items", [])]
    items.sort(key=lambda post: post["createdAt"], reverse=True)
    return items[offset:offset + limit]


def create_post(post_data: dict[str, Any]) -> dict[str, Any]:
    """Create post data and return the created payload."""
    post = {
        "id": post_data.get("id", generate_id()),
        "userId": post_data["userId"],
        "title": post_data["title"],
        "description": post_data.get("description", ""),
        "images": list(post_data.get("images", [])),
        "status": post_data.get("status", "active"),
        "hiddenBy": post_data.get("hiddenBy"),
        "hiddenAt": post_data.get("hiddenAt"),
        "createdAt": post_data.get("createdAt", now_iso()),
        "updatedAt": post_data.get("updatedAt", now_iso()),
    }
    _table().put_item(Item=post)
    return copy_record(post)


def update_post(post_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Update post and return the updated payload."""
    existing = find_post_by_id(post_id)
    if not existing:
        return None
    for key, value in updates.items():
        if value is not None and key != "id":
            existing[key] = value
    existing["updatedAt"] = now_iso()
    _table().put_item(Item=existing)
    return copy_record(existing)


def delete_post(post_id: str) -> bool:
    """Delete post data for the requested context."""
    if not find_post_by_id(post_id):
        return False
    _table().delete_item(Key={"id": post_id})
    return True


def get_posts_from_users(user_ids: list[str], limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    """Return posts authored by any of the given users, newest first."""
    all_items: list[dict[str, Any]] = []
    for user_id in user_ids:
        response = _table().query(
            IndexName="userId-index",
            KeyConditionExpression=Key("userId").eq(user_id),
            ScanIndexForward=False,
        )
        all_items.extend(copy_record(item) for item in response.get("Items", []))
    all_items.sort(key=lambda post: post["createdAt"], reverse=True)
    return all_items[offset:offset + limit]


def get_post_count_by_user_id(user_id: str) -> int:
    """Return post count by user id data for the requested context."""
    response = _table().query(
        IndexName="userId-index",
        KeyConditionExpression=Key("userId").eq(user_id),
        Select="COUNT",
    )
    return int(response.get("Count", 0))


def delete_posts_by_user_id(user_id: str) -> None:
    """Delete all posts authored by a user."""
    posts = get_posts_by_user_id(user_id, limit=10_000, offset=0)
    with _table().batch_writer() as batch:
        for post in posts:
            batch.delete_item(Key={"id": post["id"]})
