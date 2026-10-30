"""Configuration for the Strobe MCP server.

Order of precedence: environment variable > Parameter Store > built-in default.
The defaults are this student's real resources, so the server runs locally with
no setup; in ECS the task definition points MCP_SSM_PREFIX at Parameter Store.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("strobe-mcp")

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

DEFAULTS = {
    "api-base-url": "https://zu3y6kfvnh.execute-api.ap-southeast-2.amazonaws.com",
    "cognito-user-pool-id": "ap-southeast-2_gyxH5uIZM",
    "cognito-client-id": "1uotbt6057c3j5kah9ka002e85",
    "media-bucket": "n11457660-s3-media",
    "vector-bucket": "n11457660-strobe-vectors",
    "vector-index": "n11457660-strobe-content",
    "classifications-table": "n11457660-strobe-classifications",
    "agent-runs-table": "n11457660-strobe-agent-runs",
    "embed-model-id": "amazon.titan-embed-image-v1",
    "embed-dimension": "1024",
    "presign-ttl-seconds": "900",
    # A3: CloudFront domain for thumbnails; empty means fall back to presigned S3
    "cdn-domain": "",
    # A3: in-memory cache of query embeddings
    "embed-cache-size": "512",
    "embed-cache-ttl-seconds": "3600",
    # how much worse than the best match a result may be before it is dropped
    "relevance-margin": "0.08",
}


@lru_cache(maxsize=1)
def _ssm_values() -> dict[str, str]:
    prefix = os.environ.get("MCP_SSM_PREFIX")
    if not prefix:
        return {}
    try:
        ssm = boto3.client("ssm", region_name=REGION)
        values: dict[str, str] = {}
        kwargs = {"Path": prefix.rstrip("/"), "Recursive": False}
        while True:
            page = ssm.get_parameters_by_path(**kwargs)
            for p in page["Parameters"]:
                values[p["Name"].rsplit("/", 1)[-1]] = p["Value"]
            if "NextToken" not in page:
                break
            kwargs["NextToken"] = page["NextToken"]
        log.info("loaded %d parameters from %s", len(values), prefix)
        return values
    except ClientError as e:
        log.warning("Parameter Store unavailable (%s); using defaults", e.response["Error"]["Code"])
        return {}


def get(key: str) -> str:
    env_key = "MCP_" + key.replace("-", "_").upper()
    return os.environ.get(env_key) or _ssm_values().get(key) or DEFAULTS[key]


def get_int(key: str) -> int:
    return int(get(key))
