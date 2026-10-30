"""What can this account actually do with ECS, ECR and networking?

The student role has explicit denies scattered around (IAM, and SSM without
tags), so probe before writing a deploy script that assumes anything.

    python scripts/probe_ecs.py --student-id n11457660 --qut-username n11457660@qut.edu.au

Cleans up anything it creates.
"""
from __future__ import annotations

import argparse

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"


def why(e: ClientError) -> str:
    err = e.response["Error"]
    msg = err.get("Message", "")
    if "explicit deny" in msg:
        return f"DENY ({msg.split('policy: ')[-1]})"
    return f"{err['Code']}: {msg[:120]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    args = ap.parse_args()
    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": "assessment 2"}
    s = boto3.Session(region_name=REGION)

    print("=== Network ===")
    ec2 = s.client("ec2")
    vpc_id = None
    try:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if vpcs:
            vpc_id = vpcs[0]["VpcId"]
            print(f"  default VPC: {vpc_id}")
            subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
            public = [x for x in subnets if x.get("MapPublicIpOnLaunch")]
            print(f"  subnets: {len(subnets)} ({len(public)} auto-assign public IP)")
            for x in (public or subnets)[:4]:
                print(f"    {x['SubnetId']}  {x['AvailabilityZone']}")
        else:
            print("  no default VPC")
    except ClientError as e:
        print(f"  describe VPC/subnets: {why(e)}")

    print("\n=== Security group ===")
    if vpc_id:
        name = f"{sid}-probe-sg"
        try:
            sg = ec2.create_security_group(
                GroupName=name, Description="probe", VpcId=vpc_id,
                TagSpecifications=[{"ResourceType": "security-group",
                                    "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}],
            )["GroupId"]
            print(f"  CREATE OK {sg}")
            try:
                ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
                    "IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}])
                print("  ingress rule OK")
            except ClientError as e:
                print(f"  ingress rule: {why(e)}")
            ec2.delete_security_group(GroupId=sg)
            print("  cleaned up")
        except ClientError as e:
            print(f"  create: {why(e)}")
            try:
                existing = ec2.describe_security_groups(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["SecurityGroups"]
                mine = [g for g in existing if sid in g["GroupName"]]
                print(f"  existing groups you could reuse: {[g['GroupName'] for g in mine] or 'none'}")
            except ClientError as inner:
                print(f"  list groups: {why(inner)}")

    print("\n=== ECR ===")
    ecr = s.client("ecr")
    repo = f"{sid}-probe-repo"
    try:
        uri = ecr.create_repository(
            repositoryName=repo,
            tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )["repository"]["repositoryUri"]
        print(f"  CREATE OK {uri}")
        ecr.delete_repository(repositoryName=repo, force=True)
        print("  cleaned up")
    except ClientError as e:
        print(f"  create: {why(e)}")
    try:
        ecr.get_authorization_token()
        print("  docker login token OK")
    except ClientError as e:
        print(f"  auth token: {why(e)}")

    print("\n=== ECS ===")
    ecs = s.client("ecs")
    cluster = f"{sid}-probe-cluster"
    try:
        ecs.create_cluster(clusterName=cluster, tags=[{"key": k, "value": v} for k, v in tags.items()])
        print(f"  CREATE CLUSTER OK {cluster}")
        ecs.delete_cluster(cluster=cluster)
        print("  cleaned up")
    except ClientError as e:
        print(f"  create cluster: {why(e)}")

    print("\n=== Roles for task definitions ===")
    iam = s.client("iam")
    try:
        found = []
        for page in iam.get_paginator("list_roles").paginate():
            for role in page["Roles"]:
                n = role["RoleName"].lower()
                if any(k in n for k in ("ecs", "task", "cab432", "lab")):
                    found.append(role["Arn"])
        for arn in found[:12]:
            print(f"  {arn}")
        if not found:
            print("  none matched")
    except ClientError as e:
        print(f"  list roles: {why(e)}")
        print("  (use the Lambda role arn:aws:iam::901444280953:role/CAB432-Lambda-Role and see if ECS accepts it)")

    print("\n=== Route 53 ===")
    try:
        r53 = s.client("route53")
        zones = r53.list_hosted_zones()["HostedZones"]
        for z in zones[:5]:
            print(f"  {z['Name']}  {z['Id']}")
        if not zones:
            print("  no hosted zones visible")
    except ClientError as e:
        print(f"  list zones: {why(e)}")


if __name__ == "__main__":
    main()
