"""Deploy the A3 classification worker pool: ECS Fargate + queue-depth auto scaling.

Replaces the A2 classifier Lambda as the consumer of the classify queue, which
gives the assignment its two non-Lambda patterns:

  worker-pull        tasks long-poll SQS and take one message at a time
  metric-based scale CloudWatch alarms on the queue's depth drive an
                     Application Auto Scaling step policy, 0 .. --max-tasks

    queue visible >= 1          -> +1 task   (1-9 waiting)
                                   +2 tasks  (10-49 waiting)
                                   +3 tasks  (50+ waiting)     60 s cooldown
    visible + in-flight == 0    -> exactly 0 tasks, after 3 quiet minutes

Everything is idempotent; re-running rebuilds the image and rolls the service.

    python scripts/deploy_worker.py --student-id n11457660 --qut-username n11457660@qut.edu.au

    --skip-build     reuse the image already in ECR
    --no-spot        plain Fargate instead of mostly-Spot
    --rollback       re-enable the A2 Lambda consumer and park the workers at 0
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deploy_pipeline import ensure_params  # noqa: E402
from deploy_services import (  # noqa: E402
    ACCOUNT, EXECUTION_ROLE, REGION, ROOT, SECURITY_GROUP, SUBNETS, TASK_ROLE,
    docker_login, ensure_repo,
)

PURPOSE = "assessment 3"


def code(e: ClientError) -> str:
    return e.response["Error"]["Code"]


def why(e: ClientError) -> str:
    msg = e.response["Error"].get("Message", "")
    return "DENIED by course policy" if "explicit deny" in msg else f"{code(e)}: {msg[:160]}"


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# --------------------------------------------------------------------------- pieces
def update_event_rule(events, rule: str, bucket: str, queue_arn: str, tags: dict) -> None:
    """Same rule as A2, but our own thumbnails no longer create queue messages."""
    pattern = {
        "source": ["aws.s3"],
        "detail-type": ["Object Created"],
        "detail": {
            "bucket": {"name": [bucket]},
            "object": {"key": [{"anything-but": {"prefix": "thumbs/"}}]},
        },
    }
    events.put_rule(Name=rule, EventPattern=json.dumps(pattern), State="ENABLED",
                    Description="Queues every new Strobe upload (not thumbnails) for classification",
                    Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
    events.put_targets(Rule=rule, Targets=[{"Id": "classify-queue", "Arn": queue_arn}])
    print(f"  {rule}: uploads -> queue, thumbs/ excluded")


def ensure_log_group(logs, name: str, tags: dict) -> None:
    for attempt in ({"tags": tags}, {}):
        try:
            logs.create_log_group(logGroupName=name, **attempt)
            print(f"  created log group {name}")
            break
        except ClientError as e:
            if code(e) == "ResourceAlreadyExistsException":
                print(f"  log group {name} exists")
                break
            last = e
    else:
        print(f"  !! could not create {name} ({why(last)}).")
        print(f"     Create it by hand (console or `aws logs create-log-group --log-group-name {name}`),")
        print("     the same way the A2 groups were made, then re-run with --skip-build.")
        sys.exit(1)
    try:
        logs.put_retention_policy(logGroupName=name, retentionInDays=14)
    except ClientError:
        pass


def build_and_push(image: str) -> None:
    cmd = ["docker", "build", "--platform", "linux/amd64",
           "-f", os.path.join(ROOT, "worker", "Dockerfile"), "-t", image, ROOT]
    print("  $ docker build ... worker/Dockerfile")
    subprocess.run(cmd, check=True)
    print(f"  $ docker push {image}")
    subprocess.run(["docker", "push", image], check=True)


def register_task(ecs, family: str, image: str, ssm_prefix: str, tags: dict) -> str:
    arn = ecs.register_task_definition(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu="512", memory="1024",
        executionRoleArn=EXECUTION_ROLE,
        taskRoleArn=TASK_ROLE,
        runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
        containerDefinitions=[{
            "name": family,
            "image": image,
            "essential": True,
            # no portMappings: a worker takes no inbound traffic at all
            "environment": [{"name": "SSM_PREFIX", "value": ssm_prefix},
                            {"name": "AWS_REGION", "value": REGION}],
            "stopTimeout": 120,      # time to finish the in-flight image after SIGTERM
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": f"/ecs/{family}", "awslogs-region": REGION,
                "awslogs-stream-prefix": "ecs"}},
        }],
        tags=[{"key": k, "value": v} for k, v in tags.items()],
    )["taskDefinition"]["taskDefinitionArn"]
    print(f"  registered {arn.rsplit('/', 1)[-1]}")
    return arn


def enable_spot(ecs, cluster: str) -> bool:
    """Spot is ~70% cheaper and a queue worker is the ideal interruptible job."""
    try:
        current = ecs.describe_clusters(clusters=[cluster])["clusters"][0].get("capacityProviders", [])
        wanted = sorted(set(current) | {"FARGATE", "FARGATE_SPOT"})
        if sorted(current) != wanted:
            ecs.put_cluster_capacity_providers(
                cluster=cluster, capacityProviders=wanted,
                defaultCapacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}])
        print("  cluster offers FARGATE and FARGATE_SPOT")
        return True
    except ClientError as e:
        print(f"  Spot unavailable ({why(e)}); using plain Fargate")
        return False


def enable_container_insights(ecs, cluster: str) -> None:
    """Gives ECS/ContainerInsights RunningTaskCount: the scaling graph for the video."""
    try:
        ecs.update_cluster_settings(cluster=cluster,
                                    settings=[{"name": "containerInsights", "value": "enabled"}])
        print("  Container Insights on (RunningTaskCount / DesiredTaskCount metrics)")
    except ClientError as e:
        print(f"  Container Insights not enabled ({why(e)}); use the ECS console's task count instead")


def ensure_service(ecs, cluster: str, name: str, task_arn: str, spot: bool, tags: dict) -> str:
    live = [s for s in ecs.describe_services(cluster=cluster, services=[name])["services"]
            if s["status"] != "INACTIVE"]
    if live:
        # desiredCount is deliberately not touched: auto scaling owns it now
        ecs.update_service(cluster=cluster, service=name, taskDefinition=task_arn, forceNewDeployment=True)
        print(f"  updated service {name}")
        return live[0]["serviceArn"]
    placement = ({"capacityProviderStrategy": [
                    {"capacityProvider": "FARGATE_SPOT", "weight": 4},
                    {"capacityProvider": "FARGATE", "weight": 1}]}
                 if spot else {"launchType": "FARGATE"})
    arn = ecs.create_service(
        cluster=cluster, serviceName=name, taskDefinition=task_arn,
        desiredCount=0,                              # scale from zero; the queue decides
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": SUBNETS, "securityGroups": [SECURITY_GROUP],
            "assignPublicIp": "ENABLED"}},           # no NAT gateway: reach AWS APIs directly
        deploymentConfiguration={"minimumHealthyPercent": 0, "maximumPercent": 200},
        tags=[{"key": k, "value": v} for k, v in tags.items()],
        enableECSManagedTags=True, propagateTags="SERVICE",
        **placement,
    )["service"]["serviceArn"]
    print(f"  created service {name} at 0 tasks")
    return arn


def ensure_scaling(aas, cw, cluster: str, service: str, queue_name: str, max_tasks: int,
                   sid: str, tags: dict) -> dict:
    resource = f"service/{cluster}/{service}"
    dim = "ecs:service:DesiredCount"
    try:
        aas.register_scalable_target(ServiceNamespace="ecs", ResourceId=resource, ScalableDimension=dim,
                                     MinCapacity=0, MaxCapacity=max_tasks, Tags=tags)
    except ClientError as e:
        if code(e) not in ("ValidationException",):
            raise
        # Tags are only accepted when the target is first created
        aas.register_scalable_target(ServiceNamespace="ecs", ResourceId=resource, ScalableDimension=dim,
                                     MinCapacity=0, MaxCapacity=max_tasks)
    print(f"  scalable target {resource}: 0..{max_tasks} tasks")

    out_arn = aas.put_scaling_policy(
        PolicyName=f"{sid}-strobe-worker-scale-out", ServiceNamespace="ecs",
        ResourceId=resource, ScalableDimension=dim, PolicyType="StepScaling",
        StepScalingPolicyConfiguration={
            "AdjustmentType": "ChangeInCapacity",
            "Cooldown": 60,
            "MetricAggregationType": "Maximum",
            # bounds are relative to the alarm threshold (1 visible message)
            "StepAdjustments": [
                {"MetricIntervalLowerBound": 0, "MetricIntervalUpperBound": 9, "ScalingAdjustment": 1},
                {"MetricIntervalLowerBound": 9, "MetricIntervalUpperBound": 49, "ScalingAdjustment": 2},
                {"MetricIntervalLowerBound": 49, "ScalingAdjustment": 3},
            ],
        },
    )["PolicyARN"]
    in_arn = aas.put_scaling_policy(
        PolicyName=f"{sid}-strobe-worker-scale-in", ServiceNamespace="ecs",
        ResourceId=resource, ScalableDimension=dim, PolicyType="StepScaling",
        StepScalingPolicyConfiguration={
            "AdjustmentType": "ExactCapacity",
            "Cooldown": 60,
            "MetricAggregationType": "Maximum",
            "StepAdjustments": [{"MetricIntervalUpperBound": 0, "ScalingAdjustment": 0}],
        },
    )["PolicyARN"]
    print("  step policies: scale-out (+1/+2/+3), scale-in (to 0)")

    tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
    queue_dim = [{"Name": "QueueName", "Value": queue_name}]
    out_alarm = f"{sid}-strobe-worker-backlog"
    cw.put_metric_alarm(
        AlarmName=out_alarm,
        AlarmDescription="Images are waiting: add classification workers",
        Namespace="AWS/SQS", MetricName="ApproximateNumberOfMessagesVisible", Dimensions=queue_dim,
        Statistic="Maximum", Period=60, EvaluationPeriods=1, DatapointsToAlarm=1,
        Threshold=1, ComparisonOperator="GreaterThanOrEqualToThreshold",
        TreatMissingData="notBreaching", AlarmActions=[out_arn], Tags=tag_list,
    )
    in_alarm = f"{sid}-strobe-worker-idle"
    cw.put_metric_alarm(
        AlarmName=in_alarm,
        AlarmDescription="Nothing waiting and nothing in flight: scale workers to zero",
        Metrics=[
            {"Id": "visible", "ReturnData": False, "MetricStat": {"Metric": {
                "Namespace": "AWS/SQS", "MetricName": "ApproximateNumberOfMessagesVisible",
                "Dimensions": queue_dim}, "Period": 60, "Stat": "Maximum"}},
            {"Id": "inflight", "ReturnData": False, "MetricStat": {"Metric": {
                "Namespace": "AWS/SQS", "MetricName": "ApproximateNumberOfMessagesNotVisible",
                "Dimensions": queue_dim}, "Period": 60, "Stat": "Maximum"}},
            {"Id": "total", "Expression": "visible + inflight", "Label": "backlog incl. in-flight",
             "ReturnData": True},
        ],
        EvaluationPeriods=3, DatapointsToAlarm=3,
        Threshold=0, ComparisonOperator="LessThanOrEqualToThreshold",
        TreatMissingData="breaching",          # an idle queue may stop reporting: that means empty
        AlarmActions=[in_arn], Tags=tag_list,
    )
    print(f"  alarms {out_alarm} / {in_alarm}")
    return {"scale_out_policy": out_arn, "scale_in_policy": in_arn,
            "alarms": [f"arn:aws:cloudwatch:{REGION}:{ACCOUNT}:alarm:{n}" for n in (out_alarm, in_alarm)]}


def set_lambda_consumer(lam, function: str, queue_arn: str, enabled: bool) -> None:
    mappings = lam.list_event_source_mappings(EventSourceArn=queue_arn, FunctionName=function)
    for m in mappings["EventSourceMappings"]:
        lam.update_event_source_mapping(UUID=m["UUID"], Enabled=enabled)
    state = "re-enabled" if enabled else "disabled"
    print(f"  A2 Lambda consumer {state} ({len(mappings['EventSourceMappings'])} mapping)")


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--media-bucket", default=None)
    ap.add_argument("--cluster", default=None, help="defaults to <sid>-strobe-a2 (reused, per the FAQ)")
    ap.add_argument("--max-tasks", type=int, default=5)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--no-spot", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": PURPOSE}
    bucket = args.media_bucket or f"{sid}-s3-media"
    cluster = args.cluster or f"{sid}-strobe-a2"
    family = f"{sid}-strobe-worker"
    queue_name = f"{sid}-strobe-classify"
    ssm_prefix = f"/{sid}/strobe-a2/classifier"     # the A2 classifier config, reused as-is

    s = boto3.Session(region_name=REGION)
    sqs, ecs, ecr = s.client("sqs"), s.client("ecs"), s.client("ecr")
    aas, cw, lam = s.client("application-autoscaling"), s.client("cloudwatch"), s.client("lambda")
    events, logs, ssm = s.client("events"), s.client("logs"), s.client("ssm")

    queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]

    if args.rollback:
        step("Rollback to the A2 Lambda consumer")
        set_lambda_consumer(lam, f"{sid}-strobe-classifier", queue_arn, True)
        aas.register_scalable_target(ServiceNamespace="ecs", ResourceId=f"service/{cluster}/{family}",
                                     ScalableDimension="ecs:service:DesiredCount", MinCapacity=0, MaxCapacity=0)
        ecs.update_service(cluster=cluster, service=family, desiredCount=0)
        print("  worker service parked at 0 tasks")
        return

    step("Configuration")
    ensure_params(ssm, ssm_prefix, {"queue-url": queue_url}, tags)
    update_event_rule(events, f"{sid}-strobe-upload-to-classify", bucket, queue_arn, tags)

    step("Image")
    ensure_log_group(logs, f"/ecs/{family}", tags)
    repo = ensure_repo(ecr, family, tags)
    image = f"{repo}:latest"
    if not args.skip_build:
        docker_login(ecr)
        build_and_push(image)

    step("ECS")
    spot = (not args.no_spot) and enable_spot(ecs, cluster)
    enable_container_insights(ecs, cluster)
    task_arn = register_task(ecs, family, image, ssm_prefix, tags)
    service_arn = ensure_service(ecs, cluster, family, task_arn, spot, tags)

    step("Auto scaling")
    try:
        scaling = ensure_scaling(aas, cw, cluster, family, queue_name, args.max_tasks, sid, tags)
    except ClientError as e:
        print(f"  !! auto scaling failed: {why(e)}")
        print("     Run scripts/probe_a3.py and send me the output.")
        sys.exit(1)

    step("Hand over from the Lambda")
    set_lambda_consumer(lam, f"{sid}-strobe-classifier", queue_arn, False)

    step("Done")
    print("  Test it:   python scripts/load_test.py --count 5")
    print(f"  Logs:      aws logs tail /ecs/{family} --follow --region {REGION}")
    print("  Expect ~1-2 min for the first task (alarm period + Fargate start), and ~3-4 min")
    print("  after the queue empties for the service to go back to 0.")
    print("\nFor the submission YAML:")
    print(f"  ECS service        {service_arn}")
    print(f"  Task definition    {task_arn}")
    print(f"  ECR repository     arn:aws:ecr:{REGION}:{ACCOUNT}:repository/{family}")
    print(f"  Scaling target     service/{cluster}/{family}")
    print(f"  Scale-out policy   {scaling['scale_out_policy']}")
    print(f"  Scale-in policy    {scaling['scale_in_policy']}")
    for a in scaling["alarms"]:
        print(f"  CloudWatch alarm   {a}")


if __name__ == "__main__":
    main()
