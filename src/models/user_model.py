from __future__ import annotations

from typing import Any

from ..config.settings import settings
from .dynamo_common import Key, copy_record, now_iso, table


def _table():
    """Return the users DynamoDB table handle."""
    return table(settings.users_table)


def sanitize_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a user object without sensitive fields (kept for API-shape parity)."""
    if user is None:
        return None
    sanitized = dict(user)
    sanitized.pop("password", None)
    return sanitized


def find_user_by_id(user_id: str) -> dict[str, Any] | None:
    """Find user by id (Cognito sub) and return it when available."""
    response = _table().get_item(Key={"id": user_id})
    return sanitize_user(copy_record(response.get("Item")))


def find_user_by_email(email: str) -> dict[str, Any] | None:
    """Find the app-level user row by email using the email GSI."""
    response = _table().query(
        IndexName="email-index",
        KeyConditionExpression=Key("email").eq(email.lower()),
        Limit=1,
    )
    items = response.get("Items", [])
    return sanitize_user(copy_record(items[0])) if items else None


def get_all_users() -> list[dict[str, Any]]:
    """Return all users (scan; acceptable at assignment scale)."""
    response = _table().scan()
    return [sanitize_user(copy_record(item)) for item in response.get("Items", [])]


def create_user(user_data: dict[str, Any]) -> dict[str, Any]:
    """Create the app-level user row keyed by the Cognito subject id."""
    user = {
        "id": user_data["id"],
        "username": user_data["username"],
        "email": user_data["email"].lower(),
        "role": user_data.get("role", "user"),
        "createdAt": user_data.get("createdAt", now_iso()),
        "updatedAt": user_data.get("updatedAt", now_iso()),
    }
    _table().put_item(Item=user)
    return sanitize_user(copy_record(user))


def update_user(user_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Update mutable fields on a user row."""
    existing = find_user_by_id(user_id)
    if not existing:
        return None
    existing.update({key: value for key, value in updates.items() if value is not None})
    existing["updatedAt"] = now_iso()
    _table().put_item(Item=existing)
    return sanitize_user(copy_record(existing))


def delete_user(user_id: str) -> bool:
    """Delete the user row by id."""
    if not find_user_by_id(user_id):
        return False
    _table().delete_item(Key={"id": user_id})
    return True


def username_exists(username: str) -> bool:
    """Return whether a username (== email in this app) already exists."""
    return email_exists(username)


def email_exists(email: str) -> bool:
    """Return whether an email address already exists in storage."""
    return find_user_by_email(email) is not None


def search_users(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Case-insensitive substring search over usernames (scan-based)."""
    if not query:
        return get_all_users()[:limit]
    lowered = query.lower()
    response = _table().scan()
    matches = [item for item in response.get("Items", []) if lowered in item["username"].lower()]
    return [sanitize_user(copy_record(item)) for item in matches[:limit]]


def delete_user_with_cascade(user_id: str) -> bool:
    """Delete a user row and cascade related posts, comments, likes, follows, moments."""
    if not find_user_by_id(user_id):
        return False

    from .comment_model import delete_comments_by_post_id
    from .follow_model import remove_follows_by_user_id
    from .like_model import remove_likes_by_user_id
    from .moment_model import delete_moments_by_user_id
    from .post_model import delete_posts_by_user_id, get_posts_by_user_id

    posts = get_posts_by_user_id(user_id, limit=10_000, offset=0)
    delete_posts_by_user_id(user_id)
    remove_likes_by_user_id(user_id)
    remove_follows_by_user_id(user_id)
    delete_moments_by_user_id(user_id)

    for post in posts:
        delete_comments_by_post_id(post["id"])

    _table().delete_item(Key={"id": user_id})
    return True
