from __future__ import annotations

from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from ..config.settings import settings

_resource = None


def _dynamo():
    """Return a cached boto3 DynamoDB resource (created lazily per Lambda container)."""
    global _resource
    if _resource is None:
        _resource = boto3.resource("dynamodb", region_name=settings.aws_region)
    return _resource


def table(name: str):
    """Return a boto3 Table handle for the given table name."""
    return _dynamo().Table(name)


def now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return settings.now_iso()


def clean(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert DynamoDB Decimal values back to plain int/float for JSON responses."""
    if item is None:
        return None

    def _convert(value: Any) -> Any:
        if isinstance(value, Decimal):
            return int(value) if value % 1 == 0 else float(value)
        if isinstance(value, list):
            return [_convert(v) for v in value]
        if isinstance(value, dict):
            return {k: _convert(v) for k, v in value.items()}
        return value

    return {key: _convert(value) for key, value in item.items()}


def copy_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a cleaned shallow copy of a DynamoDB item."""
    return clean(record)


__all__ = ["table", "now_iso", "clean", "copy_record", "Key"]
