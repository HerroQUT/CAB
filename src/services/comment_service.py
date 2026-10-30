from __future__ import annotations

from ..errors import forbidden_error, not_found_error, validation_error
from ..models.comment_model import (
    create_comment as create_comment_record,
    delete_comment as delete_comment_record,
    find_comment_by_id,
    get_comments_by_post_id,
)
from ..models.post_model import find_post_by_id
from ..models.user_model import find_user_by_id
from ..utils.enrichment import enrich_user
from ..utils.validation import validate_comment_text


def create_comment(post_id: str, user_id: str, text: str) -> dict:
    """Create a validated comment on a post and enrich it with author details."""
    if not find_post_by_id(post_id):
        raise not_found_error("Post not found")
    if not find_user_by_id(user_id):
        raise not_found_error("User not found")
    validation = validate_comment_text(text)
    if not validation["valid"]:
        raise validation_error(str(validation["error"]))

    comment = create_comment_record({"postId": post_id, "userId": user_id, "text": text.strip()})
    author = find_user_by_id(user_id)
    comment["author"] = {"id": author["id"], "username": author["username"]}
    return comment


def get_comments_by_post(post_id: str) -> list[dict]:
    """Return comments for a post enriched with author details."""
    if not find_post_by_id(post_id):
        raise not_found_error("Post not found")
    comments = get_comments_by_post_id(post_id)
    enriched = []
    for comment in comments:
        author = find_user_by_id(comment["userId"])
        enriched.append({**comment, "author": {"id": author["id"], "username": author["username"]} if author else None})
    return enriched


def delete_comment(comment_id: str, user_id: str) -> None:
    """Delete a comment if and only if the requester is the author."""
    comment = find_comment_by_id(comment_id)
    if not comment:
        raise not_found_error("Comment not found")
    if comment["userId"] != user_id:
        raise forbidden_error("You can only delete your own comment")
    delete_comment_record(comment_id)

