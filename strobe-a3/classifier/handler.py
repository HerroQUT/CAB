"""Async image classifier for Strobe.

Flow (A3):  S3 media bucket --(EventBridge "Object Created")--> SQS --> worker/worker.py
            (an auto-scaled ECS Fargate service that long-polls the queue)
Flow (A2):  the same queue drained by a Lambda via handler() below - kept only
            as a rollback path; its event source mapping is disabled in A3.

For each uploaded image at key  <userId>/<postId>/<fileId>  it:
  1. downloads the object and normalises it (RGB JPEG, <= 2048 px longest side);
  2. asks Gemma 3 (vision) for labels + a one-line caption, as JSON;
  3. embeds the image with Titan Multimodal Embeddings G1 (1024-d);
  4. writes a 640 px thumbnail to thumbs/<userId>/<postId>/<fileId>.jpg, the
     object CloudFront caches at the edge (A3);
  5. writes the vector to S3 Vectors with ownership metadata;
  6. writes a Classification row to DynamoDB.

Every write is keyed by fileId, so SQS redelivery simply overwrites the same
records (idempotent). Failed messages are reported individually via
batchItemFailures and end up in the DLQ after maxReceiveCount attempts.

Configuration comes from Parameter Store under $SSM_PREFIX (not env vars):
  gemma-model-id, embed-model-id, vector-bucket, vector-index, classifications-table
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from PIL import Image, ImageOps

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_EDGE = 2048          # Titan MM hard limit is 2048x2048
EMBED_DIM = 1024
MAX_LABELS = 10
THUMB_EDGE = 640
THUMB_PREFIX = "thumbs/"
# Thumbnails are immutable (a new upload gets a new fileId), so edge caches and
# browsers may keep them for a year without ever revalidating.
THUMB_CACHE_CONTROL = "public, max-age=31536000, immutable"

CLASSIFY_PROMPT = (
    "You are classifying a photo from a personal photo-sharing app.\n"
    "Return ONLY a JSON object, no prose, no code fences, in exactly this shape:\n"
    '{"labels": ["<noun>", ...], "caption": "<one sentence>"}\n'
    "labels: 3 to 10 lowercase single nouns or short noun phrases for the main "
    "subjects, animals, objects, setting and activity (e.g. \"dog\", \"beach\", "
    "\"birthday cake\").\n"
    "caption: one plain factual sentence describing the photo."
)

_s3 = boto3.client("s3")
# Adaptive retries: when several workers hit the shared Bedrock quota at once,
# the client backs off and rate-limits itself instead of failing the message.
_brt = boto3.client("bedrock-runtime",
                    config=BotoConfig(retries={"mode": "adaptive", "max_attempts": 8}))
_s3v = boto3.client("s3vectors")
_ddb = boto3.resource("dynamodb")
_config: "Config | None" = None
_params: dict[str, str] = {}


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class Config:
    gemma_model_id: str
    embed_model_id: str
    vector_bucket: str
    vector_index: str
    classifications_table: str


def load_config() -> Config:
    """Read configuration once per container from Parameter Store."""
    global _config, _params
    if _config is None:
        prefix = os.environ["SSM_PREFIX"].rstrip("/")
        ssm = boto3.client("ssm")
        params: dict[str, str] = {}
        kwargs = {"Path": prefix, "Recursive": False}
        while True:
            page = ssm.get_parameters_by_path(**kwargs)
            for p in page["Parameters"]:
                params[p["Name"].rsplit("/", 1)[-1]] = p["Value"]
            if "NextToken" not in page:
                break
            kwargs["NextToken"] = page["NextToken"]
        _params = params
        _config = Config(
            gemma_model_id=params["gemma-model-id"],
            embed_model_id=params["embed-model-id"],
            vector_bucket=params["vector-bucket"],
            vector_index=params["vector-index"],
            classifications_table=params["classifications-table"],
        )
    return _config


def param(name: str, default: str | None = None) -> str | None:
    """Any other value under $SSM_PREFIX (e.g. queue-url for the ECS worker)."""
    load_config()
    return _params.get(name, default)


# --------------------------------------------------------------------------- pure helpers
@dataclass(frozen=True)
class ImageRef:
    bucket: str
    key: str
    user_id: str
    post_id: str
    file_id: str


def parse_event_body(body: str) -> ImageRef | None:
    """Turn an SQS message body (an EventBridge S3 event) into an ImageRef.

    Returns None for events we deliberately ignore (not an object-created
    event, or a key that is not <userId>/<postId>/<fileId>).
    """
    event = json.loads(body)
    if event.get("source") != "aws.s3" or event.get("detail-type") != "Object Created":
        return None
    detail = event["detail"]
    key = detail["object"]["key"]
    if key.startswith(THUMB_PREFIX):
        return None          # our own thumbnails must never be re-classified
    parts = key.split("/")
    if len(parts) != 3 or not all(parts):
        return None
    file_id = parts[2].rsplit(".", 1)[0]  # tolerate an extension if one is ever added
    return ImageRef(detail["bucket"]["name"], key, parts[0], parts[1], file_id)


def normalise_image(raw: bytes) -> tuple[bytes, int, int]:
    """Return (jpeg_bytes, width, height): EXIF-rotated, RGB, longest edge <= MAX_EDGE."""
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        if getattr(img, "is_animated", False):
            img.seek(0)                      # first frame of a GIF/WebP animation
        img = img.convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE))  # preserves aspect ratio, never upscales
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=88)
        return out.getvalue(), img.width, img.height


def make_thumbnail(jpeg: bytes, edge: int = THUMB_EDGE) -> bytes:
    """A small JPEG for feeds and chat; this is what the CDN serves and caches."""
    with Image.open(io.BytesIO(jpeg)) as img:
        img = img.convert("RGB")
        img.thumbnail((edge, edge))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=80, optimize=True)
        return out.getvalue()


def thumb_key(ref: "ImageRef") -> str:
    return f"{THUMB_PREFIX}{ref.user_id}/{ref.post_id}/{ref.file_id}.jpg"


def parse_classification(text: str) -> dict:
    """Extract {"labels": [...], "caption": str} from a model reply, defensively."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
    data = json.loads(match.group(0))

    labels_in = data.get("labels")
    if not isinstance(labels_in, list):
        raise ValueError("labels missing or not a list")
    labels: list[str] = []
    for item in labels_in:
        if isinstance(item, str):
            label = re.sub(r"\s+", " ", item).strip().lower()
            if label and label not in labels:
                labels.append(label[:40])
    if not labels:
        raise ValueError("no usable labels")

    caption = data.get("caption")
    caption = caption.strip() if isinstance(caption, str) else ""
    return {"labels": labels[:MAX_LABELS], "caption": caption[:300]}


# --------------------------------------------------------------------------- AWS calls
def classify(cfg: Config, jpeg: bytes) -> dict:
    """Ask Gemma for labels + caption; one retry with a stricter nudge on bad JSON."""
    prompt = CLASSIFY_PROMPT
    last_error: Exception | None = None
    for attempt in range(2):
        response = _brt.converse(
            modelId=cfg.gemma_model_id,
            messages=[{
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": jpeg}}},
                    {"text": prompt},  # instructions in the user turn: safe even without system-prompt support
                ],
            }],
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
        try:
            return parse_classification(text)
        except (ValueError, json.JSONDecodeError) as e:
            last_error = e
            log.warning("classification parse failed (attempt %d): %s", attempt + 1, e)
            prompt = CLASSIFY_PROMPT + "\nYour previous reply was not valid JSON. Output the JSON object only."
    raise RuntimeError(f"classification failed: {last_error}")


def embed_image(cfg: Config, jpeg: bytes) -> list[float]:
    response = _brt.invoke_model(
        modelId=cfg.embed_model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "inputImage": base64.b64encode(jpeg).decode(),
            "embeddingConfig": {"outputEmbeddingLength": EMBED_DIM},
        }),
    )
    return json.loads(response["body"].read())["embedding"]


def write_thumbnail(ref: ImageRef, jpeg: bytes) -> str | None:
    """Best effort: a missing thumbnail must not cost us the classification."""
    key = thumb_key(ref)
    try:
        _s3.put_object(Bucket=ref.bucket, Key=key, Body=make_thumbnail(jpeg),
                       ContentType="image/jpeg", CacheControl=THUMB_CACHE_CONTROL)
        return key
    except ClientError as e:
        log.warning("thumbnail not written for %s: %s", ref.file_id, e.response["Error"]["Code"])
        return None


def process(ref: ImageRef) -> dict:
    cfg = load_config()
    started = time.monotonic()

    obj = _s3.get_object(Bucket=ref.bucket, Key=ref.key)
    uploaded_at = obj["LastModified"]
    jpeg, width, height = normalise_image(obj["Body"].read())

    thumb = write_thumbnail(ref, jpeg)
    result = classify(cfg, jpeg)
    vector = embed_image(cfg, jpeg)
    now = datetime.now(timezone.utc)

    # Embedding/Chunk entity: lives in S3 Vectors, keyed by image.
    _s3v.put_vectors(
        vectorBucketName=cfg.vector_bucket,
        indexName=cfg.vector_index,
        vectors=[{
            "key": f"image#{ref.file_id}",
            "data": {"float32": vector},
            "metadata": {
                # filterable
                "userId": ref.user_id,
                "postId": ref.post_id,
                "imageId": ref.file_id,
                "kind": "image",
                "labels": result["labels"],
                "createdAt": int(uploaded_at.timestamp()),
                # non-filterable (declared on the index)
                "caption": result["caption"],
                "s3Key": ref.key,
            },
        }],
    )

    # Classification result entity: DynamoDB, attached to the image/post.
    row = {
        "imageId": ref.file_id,
        "userId": ref.user_id,
        "postId": ref.post_id,
        "s3Key": ref.key,
        "labels": result["labels"],
        "caption": result["caption"],
        "width": width,
        "height": height,
        "modelId": cfg.gemma_model_id,
        "embeddingModelId": cfg.embed_model_id,
        "vectorKey": f"image#{ref.file_id}",
        "uploadedAt": uploaded_at.isoformat(),
        "classifiedAt": now.isoformat().replace("+00:00", "Z"),
    }
    if thumb:
        row["thumbKey"] = thumb
    _ddb.Table(cfg.classifications_table).put_item(Item=row)

    summary = {
        "msg": "classified",
        "imageId": ref.file_id,
        "userId": ref.user_id,
        "labels": result["labels"],
        "thumbnail": bool(thumb),
        "latencyMs": int((time.monotonic() - started) * 1000),
        "queueLagSec": int(now.timestamp() - uploaded_at.timestamp()),
    }
    log.info(json.dumps(summary))
    return summary


# --------------------------------------------------------------------------- entry point
def handler(event: dict, _context) -> dict:
    """SQS batch handler with partial-batch failure reporting."""
    failures = []
    for record in event.get("Records", []):
        message_id = record["messageId"]
        try:
            ref = parse_event_body(record["body"])
            if ref is None:
                log.info("ignored message %s", message_id)
                continue
            process(ref)
        except _s3.exceptions.NoSuchKey:
            log.warning("object gone before processing (deleted?) - dropping %s", message_id)
        except Exception:
            log.exception("failed message %s", message_id)
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}
