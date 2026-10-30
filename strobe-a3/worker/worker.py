"""Strobe A3 classification worker: the worker-pull half of the pipeline.

    S3 upload --EventBridge--> SQS queue <--long-poll-- N of these (ECS Fargate)

Each task pulls ONE message at a time, runs the same classify/embed/thumbnail
code the A2 Lambda ran (classifier/handler.py), and deletes the message only
after every write has succeeded. The number of tasks is not decided here: an
Application Auto Scaling policy watches the queue depth in CloudWatch and sets
the service's desired count between 0 and N (see scripts/deploy_worker.py).

Failure handling, all of it SQS-native so there is no state in the worker:
  - success                 -> DeleteMessage
  - ignorable / object gone -> DeleteMessage (retrying cannot help)
  - any other error         -> ChangeMessageVisibility to a growing back-off,
                               so the retry comes back sooner than the 9-minute
                               visibility timeout; after 3 receives the queue's
                               redrive policy moves it to the DLQ.
  - SIGTERM (scale-in, Spot reclaim, redeploy)
                            -> finish the current message, then exit. Anything
                               unfinished simply becomes visible again.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import urllib.request

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handler  # noqa: E402  (classifier/handler.py, copied in by the Dockerfile)

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("strobe-worker")

WAIT_SECONDS = 20                    # SQS long polling: no busy-looping, no empty-receive cost
RETRY_BACKOFF = [15, 60, 180]        # seconds before the 1st, 2nd, 3rd retry becomes visible

_stopping = False


def _request_stop(signum, _frame) -> None:
    global _stopping
    _stopping = True
    log.info(json.dumps({"msg": "stop requested", "signal": signum, "worker": WORKER_ID}))


def worker_id() -> str:
    """Short ECS task id, so the logs show which task handled which image."""
    uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if uri:
        try:
            with urllib.request.urlopen(f"{uri}/task", timeout=2) as r:
                return json.load(r)["TaskARN"].rsplit("/", 1)[-1][:8]
        except Exception:  # noqa: BLE001 - metadata is a nicety, never fatal
            pass
    return socket.gethostname()[:12]


WORKER_ID = worker_id()


def backoff_for(receive_count: int) -> int:
    return RETRY_BACKOFF[min(max(receive_count, 1), len(RETRY_BACKOFF)) - 1]


def handle(sqs, queue_url: str, message: dict) -> str:
    """Process one message; returns the outcome for logging and tests."""
    receipt = message["ReceiptHandle"]
    receives = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
    try:
        ref = handler.parse_event_body(message["Body"])
        if ref is None:
            outcome = "ignored"
            try:
                body = json.loads(message["Body"])
                what = body.get("detail", {}).get("object", {}).get("key") or body.get("detail-type")
            except (ValueError, AttributeError):
                what = "unparseable body"
            log.info(json.dumps({"msg": "ignored", "worker": WORKER_ID, "what": what}))
        else:
            summary = handler.process(ref)
            log.info(json.dumps({"msg": "done", "worker": WORKER_ID, "imageId": ref.file_id,
                                 "latencyMs": summary.get("latencyMs"),
                                 "queueLagSec": summary.get("queueLagSec"),
                                 "attempt": receives}))
            outcome = "processed"
    except handler._s3.exceptions.NoSuchKey:
        outcome = "gone"
    except Exception as e:  # noqa: BLE001 - every failure goes back to the queue
        delay = backoff_for(receives)
        log.exception(json.dumps({"msg": "failed", "worker": WORKER_ID, "attempt": receives,
                                  "retryInSec": delay, "error": type(e).__name__}))
        try:
            sqs.change_message_visibility(QueueUrl=queue_url, ReceiptHandle=receipt,
                                          VisibilityTimeout=delay)
        except Exception:  # noqa: BLE001 - worst case it reappears after the full timeout
            log.warning("could not shorten visibility; message reappears after the queue timeout")
        return "retry"

    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
    return outcome


def main() -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    handler.load_config()                       # fail fast if Parameter Store is unreachable
    queue_url = os.environ.get("QUEUE_URL") or handler.param("queue-url")
    if not queue_url:
        sys.exit("no queue-url in Parameter Store and no QUEUE_URL set")
    sqs = boto3.client("sqs")
    log.info(json.dumps({"msg": "worker started", "worker": WORKER_ID, "queue": queue_url.rsplit("/", 1)[-1]}))

    processed = 0
    while not _stopping:
        response = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,              # one in flight per task keeps "queue depth" == "work per task"
            WaitTimeSeconds=WAIT_SECONDS,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        for message in response.get("Messages", []):
            if handle(sqs, queue_url, message) == "processed":
                processed += 1

    log.info(json.dumps({"msg": "worker stopped cleanly", "worker": WORKER_ID, "processed": processed}))


if __name__ == "__main__":
    main()
