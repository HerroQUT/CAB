from __future__ import annotations

from typing import Any

from ..config.settings import settings
from ..utils.id_generator import generate_id
from .dynamo_common import Key, copy_record, now_iso, table


def _table():
    """Return the moments DynamoDB table handle."""
    return table(settings.moments_table)


def create_moment(moment_data: dict[str, Any]) -> dict[str, Any]:
    """Create moment data and return the created payload."""
    moment = {
        "id": moment_data.get("id", generate_id()),
        "userId": moment_data["userId"],
        "imageUrl": moment_data["imageUrl"],
        "caption": moment_data.get("caption", ""),
        "status": moment_data.get("status", "active"),
        "hiddenBy": moment_data.get("hiddenBy"),
        "hiddenAt": moment_data.get("hiddenAt"),
        "archivedAt": moment_data.get("archivedAt"),
        "createdAt": moment_data.get("createdAt", now_iso()),
        "expiresAt": moment_data.get("expiresAt"),
        "updatedAt": moment_data.get("updatedAt", now_iso()),
    }
    _table().put_item(Item=moment)
    return copy_record(moment)


def find_moment_by_id(moment_id: str) -> dict[str, Any] | None:
    """Find moment by id and return it when available."""
    response = _table().get_item(Key={"id": moment_id})
    return copy_record(response.get("Item"))


def update_moment(moment_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Update moment and return the updated payload."""
    existing = find_moment_by_id(moment_id)
    if not existing:
        return None
    for key, value in updates.items():
        if value is not None and key != "id":
            existing[key] = value
    existing["updatedAt"] = now_iso()
    _table().put_item(Item=existing)
    return copy_record(existing)


def delete_moment(moment_id: str) -> bool:
    """Delete moment data for the requested context."""
    if not find_moment_by_id(moment_id):
        return False
    _table().delete_item(Key={"id": moment_id})
    return True


def get_moments_by_user_ids(user_ids: list[str]) -> list[dict[str, Any]]:
    """Return moments for the given users, newest first, via the userId-index GSI."""
    all_items: list[dict[str, Any]] = []
    for user_id in user_ids:
        response = _table().query(
            IndexName="userId-index",
            KeyConditionExpression=Key("userId").eq(user_id),
            ScanIndexForward=False,
        )
        all_items.extend(copy_record(item) for item in response.get("Items", []))
    all_items.sort(key=lambda moment: moment["createdAt"], reverse=True)
    return all_items


def get_moments_by_user_id(user_id: str) -> list[dict[str, Any]]:
    """Return moments by user id data for the requested context."""
    return get_moments_by_user_ids([user_id])


def delete_moments_by_user_id(user_id: str) -> None:
    """Delete all moments authored by a user."""
    moments = get_moments_by_user_id(user_id)
    with _table().batch_writer() as batch:
        for moment in moments:
            batch.delete_item(Key={"id": moment["id"]})
