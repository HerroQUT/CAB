"""Retag every A1/A2 resource that A3 reuses to purpose=assessment 3.

Discovery uses the Resource Groups Tagging API (the same data Resource Explorer
shows): everything tagged with your qut-username in ap-southeast-2. The bulk
tag:TagResources call is denied to students, so each resource is retagged
through its own service's API instead. Only the purpose tag changes.

    python scripts/retag_a3.py --qut-username n11457660@qut.edu.au --dry-run
    python scripts/retag_a3.py --qut-username n11457660@qut.edu.au

Skipped on purpose: running ECS tasks and their network interfaces. They are
replaced on every deploy and inherit the service's tags (propagateTags).
"""
from __future__ import annotations

import argparse
from collections import Counter

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
TARGET = "assessment 3"
TAG = {"purpose": TARGET}
PAIRS = [{"Key": "purpose", "Value": TARGET}]


def short(arn: str) -> str:
    return arn.split(":", 5)[-1][:90]


def retag(s: boto3.Session, arn: str) -> str:
    """Tag one ARN via its own service. Returns 'ok', 'skip' or raises."""
    service = arn.split(":")[2]
    rest = arn.split(":", 5)[-1]

    if service == "ec2" or (service == "ecs" and rest.startswith("task/")):
        return "skip"
    if service == "dynamodb":
        s.client("dynamodb").tag_resource(ResourceArn=arn, Tags=PAIRS)
    elif service == "lambda":
        s.client("lambda").tag_resource(Resource=arn, Tags=TAG)
    elif service == "ecs":
        s.client("ecs").tag_resource(resourceArn=arn, tags=[{"key": "purpose", "value": TARGET}])
    elif service == "ecr":
        s.client("ecr").tag_resource(resourceArn=arn, tags=PAIRS)
    elif service == "events":
        s.client("events").tag_resource(ResourceARN=arn, Tags=PAIRS)
    elif service == "cognito-idp":
        s.client("cognito-idp").tag_resource(ResourceArn=arn, Tags=TAG)
    elif service == "apigateway":
        s.client("apigatewayv2").tag_resource(ResourceArn=arn, Tags=TAG)
    elif service == "acm":
        s.client("acm").add_tags_to_certificate(CertificateArn=arn, Tags=PAIRS)
    elif service == "ssm":
        name = "/" + rest.split("parameter/", 1)[1]
        s.client("ssm").add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=PAIRS)
    elif service == "sqs":
        sqs = s.client("sqs")
        sqs.tag_queue(QueueUrl=sqs.get_queue_url(QueueName=arn.split(":")[-1])["QueueUrl"], Tags=TAG)
    elif service == "s3":
        s3, bucket = s.client("s3"), arn.split(":::")[-1]
        # Buckets with attribute-based access control refuse PutBucketTagging;
        # their tags can only change through the S3 Control TagResource API.
        try:
            account = s.client("sts").get_caller_identity()["Account"]
            s.client("s3control").tag_resource(AccountId=account, ResourceArn=arn, Tags=PAIRS)
            return "ok"
        except (ClientError, AttributeError):
            pass
        try:   # PutBucketTagging replaces the whole set, so merge
            current = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
        except ClientError:
            current = {}
        current.update(TAG)
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in current.items()]})
    elif service == "s3vectors":
        s.client("s3vectors").tag_resource(resourceArn=arn, tags=TAG)
    else:
        raise RuntimeError(f"no tagger for service '{service}'")
    return "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    s = boto3.Session(region_name=REGION)
    tagging = s.client("resourcegroupstaggingapi")
    found: list[tuple[str, str]] = []
    for page in tagging.get_paginator("get_resources").paginate(
            TagFilters=[{"Key": "qut-username", "Values": [args.qut_username]}]):
        for r in page["ResourceTagMappingList"]:
            purpose = next((t["Value"] for t in r["Tags"] if t["Key"] == "purpose"), "(none)")
            found.append((r["ResourceARN"], purpose))

    print(f"{len(found)} resources tagged qut-username={args.qut_username}")
    print("  by purpose:", dict(Counter(p for _, p in found)))
    todo = [arn for arn, p in found if p != TARGET]
    print(f"  {len(todo)} need retagging")
    if args.dry_run:
        for arn in todo:
            print(f"    {short(arn)}")
        return

    ok, skipped, failed = 0, 0, {}
    for arn in todo:
        try:
            if retag(s, arn) == "ok":
                ok += 1
            else:
                skipped += 1
        except ClientError as e:
            msg = e.response["Error"].get("Message", "")
            failed[arn] = "denied by course policy" if "not authorized" in msg or "explicit deny" in msg \
                else f"{e.response['Error']['Code']}: {msg[:100]}"
        except Exception as e:  # noqa: BLE001
            failed[arn] = str(e)

    print(f"\nRetagged {ok}, skipped {skipped} (running tasks / ENIs), failed {len(failed)}.")
    for arn, reason in failed.items():
        print(f"    {short(arn)}\n        {reason}")
    if failed:
        print("Tag the failures by hand in the console, or note them in the report as an account constraint.")


if __name__ == "__main__":
    main()
