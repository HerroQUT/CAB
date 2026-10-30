from __future__ import annotations

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from constructs import Construct


class StrobeStack(Stack):
    """Serverless Strobe backend: API Gateway HTTP API + 6 Lambdas + DynamoDB + S3 + Cognito."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        student_id: str,
        subdomain: str,
        hosted_zone_id: str,
        hosted_zone_name: str,
        qut_username: str,
        lambda_role_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # The course account provides one pre-scoped IAM role and forbids creating
        # new roles/policies (see the "AWS" doc: "you cannot create new IAM users,
        # roles, or policies"). `mutable=False` stops CDK from trying to attach any
        # inline/managed policies to it — every Lambda below reuses this same role
        # as-is, and we never call grant_*()/add_to_role_policy() anywhere in this
        # file. If the role doesn't already have DynamoDB/S3/Cognito permissions,
        # deploys will succeed but invocations will fail with AccessDenied — ask
        # the teaching team to confirm what the shared role covers.
        execution_role = iam.Role.from_role_arn(self, "ExecutionRole", lambda_role_arn, mutable=False)

        # ---------------------------------------------------------------- DynamoDB
        # users/posts/comments/moments are looked up by their own synthetic `id`
        # (services call find_x_by_id(id) with nothing else), so id-as-PK + a GSI
        # for the secondary "by owner" access pattern is the correct shape there.
        #
        # likes/follows are inherent many-to-many join records where every lookup
        # already has both sides of the relationship available, so they get real
        # composite primary keys (adjacency-list pattern) instead of a synthetic id.
        tables = {
            "users": self._make_table(student_id, "users", pk="id", gsis=[("email-index", "email", None)]),
            "posts": self._make_table(student_id, "posts", pk="id", gsis=[("userId-index", "userId", "createdAt")]),
            "comments": self._make_table(
                student_id, "comments", pk="id", gsis=[("postId-index", "postId", "createdAt")]
            ),
            "likes": self._make_table(
                student_id,
                "likes",
                pk="postId",
                sk="userId",
                gsis=[("userId-index", "userId", "postId")],
            ),
            "follows": self._make_table(
                student_id,
                "follows",
                pk="followerId",
                sk="followeeId",
                gsis=[("followee-index", "followeeId", "followerId")],
            ),
            "moments": self._make_table(student_id, "moments", pk="id", gsis=[("userId-index", "userId", "createdAt")]),
        }

        # ---------------------------------------------------------------- S3
        media_bucket = s3.Bucket(
            self,
            "MediaBucket",
            bucket_name=f"{student_id}-s3-media",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            # NOT auto_delete_objects=True: that option makes CDK provision its own
            # custom-resource Lambda + IAM role to empty the bucket on `cdk destroy`,
            # which this account doesn't permit (see the IAM note above). Empty the
            # bucket manually before destroying the stack if you need to tear down.
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                )
            ],
        )

        # ---------------------------------------------------------------- Cognito
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name=f"{student_id}-cognitoauth",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True, username=False),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(email=cognito.StandardAttribute(required=True, mutable=True)),
            password_policy=cognito.PasswordPolicy(
                min_length=6,
                require_lowercase=False,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            email=cognito.UserPoolEmail.with_ses(
                from_email="noreply@cab432.com",
                ses_verified_domain=hosted_zone_name,
                ses_region=self.region,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        moderator_group_name = "moderator"
        cognito.CfnUserPoolGroup(
            self,
            "ModeratorGroup",
            user_pool_id=user_pool.user_pool_id,
            group_name=moderator_group_name,
            description="Users allowed to hide posts/moments (User Story 5)",
        )

        app_client = user_pool.add_client(
            "AppClient",
            auth_flows=cognito.AuthFlow(admin_user_password=True, user_password=True),
            generate_secret=False,
            access_token_validity=Duration.hours(24),
            id_token_validity=Duration.hours(24),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
        )

        # ---------------------------------------------------------------- Lambdas
        common_env = {
            "USERS_TABLE": tables["users"].table_name,
            "POSTS_TABLE": tables["posts"].table_name,
            "COMMENTS_TABLE": tables["comments"].table_name,
            "LIKES_TABLE": tables["likes"].table_name,
            "FOLLOWS_TABLE": tables["follows"].table_name,
            "MOMENTS_TABLE": tables["moments"].table_name,
            "MEDIA_BUCKET": media_bucket.bucket_name,
            "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
            "COGNITO_CLIENT_ID": app_client.user_pool_client_id,
            "COGNITO_MODERATOR_GROUP": moderator_group_name,
        }

        code_asset = _lambda.Code.from_asset(
            "../src",
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_13.bundling_image,
                command=[
                    "bash",
                    "-c",
                    "pip install -r requirements-lambda.txt -t /asset-output && mkdir -p /asset-output/strobe && cp -au . /asset-output/strobe",
                ],
            ),
        )

        def make_lambda(name: str, handler_module: str) -> _lambda.Function:
            return _lambda.Function(
                self,
                f"{name.title()}Fn",
                function_name=f"{student_id}-lambda-{name}",
                runtime=_lambda.Runtime.PYTHON_3_13,
                handler=f"strobe.lambda_handlers.{handler_module}.handler",
                code=code_asset,
                environment=common_env,
                timeout=Duration.seconds(15),
                memory_size=256,
                role=execution_role,
            )

        auth_fn = make_lambda("auth", "auth_handler")
        user_fn = make_lambda("user", "user_handler")
        post_fn = make_lambda("post", "post_handler")
        feed_fn = make_lambda("feed", "feed_handler")
        moment_fn = make_lambda("moment", "moment_handler")
        upload_fn = make_lambda("upload", "upload_handler")

        # ---------------------------------------------------------------- API Gateway (HTTP API)
        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=f"{student_id}-api",
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigwv2.CorsHttpMethod.ANY],
                allow_headers=["*"],
            ),
        )

        def integration(fn: _lambda.Function) -> apigwv2_integrations.HttpLambdaIntegration:
            return apigwv2_integrations.HttpLambdaIntegration(f"{fn.node.id}Integration", fn)

        route_table = [
            ("POST", "/v1/auth/register", auth_fn),
            ("POST", "/v1/auth/login", auth_fn),
            ("GET", "/v1/users", user_fn),
            ("GET", "/v1/users/{id}", user_fn),
            ("DELETE", "/v1/users/{id}", user_fn),
            ("POST", "/v1/users/{userId}/follow", user_fn),
            ("DELETE", "/v1/users/{userId}/follow", user_fn),
            ("GET", "/v1/users/{userId}/followers", user_fn),
            ("GET", "/v1/users/{userId}/following", user_fn),
            ("POST", "/v1/posts", post_fn),
            ("GET", "/v1/posts/user/{userId}", post_fn),
            ("GET", "/v1/posts/{id}", post_fn),
            ("PUT", "/v1/posts/{id}", post_fn),
            ("DELETE", "/v1/posts/{id}", post_fn),
            ("POST", "/v1/posts/{id}/like", post_fn),
            ("DELETE", "/v1/posts/{id}/like", post_fn),
            ("POST", "/v1/posts/{id}/hide", post_fn),
            ("GET", "/v1/posts/{postId}/comments", post_fn),
            ("POST", "/v1/posts/{postId}/comments", post_fn),
            ("DELETE", "/v1/posts/{postId}/comments/{commentId}", post_fn),
            ("GET", "/v1/feed", feed_fn),
            ("POST", "/v1/moments", moment_fn),
            ("GET", "/v1/moments/feed", moment_fn),
            ("GET", "/v1/moments/archive", moment_fn),
            ("POST", "/v1/moments/{id}/hide", moment_fn),
            ("DELETE", "/v1/moments/{id}", moment_fn),
            ("POST", "/v1/uploads/url", upload_fn),
        ]

        method_map = {
            "GET": apigwv2.HttpMethod.GET,
            "POST": apigwv2.HttpMethod.POST,
            "PUT": apigwv2.HttpMethod.PUT,
            "DELETE": apigwv2.HttpMethod.DELETE,
        }

        for method, path, fn in route_table:
            http_api.add_routes(path=path, methods=[method_map[method]], integration=integration(fn))

        # ---------------------------------------------------------------- Custom domain (US1/US2)
        hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
            self, "HostedZone", hosted_zone_id=hosted_zone_id, zone_name=hosted_zone_name
        )

        certificate = acm.Certificate(
            self,
            "ApiCertificate",
            domain_name=subdomain,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        domain_name = apigwv2.DomainName(
            self,
            "ApiDomainName",
            domain_name=subdomain,
            certificate=certificate,
            security_policy=apigwv2.SecurityPolicy.TLS_1_2,
        )

        apigwv2.HttpApiMapping(
            self,
            "ApiMapping",
            api=http_api,
            domain_name=domain_name,
        )

        route53.ARecord(
            self,
            "ApiAliasRecord",
            zone=hosted_zone,
            record_name=subdomain,
            target=route53.RecordTarget.from_alias(
                targets.ApiGatewayv2DomainProperties(
                    domain_name.regional_domain_name,
                    domain_name.regional_hosted_zone_id,
                )
            ),
        )

        # ---------------------------------------------------------------- Outputs
        CfnOutput(self, "ApiUrl", value=f"https://{subdomain}")
        CfnOutput(self, "ApiGatewayExecuteArn", value=f"arn:aws:execute-api:{self.region}:{self.account}:{http_api.http_api_id}")
        CfnOutput(self, "ApiGatewayStageArn", value=f"arn:aws:apigateway:{self.region}::/apis/{http_api.http_api_id}/stages/$default")
        CfnOutput(self, "MediaBucketArn", value=media_bucket.bucket_arn)
        CfnOutput(self, "UserPoolArn", value=user_pool.user_pool_arn)
        CfnOutput(self, "CertificateArn", value=certificate.certificate_arn)
        for entity, t in tables.items():
            CfnOutput(self, f"{entity.title()}TableArn", value=t.table_arn)
        CfnOutput(self, "AuthLambdaArn", value=auth_fn.function_arn)
        CfnOutput(self, "UserLambdaArn", value=user_fn.function_arn)
        CfnOutput(self, "PostLambdaArn", value=post_fn.function_arn)
        CfnOutput(self, "FeedLambdaArn", value=feed_fn.function_arn)
        CfnOutput(self, "MomentLambdaArn", value=moment_fn.function_arn)
        CfnOutput(self, "UploadLambdaArn", value=upload_fn.function_arn)

    def _make_table(
        self,
        student_id: str,
        entity: str,
        *,
        pk: str,
        sk: str | None = None,
        gsis: list[tuple[str, str, str | None]] | None = None,
    ) -> dynamodb.Table:
        """Create a per-entity DynamoDB table with the given key schema, PITR (7-day), and GSIs."""
        table = dynamodb.Table(
            self,
            f"{entity.title()}Table",
            table_name=f"{student_id}-ddb-{entity}",
            partition_key=dynamodb.Attribute(name=pk, type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name=sk, type=dynamodb.AttributeType.STRING) if sk else None,
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True,
                recovery_period_in_days=7,
            ),
        )
        for index_name, index_pk, index_sk in gsis or []:
            table.add_global_secondary_index(
                index_name=index_name,
                partition_key=dynamodb.Attribute(name=index_pk, type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name=index_sk, type=dynamodb.AttributeType.STRING) if index_sk else None,
            )
        return table
