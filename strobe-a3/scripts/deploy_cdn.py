"""Edge caching for Strobe thumbnails: CloudFront in front of the private media bucket.

    browser / chat --HTTPS--> CloudFront edge (Sydney, Melbourne, ...) --OAC--> S3 media bucket

  - The bucket stays private. CloudFront signs its own requests to S3 with
    Origin Access Control, and the bucket policy lets that one distribution
    read ONLY thumbs/*. Originals are still reachable solely through
    short-lived presigned URLs issued after an ownership check.
  - Thumbnails are written with Cache-Control "max-age=31536000, immutable"
    and never change (a new upload gets a new fileId), so the edge can keep
    them for a year and nothing ever needs invalidating.
  - The MCP server learns the CDN domain from Parameter Store (cdn-domain).

    python scripts/deploy_cdn.py --student-id n11457660 --qut-username n11457660@qut.edu.au
    python scripts/deploy_cdn.py ... --backfill      also make thumbnails for A2-era photos

Idempotent: finds its own OAC and distribution by name and comment.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "classifier"))
from deploy_pipeline import ensure_params  # noqa: E402

REGION = "ap-southeast-2"
ACCOUNT = "901444280953"
PURPOSE = "assessment 3"
CACHING_OPTIMIZED = "658327ea-f89d-4fab-a63d-7e88639e58f6"   # AWS managed cache policy


def why(e: ClientError) -> str:
    msg = e.response["Error"].get("Message", "")
    return "DENIED by course policy" if "explicit deny" in msg else f"{e.response['Error']['Code']}: {msg[:160]}"


def ensure_oac(cf, name: str, reuse: str | None = None) -> str:
    """Our own OAC if possible, otherwise reuse an equivalent one in the account.

    An OAC holds no secret and grants nothing by itself: it only tells CloudFront
    to SigV4-sign its requests to S3. What actually authorises access is OUR
    bucket policy, which names OUR distribution ARN. So when the shared account
    has hit its OAC quota (100), attaching any existing S3 / sigv4 / always OAC
    is equally secure. CloudFront also refuses to delete an OAC while a
    distribution uses it, so a reused one cannot vanish from under us.
    """
    candidates = []
    marker = None
    while True:
        page = cf.list_origin_access_controls(**({"Marker": marker} if marker else {}))
        listing = page["OriginAccessControlList"]
        for item in listing.get("Items", []):
            if item["Name"] == name or item["Id"] == reuse:
                print(f"  using OAC {item['Name']} ({item['Id']})")
                return item["Id"]
            if (item.get("OriginAccessControlOriginType") == "s3" and item.get("SigningProtocol") == "sigv4"
                    and item.get("SigningBehavior") == "always"):
                candidates.append(item)
        marker = listing.get("NextMarker")
        if not marker:
            break
    try:
        oac = cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": name, "Description": "Strobe media bucket, thumbnails only",
            "SigningProtocol": "sigv4", "SigningBehavior": "always", "OriginAccessControlOriginType": "s3",
        })["OriginAccessControl"]
        print(f"  created OAC {name} ({oac['Id']})")
        return oac["Id"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "TooManyOriginAccessControls" or not candidates:
            raise
    chosen = candidates[0]
    print(f"  account OAC quota is full; reusing equivalent OAC {chosen['Name']} ({chosen['Id']})")
    print("  (safe: access is granted by our bucket policy, scoped to our distribution ARN)")
    return chosen["Id"]


def find_distribution(cf, comment: str) -> dict | None:
    for page in cf.get_paginator("list_distributions").paginate():
        for d in page["DistributionList"].get("Items", []):
            if d.get("Comment") == comment:
                return d
    return None


def ensure_distribution(cf, comment: str, bucket: str, oac_id: str, tags: dict) -> dict:
    existing = find_distribution(cf, comment)
    if existing:
        print(f"  distribution {existing['Id']} exists ({existing['DomainName']}, {existing['Status']})")
        return existing
    config = {
        "CallerReference": str(uuid.uuid4()),
        "Comment": comment,
        "Enabled": True,
        "PriceClass": "PriceClass_All",          # the only class with Australian edge locations
        "HttpVersion": "http2and3",
        "IsIPV6Enabled": True,
        "Origins": {"Quantity": 1, "Items": [{
            "Id": "media",
            "DomainName": f"{bucket}.s3.{REGION}.amazonaws.com",
            "S3OriginConfig": {"OriginAccessIdentity": ""},
            "OriginAccessControlId": oac_id,
        }]},
        "DefaultCacheBehavior": {
            "TargetOriginId": "media",
            "ViewerProtocolPolicy": "redirect-to-https",
            "CachePolicyId": CACHING_OPTIMIZED,
            "Compress": True,
            "AllowedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"],
                               "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
        },
    }
    d = cf.create_distribution_with_tags(DistributionConfigWithTags={
        "DistributionConfig": config,
        "Tags": {"Items": [{"Key": k, "Value": v} for k, v in tags.items()]},
    })["Distribution"]
    print(f"  created distribution {d['Id']} ({d['DomainName']}); edge deployment takes ~5 min")
    return d


def allow_cloudfront(s3, bucket: str, distribution_arn: str) -> None:
    """Merge one statement into the bucket policy: this distribution, thumbs/* only."""
    try:
        policy = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchBucketPolicy":
            raise
        policy = {"Version": "2012-10-17", "Statement": []}
    sid = "StrobeCloudFrontThumbnailsOnly"
    policy["Statement"] = [st for st in policy["Statement"] if st.get("Sid") != sid]
    policy["Statement"].append({
        "Sid": sid,
        "Effect": "Allow",
        "Principal": {"Service": "cloudfront.amazonaws.com"},
        "Action": "s3:GetObject",
        "Resource": f"arn:aws:s3:::{bucket}/thumbs/*",
        "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
    })
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))
    print(f"  bucket policy: {distribution_arn.rsplit('/', 1)[-1]} may read thumbs/* and nothing else")


def backfill(s3, ddb, bucket: str, table_name: str) -> None:
    """Thumbnails for photos classified before A3 (run from your laptop)."""
    import handler  # classifier/handler.py; Pillow needed locally
    table = ddb.Table(table_name)
    done = skipped = 0
    kwargs: dict = {}
    while True:
        page = table.scan(**kwargs)
        for item in page["Items"]:
            if item.get("thumbKey") or not item.get("s3Key"):
                skipped += 1
                continue
            parts = item["s3Key"].split("/")
            if len(parts) != 3:
                skipped += 1
                continue
            try:
                raw = s3.get_object(Bucket=bucket, Key=item["s3Key"])["Body"].read()
            except ClientError:
                skipped += 1
                continue
            jpeg, _, _ = handler.normalise_image(raw)
            key = f"thumbs/{parts[0]}/{parts[1]}/{item['imageId']}.jpg"
            s3.put_object(Bucket=bucket, Key=key, Body=handler.make_thumbnail(jpeg),
                          ContentType="image/jpeg", CacheControl=handler.THUMB_CACHE_CONTROL)
            table.update_item(Key={"imageId": item["imageId"]},
                              UpdateExpression="SET thumbKey = :k", ExpressionAttributeValues={":k": key})
            done += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    print(f"  backfilled {done} thumbnails ({skipped} skipped)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--media-bucket", default=None)
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--wait", action="store_true", help="block until the edge deployment finishes")
    ap.add_argument("--reuse-oac", default=None, help="id of an existing OAC to use")
    args = ap.parse_args()

    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": PURPOSE}
    bucket = args.media_bucket or f"{sid}-s3-media"
    s = boto3.Session(region_name=REGION)
    cf, s3, ssm = s.client("cloudfront"), s.client("s3"), s.client("ssm")

    print("=== CloudFront ===")
    try:
        d = find_distribution(cf, f"{sid}-strobe-media")
        if d:
            print(f"  distribution {d['Id']} exists ({d['DomainName']}, {d['Status']})")
        else:
            oac_id = ensure_oac(cf, f"{sid}-strobe-media-oac", args.reuse_oac)
            d = ensure_distribution(cf, f"{sid}-strobe-media", bucket, oac_id, tags)
    except ClientError as e:
        sys.exit(f"  !! CloudFront refused: {why(e)}\n  Run scripts/probe_a3.py and send me the output.")
    dist_arn = f"arn:aws:cloudfront::{ACCOUNT}:distribution/{d['Id']}"

    print("\n=== S3 ===")
    try:
        allow_cloudfront(s3, bucket, dist_arn)
    except ClientError as e:
        sys.exit(f"  !! could not update the bucket policy: {why(e)}")

    print("\n=== Parameter Store (read by the MCP server) ===")
    ensure_params(ssm, f"/{sid}/strobe-a2/mcp",
                  {"cdn-domain": d["DomainName"], "cdn-distribution-id": d["Id"]}, tags)

    if args.backfill:
        print("\n=== Backfill ===")
        backfill(s3, s.resource("dynamodb"), bucket, f"{sid}-strobe-classifications")

    if args.wait:
        print("\nWaiting for the edge deployment ...")
        cf.get_waiter("distribution_deployed").wait(Id=d["Id"])

    print("\n=== Done ===")
    print(f"  CDN domain:   https://{d['DomainName']}")
    print(f"  Distribution: {dist_arn}")
    print("  Next: redeploy the MCP server so it picks up cdn-domain, then the agent:")
    print(f"    python scripts/deploy_services.py --student-id {sid} --qut-username {args.qut_username} \\")
    print("        --webui-dir <acp_client dir> --only mcp")
    print(f"    python scripts/refresh_dns.py --student-id {sid}")
    print("  Prove the cache (2nd request should say 'Hit from cloudfront'):")
    print(f"    curl -sI https://{d['DomainName']}/thumbs/<userId>/<postId>/<fileId>.jpg | grep -i x-cache")


if __name__ == "__main__":
    main()
