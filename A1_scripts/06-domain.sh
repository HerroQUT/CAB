#!/usr/bin/env bash
# Requests an ACM certificate for the subdomain, writes its DNS validation
# record into the shared hosted zone, waits for issuance, then attaches the
# custom domain to the API and points Route53 at it.
#
# The wait for certificate issuance usually takes 2-5 minutes. If it times out,
# just re-run this script - it is safe to run repeatedly.
source "$(dirname "${BASH_SOURCE[0]}")/00-config.sh"
require_aws
load_state

# Look the certificate up with awk rather than a JMESPath pipe expression:
# the shared account contains many certificates and '| [0]' proved unreliable.
CERT_ARN=$(aws acm list-certificates --region "$REGION" \
  --output text --query 'CertificateSummaryList[].[DomainName,CertificateArn]' 2>/dev/null \
  | awk -v d="$DOMAIN" '$1==d {print $2; exit}')

if [ -z "$CERT_ARN" ]; then
  echo "Requesting certificate for ${DOMAIN} ..."
  CERT_ARN=$(aws acm request-certificate \
    --domain-name "$DOMAIN" \
    --validation-method DNS \
    --region "$REGION" \
    --tags "Key=qut-username,Value=${QUT_USERNAME}" "Key=purpose,Value=${PURPOSE}" \
    --query 'CertificateArn' --output text)
  echo "  waiting for validation record to be generated ..."
  sleep 10
else
  echo "Certificate already requested: $CERT_ARN"
fi

case "$CERT_ARN" in
  arn:aws:acm:*) ;;
  *) echo "ERROR: invalid certificate ARN resolved: '$CERT_ARN'"; exit 1;;
esac

save_state "CERTIFICATE_ARN" "$CERT_ARN"

STATUS=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$REGION" \
  --query 'Certificate.Status' --output text)

if [ "$STATUS" != "ISSUED" ]; then
  echo "Writing DNS validation record ..."
  RR_NAME=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$REGION" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Name' --output text)
  RR_VALUE=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$REGION" \
    --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Value' --output text)

  aws route53 change-resource-record-sets \
    --hosted-zone-id "$HOSTED_ZONE_ID" \
    --change-batch "{
      \"Changes\":[{
        \"Action\":\"UPSERT\",
        \"ResourceRecordSet\":{
          \"Name\":\"${RR_NAME}\",
          \"Type\":\"CNAME\",
          \"TTL\":300,
          \"ResourceRecords\":[{\"Value\":\"${RR_VALUE}\"}]
        }
      }]
    }" >/dev/null

  echo "  waiting for ACM to validate (this can take a few minutes) ..."
  aws acm wait certificate-validated --certificate-arn "$CERT_ARN" --region "$REGION" \
    || { echo "Timed out. Re-run this script; validation may still be in progress."; exit 1; }
fi

echo "Certificate ISSUED."

if aws apigatewayv2 get-domain-name --domain-name "$DOMAIN" --region "$REGION" >/dev/null 2>&1; then
  echo "Custom domain already exists."
else
  echo "Creating custom domain ${DOMAIN} with TLS 1.2 ..."
  aws apigatewayv2 create-domain-name \
    --domain-name "$DOMAIN" \
    --region "$REGION" \
    --domain-name-configurations "CertificateArn=${CERT_ARN},EndpointType=REGIONAL,SecurityPolicy=TLS_1_2" \
    --tags "qut-username=${QUT_USERNAME},purpose=${PURPOSE}" >/dev/null
fi

TARGET=$(aws apigatewayv2 get-domain-name --domain-name "$DOMAIN" --region "$REGION" \
  --query 'DomainNameConfigurations[0].ApiGatewayDomainName' --output text)
TARGET_ZONE=$(aws apigatewayv2 get-domain-name --domain-name "$DOMAIN" --region "$REGION" \
  --query 'DomainNameConfigurations[0].HostedZoneId' --output text)

echo "Mapping domain to API ..."
aws apigatewayv2 create-api-mapping \
  --api-id "$API_ID" \
  --domain-name "$DOMAIN" \
  --stage '$default' \
  --region "$REGION" >/dev/null 2>&1 || echo "  mapping already exists, fine."

echo "Creating Route53 alias record ..."
aws route53 change-resource-record-sets \
  --hosted-zone-id "$HOSTED_ZONE_ID" \
  --change-batch "{
    \"Changes\":[{
      \"Action\":\"UPSERT\",
      \"ResourceRecordSet\":{
        \"Name\":\"${DOMAIN}\",
        \"Type\":\"A\",
        \"AliasTarget\":{
          \"DNSName\":\"${TARGET}\",
          \"HostedZoneId\":\"${TARGET_ZONE}\",
          \"EvaluateTargetHealth\":false
        }
      }
    }]
  }" >/dev/null

echo
echo "Domain ready: https://${DOMAIN}"
echo "DNS can take a few minutes to propagate. Then test:"
echo "  curl https://${DOMAIN}/v1/users"
