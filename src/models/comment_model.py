from __future__ import annotations

from typing import Any

from ..config.settings import settings
from ..utils.id_generator import generate_id
from .dynamo_common import Key, copy_record, now_iso, table


def _table():
    """Return the comments DynamoDB table handle."""
    return table(settings.comments_table)


def find_comment_by_id(comment_id: str) -> dict[str, Any] | None:
    """Find comment by id and return it when available."""
    response = _table().get_item(Key={"id": comment_id})
    return copy_record(response.get("Item"))


def get_comments_by_post_id(post_id: str) -> list[dict[str, Any]]:
    """Return comments for a post in ascending creation order via the postId-index GSI."""
    response = _table().query(
        IndexName="postId-index",
        KeyConditionExpression=Key("postId").eq(post_id),
        ScanIndexForward=True,
    )
    return [copy_record(item) for item in response.get("Items", [])]


def create_comment(comment_data: dict[str, Any]) -> dict[str, Any]:
    """Create comment data and return the created payload."""
    comment = {
        "id": comment_data.get("id", generate_id()),
        "postId": comment_data["postId"],
        "userId": comment_data["userId"],
        "text": comment_data["text"],
        "createdAt": comment_data.get("createdAt", now_iso()),
        "updatedAt": comment_data.get("updatedAt", now_iso()),
    }
    _table().put_item(Item=comment)
    return copy_record(comment)


def delete_comment(comment_id: str) -> bool:
    """Delete comment data for the requested context."""
    if not find_comment_by_id(comment_id):
        return False
    _table().delete_item(Key={"id": comment_id})
    return True


def get_comment_count_by_post_id(post_id: str) -> int:
    """Return comment count by post id data for the requested context."""
    response = _table().query(
        IndexName="postId-index",
        KeyConditionExpression=Key("postId").eq(post_id),
        Select="COUNT",
    )
    return int(response.get("Count", 0))


def delete_comments_by_post_id(post_id: str) -> None:
    """Delete all comments belonging to a post."""
    comments = get_comments_by_post_id(post_id)
    with _table().batch_writer() as batch:
        for comment in comments:
            batch.delete_item(Key={"id": comment["id"]})
