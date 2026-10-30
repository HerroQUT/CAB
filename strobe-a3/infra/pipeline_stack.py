"""CDK stack: async classification pipeline for Strobe A2.

S3 media bucket (A1, EventBridge enabled)
  -> EventBridge rule (Object Created, this bucket only)
  -> SQS queue (DLQ after 3 failed receives)
  -> classifier Lambda (max 2 concurrent, partial-batch failures)
  -> Bedrock (Gemma 3 vision + Titan MM embeddings)
  -> S3 Vectors index + DynamoDB classifications table

Context (cdk.json): studentId, qutUsername, mediaBucketName,
vectorBucketName, vectorIndexName, and optionally lambdaRoleArn
(the CAB432 pre-scoped role, if you cannot create roles).
"""
from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sqs as sqs,
    aws_ssm as ssm,
)
from constructs import Construct

GEMMA = "google.gemma-3-12b-it"
TITAN_MM = "amazon.titan-embed-image-v1"
LAMBDA_TIMEOUT = Duration.seconds(90)


class PipelineStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        ctx = self.node.try_get_context
        sid: str = ctx("studentId")
        media_bucket_name: str = ctx("mediaBucketName")
        vector_bucket: str = ctx("vectorBucketName")
        vector_index: str = ctx("vectorIndexName")
        role_arn: str | None = ctx("lambdaRoleArn")

        Tags.of(self).add("qut-username", ctx("qutUsername"))
        Tags.of(self).add("purpose", "assessment 2")

        media_bucket = s3.Bucket.from_bucket_name(self, "MediaBucket", media_bucket_name)

        # ---- Classification result entity
        table = ddb.Table(
            self, "Classifications",
            table_name=f"{sid}-strobe-classifications",
            partition_key=ddb.Attribute(name="imageId", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        table.add_global_secondary_index(
            index_name="byUser",
            partition_key=ddb.Attribute(name="userId", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="classifiedAt", type=ddb.AttributeType.STRING),
        )
        table.add_global_secondary_index(
            index_name="byPost",
            partition_key=ddb.Attribute(name="postId", type=ddb.AttributeType.STRING),
        )

        # ---- Queue + DLQ (visibility timeout must exceed the Lambda timeout)
        dlq = sqs.Queue(self, "ClassifyDLQ", queue_name=f"{sid}-strobe-classify-dlq",
                        retention_period=Duration.days(14))
        queue = sqs.Queue(
            self, "ClassifyQueue",
            queue_name=f"{sid}-strobe-classify",
            visibility_timeout=Duration.seconds(LAMBDA_TIMEOUT.to_seconds() * 6),
            dead_letter_queue=sqs.DeadLetterQueue(queue=dlq, max_receive_count=3),
        )

        # ---- S3 upload -> EventBridge -> SQS
        rule = events.Rule(
            self, "UploadRule",
            rule_name=f"{sid}-strobe-upload-to-classify",
            description="Queues every new Strobe media object for classification",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [media_bucket_name]}},
            ),
        )
        rule.add_target(targets.SqsQueue(queue))  # adds the queue resource policy for events.amazonaws.com

        # ---- Parameter Store config (read by the Lambda at cold start)
        prefix = f"/{sid}/strobe-a2/classifier"
        params = {
            "gemma-model-id": GEMMA,
            "embed-model-id": TITAN_MM,
            "vector-bucket": vector_bucket,
            "vector-index": vector_index,
            "classifications-table": table.table_name,
        }
        for name, value in params.items():
            ssm.StringParameter(self, f"Param-{name}", parameter_name=f"{prefix}/{name}", string_value=value)

        # ---- Worker
        role = (iam.Role.from_role_arn(self, "CabRole", role_arn, mutable=False)
                if role_arn else None)
        log_group = logs.LogGroup(self, "ClassifierLogs",
                                  log_group_name=f"/aws/lambda/{sid}-strobe-classifier",
                                  retention=logs.RetentionDays.TWO_WEEKS,
                                  removal_policy=RemovalPolicy.DESTROY)
        fn = lambda_.Function(
            self, "Classifier",
            function_name=f"{sid}-strobe-classifier",
            description="Classifies and embeds uploaded Strobe images (SQS worker)",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../classifier/build"),
            memory_size=1024,          # Pillow resize of 10 MB photos
            timeout=LAMBDA_TIMEOUT,
            environment={"SSM_PREFIX": prefix},  # a path, not a secret
            role=role,
            log_group=log_group,
        )
        fn.add_event_source(sources.SqsEventSource(
            queue,
            batch_size=2,
            max_batching_window=Duration.seconds(2),
            max_concurrency=2,                 # protect the shared Bedrock quota
            report_batch_item_failures=True,
        ))

        if role is None:  # only when CDK is allowed to manage the role
            media_bucket.grant_read(fn)
            table.grant_write_data(fn)
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[f"arn:aws:bedrock:{self.region}::foundation-model/{m}" for m in (GEMMA, TITAN_MM)],
            ))
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["s3vectors:PutVectors"],
                resources=[f"arn:aws:s3vectors:{self.region}:{self.account}:bucket/{vector_bucket}/index/{vector_index}"],
            ))
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["ssm:GetParametersByPath"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{prefix}"],
            ))

        for name, value in {
            "ClassificationsTableArn": table.table_arn,
            "ClassifyQueueArn": queue.queue_arn,
            "ClassifyDlqArn": dlq.queue_arn,
            "UploadRuleArn": rule.rule_arn,
            "ClassifierFunctionArn": fn.function_arn,
        }.items():
            CfnOutput(self, name, value=value)
