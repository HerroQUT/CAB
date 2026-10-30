"""Check that retrieval works end to end, before the MCP server wraps it.

    python scripts/search_test.py find me the photos of my dogs
    python scripts/search_test.py birthday cake
    python scripts/search_test.py --image ~/Desktop/dog.jpg

Text queries and image queries hit the same index, because Titan Multimodal
puts both in one vector space. Distance is cosine: lower is a closer match.
"""
from __future__ import annotations

import argparse
import base64
import json

import boto3

REGION = "ap-southeast-2"
EMBED_MODEL = "amazon.titan-embed-image-v1"
VECTOR_BUCKET = "n11457660-strobe-vectors"
VECTOR_INDEX = "n11457660-strobe-content"
DEFAULT_USER = "294e9448-1031-70f5-a26c-c063178018a0"

session = boto3.Session(region_name=REGION)
brt = session.client("bedrock-runtime")
s3v = session.client("s3vectors")


def embed(*, text: str | None = None, image_path: str | None = None) -> list[float]:
    body: dict = {"embeddingConfig": {"outputEmbeddingLength": 1024}}
    if text:
        body["inputText"] = text[:1000]          # Titan MM caps text at 256 tokens
    if image_path:
        with open(image_path, "rb") as fh:
            body["inputImage"] = base64.b64encode(fh.read()).decode()
    response = brt.invoke_model(modelId=EMBED_MODEL, contentType="application/json",
                                accept="application/json", body=json.dumps(body))
    return json.loads(response["body"].read())["embedding"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("words", nargs="*", help="text query")
    ap.add_argument("--image", help="search with a photo instead of words")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    query = " ".join(args.words)
    if not query and not args.image:
        query = "photos of my dogs"

    vector = embed(text=query or None, image_path=args.image)
    hits = s3v.query_vectors(
        vectorBucketName=VECTOR_BUCKET,
        indexName=VECTOR_INDEX,
        topK=args.top_k,
        queryVector={"float32": vector},
        # Ownership is enforced here, not by the model: the MCP server will take
        # userId from the verified Cognito token, never from a tool argument.
        # S3 Vectors filters are MongoDB-style; combine terms with $and.
        filter={"$and": [{"userId": {"$eq": args.user}}, {"kind": {"$eq": "image"}}]},
        returnMetadata=True,
        returnDistance=True,
    )

    label = args.image if args.image else repr(query)
    print(f"query: {label}   (lower distance = closer match)")
    if not hits["vectors"]:
        print("  no matches - is the userId right?")
    for v in hits["vectors"]:
        meta = v["metadata"]
        print(f"  {v['distance']:.3f}  {meta['imageId']:10s} {meta['caption']}")
        print(f"         labels: {', '.join(meta.get('labels', []))}")


if __name__ == "__main__":
    main()
