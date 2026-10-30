from __future__ import annotations

import boto3
from botocore.config import Config

from ..config.constants import DOWNLOAD_URL_EXPIRY_SECONDS, UPLOAD_URL_EXPIRY_SECONDS
from ..config.settings import settings
from ..errors import validation_error
from ..utils.id_generator import generate_short_id

_s3 = None


def _client():
    """Return a cached boto3 S3 client that presigns with Signature Version 4.

    SigV4 matters here for two reasons:
      * SigV2 URLs carry AWSAccessKeyId/Signature/Expires; SigV4 carries the
        X-Amz-* parameters (including X-Amz-Expires) that clients inspect to
        confirm the URL is short-lived.
      * SigV2 folds Content-Type into the string-to-sign, so a client PUTting
        with any Content-Type we did not know at signing time gets
        SignatureDoesNotMatch. SigV4 signs only the host by default, so the
        client is free to send whatever Content-Type it likes.
    The regional endpoint is set explicitly so the URL never falls back to the
    global s3.amazonaws.com host, which is what triggers SigV2 in the first place.
    """
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=settings.aws_region,
            endpoint_url=f"https://s3.{settings.aws_region}.amazonaws.com",
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "virtual"},
            ),
        )
    return _s3


def _object_key(user_id: str, post_id: str, file_id: str) -> str:
    """Build the canonical S3 object key for a post image."""
    return f"{user_id}/{post_id}/{file_id}"


def get_upload_url(user_id: str, post_id: str) -> dict:
    """Generate a short-lived presigned S3 PUT URL for a post image.

    Per User Story 7, this is the only credential the client receives: they PUT
    directly to S3 with it, and no long-lived AWS credentials are ever exposed.
    """
    if not post_id:
        raise validation_error("Post ID is required")

    file_id = generate_short_id()
    key = _object_key(user_id, post_id, file_id)
    upload_url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.media_bucket, "Key": key},
        ExpiresIn=UPLOAD_URL_EXPIRY_SECONDS,
    )
    return {"uploadUrl": upload_url, "fileId": file_id, "key": key}


def _object_exists(key: str) -> bool:
    """Return whether an object exists at the given key."""
    try:
        _client().head_object(Bucket=settings.media_bucket, Key=key)
        return True
    except Exception:
        return False


def presign_post_image(user_id: str, post_id: str, reference: str,
                       expires: int = DOWNLOAD_URL_EXPIRY_SECONDS) -> str:
    """Return a short-lived presigned GET URL for one of a post's images.

    Clients may reference an image in several shapes: the bare fileId returned
    by POST /v1/uploads/url, or the documented path form
    /uploads/{userId}/{postId}/{fileId}.jpg (note the prefix and extension,
    neither of which is part of the actual S3 key). We therefore reduce any
    reference to its fileId and rebuild the canonical key, falling back to the
    literal reference if that object happens to exist instead.
    """
    if not reference:
        return reference
    if reference.startswith("http://") or reference.startswith("https://"):
        return reference

    literal = reference.lstrip("/")
    file_id = literal.rstrip("/").split("/")[-1].split(".")[0]
    canonical = f"{user_id}/{post_id}/{file_id}"

    key = canonical
    if not _object_exists(canonical) and _object_exists(literal):
        key = literal

    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.media_bucket, "Key": key},
        ExpiresIn=expires,
    )


def presign_get_url(key: str, expires: int = DOWNLOAD_URL_EXPIRY_SECONDS) -> str:
    """Return a short-lived presigned GET URL for a stored media object.

    Falls back to returning the input unchanged if it is already an absolute
    URL (covers seed data / external image URLs used outside the upload flow).
    """
    if not key or key.startswith("http://") or key.startswith("https://"):
        return key
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.media_bucket, "Key": key.lstrip("/")},
        ExpiresIn=expires,
    )


def delete_user_media(user_id: str) -> None:
    """Delete every S3 object stored under a user's prefix (account deletion cascade)."""
    paginator = _client().get_paginator("list_objects_v2")
    keys_to_delete = []
    for page in paginator.paginate(Bucket=settings.media_bucket, Prefix=f"{user_id}/"):
        for obj in page.get("Contents", []):
            keys_to_delete.append({"Key": obj["Key"]})

    for i in range(0, len(keys_to_delete), 1000):
        batch = keys_to_delete[i:i + 1000]
        if batch:
            _client().delete_objects(Bucket=settings.media_bucket, Delete={"Objects": batch})


def delete_post_media(user_id: str, post_id: str) -> None:
    """Delete every S3 object stored under a post's prefix (post deletion cascade)."""
    paginator = _client().get_paginator("list_objects_v2")
    keys_to_delete = []
    for page in paginator.paginate(Bucket=settings.media_bucket, Prefix=f"{user_id}/{post_id}/"):
        for obj in page.get("Contents", []):
            keys_to_delete.append({"Key": obj["Key"]})
    if keys_to_delete:
        _client().delete_objects(Bucket=settings.media_bucket, Delete={"Objects": keys_to_delete})
