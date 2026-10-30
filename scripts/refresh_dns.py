"""Point the DNS records at whatever IPs the tasks have right now.

Fargate gives each task a fresh public and private address, so any restart or
redeploy breaks three things: the two public A records, and the agent's
AGENT_MCP_URL, which holds the MCP task's private address.

Run this after any restart, and before the demo:

    python scripts/refresh_dns.py --student-id n11457660

    --check     report only, change nothing
    --no-agent  skip the agent redeploy even if the MCP address moved
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-2"
ZONE_NAME = "cab432.com."
SERVICES = {"mcp": 8080, "agent": 3000, "webui": 80}


def task_addresses(ecs, ec2, cluster: str, service: str) -> tuple[str | None, str | None]:
    """(public, private) for the running task, or (None, None)."""
    tasks = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
    if not tasks:
        return None, None
    described = ecs.describe_tasks(cluster=cluster, tasks=tasks)["tasks"][0]
    if described["lastStatus"] != "RUNNING":
        return None, None

    interfaces = described["containers"][0].get("networkInterfaces", [])
    private = interfaces[0].get("privateIpv4Address") if interfaces else None

    eni = None
    for attachment in described.get("attachments", []):
        eni = next((d["value"] for d in attachment["details"] if d["name"] == "networkInterfaceId"), eni)
    public = None
    if eni:
        details = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni])["NetworkInterfaces"][0]
        public = details.get("Association", {}).get("PublicIp")
    return public, private


def current_record(r53, zone_id: str, name: str) -> str | None:
    try:
        sets = r53.list_resource_record_sets(
            HostedZoneId=zone_id, StartRecordName=name, StartRecordType="A", MaxItems="1",
        )["ResourceRecordSets"]
        for record in sets:
            if record["Name"] == name and record["Type"] == "A":
                return record["ResourceRecords"][0]["Value"]
    except ClientError:
        pass
    return None


def upsert(r53, zone_id: str, name: str, ip: str) -> None:
    r53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={"Changes": [{
            "Action": "UPSERT",
            "ResourceRecordSet": {"Name": name, "Type": "A", "TTL": 60,
                                  "ResourceRecords": [{"Value": ip}]},
        }]},
    )


def agent_mcp_url(ecs, family: str) -> str | None:
    task = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]
    for pair in task["containerDefinitions"][0].get("environment", []):
        if pair["name"] == "AGENT_MCP_URL":
            return pair["value"]
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    ap.add_argument("--qut-username", default="n11457660@qut.edu.au")
    ap.add_argument("--webui-dir", default="~/Documents/QUT/ONE LAST TIME/CAB/A2/acp_client")
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--no-agent", action="store_true", help="never redeploy the agent")
    args = ap.parse_args()

    sid = args.student_id
    cluster = f"{sid}-strobe-a2"
    session = boto3.Session(region_name=REGION)
    ecs, ec2, r53 = session.client("ecs"), session.client("ec2"), session.client("route53")

    zones = [z for z in r53.list_hosted_zones()["HostedZones"] if z["Name"] == ZONE_NAME]
    if not zones:
        sys.exit("cab432.com hosted zone not visible")
    zone_id = zones[0]["Id"]

    addresses: dict[str, tuple[str | None, str | None]] = {}
    changed = []
    print(f"{'service':8} {'public':16} {'dns now':16} {'private':16}")
    for key, port in SERVICES.items():
        public, private = task_addresses(ecs, ec2, cluster, f"{sid}-strobe-{key}")
        addresses[key] = (public, private)
        host = f"{sid}-{key}.cab432.com."
        existing = current_record(r53, zone_id, host)
        print(f"{key:8} {public or '(not running)':16} {existing or '(none)':16} {private or '-':16}")
        if public and existing != public:
            changed.append((key, host, public))

    if not changed:
        print("\nDNS already matches the running tasks.")
    elif args.check:
        print("\nWould update:", ", ".join(k for k, _, _ in changed))
    else:
        print()
        for key, host, ip in changed:
            upsert(r53, zone_id, host, ip)
            print(f"  {host} -> {ip}")
        print("  (records have a 60s TTL, so allow a minute)")

    # The agent holds the MCP task's private address; if that moved, the agent
    # is pointing at a machine that no longer exists.
    mcp_private = addresses["mcp"][1]
    if mcp_private:
        expected = f"http://{mcp_private}:8080/mcp"
        actual = agent_mcp_url(ecs, f"{sid}-strobe-agent")
        if actual != expected:
            print(f"\nAgent points at {actual}, but the MCP task is now at {mcp_private}.")
            if args.check or args.no_agent:
                print("Redeploy the agent to fix it:")
                print(f"  python scripts/deploy_services.py --student-id {sid} "
                      f"--qut-username {args.qut_username} --webui-dir '{args.webui_dir}' --only agent")
            else:
                print("Redeploying the agent ...\n")
                subprocess.run([
                    sys.executable, "scripts/deploy_services.py",
                    "--student-id", sid, "--qut-username", args.qut_username,
                    "--webui-dir", args.webui_dir, "--only", "agent",
                ], check=False)
                time.sleep(20)
                public, _ = task_addresses(ecs, ec2, cluster, f"{sid}-strobe-agent")
                if public:
                    upsert(r53, zone_id, f"{sid}-agent.cab432.com.", public)
                    print(f"\n  {sid}-agent.cab432.com -> {public} (after redeploy)")
        else:
            print("\nAgent is pointed at the right MCP address.")

    print(f"\nChat:  http://{sid}-webui.cab432.com")
    print(f"Agent: ws://{sid}-agent.cab432.com:3000/acp")
    print(f"MCP:   http://{sid}-mcp.cab432.com:8080/mcp")


if __name__ == "__main__":
    main()
