"""Can this account do what A3 needs? Run once before deploying anything.

    python scripts/probe_a3.py --student-id n11457660 --qut-username n11457660@qut.edu.au

Every check is read-only, or creates a throwaway thing and deletes it straight
away. Paste the whole output back if anything says FAIL.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import uuid

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
ACCOUNT = "901444280953"
TASK_ROLE = "Task-Role-CAB432-ECS"
NEEDED = {   # what the worker's task role must allow
    "sqs:ReceiveMessage": "pull work", "sqs:DeleteMessage": "ack work",
    "sqs:ChangeMessageVisibility": "retry back-off", "s3:GetObject": "read uploads",
    "s3:PutObject": "write thumbnails", "bedrock:InvokeModel": "classify + embed",
    "s3vectors:PutVectors": "store vectors", "dynamodb:PutItem": "store classification",
    "ssm:GetParametersByPath": "read config",
}
results: list[tuple[str, str, str]] = []


def why(e: ClientError) -> str:
    msg = e.response["Error"].get("Message", "")
    return "explicit deny" if "explicit deny" in msg else f"{e.response['Error']['Code']}: {msg[:120]}"


def check(name: str, fn) -> None:
    try:
        detail = fn() or ""
        results.append(("OK", name, detail))
    except ClientError as e:
        results.append(("FAIL", name, why(e)))
    except Exception as e:  # noqa: BLE001
        results.append(("FAIL", name, f"{type(e).__name__}: {e}"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    args = ap.parse_args()
    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": "assessment 3"}
    s = boto3.Session(region_name=REGION)
    cluster = f"{sid}-strobe-a2"

    check("whoami", lambda: s.client("sts").get_caller_identity()["Arn"].rsplit("/", 1)[-1])

    ecs = s.client("ecs")
    check("ECS cluster", lambda: "{status}, providers={capacityProviders}".format(
        **ecs.describe_clusters(clusters=[cluster])["clusters"][0]))

    iam = s.client("iam")
    check("auto-scaling service-linked role", lambda: iam.get_role(
        RoleName="AWSServiceRoleForApplicationAutoScaling_ECSService")["Role"]["Arn"].rsplit("/", 1)[-1])

    aas = s.client("application-autoscaling")

    def try_scalable_target():
        rid = f"service/{cluster}/{sid}-strobe-mcp"        # harmless: pinned at its current 1 task
        aas.register_scalable_target(ServiceNamespace="ecs", ResourceId=rid,
                                     ScalableDimension="ecs:service:DesiredCount", MinCapacity=1, MaxCapacity=1)
        aas.deregister_scalable_target(ServiceNamespace="ecs", ResourceId=rid,
                                       ScalableDimension="ecs:service:DesiredCount")
        return "register + deregister worked"
    check("Application Auto Scaling on ECS", try_scalable_target)

    cw = s.client("cloudwatch")

    def try_alarm():
        name = f"{sid}-probe-{uuid.uuid4().hex[:6]}"
        cw.put_metric_alarm(AlarmName=name, Namespace="AWS/SQS", MetricName="ApproximateNumberOfMessagesVisible",
                            Dimensions=[{"Name": "QueueName", "Value": f"{sid}-strobe-classify"}],
                            Statistic="Maximum", Period=60, EvaluationPeriods=1, Threshold=1,
                            ComparisonOperator="GreaterThanOrEqualToThreshold",
                            Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
        cw.delete_alarms(AlarmNames=[name])
        return "create + delete worked"
    check("CloudWatch alarm", try_alarm)

    def try_dashboard():
        name = f"{sid}-probe-{uuid.uuid4().hex[:6]}"
        cw.put_dashboard(DashboardName=name, DashboardBody=json.dumps({"widgets": []}))
        cw.delete_dashboards(DashboardNames=[name])
        return "create + delete worked"
    check("CloudWatch dashboard", try_dashboard)

    cf = s.client("cloudfront")
    check("CloudFront list", lambda: f"{cf.list_distributions()['DistributionList'].get('Quantity', 0)} visible")

    def try_oac():
        oac = cf.create_origin_access_control(OriginAccessControlConfig={
            "Name": f"{sid}-probe-{uuid.uuid4().hex[:6]}", "SigningProtocol": "sigv4",
            "SigningBehavior": "always", "OriginAccessControlOriginType": "s3"})
        cf.delete_origin_access_control(Id=oac["OriginAccessControl"]["Id"], IfMatch=oac["ETag"])
        return "create + delete worked"
    check("CloudFront origin access control", try_oac)

    s3 = s.client("s3")

    def bucket_policy():
        try:
            policy = json.loads(s3.get_bucket_policy(Bucket=f"{sid}-s3-media")["Policy"])
            return f"{len(policy['Statement'])} statement(s) already"
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchBucketPolicy":
                return "no policy yet"
            raise
    check("media bucket policy (read)", bucket_policy)

    check("ECR", lambda: f"{len(s.client('ecr').describe_repositories()['repositories'])} repos visible")

    # --- the task role the worker will run as
    def role_policies():
        names = [p["PolicyName"] for p in iam.list_attached_role_policies(RoleName=TASK_ROLE)["AttachedPolicies"]]
        names += iam.list_role_policies(RoleName=TASK_ROLE)["PolicyNames"]
        return ", ".join(names) or "(none)"
    check(f"{TASK_ROLE} policies", role_policies)

    def simulate():
        arn = f"arn:aws:iam::{ACCOUNT}:role/{TASK_ROLE}"
        res = iam.simulate_principal_policy(PolicySourceArn=arn, ActionNames=list(NEEDED))
        denied = [r["EvalActionName"] for r in res["EvaluationResults"] if r["EvalDecision"] != "allowed"]
        return "all allowed" if not denied else "NOT allowed: " + ", ".join(denied)
    check(f"{TASK_ROLE} can run the worker", simulate)

    # --- local tooling
    def docker():
        if not shutil.which("docker"):
            raise RuntimeError("docker not on PATH")
        subprocess.run(["docker", "info"], check=True, capture_output=True)
        return "running"
    check("docker (local)", docker)

    width = max(len(n) for _, n, _ in results)
    print()
    for status, name, detail in results:
        print(f"  {status:4}  {name:{width}}  {detail}")
    fails = [n for st, n, _ in results if st == "FAIL"]
    print(f"\n{len(results) - len(fails)} OK, {len(fails)} FAIL")
    if any("can run the worker" in n or "policies" in n for n in fails):
        print("Role checks failing is common (students often cannot read IAM); the worker's logs")
        print("will show any real AccessDenied once it runs.")


if __name__ == "__main__":
    main()
