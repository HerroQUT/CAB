from __future__ import annotations

from ..services.comment_service import create_comment, delete_comment, get_comments_by_post


def create_comment_controller(post_id: str, user_id: str, payload: dict) -> tuple[int, dict]:
    """Create a post comment and shape the HTTP response body."""
    comment = create_comment(post_id, user_id, payload.get("text", ""))
    return 201, {"message": "Comment created successfully", "comment": comment}


def get_post_comments_controller(post_id: str) -> tuple[int, dict]:
    """List comments for a post and shape the HTTP response body."""
    comments = get_comments_by_post(post_id)
    return 200, {"message": "Comments retrieved successfully", "comments": comments}


def delete_comment_controller(comment_id: str, user_id: str) -> tuple[int, dict]:
    """Delete a comment and return success response payload."""
    delete_comment(comment_id, user_id)
    return 200, {"message": "Comment deleted successfully"}

