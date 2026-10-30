"""Tag the Assessment 1 resources that Assessment 2 reuses.

The FAQ says repurposed resources must appear in the A2 declaration; tagging
them `purpose: assessment 2` keeps them out of any A1 cleanup and makes the
declaration verifiable.

Some tagging calls are blocked by the course deny policy (CloudWatch log groups,
for instance). Those are reported rather than treated as failures.

    python scripts/retag_a1.py --student-id n11457660 --qut-username n11457660@qut.edu.au
    --check   report current tags, change nothing
"""
from __future__ import annotations

import argparse

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
ACCOUNT = "901444280953"
COGNITO_POOL = "ap-southeast-2_gyxH5uIZM"


def why(e: ClientError) -> str:
    err = e.response["Error"]
    message = err.get("Message", "")
    if "explicit deny" in message:
        return "DENIED by course policy"
    return err["Code"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": "assessment 2"}
    pairs = [{"Key": k, "Value": v} for k, v in tags.items()]
    session = boto3.Session(region_name=REGION)
    done, blocked = [], []

    def record(label: str, ok: bool, detail: str = "") -> None:
        (done if ok else blocked).append(label)
        print(f"  {'OK  ' if ok else 'skip'} {label}{('  ' + detail) if detail else ''}")

    print("DynamoDB tables:")
    ddb = session.client("dynamodb")
    for page in ddb.get_paginator("list_tables").paginate():
        for name in page["TableNames"]:
            if not name.startswith(f"{sid}-ddb-"):
                continue
            arn = f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{name}"
            if args.check:
                current = ddb.list_tags_of_resource(ResourceArn=arn)["Tags"]
                record(name, any(t["Key"] == "purpose" and t["Value"] == "assessment 2" for t in current),
                       str({t["Key"]: t["Value"] for t in current}))
                continue
            try:
                ddb.tag_resource(ResourceArn=arn, Tags=pairs)
                record(name, True)
            except ClientError as e:
                record(name, False, why(e))

    print("\nS3 bucket:")
    s3 = session.client("s3")
    bucket = f"{sid}-s3-media"
    try:
        if args.check:
            try:
                current = s3.get_bucket_tagging(Bucket=bucket)["TagSet"]
            except ClientError:
                current = []
            record(bucket, any(t["Key"] == "purpose" and t["Value"] == "assessment 2" for t in current),
                   str({t["Key"]: t["Value"] for t in current}))
        else:
            # PutBucketTagging replaces the whole set, so keep what is there.
            try:
                existing = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=bucket)["TagSet"]}
            except ClientError:
                existing = {}
            existing.update(tags)
            s3.put_bucket_tagging(Bucket=bucket,
                                  Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in existing.items()]})
            record(bucket, True)
    except ClientError as e:
        record(bucket, False, why(e))

    print("\nLambda functions:")
    lam = session.client("lambda")
    for page in lam.get_paginator("list_functions").paginate():
        for fn in page["Functions"]:
            name = fn["FunctionName"]
            if not name.startswith(f"{sid}-lambda-"):
                continue
            if args.check:
                current = lam.list_tags(Resource=fn["FunctionArn"])["Tags"]
                record(name, current.get("purpose") == "assessment 2", str(current))
                continue
            try:
                lam.tag_resource(Resource=fn["FunctionArn"], Tags=tags)
                record(name, True)
            except ClientError as e:
                record(name, False, why(e))

    print("\nCognito user pool:")
    cognito = session.client("cognito-idp")
    pool_arn = f"arn:aws:cognito-idp:{REGION}:{ACCOUNT}:userpool/{COGNITO_POOL}"
    try:
        if args.check:
            current = cognito.list_tags_for_resource(ResourceArn=pool_arn)["Tags"]
            record(COGNITO_POOL, current.get("purpose") == "assessment 2", str(current))
        else:
            cognito.tag_resource(ResourceArn=pool_arn, Tags=tags)
            record(COGNITO_POOL, True)
    except ClientError as e:
        record(COGNITO_POOL, False, why(e))

    print("\nAPI Gateway:")
    apigw = session.client("apigatewayv2")
    apis, token = [], None
    while True:
        page = apigw.get_apis(MaxResults="500", **({"NextToken": token} if token else {}))
        apis += page["Items"]
        token = page.get("NextToken")
        if not token:
            break
    for api in apis:
        if sid not in api["Name"]:
            continue
        arn = f"arn:aws:apigateway:{REGION}::/apis/{api['ApiId']}"
        if args.check:
            record(api["Name"], api.get("Tags", {}).get("purpose") == "assessment 2", str(api.get("Tags", {})))
            continue
        try:
            apigw.tag_resource(ResourceArn=arn, Tags=tags)
            record(api["Name"], True)
        except ClientError as e:
            record(api["Name"], False, why(e))

    print(f"\n{len(done)} tagged, {len(blocked)} not.")
    if blocked and not args.check:
        print("Blocked or already-correct items:", ", ".join(blocked))
        print("If a tag is denied by policy, say so in the presentation: it is an account")
        print("constraint, not an oversight.")


if __name__ == "__main__":
    main()
