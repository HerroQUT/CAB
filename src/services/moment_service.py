from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config.constants import ROLE_MODERATOR, TOKEN_EXPIRY_HOURS
from ..errors import forbidden_error, not_found_error, validation_error
from ..models.follow_model import get_followers, get_following
from ..models.moment_model import (
    create_moment as create_moment_record,
    delete_moment as delete_moment_record,
    find_moment_by_id,
    get_moments_by_user_ids,
    update_moment as update_moment_record,
)
from ..models.user_model import find_user_by_id
from ..utils.validation import validate_moment_caption


def create_moment(user_id: str, payload: dict) -> dict:
    """Create a validated moment with a 24-hour expiry window."""
    image_url = payload.get("imageUrl")
    caption = payload.get("caption", "")
    if not image_url:
        raise validation_error("Image URL is required")
    validation = validate_moment_caption(caption)
    if not validation["valid"]:
        raise validation_error(str(validation["error"]))
    now = datetime.now(timezone.utc)
    moment = create_moment_record(
        {
            "userId": user_id,
            "imageUrl": image_url,
            "caption": caption or "",
            "status": "active",
            "expiresAt": (now + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        }
    )
    return moment


def _archive_expired(moment: dict) -> dict:
    """Archive a moment in-place when it has passed its expiry timestamp."""
    if moment.get("status") == "active" and moment.get("expiresAt"):
        expires_at = datetime.fromisoformat(moment["expiresAt"].replace("Z", "+00:00"))
        if expires_at <= datetime.now(timezone.utc):
            return update_moment_record(moment["id"], {"status": "archived", "archivedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}) or moment
    return moment


def _visible(moment: dict, viewer_id: str | None, viewer_role: str | None) -> bool:
    """Return visibility of a moment for a viewer under role and follow rules."""
    if viewer_role == ROLE_MODERATOR:
        return True
    return viewer_id == moment["userId"] or viewer_id in get_followers(moment["userId"])


def get_moment_feed(user_id: str, viewer_role: str | None = None) -> list[dict]:
    """Return active moments for the user and followed accounts."""
    if not find_user_by_id(user_id):
        raise not_found_error("User not found")

    user_ids = [user_id] + get_following(user_id)
    moments = [
        _archive_expired(moment)
        for moment in get_moments_by_user_ids(user_ids)
        if moment.get("status") == "active"
    ]
    return [moment for moment in moments if _visible(moment, user_id, viewer_role)]


def get_moment_archive(user_id: str, viewer_role: str | None = None) -> list[dict]:
    """Return archived moments visible to the requesting user."""
    if not find_user_by_id(user_id):
        raise not_found_error("User not found")

    user_ids = [user_id] + get_following(user_id)
    moments = [_archive_expired(moment) for moment in get_moments_by_user_ids(user_ids)]
    return [moment for moment in moments if moment.get("status") == "archived" and _visible(moment, user_id, viewer_role)]


def hide_moment(moment_id: str, moderator_id: str, moderator_role: str) -> dict:
    """Hide a moment as a moderator and record moderation metadata."""
    if moderator_role != ROLE_MODERATOR:
        raise forbidden_error("Moderator access required")
    moment = find_moment_by_id(moment_id)
    if not moment:
        raise not_found_error("Moment not found")
    return update_moment_record(moment_id, {"status": "hidden", "hiddenBy": moderator_id, "hiddenAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})


def delete_moment(moment_id: str, requester_id: str, requester_role: str) -> None:
    """Delete a moment if requester is owner or moderator."""
    moment = find_moment_by_id(moment_id)
    if not moment:
        raise not_found_error("Moment not found")
    if requester_role != ROLE_MODERATOR and moment["userId"] != requester_id:
        raise forbidden_error("You cannot delete this moment")
    delete_moment_record(moment_id)

