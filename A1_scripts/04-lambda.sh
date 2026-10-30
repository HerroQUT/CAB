#!/usr/bin/env bash
# Packages the application into a Lambda zip and creates (or updates) the six
# functions - one per controller.
#
# Packaging note: the application uses relative imports (from ..config import
# settings), so the code MUST sit inside a package directory in the zip. We put
# it at strobe/ and use handlers like strobe.lambda_handlers.auth_handler.handler.
# Flattening it to the zip root would break every function on cold start.
source "$(dirname "${BASH_SOURCE[0]}")/00-config.sh"
require_aws
load_state

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build"
ZIP="${ROOT}/lambda.zip"

echo "Packaging ..."
rm -rf "$BUILD" "$ZIP"
mkdir -p "$BUILD/strobe"

# PyJWT[crypto] pulls in `cryptography`, which ships compiled binaries. Building
# on macOS would install darwin/arm64 wheels that crash on Lambda's linux/x86_64
# runtime, so we force manylinux wheels regardless of the build machine.
python3 -m pip install -r "${ROOT}/src/requirements-lambda.txt" -t "$BUILD" \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  --quiet --upgrade
cp -r "${ROOT}/src/." "$BUILD/strobe/"
rm -f "$BUILD/strobe/requirements-lambda.txt"
find "$BUILD" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

( cd "$BUILD" && zip -rq "$ZIP" . )
echo "  built $(du -h "$ZIP" | cut -f1) zip"

ENV_VARS="Variables={USERS_TABLE=${STUDENT_ID}-ddb-users,POSTS_TABLE=${STUDENT_ID}-ddb-posts,COMMENTS_TABLE=${STUDENT_ID}-ddb-comments,LIKES_TABLE=${STUDENT_ID}-ddb-likes,FOLLOWS_TABLE=${STUDENT_ID}-ddb-follows,MOMENTS_TABLE=${STUDENT_ID}-ddb-moments,MEDIA_BUCKET=${BUCKET},COGNITO_USER_POOL_ID=${USER_POOL_ID},COGNITO_CLIENT_ID=${COGNITO_CLIENT_ID},COGNITO_MODERATOR_GROUP=${MODERATOR_GROUP}}"

for controller in "${CONTROLLERS[@]}"; do
  fn="${STUDENT_ID}-lambda-${controller}"
  handler="strobe.lambda_handlers.${controller}_handler.handler"

  if aws lambda get-function --function-name "$fn" --region "$REGION" >/dev/null 2>&1; then
    echo "Updating $fn ..."
    aws lambda update-function-code \
      --function-name "$fn" --zip-file "fileb://${ZIP}" --region "$REGION" >/dev/null
    aws lambda wait function-updated --function-name "$fn" --region "$REGION"
    aws lambda update-function-configuration \
      --function-name "$fn" --handler "$handler" --environment "$ENV_VARS" \
      --timeout 15 --memory-size 256 --region "$REGION" >/dev/null
  else
    echo "Creating $fn ..."
    aws lambda create-function \
      --function-name "$fn" \
      --runtime python3.13 \
      --role "$LAMBDA_ROLE_ARN" \
      --handler "$handler" \
      --zip-file "fileb://${ZIP}" \
      --timeout 15 \
      --memory-size 256 \
      --environment "$ENV_VARS" \
      --region "$REGION" \
      --tags "qut-username=${QUT_USERNAME},purpose=${PURPOSE}" >/dev/null
  fi

  aws lambda wait function-active --function-name "$fn" --region "$REGION"
  arn=$(aws lambda get-function --function-name "$fn" --region "$REGION" \
        --query 'Configuration.FunctionArn' --output text)
  save_state "LAMBDA_ARN_${controller}" "$arn"
done

echo
echo "All six Lambda functions ready."
