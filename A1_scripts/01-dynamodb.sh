#!/usr/bin/env bash
# Creates the six DynamoDB tables, one per entity, each with the GSIs the
# application code queries, 7-day point-in-time recovery, and the required tags.
source "$(dirname "${BASH_SOURCE[0]}")/00-config.sh"
require_aws

TAGS=("Key=qut-username,Value=${QUT_USERNAME}" "Key=purpose,Value=${PURPOSE}")

create_table() {
  local entity="$1"; shift
  local name="${STUDENT_ID}-ddb-${entity}"

  if aws dynamodb describe-table --table-name "$name" --region "$REGION" >/dev/null 2>&1; then
    echo "Table $name already exists, skipping create."
  else
    echo "Creating table $name ..."
    aws dynamodb create-table \
      --table-name "$name" \
      --billing-mode PAY_PER_REQUEST \
      --region "$REGION" \
      --tags "${TAGS[@]}" \
      "$@" >/dev/null
    aws dynamodb wait table-exists --table-name "$name" --region "$REGION"
  fi

  echo "  enabling 7-day PITR ..."
  aws dynamodb update-continuous-backups \
    --table-name "$name" \
    --region "$REGION" \
    --point-in-time-recovery-specification \
      "PointInTimeRecoveryEnabled=true,RecoveryPeriodInDays=7" >/dev/null 2>&1 \
    || echo "  (PITR call failed - set it manually in the console, it is worth 0.25 marks)"

  local arn
  arn=$(aws dynamodb describe-table --table-name "$name" --region "$REGION" \
        --query 'Table.TableArn' --output text)
  save_state "TABLE_ARN_${entity}" "$arn"
}

# users: looked up by id; email GSI backs login/duplicate-detection.
create_table users \
  --attribute-definitions AttributeName=id,AttributeType=S AttributeName=email,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --global-secondary-indexes '[{"IndexName":"email-index","KeySchema":[{"AttributeName":"email","KeyType":"HASH"}],"Projection":{"ProjectionType":"ALL"}}]'

# posts: looked up by id; userId+createdAt GSI backs profile and feed queries.
create_table posts \
  --attribute-definitions AttributeName=id,AttributeType=S AttributeName=userId,AttributeType=S AttributeName=createdAt,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --global-secondary-indexes '[{"IndexName":"userId-index","KeySchema":[{"AttributeName":"userId","KeyType":"HASH"},{"AttributeName":"createdAt","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]'

# comments: looked up by id; postId+createdAt GSI lists a post's comments in order.
create_table comments \
  --attribute-definitions AttributeName=id,AttributeType=S AttributeName=postId,AttributeType=S AttributeName=createdAt,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --global-secondary-indexes '[{"IndexName":"postId-index","KeySchema":[{"AttributeName":"postId","KeyType":"HASH"},{"AttributeName":"createdAt","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]'

# likes: many-to-many join, so a real composite key (postId + userId).
create_table likes \
  --attribute-definitions AttributeName=postId,AttributeType=S AttributeName=userId,AttributeType=S \
  --key-schema AttributeName=postId,KeyType=HASH AttributeName=userId,KeyType=RANGE \
  --global-secondary-indexes '[{"IndexName":"userId-index","KeySchema":[{"AttributeName":"userId","KeyType":"HASH"},{"AttributeName":"postId","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]'

# follows: self-referencing many-to-many, adjacency-list key design.
create_table follows \
  --attribute-definitions AttributeName=followerId,AttributeType=S AttributeName=followeeId,AttributeType=S \
  --key-schema AttributeName=followerId,KeyType=HASH AttributeName=followeeId,KeyType=RANGE \
  --global-secondary-indexes '[{"IndexName":"followee-index","KeySchema":[{"AttributeName":"followeeId","KeyType":"HASH"},{"AttributeName":"followerId","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]'

# moments: looked up by id; userId+createdAt GSI backs the moment feed.
create_table moments \
  --attribute-definitions AttributeName=id,AttributeType=S AttributeName=userId,AttributeType=S AttributeName=createdAt,AttributeType=S \
  --key-schema AttributeName=id,KeyType=HASH \
  --global-secondary-indexes '[{"IndexName":"userId-index","KeySchema":[{"AttributeName":"userId","KeyType":"HASH"},{"AttributeName":"createdAt","KeyType":"RANGE"}],"Projection":{"ProjectionType":"ALL"}}]'

echo
echo "All six DynamoDB tables ready."
