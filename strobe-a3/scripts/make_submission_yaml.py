"""Generate the A2 submission YAML from what is actually deployed.

Enumerates this student's resources rather than trusting a hand-written list,
so nothing repurposed from Assessment 1 gets left out. Descriptions are written
here; check them and edit the output as you like.

    python scripts/make_submission_yaml.py --student-id n11457660 \
        --qut-username n11457660@qut.edu.au > a2-submission.yaml

    --check   list anything found that has no description yet
"""
from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
ACCOUNT = "901444280953"

# What each resource is for. Anything found but not listed here is still
# emitted, flagged with TODO, so nothing silently disappears.
DESCRIPTIONS = {
    # --- Assessment 1, repurposed
    "n11457660-ddb-users": "A1: user accounts; the agent reads a user's own profile through the A1 API",
    "n11457660-ddb-posts": "A1: posts; read by the agent for album narration and date questions",
    "n11457660-ddb-comments": "A1: comments; read by the agent for context on a post",
    "n11457660-ddb-likes": "A1: likes; unchanged from Assessment 1",
    "n11457660-ddb-follows": "A1: follows; unchanged from Assessment 1",
    "n11457660-ddb-moments": "A1: moments; unchanged from Assessment 1",
    "n11457660-s3-media": "A1: uploaded photos; now also the event source that triggers classification",
    "n11457660-lambda-auth": "A1: registration and login; issues the Cognito token the chat client signs in with",
    "n11457660-lambda-user": "A1: user endpoints, called by the MCP server as the signed-in user",
    "n11457660-lambda-post": "A1: post endpoints, called by the MCP server as the signed-in user",
    "n11457660-lambda-comment": "A1: comment endpoints, called by the MCP server as the signed-in user",
    "n11457660-lambda-upload": "A1: presigned upload URLs; unchanged, classification happens after the fact",
    "n11457660-lambda-moment": "A1: moment endpoints; unchanged from Assessment 1",
    "n11457660-lambda-feed": "A1: feed endpoints; unchanged from Assessment 1",
    "n11457660-lambda-follow": "A1: follow endpoints; unchanged from Assessment 1",

    # --- Assessment 2
    "n11457660-strobe-classifications": "A2: classification results (labels, caption, model, timestamp) per image",
    "n11457660-strobe-agent-runs": "A2: agent run log; each scheduled run records its summary and the runs it built on",
    "n11457660-strobe-classify": "A2: queues each uploaded image for classification, decoupling it from the upload request",
    "n11457660-strobe-classify-dlq": "A2: dead letter queue for images that fail classification three times",
    "n11457660-strobe-upload-to-classify": "A2: routes S3 Object Created events for the media bucket onto the classify queue",
    "n11457660-strobe-heartbeat-daily": "A2: daily schedule that triggers the retrospective agent with no user request",
    "n11457660-strobe-classifier": "A2: SQS worker: classifies and embeds each new image, then persists both",
    "n11457660-strobe-heartbeat": "A2: scheduled agent run: writes an 'on this day' or recent recap per user",
    "n11457660-strobe-mcp": "A2: MCP server exposing read-only Strobe tools, scoped to the caller's Cognito identity",
    "n11457660-strobe-agent": "A2: ACP agent: runs the Bedrock tool-use loop and serves the browser over WebSocket",
    "n11457660-strobe-webui": "A2: ACP chat web client with Cognito sign-in",
    "n11457660-strobe-a2": "A2: Fargate cluster hosting the web client, agent and MCP server",
    "n11457660-strobe-vectors": "A2: S3 Vectors bucket holding image and text embeddings",
    "n11457660-strobe-content": "A2: vector index (1024-d, cosine) with userId/postId/labels metadata for per-user filtering",
}

BEDROCK = [
    ("google.gemma-3-12b-it",
     "A2: chat, vision and narration; classifies uploaded photos and drives the agent's tool loop"),
    ("amazon.titan-embed-image-v1",
     "A2: multimodal embeddings; image and text queries share one vector space"),
]


def describe(name: str) -> str:
    return DESCRIPTIONS.get(name, f"TODO describe {name}")


def emit(entries: list[tuple[str, str, str]]) -> None:
    print("cloudInfrastructure:")
    for kind, arn, description in entries:
        print(f"  - type: {kind}")
        print(f"    arn: {arn}")
        safe = description.replace('"', "'")
        print(f'    description: "{safe}"')
        print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--check", action="store_true", help="list resources with no description")
    args = ap.parse_args()
    sid = args.student_id
    s = boto3.Session(region_name=REGION)
    entries: list[tuple[str, str, str]] = []

    def add(kind: str, arn: str, name: str) -> None:
        entries.append((kind, arn, describe(name)))

    # Bedrock
    for model_id, description in BEDROCK:
        entries.append(("Bedrock model", f"arn:aws:bedrock:{REGION}::foundation-model/{model_id}", description))

    # DynamoDB
    ddb = s.client("dynamodb")
    for page in ddb.get_paginator("list_tables").paginate():
        for name in page["TableNames"]:
            if name.startswith(sid):
                add("DynamoDB table", f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{name}", name)

    # S3
    for bucket in s.client("s3").list_buckets()["Buckets"]:
        if bucket["Name"].startswith(sid):
            add("S3 bucket", f"arn:aws:s3:::{bucket['Name']}", bucket["Name"])

    # S3 Vectors
    try:
        s3v = s.client("s3vectors")
        for vb in s3v.list_vector_buckets().get("vectorBuckets", []):
            name = vb["vectorBucketName"]
            if not name.startswith(sid):
                continue
            for index in s3v.list_indexes(vectorBucketName=name).get("indexes", []):
                entries.append(("Vector store", index["indexArn"], describe(index["indexName"])))
    except ClientError as e:
        print(f"# could not list vector indexes: {e}", file=sys.stderr)

    # Lambda
    lam = s.client("lambda")
    for page in lam.get_paginator("list_functions").paginate():
        for fn in page["Functions"]:
            if fn["FunctionName"].startswith(sid):
                add("Lambda function", fn["FunctionArn"], fn["FunctionName"])

    # SQS
    sqs = s.client("sqs")
    for url in sqs.list_queues(QueueNamePrefix=sid).get("QueueUrls", []):
        arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        add("SQS queue", arn, arn.split(":")[-1])

    # EventBridge
    events = s.client("events")
    for rule in events.list_rules(NamePrefix=sid)["Rules"]:
        add("EventBridge rule", rule["Arn"], rule["Name"])

    # ECS
    ecs = s.client("ecs")
    cluster = f"{sid}-strobe-a2"
    try:
        clusters = ecs.describe_clusters(clusters=[cluster])["clusters"]
        if clusters and clusters[0]["status"] == "ACTIVE":
            add("ECS cluster", clusters[0]["clusterArn"], cluster)
            for arn in ecs.list_services(cluster=cluster)["serviceArns"]:
                name = arn.split("/")[-1]
                kind = "MCP server" if name.endswith("-mcp") else "ECS service"
                add(kind, arn, name)
    except ClientError:
        pass

    # ECR
    try:
        for page in s.client("ecr").get_paginator("describe_repositories").paginate():
          for repo in page["repositories"]:
            if repo["repositoryName"].startswith(sid):
                entries.append(("ECR repository", repo["repositoryArn"],
                                f"A2: container image for {repo['repositoryName'].split('-')[-1]}"))
    except ClientError:
        pass

    # Cognito + API Gateway (A1, repurposed)
    try:
        cognito = s.client("cognito-idp")
        for page in cognito.get_paginator("list_user_pools").paginate(MaxResults=60):
            for pool in page["UserPools"]:
                details = cognito.describe_user_pool(UserPoolId=pool["Id"])["UserPool"]
                if sid in pool["Name"] or sid in str(details.get("Arn", "")):
                    entries.append(("Cognito user pool", details["Arn"],
                                    "A1: user identity; the chat client signs in here and the MCP server "
                                    "verifies the token on every tool call"))
    except ClientError:
        pass

    try:
        apigw = s.client("apigatewayv2")
        apis, token = [], None
        while True:
            page = apigw.get_apis(MaxResults="500", **({"NextToken": token} if token else {}))
            apis += page["Items"]
            token = page.get("NextToken")
            if not token:
                break
        for api in apis:
            if sid in api["Name"]:
                entries.append(("API Gateway HTTP API",
                                f"arn:aws:apigateway:{REGION}::/apis/{api['ApiId']}",
                                "A1: the Strobe API; the MCP server calls it as the signed-in user so A1's "
                                "authorisation rules still apply"))
    except ClientError:
        pass

    # Parameter Store
    try:
        ssm = s.client("ssm")
        for page in ssm.get_paginator("describe_parameters").paginate():
            for p in page["Parameters"]:
                if p["Name"].startswith(f"/{sid}/strobe-a2"):
                    entries.append(("Parameter Store parameter",
                                    f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter{p['Name']}",
                                    f"A2: runtime configuration ({p['Name'].rsplit('/', 1)[-1]}), "
                                    "read at start-up instead of being baked into images"))
    except ClientError:
        pass

    # Route 53: the names the browser and the agent actually resolve
    try:
        r53 = s.client("route53")
        zones = [z for z in r53.list_hosted_zones()["HostedZones"] if z["Name"] == "cab432.com."]
        if zones:
            zone_id = zones[0]["Id"].split("/")[-1]
            for key, role in (("webui", "the chat front end"), ("agent", "the ACP WebSocket endpoint"),
                              ("mcp", "the MCP server")):
                entries.append(("Route 53 record",
                                f"arn:aws:route53:::hostedzone/{zone_id}#{sid}-{key}.cab432.com",
                                f"A2: stable DNS name for {role}, repointed when a Fargate task restarts"))
    except ClientError:
        pass

    if args.check:
        missing = [arn for _, arn, d in entries if d.startswith("TODO")]
        print(f"{len(entries)} resources found, {len(missing)} without a description:")
        for arn in missing:
            print(f"  {arn}")
        return

    emit(entries)
    print("metaData:")
    print("  track: \"A\"")
    print(f"  qutUsernameTag: {args.qut_username}")


if __name__ == "__main__":
    main()
