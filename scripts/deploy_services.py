"""Build, push and run the three Strobe A2 containers on ECS Fargate.

    MCP server   ->  port 8080   (tools; verifies the user's Cognito token)
    ACP agent    ->  port 3000   (Bedrock loop; MCP client; WebSocket for the browser)
    Web client   ->  port 80     (static ACP chat UI)

Those ports are dictated by CAB432SG, the only security group this account lets
us use: creating or modifying groups is denied, so the services fit the ports it
already opens to the world.

Deploy order matters: the MCP server first, because the agent needs its address,
then the agent, because the web client needs its WebSocket URL.

    python scripts/deploy_services.py --student-id n11457660 \
        --qut-username n11457660@qut.edu.au \
        --webui-dir ~/Documents/QUT/"ONE LAST TIME"/CAB/A2/acp_client

Re-running rebuilds and redeploys whatever you point it at; use --only to limit
it (--only mcp, --only agent, --only webui).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
ACCOUNT = "901444280953"
VPC = "vpc-007bab53289655834"
SUBNETS = ["subnet-075811427d5564cf9", "subnet-05a3b8177138c8b14", "subnet-04ca053dcbe5f49cc"]
SECURITY_GROUP = "sg-032bd1ff8cf77dbb9"          # CAB432SG
# ECS will only assume roles that trust ecs-tasks.amazonaws.com; the course
# provides these two. Execution = pulling images and writing logs;
# Task = what the container itself may call (Bedrock, DynamoDB, S3 Vectors).
EXECUTION_ROLE = f"arn:aws:iam::{ACCOUNT}:role/Execution-Role-CAB432-ECS"
TASK_ROLE = f"arn:aws:iam::{ACCOUNT}:role/Task-Role-CAB432-ECS"
ZONE_NAME = "cab432.com."

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))

SERVICES = {
    "mcp":   {"port": 8080, "context": os.path.join(ROOT, "mcp_server"), "cpu": "512",  "memory": "1024"},
    "agent": {"port": 3000, "context": os.path.join(ROOT, "agent"),      "cpu": "512",  "memory": "1024"},
    "webui": {"port": 80,   "context": None,                             "cpu": "256",  "memory": "512"},
}


def run(cmd: list[str], **kwargs) -> None:
    print("  $ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    subprocess.run(cmd, check=True, **kwargs)


def code(e: ClientError) -> str:
    return e.response["Error"]["Code"]


# --------------------------------------------------------------------------- ECR
def ensure_repo(ecr, name: str, tags: dict) -> str:
    try:
        uri = ecr.create_repository(
            repositoryName=name,
            tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )["repository"]["repositoryUri"]
        print(f"  created repo {name}")
    except ClientError as e:
        if code(e) != "RepositoryAlreadyExistsException":
            raise
        uri = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]["repositoryUri"]
    return uri


def docker_login(ecr) -> None:
    token = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)
    run(["docker", "login", "--username", user, "--password-stdin", token["proxyEndpoint"]],
        input=password.encode())


def build_and_push(context: str, image: str, build_args: dict | None = None) -> None:
    # Fargate is x86_64; an Apple Silicon build must be told so explicitly.
    cmd = ["docker", "build", "--platform", "linux/amd64", "-t", image]
    for key, value in (build_args or {}).items():
        cmd += ["--build-arg", f"{key}={value}"]
    cmd.append(context)
    run(cmd)
    run(["docker", "push", image])


# --------------------------------------------------------------------------- ECS
def ensure_cluster(ecs, name: str, tags: dict) -> None:
    clusters = ecs.describe_clusters(clusters=[name])["clusters"]
    if clusters and clusters[0]["status"] == "ACTIVE":
        print(f"  cluster {name} already exists")
        return
    ecs.create_cluster(clusterName=name, tags=[{"key": k, "value": v} for k, v in tags.items()])
    print(f"  created cluster {name}")


def register_task(ecs, family: str, image: str, port: int, env: dict, cpu: str, memory: str, tags: dict) -> str:
    arn = ecs.register_task_definition(
        family=family,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu=cpu, memory=memory,
        executionRoleArn=EXECUTION_ROLE,
        taskRoleArn=TASK_ROLE,
        runtimePlatform={"cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX"},
        containerDefinitions=[{
            "name": family,
            "image": image,
            "essential": True,
            "portMappings": [{"containerPort": port, "protocol": "tcp"}],
            "environment": [{"name": k, "value": v} for k, v in env.items()],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": f"/ecs/{family}",
                    "awslogs-region": REGION,
                    "awslogs-stream-prefix": "ecs",
                    # groups are pre-created by hand: the execution role may write to a
                    # log group but not create one
                },
            },
        }],
        tags=[{"key": k, "value": v} for k, v in tags.items()],
    )["taskDefinition"]["taskDefinitionArn"]
    print(f"  registered {arn.split('/')[-1]}")
    return arn


def ensure_service(ecs, cluster: str, name: str, task_arn: str, tags: dict) -> None:
    network = {"awsvpcConfiguration": {
        "subnets": SUBNETS,
        "securityGroups": [SECURITY_GROUP],
        "assignPublicIp": "ENABLED",
    }}
    existing = ecs.describe_services(cluster=cluster, services=[name])["services"]
    live = [s for s in existing if s["status"] != "INACTIVE"]
    if live:
        ecs.update_service(cluster=cluster, service=name, taskDefinition=task_arn,
                           desiredCount=1, forceNewDeployment=True)
        print(f"  updated service {name}")
    else:
        ecs.create_service(
            cluster=cluster, serviceName=name, taskDefinition=task_arn,
            desiredCount=1, launchType="FARGATE", networkConfiguration=network,
            tags=[{"key": k, "value": v} for k, v in tags.items()],
            enableECSManagedTags=True, propagateTags="SERVICE",
        )
        print(f"  created service {name}")


def public_ip(ecs, ec2, cluster: str, service: str, timeout: int = 300) -> str | None:
    """Wait for a running task and return its public IP."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tasks = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
        if tasks:
            described = ecs.describe_tasks(cluster=cluster, tasks=tasks)["tasks"][0]
            if described["lastStatus"] == "RUNNING":
                for attachment in described.get("attachments", []):
                    eni = next((d["value"] for d in attachment["details"]
                                if d["name"] == "networkInterfaceId"), None)
                    if eni:
                        interface = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])
                        association = interface["NetworkInterfaces"][0].get("Association", {})
                        ip = association.get("PublicIp")
                        if ip:
                            return ip
        print("    waiting for a running task ...")
        time.sleep(15)
    return None


def mcp_private_ip(ecs, cluster: str, service: str) -> str | None:
    """The private address of the running MCP task, for agent -> MCP traffic."""
    tasks = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
    if not tasks:
        return None
    described = ecs.describe_tasks(cluster=cluster, tasks=tasks)["tasks"][0]
    interfaces = described["containers"][0].get("networkInterfaces", [])
    return interfaces[0].get("privateIpv4Address") if interfaces else None


def upsert_dns(r53, host: str, ip: str) -> str | None:
    """Best effort: a stable name beats an IP that changes on every restart."""
    try:
        zones = [z for z in r53.list_hosted_zones()["HostedZones"] if z["Name"] == ZONE_NAME]
        if not zones:
            return None
        r53.change_resource_record_sets(
            HostedZoneId=zones[0]["Id"],
            ChangeBatch={"Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": host, "Type": "A", "TTL": 60,
                    "ResourceRecords": [{"Value": ip}],
                },
            }]},
        )
        print(f"  DNS {host} -> {ip}")
        return host
    except ClientError as e:
        print(f"  (no DNS record: {code(e)})")
        return None


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    ap.add_argument("--webui-dir", required=True, help="the unzipped CAB432 ACP client")
    ap.add_argument("--only", choices=["mcp", "agent", "webui"], help="redeploy just one service")
    args = ap.parse_args()

    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": "assessment 2"}
    SERVICES["webui"]["context"] = os.path.expanduser(args.webui_dir)
    if not os.path.isdir(SERVICES["webui"]["context"]):
        sys.exit(f"web client directory not found: {SERVICES['webui']['context']}")

    session = boto3.Session(region_name=REGION)
    ecr, ecs, ec2 = session.client("ecr"), session.client("ecs"), session.client("ec2")
    r53 = session.client("route53")
    cluster = f"{sid}-strobe-a2"

    print("Cluster:")
    ensure_cluster(ecs, cluster, tags)
    print("ECR login:")
    docker_login(ecr)

    endpoints: dict[str, str] = {}
    order = [args.only] if args.only else ["mcp", "agent", "webui"]

    for key in order:
        spec = SERVICES[key]
        family = f"{sid}-strobe-{key}"
        print(f"\n=== {key} ===")
        repo_uri = ensure_repo(ecr, family, tags)
        image = f"{repo_uri}:latest"

        env = {"AWS_REGION": REGION}
        build_args = {}
        if key == "mcp":
            env["MCP_SSM_PREFIX"] = f"/{sid}/strobe-a2/mcp"
            env["PORT"] = "8080"
        elif key == "agent":
            # Inside the VPC the agent must use the MCP task's PRIVATE address:
            # traffic to a task's public ip would have to hairpin out through the
            # internet gateway and back, which does not work for ENI public ips.
            mcp_host = mcp_private_ip(ecs, cluster, f"{sid}-strobe-mcp")
            if mcp_host:
                print(f"  MCP private address: {mcp_host}")
            else:
                mcp_host = input("  MCP private IP: ").strip()
            env["AGENT_MCP_URL"] = f"http://{mcp_host}:8080/mcp"
            env["AGENT_ACP_PORT"] = "3000"
            env["AGENT_SSM_PREFIX"] = f"/{sid}/strobe-a2/agent"
        else:
            agent_host = endpoints.get("agent") or input("  agent host: ").strip()
            build_args["ACP_ENDPOINT"] = f"ws://{agent_host}:3000/acp"
            build_args["STROBE_API"] = "https://zu3y6kfvnh.execute-api.ap-southeast-2.amazonaws.com"

        build_and_push(spec["context"], image, build_args)
        task_arn = register_task(ecs, family, image, spec["port"], env, spec["cpu"], spec["memory"], tags)
        ensure_service(ecs, cluster, family, task_arn, tags)

        ip = public_ip(ecs, ec2, cluster, family)
        if not ip:
            sys.exit(f"  {key} never reached RUNNING - check: aws logs tail /ecs/{family} --follow")
        host = upsert_dns(r53, f"{sid}-{key}.cab432.com.", ip) or ip
        endpoints[key] = host.rstrip(".") if host != ip else ip
        print(f"  {key} is up at {endpoints[key]}:{spec['port']}")

    print("\n=== Deployed ===")
    if "webui" in endpoints:
        print(f"  Open the chat:  http://{endpoints['webui']}")
    if "agent" in endpoints:
        print(f"  Agent:          ws://{endpoints['agent']}:3000/acp")
    if "mcp" in endpoints:
        print(f"  MCP:            http://{endpoints['mcp']}:8080/mcp")
    print("\nARNs for the YAML:")
    for key in endpoints:
        print(f"  arn:aws:ecs:{REGION}:{ACCOUNT}:service/{cluster}/{sid}-strobe-{key}")
    print(f"  arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{cluster}")
    print("\nLogs:  aws logs tail /ecs/<service-name> --follow")


if __name__ == "__main__":
    main()
