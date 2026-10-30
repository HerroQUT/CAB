"""Round two: find the VPC, and find out whether ECS will accept a role.

    python scripts/probe_ecs2.py --student-id n11457660 --qut-username n11457660@qut.edu.au

Registering a task definition with an executionRoleArn needs iam:PassRole,
which is the likeliest remaining blocker. This tries each candidate role and
deregisters whatever it manages to create.
"""
from __future__ import annotations

import argparse

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
CANDIDATE_ROLES = [
    "arn:aws:iam::901444280953:role/CAB432-Instance-Role",
    "arn:aws:iam::901444280953:role/CAB432-Lambda-Role",
]


def why(e: ClientError) -> str:
    err = e.response["Error"]
    msg = err.get("Message", "")
    if "explicit deny" in msg:
        return f"DENY ({msg.split('policy: ')[-1]})"
    return f"{err['Code']}: {msg[:160]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", required=True)
    args = ap.parse_args()
    sid = args.student_id
    tags = {"qut-username": args.qut_username, "purpose": "assessment 2"}
    s = boto3.Session(region_name=REGION)
    ec2, ecs = s.client("ec2"), s.client("ecs")

    print("=== VPCs ===")
    chosen_vpc, public_subnets = None, []
    try:
        for vpc in ec2.describe_vpcs()["Vpcs"]:
            name = next((t["Value"] for t in vpc.get("Tags", []) if t["Key"] == "Name"), "")
            print(f"  {vpc['VpcId']}  {vpc['CidrBlock']}  {name}")
            subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc["VpcId"]]}])["Subnets"]
            public = [x for x in subnets if x.get("MapPublicIpOnLaunch")]
            for x in subnets:
                subnet_name = next((t["Value"] for t in x.get("Tags", []) if t["Key"] == "Name"), "")
                flag = "public " if x.get("MapPublicIpOnLaunch") else "private"
                print(f"      {x['SubnetId']}  {x['AvailabilityZone']}  {flag}  {subnet_name}")
            if public and not chosen_vpc:
                chosen_vpc = vpc["VpcId"]
                public_subnets = [x["SubnetId"] for x in public]
    except ClientError as e:
        print(f"  {why(e)}")

    if chosen_vpc:
        print(f"\n  -> use VPC {chosen_vpc} with subnets {', '.join(public_subnets[:3])}")
    else:
        print("\n  -> no public subnets found; services would need a NAT or public IP another way")

    print("\n=== Security groups ===")
    if chosen_vpc:
        name = f"{sid}-a2-sg"
        try:
            sg = ec2.create_security_group(
                GroupName=name, Description="Strobe A2 services", VpcId=chosen_vpc,
                TagSpecifications=[{"ResourceType": "security-group",
                                    "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}],
            )["GroupId"]
            print(f"  created {sg} (kept: the deploy script will reuse it)")
            for port in (80, 7331, 8000):
                try:
                    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[{
                        "IpProtocol": "tcp", "FromPort": port, "ToPort": port,
                        "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Strobe A2"}]}])
                    print(f"    opened {port}")
                except ClientError as e:
                    print(f"    {port}: {why(e)}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidGroup.Duplicate":
                print(f"  {name} already exists - fine")
            else:
                print(f"  create: {why(e)}")
                groups = ec2.describe_security_groups(
                    Filters=[{"Name": "vpc-id", "Values": [chosen_vpc]}])["SecurityGroups"]
                print(f"  existing: {[g['GroupName'] for g in groups][:10]}")

    print("\n=== iam:PassRole via task definition ===")
    working = []
    for role in CANDIDATE_ROLES:
        family = f"{sid}-probe-td"
        try:
            td = ecs.register_task_definition(
                family=family,
                requiresCompatibilities=["FARGATE"],
                networkMode="awsvpc",
                cpu="256", memory="512",
                executionRoleArn=role,
                taskRoleArn=role,
                containerDefinitions=[{
                    "name": "probe",
                    "image": "public.ecr.aws/docker/library/busybox:latest",
                    "essential": True,
                }],
                tags=[{"key": k, "value": v} for k, v in tags.items()],
            )["taskDefinition"]["taskDefinitionArn"]
            print(f"  OK   {role.split('/')[-1]}")
            working.append(role)
            ecs.deregister_task_definition(taskDefinition=td)
        except ClientError as e:
            print(f"  FAIL {role.split('/')[-1]}: {why(e)}")

    print("\n=== Result ===")
    if working:
        print(f"Use this role for ECS tasks: {working[0]}")
    else:
        print("No role can be passed to ECS. Ask the teaching team which role to use for task")
        print("definitions, or whether one needs creating.")


if __name__ == "__main__":
    main()
