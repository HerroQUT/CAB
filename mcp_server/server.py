"""Strobe MCP server.

Exposes the user's own Strobe content to the agent as MCP tools. Deliberately
read-only: the agent can find and describe things, never post or delete on the
user's behalf.

Two data paths:
  - Semantic search goes straight to S3 Vectors, filtered by the caller's id.
  - Everything else goes through the Assessment 1 HTTP API with the caller's
    own token, so A1's authorisation rules (hidden posts, ownership) still
    apply and are not reimplemented here.

Identity comes from the Cognito token on the HTTP request, never from a tool
argument.
"""
from __future__ import annotations

import base64
import json
import logging
from contextvars import ContextVar
from typing import Any

import boto3
import httpx
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("strobe-mcp")

# Per-request identity, set by the middleware below.
_claims: ContextVar[dict] = ContextVar("claims", default={})
_bearer: ContextVar[str] = ContextVar("bearer", default="")

session = boto3.Session(region_name=config.REGION)
_brt = session.client("bedrock-runtime")
_s3v = session.client("s3vectors")
_s3 = session.client("s3")
_ddb = session.resource("dynamodb")

mcp = MCPServer("strobe")


# --------------------------------------------------------------------------- helpers
def caller_id() -> str:
    claims = _claims.get()
    if not claims:
        raise ValueError("Not authenticated: this tool needs the user's Cognito token.")
    return claims["sub"]


async def api_get(path: str, params: dict | None = None) -> Any:
    """Call the Assessment 1 API as the end user."""
    url = config.get("api-base-url").rstrip("/") + path
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params, headers={"Authorization": f"Bearer {_bearer.get()}"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def embed(text: str | None = None, image_bytes: bytes | None = None) -> list[float]:
    body: dict = {"embeddingConfig": {"outputEmbeddingLength": config.get_int("embed-dimension")}}
    if text:
        body["inputText"] = text[:1000]      # Titan Multimodal caps text at 256 tokens
    if image_bytes:
        body["inputImage"] = base64.b64encode(image_bytes).decode()
    response = _brt.invoke_model(modelId=config.get("embed-model-id"), contentType="application/json",
                                accept="application/json", body=json.dumps(body))
    return json.loads(response["body"].read())["embedding"]


def vector_search(vector: list[float], kind: str | None, top_k: int) -> list[dict]:
    terms: list[dict] = [{"userId": {"$eq": caller_id()}}]
    if kind:
        terms.append({"kind": {"$eq": kind}})
    hits = _s3v.query_vectors(
        vectorBucketName=config.get("vector-bucket"),
        indexName=config.get("vector-index"),
        topK=max(1, min(top_k, 25)),
        queryVector={"float32": vector},
        filter={"$and": terms} if len(terms) > 1 else terms[0],
        returnMetadata=True,
        returnDistance=True,
    )
    # Nearest-neighbour search always returns topK rows, however poor they are.
    # Keep the best match and anything close to it, so "my dogs" does not come
    # back with the user's whole library attached.
    vectors = hits.get("vectors", [])
    if vectors:
        best = min(float(v.get("distance", 0)) for v in vectors)
        margin = float(config.get("relevance-margin"))
        vectors = [v for v in vectors if float(v.get("distance", 0)) <= best + margin]

    results = []
    for v in vectors:
        meta = v.get("metadata", {})
        results.append({
            "imageId": meta.get("imageId"),
            "postId": meta.get("postId"),
            "caption": meta.get("caption"),
            "labels": meta.get("labels", []),
            "kind": meta.get("kind"),
            "distance": round(float(v.get("distance", 0)), 4),
        })
    return results


def classification_row(image_id: str) -> dict | None:
    table = _ddb.Table(config.get("classifications-table"))
    item = table.get_item(Key={"imageId": image_id}).get("Item")
    # Ownership check: the row records who uploaded the image.
    if item and item.get("userId") != caller_id():
        return None
    return item


# --------------------------------------------------------------------------- tools
@mcp.tool()
def search_my_content(query: str, top_k: int = 8, kind: str = "image") -> list[dict]:
    """Semantic search over the user's own photos and posts.

    Finds content by meaning rather than keywords: "my dogs" matches a photo
    classified as a golden retriever even when no text says "dog".

    Args:
        query: what to look for, in natural language.
        top_k: how many results to return (1-25).
        kind: "image", "post", "comment", or "any".
    Returns: matches ordered by similarity; lower distance is a closer match.
    """
    results = vector_search(embed(text=query), None if kind == "any" else kind, top_k)
    log.info(json.dumps({"tool": "search_my_content", "user": caller_id(),
                         "query": query, "hits": len(results)}))
    return results


@mcp.tool()
def find_similar_images(image_base64: str, top_k: int = 8) -> list[dict]:
    """Find the user's photos that look like an attached photo.

    Use when the user sends an image and asks for similar ones. The attached
    photo is embedded with the same model as their library, so image and text
    queries search the same space.
    """
    results = vector_search(embed(image_bytes=base64.b64decode(image_base64)), "image", top_k)
    log.info(json.dumps({"tool": "find_similar_images", "user": caller_id(), "hits": len(results)}))
    return results


@mcp.tool()
def get_classification(image_id: str) -> dict:
    """Labels and caption produced for one photo by the upload classifier.

    Returns an empty object if the photo is not the user's, or has not been
    classified yet (classification runs a few seconds after upload).
    """
    item = classification_row(image_id)
    if not item:
        return {}
    return {
        "imageId": item["imageId"],
        "postId": item.get("postId"),
        "caption": item.get("caption"),
        "labels": item.get("labels", []),
        "classifiedAt": item.get("classifiedAt"),
        "modelId": item.get("modelId"),
    }


@mcp.tool()
def get_image_urls(image_ids: list[str]) -> list[dict]:
    """Temporary viewable URLs for the user's photos, to show them in chat.

    Only returns URLs for images belonging to the caller. Links expire.
    """
    ttl = config.get_int("presign-ttl-seconds")
    out = []
    for image_id in image_ids[:12]:
        item = classification_row(image_id)
        if not item:
            continue
        out.append({
            "imageId": image_id,
            "caption": item.get("caption"),
            "url": _s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": config.get("media-bucket"), "Key": item["s3Key"]},
                ExpiresIn=ttl,
            ),
            "expiresInSeconds": ttl,
        })
    return out


@mcp.tool()
async def get_post(post_id: str) -> dict:
    """One post with its title, description, images and stats."""
    result = await api_get(f"/v1/posts/{post_id}")
    return result.get("post", {}) if result else {}


@mcp.tool()
async def list_my_posts(limit: int = 20, offset: int = 0) -> list[dict]:
    """The user's own posts, newest first.

    Useful for date-based questions ("what did I post in June?") and for
    sampling how the user writes before narrating an album in their voice.
    """
    result = await api_get(f"/v1/posts/user/{caller_id()}", {"limit": limit, "offset": offset})
    return result.get("posts", []) if result else []


@mcp.tool()
async def get_post_comments(post_id: str, limit: int = 20) -> list[dict]:
    """Comments on a post, for context when describing or narrating it."""
    result = await api_get(f"/v1/posts/{post_id}/comments", {"limit": limit})
    return result.get("comments", []) if result else []


@mcp.tool()
def get_agent_runs(limit: int = 5) -> list[dict]:
    """Summaries the scheduled agent produced for this user on earlier runs.

    Lets the assistant refer to what it noticed before ("on this day" recaps,
    monthly summaries) instead of starting cold each conversation.
    """
    try:
        table = _ddb.Table(config.get("agent-runs-table"))
        response = table.query(
            KeyConditionExpression=Key("userId").eq(caller_id()),
            ScanIndexForward=False,
            Limit=max(1, min(limit, 20)),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "AccessDeniedException"):
            return []          # heartbeat not deployed yet
        raise
    return [{
        "runAt": item.get("runAt"),
        "type": item.get("type"),
        "summary": item.get("summary"),
        "postIds": item.get("postIds", []),
    } for item in response.get("Items", [])]


# --------------------------------------------------------------------------- app
class CognitoMiddleware(BaseHTTPMiddleware):
    """Verify the Cognito token once per request and stash the claims."""

    async def dispatch(self, request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        token = request.headers.get("authorization", "")
        try:
            claims = auth.verify(token)
            _claims.set(claims)
            _bearer.set(token.removeprefix("Bearer ").strip())
        except auth.AuthError as e:
            log.info("unauthenticated request: %s", e)
            _claims.set({})
            _bearer.set("")
        return await call_next(request)


# stateless: every call carries its own token, so no server-side session state
app = mcp.streamable_http_app(stateless_http=True, json_response=True, host='0.0.0.0')
app.add_middleware(CognitoMiddleware)


async def healthz(_request):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("ok\n")


app.add_route("/healthz", healthz)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
