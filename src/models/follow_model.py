from __future__ import annotations

from typing import Any

from ..config.settings import settings
from ..utils.id_generator import generate_id
from .dynamo_common import Key, copy_record, now_iso, table


def _table():
    """Return the follows DynamoDB table handle.

    Primary key: followerId (partition) + followeeId (sort) - the classic
    DynamoDB adjacency-list pattern for a self-referencing many-to-many
    relationship. This gives O(1) direct lookups for "does A follow B" and a
    direct Query for "who does A follow" with no GSI needed. A GSI on
    followeeId covers the remaining pattern: "who follows B".
    """
    return table(settings.follows_table)


def find_follow(follower_id: str, followee_id: str) -> dict[str, Any] | None:
    """Find follow and return it when available via a direct key lookup."""
    response = _table().get_item(Key={"followerId": follower_id, "followeeId": followee_id})
    return copy_record(response.get("Item"))


def is_following(follower_id: str | None, followee_id: str) -> bool:
    """Return whether following is true for the current context."""
    if not follower_id:
        return False
    return find_follow(follower_id, followee_id) is not None


def get_following(user_id: str) -> list[str]:
    """Return the list of followee ids for a user via a direct Query on the base table."""
    response = _table().query(KeyConditionExpression=Key("followerId").eq(user_id))
    return [item["followeeId"] for item in response.get("Items", [])]


def get_followers(user_id: str) -> list[str]:
    """Return the list of follower ids for a user via the followee-index GSI."""
    response = _table().query(
        IndexName="followee-index",
        KeyConditionExpression=Key("followeeId").eq(user_id),
    )
    return [item["followerId"] for item in response.get("Items", [])]


def get_following_count(user_id: str) -> int:
    """Return following count data for the requested context."""
    response = _table().query(
        KeyConditionExpression=Key("followerId").eq(user_id),
        Select="COUNT",
    )
    return int(response.get("Count", 0))


def get_follower_count(user_id: str) -> int:
    """Return follower count data for the requested context."""
    response = _table().query(
        IndexName="followee-index",
        KeyConditionExpression=Key("followeeId").eq(user_id),
        Select="COUNT",
    )
    return int(response.get("Count", 0))


def create_follow(follow_data: dict[str, Any]) -> dict[str, Any]:
    """Create follow data and return the created payload."""
    follow = {
        "id": follow_data.get("id", generate_id()),
        "followerId": follow_data["followerId"],
        "followeeId": follow_data["followeeId"],
        "createdAt": follow_data.get("createdAt", now_iso()),
    }
    _table().put_item(Item=follow)
    return copy_record(follow)


def remove_follow(follower_id: str, followee_id: str) -> bool:
    """Remove follow data for the requested context."""
    response = _table().delete_item(
        Key={"followerId": follower_id, "followeeId": followee_id},
        ReturnValues="ALL_OLD",
    )
    return "Attributes" in response


def remove_follows_by_user_id(user_id: str) -> None:
    """Remove all follow rows where the user is either side of the relationship."""
    as_follower = _table().query(KeyConditionExpression=Key("followerId").eq(user_id)).get("Items", [])
    as_followee = _table().query(
        IndexName="followee-index",
        KeyConditionExpression=Key("followeeId").eq(user_id),
    ).get("Items", [])
    with _table().batch_writer() as batch:
        for item in as_follower + as_followee:
            batch.delete_item(Key={"followerId": item["followerId"], "followeeId": item["followeeId"]})
