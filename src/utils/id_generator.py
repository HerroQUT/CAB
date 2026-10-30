from __future__ import annotations

import uuid


def generate_id() -> str:
    """Generate a UUID string for persistent entity identifiers."""
    return str(uuid.uuid4())


def generate_short_id() -> str:
    """Generate a short random identifier for lightweight records."""
    return uuid.uuid4().hex[:8]

