#!/usr/bin/env bash
# Creates the Cognito user pool, the privileged 'moderator' group, and an app
# client with the admin auth flow the login Lambda uses.
#
# Email is routed through the course-managed SES identity (noreply@cab432.com)
# rather than Cognito's built-in sender - this is an explicit assignment
# requirement and is graded (US3 AC4).
source "$(dirname "${BASH_SOURCE[0]}")/00-config.sh"
require_aws
load_state

EXISTING=$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='${USER_POOL_NAME}'].Id | [0]" --output text)

if [ "$EXISTING" != "None" ] && [ -n "$EXISTING" ]; then
  echo "User pool $USER_POOL_NAME already exists ($EXISTING), skipping create."
  POOL_ID="$EXISTING"
else
  echo "Creating user pool $USER_POOL_NAME ..."
  POOL_ID=$(aws cognito-idp create-user-pool \
    --pool-name "$USER_POOL_NAME" \
    --region "$REGION" \
    --username-attributes email \
    --auto-verified-attributes email \
    --policies '{"PasswordPolicy":{"MinimumLength":6,"RequireUppercase":false,"RequireLowercase":false,"RequireNumbers":false,"RequireSymbols":false}}' \
    --schema '[{"Name":"email","AttributeDataType":"String","Required":true,"Mutable":true}]' \
    --account-recovery-setting '{"RecoveryMechanisms":[{"Priority":1,"Name":"verified_email"}]}' \
    --email-configuration "SourceArn=${SES_IDENTITY_ARN},EmailSendingAccount=DEVELOPER,From=${SES_FROM}" \
    --user-pool-tags "qut-username=${QUT_USERNAME},purpose=${PURPOSE}" \
    --query 'UserPool.Id' --output text)
fi

echo "  pool id: $POOL_ID"
save_state "USER_POOL_ID" "$POOL_ID"
save_state "USER_POOL_ARN" "arn:aws:cognito-idp:${REGION}:${ACCOUNT_ID}:userpool/${POOL_ID}"

echo "Ensuring '${MODERATOR_GROUP}' group exists ..."
aws cognito-idp create-group \
  --group-name "$MODERATOR_GROUP" \
  --user-pool-id "$POOL_ID" \
  --region "$REGION" \
  --description "Privileged users who can hide posts and moments" >/dev/null 2>&1 \
  || echo "  group already exists, fine."

CLIENT_ID=$(aws cognito-idp list-user-pool-clients --user-pool-id "$POOL_ID" --region "$REGION" \
  --query "UserPoolClients[?ClientName=='${STUDENT_ID}-app-client'].ClientId | [0]" --output text)

if [ "$CLIENT_ID" != "None" ] && [ -n "$CLIENT_ID" ]; then
  echo "App client already exists ($CLIENT_ID), skipping create."
else
  echo "Creating app client ..."
  CLIENT_ID=$(aws cognito-idp create-user-pool-client \
    --user-pool-id "$POOL_ID" \
    --client-name "${STUDENT_ID}-app-client" \
    --region "$REGION" \
    --no-generate-secret \
    --explicit-auth-flows ALLOW_ADMIN_USER_PASSWORD_AUTH ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
    --access-token-validity 24 --id-token-validity 24 --refresh-token-validity 30 \
    --token-validity-units 'AccessToken=hours,IdToken=hours,RefreshToken=days' \
    --prevent-user-existence-errors ENABLED \
    --query 'UserPoolClient.ClientId' --output text)
fi

echo "  client id: $CLIENT_ID"
save_state "COGNITO_CLIENT_ID" "$CLIENT_ID"

echo
echo "Cognito ready."
echo "  If the SES email configuration was rejected, the pool was still created -"
echo "  check Cognito console > Messaging and set the sender to ${SES_FROM} manually."
