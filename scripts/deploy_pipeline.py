"""Deploy the Strobe A2 classification pipeline with plain AWS API calls.

Replaces CDK, which needs a CDKToolkit bootstrap stack this course account will
not allow (bootstrap creates IAM roles; the student role cannot).

Creates / updates, idempotently:
  - DynamoDB table  <sid>-strobe-classifications  (+ byUser, byPost GSIs)
  - SQS queues      <sid>-strobe-classify  and  -classify-dlq (redrive after 3)
  - SSM parameters  /<sid>/strobe-a2/classifier/*
  - Lambda          <sid>-strobe-classifier  (from classifier/build)
  - EventBridge     <sid>-strobe-upload-to-classify  ->  the queue
  - SQS event source mapping (batch 2, max concurrency 2, partial failures)

Re-running updates the function code and leaves everything else in place.

    python scripts/deploy_pipeline.py --student-id n11457660 \
        --qut-username n11457660@qut.edu.au \
        --media-bucket n11457660-s3-media \
        --vector-bucket n11457660-strobe-vectors \
        --vector-index n11457660-strobe-content

--role-arn is optional: if omitted the script reuses the execution role of an
existing <sid>-lambda-* function from Assessment 1.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
GEMMA = "google.gemma-3-12b-it"
TITAN_MM = "amazon.titan-embed-image-v1"
TIMEOUT = 90
BUILD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "classifier", "build")


def code(e: ClientError) -> str:
    return e.response["Error"]["Code"]


def log(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ role discovery
def find_role(lam, student_id: str) -> str:
    """Reuse the execution role of an existing A1 Lambda."""
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page["Functions"]:
            if fn["FunctionName"].startswith(f"{student_id}-") and "classifier" not in fn["FunctionName"]:
                log(f"  reusing execution role from {fn['FunctionName']}")
                return fn["Role"]
    sys.exit("No existing Lambda found to borrow a role from - pass --role-arn explicitly.")


# ------------------------------------------------------------------ resources
def ensure_table(ddb, name: str, tags: dict) -> str:
    try:
        ddb.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "imageId", "AttributeType": "S"},
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "postId", "AttributeType": "S"},
                {"AttributeName": "classifiedAt", "AttributeType": "S"},
            ],
            KeySchema=[{"AttributeName": "imageId", "KeyType": "HASH"}],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "byUser",
                    "KeySchema": [
                        {"AttributeName": "userId", "KeyType": "HASH"},
                        {"AttributeName": "classifiedAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "byPost",
                    "KeySchema": [{"AttributeName": "postId", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )
        log(f"  created table {name}")
        ddb.get_waiter("table_exists").wait(TableName=name)
    except ClientError as e:
        if code(e) != "ResourceInUseException":
            raise
        log(f"  table {name} already exists")
    return ddb.describe_table(TableName=name)["Table"]["TableArn"]


def ensure_queue(sqs, name: str, attrs: dict, tags: dict) -> tuple[str, str]:
    try:
        url = sqs.create_queue(QueueName=name, Attributes=attrs, tags=tags)["QueueUrl"]
        log(f"  created queue {name}")
    except ClientError as e:
        if code(e) not in ("QueueAlreadyExists", "QueueNameExists"):
            raise
        url = sqs.get_queue_url(QueueName=name)["QueueUrl"]
        sqs.set_queue_attributes(QueueUrl=url, Attributes=attrs)
        log(f"  queue {name} already exists (attributes updated)")
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    return url, arn


def allow_eventbridge(sqs, queue_url: str, queue_arn: str, rule_arn: str) -> None:
    """Resource policy so EventBridge may deliver to the queue."""
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowEventBridge",
            "Effect": "Allow",
            "Principal": {"Service": "events.amazonaws.com"},
            "Action": "sqs:SendMessage",
            "Resource": queue_arn,
            "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
        }],
    }
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"Policy": json.dumps(policy)})
    log("  queue policy allows EventBridge")


def ensure_params(ssm, prefix: str, values: dict, tags: dict) -> None:
    """Write config parameters.

    The course deny policy only allows ssm:PutParameter when the request carries
    the required tags, and SSM refuses Tags together with Overwrite. So an
    existing parameter is left alone if unchanged, and otherwise deleted and
    recreated with tags.
    """
    tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
    for key, value in values.items():
        name = f"{prefix}/{key}"
        try:
            ssm.put_parameter(Name=name, Value=value, Type="String", Tags=tag_list)
        except ClientError as e:
            if code(e) != "ParameterAlreadyExists":
                raise
            current = ssm.get_parameter(Name=name)["Parameter"]["Value"]
            if current == value:
                continue
            ssm.delete_parameter(Name=name)
            ssm.put_parameter(Name=name, Value=value, Type="String", Tags=tag_list)
    print(f"  wrote {len(values)} parameters under {prefix}")

def build_zip() -> bytes:
    if not os.path.isdir(BUILD_DIR):
        sys.exit("classifier/build missing - run ./scripts/build_classifier.sh first.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(BUILD_DIR):
            for filename in files:
                if filename.endswith((".pyc", ".pyo")) or "__pycache__" in root:
                    continue
                full = os.path.join(root, filename)
                zf.write(full, os.path.relpath(full, BUILD_DIR))
    data = buffer.getvalue()
    log(f"  packaged {len(data) / 1e6:.1f} MB zip")
    return data


def ensure_function(lam, name: str, role_arn: str, zip_bytes: bytes, ssm_prefix: str, tags: dict) -> str:
    config = dict(
        Role=role_arn, Handler="handler.handler", Runtime="python3.12",
        Timeout=TIMEOUT, MemorySize=1024,
        Environment={"Variables": {"SSM_PREFIX": ssm_prefix}},
        Description="Classifies and embeds uploaded Strobe images (SQS worker)",
    )
    try:
        arn = lam.create_function(FunctionName=name, Code={"ZipFile": zip_bytes},
                                  Architectures=["x86_64"], Tags=tags, **config)["FunctionArn"]
        log(f"  created function {name}")
    except ClientError as e:
        if code(e) != "ResourceConflictException":
            raise
        lam.update_function_code(FunctionName=name, ZipFile=zip_bytes)
        waiter = lam.get_waiter("function_updated_v2")
        waiter.wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, **config)
        waiter.wait(FunctionName=name)
        arn = lam.get_function_configuration(FunctionName=name)["FunctionArn"]
        log(f"  updated function {name}")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    return arn


def ensure_mapping(lam, queue_arn: str, function_name: str) -> None:
    existing = [m for m in lam.list_event_source_mappings(
        EventSourceArn=queue_arn, FunctionName=function_name)["EventSourceMappings"]]
    settings = dict(
        BatchSize=2,
        MaximumBatchingWindowInSeconds=2,
        ScalingConfig={"MaximumConcurrency": 2},      # protect the shared Bedrock quota
        FunctionResponseTypes=["ReportBatchItemFailures"],
    )
    if existing:
        lam.update_event_source_mapping(UUID=existing[0]["UUID"], Enabled=True, **settings)
        log("  updated event source mapping")
    else:
        lam.create_event_source_mapping(EventSourceArn=queue_arn, FunctionName=function_name,
                                        Enabled=True, **settings)
        log("  created event source mapping")


def ensure_rule(events, name: str, bucket: str, queue_arn: str, tags: dict) -> str:
    pattern = {
        "source": ["aws.s3"],
        "detail-type": ["Object Created"],
        "detail": {"bucket": {"name": [bucket]}},
    }
    arn = events.put_rule(
        Name=name, EventPattern=json.dumps(pattern), State="ENABLED",
        Description="Queues every new Strobe media object for classification",
        Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
    )["RuleArn"]
    events.put_targets(Rule=name, Targets=[{"Id": "classify-queue", "Arn": queue_arn}])
    log(f"  rule {name} -> queue")
    return arn


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--media-bucket", required=True)
    ap.add_argument("--vector-bucket", required=True)
    ap.add_argument("--vector-index", required=True)
    ap.add_argument("--role-arn", default=None)
    args = ap.parse_args()

    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": "assessment 2"}
    session = boto3.Session(region_name=REGION)
    ddb, sqs, ssm = session.client("dynamodb"), session.client("sqs"), session.client("ssm")
    lam, events = session.client("lambda"), session.client("events")

    log("Role:")
    role_arn = args.role_arn or find_role(lam, sid)

    log("DynamoDB:")
    table_name = f"{sid}-strobe-classifications"
    table_arn = ensure_table(ddb, table_name, tags)

    log("SQS:")
    _, dlq_arn = ensure_queue(sqs, f"{sid}-strobe-classify-dlq",
                              {"MessageRetentionPeriod": "1209600"}, tags)
    queue_url, queue_arn = ensure_queue(
        sqs, f"{sid}-strobe-classify",
        {
            "VisibilityTimeout": str(TIMEOUT * 6),
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 3}),
        },
        tags,
    )

    log("EventBridge:")
    rule_arn = ensure_rule(events, f"{sid}-strobe-upload-to-classify", args.media_bucket, queue_arn, tags)
    allow_eventbridge(sqs, queue_url, queue_arn, rule_arn)

    log("Parameter Store:")
    prefix = f"/{sid}/strobe-a2/classifier"
    ensure_params(ssm, prefix, {
        "gemma-model-id": GEMMA,
        "embed-model-id": TITAN_MM,
        "vector-bucket": args.vector_bucket,
        "vector-index": args.vector_index,
        "classifications-table": table_name,
    }, tags)

    log("Lambda:")
    function_name = f"{sid}-strobe-classifier"
    function_arn = ensure_function(lam, function_name, role_arn, build_zip(), prefix, tags)
    ensure_mapping(lam, queue_arn, function_name)

    log("\n=== ARNs for submission YAML ===")
    for label, value in {
        "DynamoDB table": table_arn,
        "SQS queue": queue_arn,
        "SQS DLQ": dlq_arn,
        "EventBridge rule": rule_arn,
        "Lambda function": function_arn,
        "S3 Vectors index": f"arn:aws:s3vectors:{REGION}:{session.client('sts').get_caller_identity()['Account']}"
                            f":bucket/{args.vector_bucket}/index/{args.vector_index}",
        "Bedrock (chat/vision)": f"arn:aws:bedrock:{REGION}::foundation-model/{GEMMA}",
        "Bedrock (embeddings)": f"arn:aws:bedrock:{REGION}::foundation-model/{TITAN_MM}",
    }.items():
        print(f"  {label:24s} {value}")

    print("\nNow upload a photo through your A1 flow and watch:")
    print(f"  aws logs tail /aws/lambda/{function_name} --follow --region {REGION}")


if __name__ == "__main__":
    main()
