"""Deploy the scheduled retrospective (heartbeat).

Creates, idempotently:
  - DynamoDB  <sid>-strobe-agent-runs   (PK userId, SK runAt)
  - Lambda    <sid>-strobe-heartbeat
  - EventBridge schedule <sid>-strobe-heartbeat-daily  -> the Lambda
  - SSM parameters for the MCP server's agent-runs table name

    python scripts/deploy_heartbeat.py --student-id n11457660 \
        --qut-username n11457660@qut.edu.au

Then run one immediately (this is also the demo):
    aws lambda invoke --function-name n11457660-strobe-heartbeat \
        --payload '{}' --cli-binary-format raw-in-base64-out /dev/stdout
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
PURPOSE = "assessment 3"
CHAT_MODEL = "google.gemma-3-12b-it"
# 21:00 UTC = 07:00 Brisbane
SCHEDULE = "cron(0 21 * * ? *)"
HANDLER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "heartbeat", "handler.py")


def code(e: ClientError) -> str:
    return e.response["Error"]["Code"]


def find_role(lam, student_id: str) -> str:
    for page in lam.get_paginator("list_functions").paginate():
        for fn in page["Functions"]:
            if fn["FunctionName"] == f"{student_id}-strobe-classifier":
                return fn["Role"]          # already has Bedrock + DynamoDB
    for page in lam.get_paginator("list_functions").paginate():
        for fn in page["Functions"]:
            if fn["FunctionName"].startswith(f"{student_id}-"):
                return fn["Role"]
    sys.exit("No Lambda found to borrow a role from - pass --role-arn.")


def ensure_table(ddb, name: str, tags: dict) -> str:
    try:
        ddb.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "runAt", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "runAt", "KeyType": "RANGE"},
            ],
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )
        print(f"  created table {name}")
        ddb.get_waiter("table_exists").wait(TableName=name)
    except ClientError as e:
        if code(e) != "ResourceInUseException":
            raise
        print(f"  table {name} already exists")
    return ddb.describe_table(TableName=name)["Table"]["TableArn"]


def zip_handler() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(HANDLER_PATH, "handler.py")     # boto3 only: the runtime provides it
    return buffer.getvalue()


def ensure_function(lam, name: str, role_arn: str, env: dict, tags: dict) -> str:
    config = dict(
        Role=role_arn, Handler="handler.handler", Runtime="python3.12",
        Timeout=300, MemorySize=512, Environment={"Variables": env},
        Description="Scheduled retrospectives: writes an agent run per user",
    )
    payload = zip_handler()
    try:
        arn = lam.create_function(FunctionName=name, Code={"ZipFile": payload},
                                  Architectures=["x86_64"], Tags=tags, **config)["FunctionArn"]
        print(f"  created function {name}")
    except ClientError as e:
        if code(e) != "ResourceConflictException":
            raise
        lam.update_function_code(FunctionName=name, ZipFile=payload)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, **config)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        arn = lam.get_function_configuration(FunctionName=name)["FunctionArn"]
        print(f"  updated function {name}")
    lam.get_waiter("function_active_v2").wait(FunctionName=name)
    return arn


def ensure_schedule(events, lam, rule_name: str, function_arn: str, function_name: str, tags: dict) -> str:
    rule_arn = events.put_rule(
        Name=rule_name, ScheduleExpression=SCHEDULE, State="ENABLED",
        Description="Daily Strobe retrospective (nobody asks for it)",
        Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
    )["RuleArn"]
    events.put_targets(Rule=rule_name, Targets=[{"Id": "heartbeat", "Arn": function_arn}])
    try:
        lam.add_permission(FunctionName=function_name, StatementId="events-invoke",
                           Action="lambda:InvokeFunction", Principal="events.amazonaws.com",
                           SourceArn=rule_arn)
        print("  allowed EventBridge to invoke the function")
    except ClientError as e:
        if code(e) != "ResourceConflictException":
            raise
        print("  invoke permission already present")
    print(f"  schedule {rule_name}: {SCHEDULE}")
    return rule_arn


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

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--role-arn", default=None)
    args = ap.parse_args()

    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": PURPOSE}
    session = boto3.Session(region_name=REGION)
    ddb, lam = session.client("dynamodb"), session.client("lambda")
    events, ssm = session.client("events"), session.client("ssm")

    runs_table = f"{sid}-strobe-agent-runs"
    classifications = f"{sid}-strobe-classifications"
    function_name = f"{sid}-strobe-heartbeat"
    rule_name = f"{sid}-strobe-heartbeat-daily"

    print("Role:")
    role_arn = args.role_arn or find_role(lam, sid)
    print(f"  {role_arn}")

    print("DynamoDB:")
    table_arn = ensure_table(ddb, runs_table, tags)

    print("Lambda:")
    function_arn = ensure_function(lam, function_name, role_arn, {
        "CLASSIFICATIONS_TABLE": classifications,
        "AGENT_RUNS_TABLE": runs_table,
        "CHAT_MODEL_ID": CHAT_MODEL,
        "MAX_USERS": "10",
        "RECENT_DAYS": "7",
    }, tags)

    print("EventBridge:")
    rule_arn = ensure_schedule(events, lam, rule_name, function_arn, function_name, tags)

    print("Parameter Store:")
    ensure_params(ssm, f"/{sid}/strobe-a2/mcp", {"agent-runs-table": runs_table}, tags)

    print("\n=== ARNs for submission YAML ===")
    for label, value in {
        "Agent runs table": table_arn,
        "Heartbeat function": function_arn,
        "Heartbeat schedule": rule_arn,
    }.items():
        print(f"  {label:20s} {value}")

    print("\nRun one now (this is your demo of an unprompted agent run):")
    print(f"  aws lambda invoke --function-name {function_name} --payload '{{}}' \\")
    print("      --cli-binary-format raw-in-base64-out /dev/stdout")


if __name__ == "__main__":
    main()
