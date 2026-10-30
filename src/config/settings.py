from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the Lambda deployment.

    All values are injected as Lambda environment variables by the CDK stack
    (see infra/stack.py). There is no local .env / db.json in this deployment -
    state lives in DynamoDB, media lives in S3, and identity lives in Cognito.
    """

    aws_region: str = os.getenv("AWS_REGION", "ap-southeast-2")

    # DynamoDB
    users_table: str = os.getenv("USERS_TABLE", "")
    posts_table: str = os.getenv("POSTS_TABLE", "")
    comments_table: str = os.getenv("COMMENTS_TABLE", "")
    likes_table: str = os.getenv("LIKES_TABLE", "")
    follows_table: str = os.getenv("FOLLOWS_TABLE", "")
    moments_table: str = os.getenv("MOMENTS_TABLE", "")

    # S3
    media_bucket: str = os.getenv("MEDIA_BUCKET", "")

    # Cognito
    cognito_user_pool_id: str = os.getenv("COGNITO_USER_POOL_ID", "")
    cognito_client_id: str = os.getenv("COGNITO_CLIENT_ID", "")
    cognito_moderator_group: str = os.getenv("COGNITO_MODERATOR_GROUP", "moderator")

    def now_iso(self) -> str:
        """Return the current UTC timestamp in ISO-8601 format."""
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


settings = Settings()
