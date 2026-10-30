#!/usr/bin/env bash
# Creates the media bucket. Public access is fully blocked - all reads and
# writes happen through short-lived presigned URLs issued by the upload Lambda.
source "$(dirname "${BASH_SOURCE[0]}")/00-config.sh"
require_aws

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "Bucket $BUCKET already exists, skipping create."
else
  echo "Creating bucket $BUCKET ..."
  aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
fi

echo "Blocking all public access ..."
aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"


echo "Setting CORS so browsers can PUT to presigned URLs ..."
aws s3api put-bucket-cors \
  --bucket "$BUCKET" \
  --cors-configuration '{
    "CORSRules":[{
      "AllowedHeaders":["*"],
      "AllowedMethods":["PUT","GET"],
      "AllowedOrigins":["*"],
      "ExposeHeaders":["ETag"],
      "MaxAgeSeconds":3000
    }]
  }'

echo "Tagging ..."
aws s3api put-bucket-tagging \
  --bucket "$BUCKET" \
  --tagging "TagSet=[{Key=qut-username,Value=${QUT_USERNAME}},{Key=purpose,Value=${PURPOSE}}]"

save_state "BUCKET_ARN" "arn:aws:s3:::${BUCKET}"
echo
echo "Bucket ready: arn:aws:s3:::${BUCKET}"
