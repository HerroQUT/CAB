#!/usr/bin/env bash
# Creates the HTTP API (v2 - NOT a REST API), one AWS_PROXY integration per
# Lambda, all 27 routes from the Interface spec, the $default stage, and the
# resource policies that let API Gateway invoke each function.
#
# Written for bash 3.2 (the version macOS ships) - no associative arrays,
# and no JMESPath pipe expressions, which proved unreliable here.
source "$(dirname "${BASH_SOURCE[0]}")/00-config.sh"
require_aws
load_state

# Look the API up by exact name using awk rather than a JMESPath filter.
API_ID=$(aws apigatewayv2 get-apis --region "$REGION" \
  --output text --query 'Items[].[Name,ApiId]' 2>/dev/null \
  | awk -v n="$API_NAME" '$1==n {print $2; exit}')

if [ -n "$API_ID" ]; then
  echo "API $API_NAME already exists ($API_ID), reusing."
else
  echo "Creating HTTP API $API_NAME ..."
  API_ID=$(aws apigatewayv2 create-api \
    --name "$API_NAME" \
    --protocol-type HTTP \
    --region "$REGION" \
    --cors-configuration 'AllowOrigins=*,AllowMethods=*,AllowHeaders=*' \
    --tags "qut-username=${QUT_USERNAME},purpose=${PURPOSE}" \
    --output text --query 'ApiId')
fi

case "$API_ID" in
  ""|None|*[!a-zA-Z0-9]*)
    echo "ERROR: could not determine a valid API id (got: '$API_ID')"; exit 1;;
esac

echo "  api id: $API_ID"
save_state "API_ID" "$API_ID"
save_state "API_EXECUTION_ARN" "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}"
save_state "API_STAGE_ARN" "arn:aws:apigateway:${REGION}::/apis/${API_ID}/stages/\$default"

# One integration per controller Lambda, reused across that controller's routes.
# bash 3.2 has no associative arrays, so we stash ids in INT_<controller> vars.
for controller in "${CONTROLLERS[@]}"; do
  fn="${STUDENT_ID}-lambda-${controller}"
  eval "fn_arn=\$LAMBDA_ARN_${controller}"

  int_id=$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" \
    --output text --query 'Items[].[IntegrationUri,IntegrationId]' 2>/dev/null \
    | awk -v u="$fn_arn" '$1==u {print $2; exit}')

  if [ -z "$int_id" ]; then
    int_id=$(aws apigatewayv2 create-integration \
      --api-id "$API_ID" \
      --integration-type AWS_PROXY \
      --integration-uri "$fn_arn" \
      --payload-format-version 2.0 \
      --region "$REGION" \
      --output text --query 'IntegrationId')
  fi

  eval "INT_${controller}=\$int_id"
  echo "  integration for ${controller}: ${int_id}"

  aws lambda add-permission \
    --function-name "$fn" \
    --statement-id "apigw-invoke" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
    --region "$REGION" >/dev/null 2>&1 || true
done

# route-key|controller. Matches the Interface spec exactly: 27 routes.
ROUTES="
POST /v1/auth/register|auth
POST /v1/auth/login|auth
GET /v1/users|user
GET /v1/users/{id}|user
DELETE /v1/users/{id}|user
POST /v1/users/{userId}/follow|user
DELETE /v1/users/{userId}/follow|user
GET /v1/users/{userId}/followers|user
GET /v1/users/{userId}/following|user
POST /v1/posts|post
GET /v1/posts/user/{userId}|post
GET /v1/posts/{id}|post
PUT /v1/posts/{id}|post
DELETE /v1/posts/{id}|post
POST /v1/posts/{id}/like|post
DELETE /v1/posts/{id}/like|post
POST /v1/posts/{id}/hide|post
GET /v1/posts/{postId}/comments|post
POST /v1/posts/{postId}/comments|post
DELETE /v1/posts/{postId}/comments/{commentId}|post
GET /v1/feed|feed
POST /v1/moments|moment
GET /v1/moments/feed|moment
GET /v1/moments/archive|moment
POST /v1/moments/{id}/hide|moment
DELETE /v1/moments/{id}|moment
POST /v1/uploads/url|upload
"

EXISTING_KEYS=$(aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" \
  --output text --query 'Items[].RouteKey' 2>/dev/null || echo "")

echo "Creating routes ..."
created=0
echo "$ROUTES" | while IFS= read -r entry; do
  [ -z "$entry" ] && continue
  key="${entry%%|*}"
  controller="${entry##*|}"

  if echo "$EXISTING_KEYS" | tr '\t' '\n' | grep -qxF "$key"; then
    echo "  = $key (exists)"
    continue
  fi

  eval "target_int=\$INT_${controller}"
  aws apigatewayv2 create-route \
    --api-id "$API_ID" \
    --route-key "$key" \
    --target "integrations/${target_int}" \
    --region "$REGION" >/dev/null
  echo "  + $key -> $controller"
done

echo "Ensuring \$default stage with auto-deploy ..."
aws apigatewayv2 create-stage \
  --api-id "$API_ID" \
  --stage-name '$default' \
  --auto-deploy \
  --region "$REGION" \
  --tags "qut-username=${QUT_USERNAME},purpose=${PURPOSE}" >/dev/null 2>&1 \
  || echo "  stage already exists, fine."

ROUTE_COUNT=$(aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" \
  --output text --query 'Items[].RouteKey' 2>/dev/null | tr '\t' '\n' | grep -c . || echo 0)

echo
echo "API ready with ${ROUTE_COUNT} routes (expected 27)."
echo "  https://${API_ID}.execute-api.${REGION}.amazonaws.com"
echo "Test: curl https://${API_ID}.execute-api.${REGION}.amazonaws.com/v1/users"
