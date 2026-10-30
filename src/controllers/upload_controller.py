from __future__ import annotations

from ..services.upload_service import get_upload_url


def get_upload_url_controller(user_id: str, payload: dict) -> tuple[int, dict]:
    """Generate a presigned S3 upload URL and shape the HTTP response payload."""
    result = get_upload_url(user_id, payload.get("postId", ""))
    return 200, {
        "message": "Upload URL generated",
        "uploadUrl": result["uploadUrl"],
        "fileId": result["fileId"],
    }
