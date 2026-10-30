from __future__ import annotations

# Kept for compatibility with service modules copied unmodified from the original
# project (e.g. post_service.py does `from ..models.common import now_iso`).
# There's no local JSON db in this deployment, so this just re-exports the
# DynamoDB-era helpers from dynamo_common.
from .dynamo_common import copy_record, now_iso  # noqa: F401
