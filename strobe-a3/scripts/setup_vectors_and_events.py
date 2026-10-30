"""One-off setup that CDK doesn't cover cleanly for this project.

  1. Creates the S3 Vectors bucket + index (tagged).
  2. Turns on EventBridge notifications for your EXISTING A1 media bucket,
     preserving any notification config already on it.

    python setup_vectors_and_events.py --student-id n1234567 \
        --qut-username n1234567@qut.edu.au --media-bucket <your A1 media bucket>

Safe to re-run: existing resources are left alone.
"""
from __future__ import annotations

import argparse

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
PURPOSE = "assessment 3"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--media-bucket", required=True)
    args = ap.parse_args()

    tags = {"qut-username": args.qut_username, "purpose": PURPOSE}
    vector_bucket = f"{args.student_id}-strobe-vectors"
    index = f"{args.student_id}-strobe-content"

    s3v = boto3.client("s3vectors", region_name=REGION)
    try:
        s3v.create_vector_bucket(vectorBucketName=vector_bucket, tags=tags)
        print(f"created vector bucket {vector_bucket}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        print(f"vector bucket {vector_bucket} already exists")

    try:
        s3v.create_index(
            vectorBucketName=vector_bucket,
            indexName=index,
            dataType="float32",
            dimension=1024,              # Titan Multimodal Embeddings G1 default
            distanceMetric="cosine",
            # Everything else (userId, postId, imageId, kind, labels, createdAt)
            # stays filterable so queries can be scoped to one user.
            metadataConfiguration={"nonFilterableMetadataKeys": ["caption", "s3Key", "text"]},
            tags=tags,
        )
        print(f"created index {index}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        print(f"index {index} already exists")

    arn = s3v.get_index(vectorBucketName=vector_bucket, indexName=index)["index"]["indexArn"]
    print(f"index ARN: {arn}")

    s3 = boto3.client("s3", region_name=REGION)
    config = s3.get_bucket_notification_configuration(Bucket=args.media_bucket)
    config.pop("ResponseMetadata", None)
    if "EventBridgeConfiguration" in config:
        print(f"EventBridge already enabled on {args.media_bucket}")
    else:
        config["EventBridgeConfiguration"] = {}
        s3.put_bucket_notification_configuration(Bucket=args.media_bucket, NotificationConfiguration=config)
        print(f"enabled EventBridge notifications on {args.media_bucket}")

    print("\nPut these in cdk.json context:")
    print(f'  "vectorBucketName": "{vector_bucket}",\n  "vectorIndexName": "{index}"')


if __name__ == "__main__":
    main()
