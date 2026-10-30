from __future__ import annotations

from ..services.moment_service import create_moment, delete_moment, get_moment_archive, get_moment_feed, hide_moment


def create_moment_controller(user_id: str, payload: dict) -> tuple[int, dict]:
    """Create a moment and shape the HTTP response payload."""
    moment = create_moment(user_id, payload)
    return 201, {"message": "Moment created successfully", "moment": moment}


def get_moment_feed_controller(user_id: str, user_role: str | None) -> tuple[int, dict]:
    """Fetch active moment feed and shape the HTTP response payload."""
    moments = get_moment_feed(user_id, user_role)
    return 200, {"message": "Moment feed retrieved successfully", "moments": moments}


def get_moment_archive_controller(user_id: str, user_role: str | None) -> tuple[int, dict]:
    """Fetch archived moments and shape the HTTP response payload."""
    moments = get_moment_archive(user_id, user_role)
    return 200, {"message": "Moment archive retrieved successfully", "moments": moments}


def hide_moment_controller(moment_id: str, moderator_id: str, moderator_role: str) -> tuple[int, dict]:
    """Hide a moment as moderator and shape the HTTP response payload."""
    moment = hide_moment(moment_id, moderator_id, moderator_role)
    return 200, {"message": "Moment hidden successfully", "moment": moment}


def delete_moment_controller(moment_id: str, user_id: str, user_role: str) -> tuple[int, dict]:
    """Delete a moment and return success response payload."""
    delete_moment(moment_id, user_id, user_role)
    return 200, {"message": "Moment deleted successfully"}

