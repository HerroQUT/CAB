#!/usr/bin/env bash
# Writes submission-infra.yml and submission-routes.yml using the real ARNs
# recorded by the earlier scripts. Run this LAST, after 01-06 have all
# succeeded and after you have created the moderator test account.
#
# Usage: ./07-generate-yaml.sh <test-email> <test-password>
source "$(dirname "${BASH_SOURCE[0]}")/00-config.sh"
load_state

TEST_USER="${1:-}"
TEST_PASS="${2:-}"

if [ -z "$TEST_USER" ] || [ -z "$TEST_PASS" ]; then
  echo "Usage: ./07-generate-yaml.sh <test-email> <test-password>"
  echo "  Use the confirmed moderator account you registered."
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for v in API_EXECUTION_ARN API_STAGE_ARN CERTIFICATE_ARN USER_POOL_ARN BUCKET_ARN \
         TABLE_ARN_users TABLE_ARN_posts TABLE_ARN_comments TABLE_ARN_likes \
         TABLE_ARN_follows TABLE_ARN_moments \
         LAMBDA_ARN_auth LAMBDA_ARN_user LAMBDA_ARN_post LAMBDA_ARN_feed \
         LAMBDA_ARN_moment LAMBDA_ARN_upload; do
  if [ -z "${!v:-}" ]; then
    echo "Missing ${v} in deploy state. Re-run the earlier scripts first."
    exit 1
  fi
done

cat > "${ROOT}/submission-infra.yml" <<EOF
cloudInfrastructure:
  apiGatewayExecutionARN: ${API_EXECUTION_ARN}
  apiGatewayStageARN: ${API_STAGE_ARN}
  apiRoute53Record: ${DOMAIN}
  certificateARN: ${CERTIFICATE_ARN}
  cognitoARN: ${USER_POOL_ARN}
  dynamoDBPostsTableARN: ${TABLE_ARN_posts}
  s3MediaStoreARN: ${BUCKET_ARN}
  dynamoDBTableARNs:
    - entity: user
      arn: ${TABLE_ARN_users}
    - entity: post
      arn: ${TABLE_ARN_posts}
    - entity: comment
      arn: ${TABLE_ARN_comments}
    - entity: like
      arn: ${TABLE_ARN_likes}
    - entity: follow
      arn: ${TABLE_ARN_follows}
    - entity: moment
      arn: ${TABLE_ARN_moments}
  lambdaARN:
    - controller: auth
      arn: ${LAMBDA_ARN_auth}
    - controller: feed
      arn: ${LAMBDA_ARN_feed}
    - controller: moment
      arn: ${LAMBDA_ARN_moment}
    - controller: post
      arn: ${LAMBDA_ARN_post}
    - controller: user
      arn: ${LAMBDA_ARN_user}
    - controller: upload
      arn: ${LAMBDA_ARN_upload}
  metaData:
    qutUsernameTag: ${QUT_USERNAME}
    testUsername: ${TEST_USER}
    testPassword: ${TEST_PASS}
studentDetail:
  studentID: ${STUDENT_ID}
  studentFirstName: Herro
  studentLastName: Ngejane
EOF

python3 - "$ROOT" <<'PYEOF'
import os, sys
root = sys.argv[1]
env = os.environ
domain = env["DOMAIN"]

L = {c: env[f"LAMBDA_ARN_{c}"] for c in
     ["auth", "user", "post", "feed", "moment", "upload"]}
T = {e: env[f"TABLE_ARN_{e}"] for e in
     ["users", "posts", "comments", "likes", "follows", "moments"]}
S3 = env["BUCKET_ARN"]

# (route, method, controller, primary storage entity+arn, additional storage)
routes = [
    ("/v1/auth/register", "POST", "auth", ("user", T["users"]), []),
    ("/v1/auth/login", "POST", "auth", ("user", T["users"]), []),
    ("/v1/posts", "POST", "post", ("post", T["posts"]), []),
    ("/v1/posts/user/{userId}", "GET", "post", ("post", T["posts"]), []),
    ("/v1/posts/{id}", "GET", "post", ("post", T["posts"]), [("s3", S3)]),
    ("/v1/posts/{id}", "PUT", "post", ("post", T["posts"]), []),
    ("/v1/posts/{id}", "DELETE", "post", ("post", T["posts"]),
     [("comment", T["comments"]), ("like", T["likes"]), ("s3", S3)]),
    ("/v1/posts/{id}/like", "POST", "post", ("like", T["likes"]), []),
    ("/v1/posts/{id}/like", "DELETE", "post", ("like", T["likes"]), []),
    ("/v1/posts/{id}/hide", "POST", "post", ("post", T["posts"]), []),
    ("/v1/posts/{postId}/comments", "POST", "post", ("comment", T["comments"]), []),
    ("/v1/posts/{postId}/comments", "GET", "post", ("comment", T["comments"]), []),
    ("/v1/posts/{postId}/comments/{commentId}", "DELETE", "post",
     ("comment", T["comments"]), []),
    ("/v1/feed", "GET", "feed", ("post", T["posts"]), [("follow", T["follows"])]),
    ("/v1/users", "GET", "user", ("user", T["users"]), []),
    ("/v1/users/{id}", "GET", "user", ("user", T["users"]), []),
    ("/v1/users/{id}", "DELETE", "user", ("user", T["users"]),
     [("post", T["posts"]), ("comment", T["comments"]), ("like", T["likes"]),
      ("follow", T["follows"]), ("moment", T["moments"]), ("s3", S3)]),
    ("/v1/users/{userId}/follow", "POST", "user", ("follow", T["follows"]), []),
    ("/v1/users/{userId}/follow", "DELETE", "user", ("follow", T["follows"]), []),
    ("/v1/users/{userId}/followers", "GET", "user", ("follow", T["follows"]), []),
    ("/v1/users/{userId}/following", "GET", "user", ("follow", T["follows"]), []),
    ("/v1/uploads/url", "POST", "upload", ("s3", S3), []),
    ("/v1/moments", "POST", "moment", ("moment", T["moments"]), []),
    ("/v1/moments/feed", "GET", "moment", ("moment", T["moments"]),
     [("follow", T["follows"])]),
    ("/v1/moments/archive", "GET", "moment", ("moment", T["moments"]), []),
    ("/v1/moments/{id}/hide", "POST", "moment", ("moment", T["moments"]), []),
    ("/v1/moments/{id}", "DELETE", "moment", ("moment", T["moments"]), []),
]

lines = []
for route, method, controller, (ent, arn), extra in routes:
    lines.append(f"- route: {route}")
    lines.append(f"  method: {method}")
    lines.append(f"  controller: {controller}")
    lines.append(f'  apiGatewayRoute: "{method} {route}"')
    lines.append(f"  lambdaARN: {L[controller]}")
    lines.append(f"  storage: {{entity: {ent}, arn: {arn}}}")
    if extra:
        lines.append("  additionalStorage:")
        for e, a in extra:
            lines.append(f"    - {{entity: {e}, arn: {a}}}")
    else:
        lines.append("  additionalStorage: []")
    lines.append("")

with open(os.path.join(root, "submission-routes.yml"), "w") as f:
    f.write("\n".join(lines))
print(f"  wrote {len(routes)} routes")
PYEOF

echo
echo "Generated:"
echo "  ${ROOT}/submission-infra.yml"
echo "  ${ROOT}/submission-routes.yml"
echo
python3 -c "
import yaml
for f in ['${ROOT}/submission-infra.yml','${ROOT}/submission-routes.yml']:
    yaml.safe_load(open(f))
print('Both files parse as valid YAML.')
"
grep -l 'FILL\|REPLACE_ME' "${ROOT}"/submission-*.yml 2>/dev/null \
  && echo "WARNING: placeholders still present!" \
  || echo "No placeholders remaining."
