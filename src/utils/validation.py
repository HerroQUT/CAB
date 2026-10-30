from __future__ import annotations

import re

from ..config.constants import (
    MAX_COMMENT_LENGTH,
    MAX_IMAGES_PER_POST,
    MAX_MOMENT_CAPTION_LENGTH,
    MAX_POST_DESCRIPTION_LENGTH,
    MAX_POST_TITLE_LENGTH,
    MAX_USERNAME_LENGTH,
    MIN_PASSWORD_LENGTH,
    MIN_USERNAME_LENGTH,
    ROLE_MODERATOR,
    ROLE_USER,
)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _result(valid: bool, error: str | None = None) -> dict[str, object]:
    """Build a standard validation result payload."""
    return {"valid": valid, "error": error}


def validate_username(username: str | None) -> dict[str, object]:
    """Validate username input and return a standard validation result."""
    if not username:
        return _result(False, "Username is required")
    if len(username) < MIN_USERNAME_LENGTH or len(username) > MAX_USERNAME_LENGTH:
        return _result(False, f"Username must be between {MIN_USERNAME_LENGTH} and {MAX_USERNAME_LENGTH} characters")
    if not USERNAME_RE.match(username):
        return _result(False, "Username can only contain letters, numbers, underscores, and hyphens")
    return _result(True)


def validate_email(email: str | None) -> dict[str, object]:
    """Validate email input and return a standard validation result."""
    if not email:
        return _result(False, "Email is required")
    if not EMAIL_RE.match(email):
        return _result(False, "Email is invalid")
    return _result(True)


def validate_password(password: str | None) -> dict[str, object]:
    """Validate password input and return a standard validation result."""
    if not password:
        return _result(False, "Password is required")
    if len(password) < MIN_PASSWORD_LENGTH:
        return _result(False, f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _result(True)


def validate_role(role: str | None) -> dict[str, object]:
    """Validate role input and return a standard validation result."""
    if not isinstance(role, str):
        return _result(False, "Role must be either user or moderator")
    if role not in {ROLE_USER, ROLE_MODERATOR}:
        return _result(False, "Role must be either user or moderator")
    return _result(True)


def validate_post_title(title: str | None) -> dict[str, object]:
    """Validate post title input and return a standard validation result."""
    if not title:
        return _result(False, "Title is required")
    if len(title) > MAX_POST_TITLE_LENGTH:
        return _result(False, f"Title must be at most {MAX_POST_TITLE_LENGTH} characters")
    return _result(True)


def validate_post_description(description: str | None) -> dict[str, object]:
    """Validate post description input and return a standard validation result."""
    if description is not None and len(description) > MAX_POST_DESCRIPTION_LENGTH:
        return _result(False, f"Description must be at most {MAX_POST_DESCRIPTION_LENGTH} characters")
    return _result(True)


def validate_comment_text(text: str | None) -> dict[str, object]:
    """Validate comment text input and return a standard validation result."""
    if not text or not text.strip():
        return _result(False, "Comment text is required")
    if len(text) > MAX_COMMENT_LENGTH:
        return _result(False, f"Comment must be at most {MAX_COMMENT_LENGTH} characters")
    return _result(True)


def validate_moment_caption(caption: str | None) -> dict[str, object]:
    """Validate moment caption input and return a standard validation result."""
    if caption is not None and len(caption) > MAX_MOMENT_CAPTION_LENGTH:
        return _result(False, f"Caption must be at most {MAX_MOMENT_CAPTION_LENGTH} characters")
    return _result(True)


def validate_images(images: list[str] | None) -> dict[str, object]:
    """Validate images input and return a standard validation result."""
    if images is None:
        return _result(True)
    if len(images) > MAX_IMAGES_PER_POST:
        return _result(False, f"A post can have at most {MAX_IMAGES_PER_POST} images")
    if not all(isinstance(image, str) and image for image in images):
        return _result(False, "Images must be non-empty strings")
    return _result(True)


def validate_required_fields(data: dict[str, object], required_fields: list[str]) -> dict[str, object]:
    """Validate required fields input and return a standard validation result."""
    missing = [field for field in required_fields if field not in data or data[field] in (None, "")]
    if missing:
        return _result(False, f"Missing required fields: {', '.join(missing)}")
    return _result(True)

