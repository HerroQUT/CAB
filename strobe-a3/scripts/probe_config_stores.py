"""Find out what this account lets us do with Parameter Store / Secrets Manager.

The student role has an explicit deny on ssm:PutParameter for /n11457660/...
This probes alternative paths and Secrets Manager so we know where the
classifier's config can actually live.

    python scripts/probe_config_stores.py --student-id n11457660
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
    return err["Code"]


def probe_write(ssm, name: str) -> bool:
    try:
        ssm.put_parameter(Name=name, Value="probe", Type="String", Overwrite=True)
        print(f"  WRITE OK   {name}")
        try:
            ssm.delete_parameter(Name=name)
        except ClientError:
            print(f"             (could not clean up {name} - delete it by hand)")
        return True
    except ClientError as e:
        print(f"  denied     {name}   {why(e)}")
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-id", required=True)
    args = ap.parse_args()
    sid = args.student_id
    session = boto3.Session(region_name=REGION)
    ssm = session.client("ssm")

    print("Existing parameters you can see:")
    try:
        found = 0
        for page in ssm.get_paginator("describe_parameters").paginate():
            for p in page["Parameters"]:
                print(f"  {p['Name']}")
                found += 1
        if not found:
            print("  (none)")
    except ClientError as e:
        print(f"  cannot list: {why(e)}")

    print("\nWrite attempts (Parameter Store):")
    candidates = [
        f"/{sid}/strobe-a2/classifier/probe",
        f"/{sid}/probe",
        f"/{sid}-probe",
        f"{sid}-probe",
        f"/cab432/{sid}/probe",
        f"/a2/{sid}/probe",
        f"/student/{sid}/probe",
    ]
    allowed = [name for name in candidates if probe_write(ssm, name)]

    print("\nSecrets Manager:")
    sm = session.client("secretsmanager")
    secret_name = f"{sid}-probe-secret"
    try:
        sm.create_secret(Name=secret_name, SecretString="probe")
        print(f"  WRITE OK   {secret_name}")
        try:
            sm.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
        except ClientError:
            print(f"             (could not clean up {secret_name})")
    except ClientError as e:
        print(f"  denied     {secret_name}   {why(e)}")

    print("\n=== Result ===")
    if allowed:
        print("Usable Parameter Store prefix(es):")
        for name in allowed:
            print(f"  {name.rsplit('/', 1)[0] or '(no leading slash)'}")
    else:
        print("No writable Parameter Store path found - config will go in Lambda")
        print("environment variables instead (model IDs and resource names, no secrets).")


if __name__ == "__main__":
    main()
