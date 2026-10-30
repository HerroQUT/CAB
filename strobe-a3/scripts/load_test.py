"""Burst-upload photos and record how the worker pool scales. Evidence for the video and report.

    python scripts/load_test.py --count 150 --photos ~/Pictures/demo
    python scripts/load_test.py --count 150            # synthetic images if no --photos
    python scripts/load_test.py --cleanup <run-id>     # delete everything a run created

What it does:
  1. logs in as the test user through the Strobe API (to get their Cognito sub);
  2. writes N images straight into the media bucket at <sub>/loadtest-<run>/<fileId>,
     the same key a presigned upload produces, so EventBridge -> SQS fires exactly
     as it does for a real upload (16 parallel uploads, i.e. a burst);
  3. every 10 s records queue depth, in-flight messages, the worker service's
     desired/running tasks and how many images are classified, until done.
The samples go to loadtest-<run>.csv: plot them for the scaling figure.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import random
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

REGION = "ap-southeast-2"
API = "https://zu3y6kfvnh.execute-api.ap-southeast-2.amazonaws.com"


def login(email: str, password: str) -> str:
    req = urllib.request.Request(f"{API}/v1/auth/login", method="POST",
                                 data=json.dumps({"email": email, "password": password}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        token = json.load(r)["token"]
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return claims["sub"]


def synthetic_image(i: int) -> bytes:
    from PIL import Image, ImageDraw
    rnd = random.Random(i)
    img = Image.new("RGB", (1600, 1200), tuple(rnd.randint(40, 220) for _ in range(3)))
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        x, y = rnd.randint(0, 1500), rnd.randint(0, 1100)
        r = rnd.randint(40, 260)
        draw.ellipse((x, y, x + r, y + r), fill=tuple(rnd.randint(0, 255) for _ in range(3)))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def load_photos(folder: str) -> list[bytes]:
    folder = os.path.expanduser(folder)
    files = [os.path.join(folder, f) for f in sorted(os.listdir(folder))
             if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    if not files:
        sys.exit(f"no images in {folder}")
    return [open(f, "rb").read() for f in files]


def service_counts(ecs, cluster: str, service: str) -> tuple[int, int]:
    svc = ecs.describe_services(cluster=cluster, services=[service])["services"]
    return (svc[0]["desiredCount"], svc[0]["runningCount"]) if svc else (0, 0)


def classified_count(table, post_id: str) -> int:
    n, kwargs = 0, {}
    while True:
        page = table.query(IndexName="byPost", KeyConditionExpression=Key("postId").eq(post_id),
                           Select="COUNT", **kwargs)
        n += page["Count"]
        if "LastEvaluatedKey" not in page:
            return n
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def cleanup(s, sid: str, sub: str, run: str) -> None:
    """Garbage-collect one run: originals, thumbnails, vectors, classification rows."""
    s3, s3v = s.client("s3"), s.client("s3vectors")
    table = s.resource("dynamodb").Table(f"{sid}-strobe-classifications")
    bucket, post_id = f"{sid}-s3-media", f"loadtest-{run}"
    keys = []
    for prefix in (f"{sub}/{post_id}/", f"thumbs/{sub}/{post_id}/"):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
    rows = table.query(IndexName="byPost", KeyConditionExpression=Key("postId").eq(post_id))["Items"]
    with table.batch_writer() as batch:
        for row in rows:
            batch.delete_item(Key={"imageId": row["imageId"]})
    vector_keys = [f"image#{row['imageId']}" for row in rows]
    for i in range(0, len(vector_keys), 100):
        s3v.delete_vectors(vectorBucketName=f"{sid}-strobe-vectors", indexName=f"{sid}-strobe-content",
                           keys=vector_keys[i:i + 100])
    print(f"deleted {len(keys)} objects, {len(rows)} rows, {len(vector_keys)} vectors for run {run}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", default="n11457660")
    ap.add_argument("--email", default="test1@example.com")
    ap.add_argument("--password", default="password123")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--photos", default=None, help="folder of real photos to cycle through")
    ap.add_argument("--timeout-min", type=int, default=30)
    ap.add_argument("--cleanup", metavar="RUN_ID")
    args = ap.parse_args()

    sid = args.student_id
    s = boto3.Session(region_name=REGION)
    sub = login(args.email, args.password)

    if args.cleanup:
        cleanup(s, sid, sub, args.cleanup)
        return

    s3, sqs, ecs = s.client("s3"), s.client("sqs"), s.client("ecs")
    table = s.resource("dynamodb").Table(f"{sid}-strobe-classifications")
    queue_url = sqs.get_queue_url(QueueName=f"{sid}-strobe-classify")["QueueUrl"]
    cluster, service = f"{sid}-strobe-a2", f"{sid}-strobe-worker"
    run = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
    post_id = f"loadtest-{run}"
    bucket = f"{sid}-s3-media"

    images = load_photos(args.photos) if args.photos else [synthetic_image(i) for i in range(20)]
    print(f"run {run}: uploading {args.count} images as user {sub[:8]}... ({len(images)} distinct)")

    def put(i: int) -> None:
        s3.put_object(Bucket=bucket, Key=f"{sub}/{post_id}/{uuid.uuid4().hex[:8]}",
                      Body=images[i % len(images)], ContentType="image/jpeg")

    started = time.time()
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(put, range(args.count)))
    print(f"uploaded in {time.time() - started:.1f}s; watching the pipeline (Ctrl-C to stop)\n")

    path = f"loadtest-{run}.csv"
    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["elapsed_s", "utc", "queue_visible", "in_flight", "desired_tasks", "running_tasks", "classified"])
        print(f"{'t(s)':>5} {'queued':>7} {'in-flight':>9} {'desired':>8} {'running':>8} {'done':>6}")
        deadline = started + args.timeout_min * 60
        try:
            while time.time() < deadline:
                attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=[
                    "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
                visible, inflight = int(attrs["ApproximateNumberOfMessages"]), int(attrs["ApproximateNumberOfMessagesNotVisible"])
                desired, running = service_counts(ecs, cluster, service)
                done = classified_count(table, post_id)
                elapsed = int(time.time() - started)
                out.writerow([elapsed, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              visible, inflight, desired, running, done])
                f.flush()
                print(f"{elapsed:>5} {visible:>7} {inflight:>9} {desired:>8} {running:>8} {done:>6}")
                if done >= args.count and running == 0:
                    print("\nall classified and the pool is back at zero")
                    break
                time.sleep(10)
        except KeyboardInterrupt:
            pass
    print(f"\nsamples saved to {path}")
    print(f"remove this run's data later with:  python scripts/load_test.py --cleanup {run}")


if __name__ == "__main__":
    main()
