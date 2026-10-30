#!/usr/bin/env python3
from __future__ import annotations

import aws_cdk as cdk

from stack import StrobeStack

app = cdk.App()

# ---- Fill these in for your own deployment (or pass via `cdk deploy -c key=value`) ----
student_id = app.node.try_get_context("studentId") or "n11457660"
subdomain = app.node.try_get_context("subdomain") or f"{student_id}.cab432.com"
hosted_zone_id = app.node.try_get_context("hostedZoneId") or "REPLACE_ME_ZONE_ID"
hosted_zone_name = app.node.try_get_context("hostedZoneName") or "cab432.com"
qut_username = app.node.try_get_context("qutUsername") or f"{student_id}@qut.edu.au"
# The one pre-scoped IAM role the course account provides for Lambda execution.
# Find it in the IAM console (ask the teaching team if you're not sure which one
# it is) — this stack is NOT permitted to create its own roles/policies.
lambda_role_arn = app.node.try_get_context("lambdaRoleArn") or "REPLACE_ME_LAMBDA_ROLE_ARN"
# -----------------------------------------------------------------------------------

env = cdk.Environment(
    account=app.node.try_get_context("account") or "901444280953",  # CAB432 shared account
    region="ap-southeast-2",
)

stack = StrobeStack(
    app,
    f"{student_id}-strobe-stack",
    student_id=student_id,
    subdomain=subdomain,
    hosted_zone_id=hosted_zone_id,
    hosted_zone_name=hosted_zone_name,
    qut_username=qut_username,
    lambda_role_arn=lambda_role_arn,
    env=env,
)

cdk.Tags.of(stack).add("qut-username", qut_username)
cdk.Tags.of(stack).add("purpose", "assessment 1")

app.synth()
