# Strobe A3: what changed and how to deploy it

A3 adds the three patterns A2 didn't cover: **auto scaling**, **load distribution** (neither counts if it's done by Lambda) and **caching**. Everything else from A1/A2 is reused unchanged.

| Pattern | What implements it | Where |
|---|---|---|
| Worker-pull load distribution | ECS Fargate workers long-poll the classify queue, one message per task | `worker/worker.py` |
| Metric-based auto scaling | CloudWatch alarms on queue depth drive a step policy that scales the workers from 0 to 5 tasks | `scripts/deploy_worker.py` |
| Edge caching | CloudFront with origin access control in front of the private bucket, serving `thumbs/*` only | `scripts/deploy_cdn.py` |
| In-memory caching | An LRU+TTL cache of query embeddings inside the MCP server | `mcp_server/server.py` (`EmbeddingCache`) |
| Garbage collection | `load_test.py --cleanup`, plus 14-day log retention | `scripts/load_test.py` |
| Monitoring | A dashboard showing queue depth vs. tasks, the DLQ, CPU and CDN traffic | `scripts/make_dashboard.py` |

The A2 classifier Lambda is retired. Its SQS trigger is disabled, but it is kept so that `deploy_worker.py --rollback` can switch back to it.

Code changes:

- `classifier/handler.py` now also writes a 640 px thumbnail, uses adaptive Bedrock retries and ignores `thumbs/` keys.
- The EventBridge rule now excludes `thumbs/`, so thumbnails don't loop back onto the queue.
- `get_image_urls` returns CDN thumbnail URLs, falling back to presigned S3 URLs.

## Run order (from this folder, with your AWS credentials loaded)

```bash
pip install boto3 pillow pytest
pytest tests                                   # 26 offline tests

# 0. Can the account do it? Send me the output if anything says FAIL.
python scripts/probe_a3.py --student-id n11457660 --qut-username n11457660@qut.edu.au

# 1. Retag everything reused from A1/A2
python scripts/retag_a3.py --qut-username n11457660@qut.edu.au --dry-run
python scripts/retag_a3.py --qut-username n11457660@qut.edu.au

# 2. Worker pool + auto scaling (needs Docker running)
python scripts/deploy_worker.py --student-id n11457660 --qut-username n11457660@qut.edu.au
python scripts/load_test.py --count 5          # smoke test: watch it go 0 -> 1 -> 0
aws logs tail /ecs/n11457660-strobe-worker --follow --region ap-southeast-2

# 3. CDN, then redeploy the MCP server so it picks up the CDN domain
python scripts/deploy_cdn.py --student-id n11457660 --qut-username n11457660@qut.edu.au --backfill --wait
python scripts/deploy_services.py --student-id n11457660 --qut-username n11457660@qut.edu.au \
    --webui-dir <your acp_client folder> --only mcp
python scripts/refresh_dns.py --student-id n11457660   # the MCP IP changed, so this redeploys the agent

# 4. Evidence
python scripts/make_dashboard.py --student-id n11457660
python scripts/load_test.py --count 150 --photos ~/Pictures/<some photos>   # screen-record the dashboard
curl -sI https://<cdn-domain>/thumbs/<userId>/<postId>/<fileId>.jpg | grep -i x-cache   # Miss, then Hit

# 5. Submission YAML
python scripts/make_submission_yaml.py --student-id n11457660 --qut-username n11457660@qut.edu.au > a3-submission.yaml
```

## Things to know

- **Timing:**
  - SQS publishes metrics once a minute, so expect roughly 1–2 minutes from upload to the first task: the alarm period plus Fargate start-up.
  - After the queue empties, the workers take about 3–4 minutes to scale back to 0.
  - A queue that has been idle for hours can take longer to start reporting again, so send a few images first before you record.
- **Bedrock is the real bottleneck.** More workers mean more parallel calls against a shared, throttled quota. Adaptive retries absorb throttling, and `--max-tasks` caps the fan-out. This is a good trade-off to discuss in the report.
- **Spot:** the workers run about 80% on Fargate Spot, which is roughly 70% cheaper. If Spot reclaims a task, it gets SIGTERM, finishes its current image and exits. Anything unfinished simply reappears on the queue.
- **Only thumbnails are public through the CDN, via unguessable URLs.** Originals still need an ownership-checked presigned URL. CloudFront signed URLs would be the upgrade; write this up under limitations.
