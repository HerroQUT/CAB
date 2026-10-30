# Strobe A2 – step 1: model smoke test + async classification pipeline

## 1. Smoke test (run first, paste the output back)

```bash
cd smoke_test
pip install boto3 pillow
python bedrock_smoke_test.py
```

It checks Gemma text, system prompts, native tool use, JSON tool fallback and
vision, Nemotron tool use, and that Titan Multimodal text/image vectors share a
space. The "Recommendation" line at the end picks the agent strategy (A/B/C).

## 2. Classification pipeline

```
A1 media bucket ──EventBridge "Object Created"──▶ rule ──▶ SQS ──▶ classifier Lambda
                                                    │ (3 fails)          │
                                                    ▼                    ├─▶ Bedrock: Gemma 3 (labels + caption)
                                                   DLQ                   ├─▶ Bedrock: Titan MM (1024-d vector)
                                                                         ├─▶ S3 Vectors index   (Embedding entity)
                                                                         └─▶ DynamoDB table     (Classification entity)
```

The A1 upload handler is untouched: the client still PUTs to a presigned URL, and
classification happens afterwards off the queue.

### Deploy

1. Create the vector store and enable EventBridge on your A1 media bucket:

   ```bash
   python scripts/setup_vectors_and_events.py --student-id n1234567 \
       --qut-username n1234567@qut.edu.au --media-bucket <A1 media bucket>
   ```

2. Fill in `infra/cdk.json` (studentId, qutUsername, mediaBucketName, and
   `lambdaRoleArn` if CAB432 requires the pre-scoped role – when set, CDK creates
   no IAM resources and the role must already allow S3 read, Bedrock InvokeModel,
   s3vectors:PutVectors, DynamoDB PutItem and SSM GetParametersByPath).

3. Build and deploy:

   ```bash
   ./scripts/build_classifier.sh
   cd infra && pip install -r requirements.txt && cdk deploy
   ```

### Verify (this is also your live demo of the async workflow)

1. Upload a photo through the A1 flow (`POST /v1/uploads/url`, then PUT the file).
2. Watch `/aws/lambda/n1234567-strobe-classifier` in CloudWatch: a `classified`
   JSON line appears with labels, latency and queue lag.
3. The row is in `n1234567-strobe-classifications`; the vector is in the index
   (`aws s3vectors list-vectors --vector-bucket-name ... --index-name ... --return-metadata`).

### Design notes (useful for the presentation)

- **Idempotent**: vector key `image#<fileId>` and table PK `imageId` mean SQS
  redelivery just overwrites – no duplicates.
- **Partial batch failures**: only the failed message is retried; after 3
  receives it goes to the DLQ.
- **Back-pressure**: `MaximumConcurrency = 2` on the SQS event source keeps
  the worker from flooding the shared, throttled Bedrock quota; the queue
  absorbs bursts.
- **Config in Parameter Store**, not env vars: the Lambda only receives the SSM
  path and loads model IDs / index / table names at cold start.
- **Ownership in metadata**: `userId` is filterable on every vector, so the MCP
  server's search can be scoped to the caller.
- Images are EXIF-rotated, converted to RGB JPEG and capped at 2048 px (Titan's
  limit) before either model sees them.

### Tests

```bash
pip install pytest pillow boto3
pytest tests        # offline, no AWS calls
```

## Deploying without CDK

This course account cannot be CDK-bootstrapped (bootstrap creates IAM roles;
the student role cannot, and a half-created `CDKToolkit` stack is stuck in
DELETE_FAILED). `scripts/deploy_pipeline.py` creates exactly the same resources
with direct API calls instead, and is safe to re-run:

```bash
./scripts/build_classifier.sh
python scripts/deploy_pipeline.py --student-id n11457660 \
    --qut-username n11457660@qut.edu.au \
    --media-bucket n11457660-s3-media \
    --vector-bucket n11457660-strobe-vectors \
    --vector-index n11457660-strobe-content
```

It borrows the execution role from an existing A1 Lambda (or pass `--role-arn`).
It prints every ARN you need for the submission YAML, and re-running just
updates the function code. `infra/` is left in the repo for reference only.

## Agent (ACP) + MCP server

Two containers, one job each:

- `mcp_server/` (Python): read-only Strobe tools over MCP streamable HTTP.
  Verifies the caller's Cognito token against the pool's JWKS and scopes every
  tool to that `sub`. No tool takes a userId, so the model cannot name another
  user. Vector search hits S3 Vectors directly; posts and comments go through
  the A1 API with the user's own token, so A1's rules still apply.
- `agent/` (TypeScript): ACP server for the browser client, MCP client for data,
  Bedrock for the model. Built on the deterministic ACP server from the unit.

Gemma 3 accepts Bedrock's `toolConfig` but ignores it (verified: it answered a
weather question from memory and invented a date). So the agent runs its own
JSON tool protocol - the model returns one JSON object per step, which the
agent parses and dispatches. The smoke test scored 5/5 on tool choice.

### Run both locally

```bash
# terminal 1
cd mcp_server && pip install -r requirements.txt && python server.py

# terminal 2
cd agent && npm install
AGENT_MCP_URL=http://127.0.0.1:8000/mcp \
AGENT_DEV_TOKEN="$(curl -s -X POST $API/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"...","password":"..."}' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')" \
npm run dev

# terminal 3: drive it without a browser
node probe.mjs
```

`AGENT_DEV_TOKEN` is a local convenience only. In the browser the token arrives
in `_meta.strobeToken` on `session/new`, because the web client has no auth and
browsers cannot set WebSocket headers.

`BEDROCK_STUB=1` replaces the model with canned replies, for testing the ACP and
MCP plumbing without spending Bedrock quota.
