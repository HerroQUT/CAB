"""Scheduled retrospectives for Strobe.

EventBridge triggers this daily. Nobody asks for it: that is the point. For each
user with classified photos it writes a short recap ("on this day", or a recent
round-up) and records the run.

Memory: every run reads the summaries of that user's previous runs and passes
them to the model, so today's recap can build on yesterday's instead of
repeating it. The run record stores `inputRunIds`, which is the audit trail of
what this run built on.

The chat agent surfaces these through the MCP tool get_agent_runs.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key

log = logging.getLogger()
log.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-southeast-2")
CLASSIFICATIONS_TABLE = os.environ["CLASSIFICATIONS_TABLE"]
AGENT_RUNS_TABLE = os.environ["AGENT_RUNS_TABLE"]
CHAT_MODEL_ID = os.environ["CHAT_MODEL_ID"]
MAX_USERS = int(os.environ.get("MAX_USERS", "10"))
RECENT_DAYS = int(os.environ.get("RECENT_DAYS", "7"))

_ddb = boto3.resource("dynamodb", region_name=REGION)
_brt = boto3.client("bedrock-runtime", region_name=REGION)


def previous_runs(user_id: str, limit: int = 5) -> list[dict]:
    table = _ddb.Table(AGENT_RUNS_TABLE)
    response = table.query(
        KeyConditionExpression=Key("userId").eq(user_id),
        ScanIndexForward=False,
        Limit=limit,
    )
    return response.get("Items", [])


def photos_for(user_id: str) -> list[dict]:
    """This user's classified photos, newest first."""
    table = _ddb.Table(CLASSIFICATIONS_TABLE)
    response = table.query(
        IndexName="byUser",
        KeyConditionExpression=Key("userId").eq(user_id),
        ScanIndexForward=False,
        Limit=50,
    )
    return response.get("Items", [])


def pick_subject(photos: list[dict], now: datetime) -> tuple[str, list[dict]]:
    """Choose what this run is about: an anniversary if there is one, else recent activity."""
    on_this_day = []
    for photo in photos:
        stamp = photo.get("uploadedAt") or photo.get("classifiedAt") or ""
        try:
            when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.year < now.year and (when.month, when.day) == (now.month, now.day):
            on_this_day.append(photo)
    if on_this_day:
        return "on_this_day", on_this_day[:8]

    cutoff = now - timedelta(days=RECENT_DAYS)
    recent = []
    for photo in photos:
        stamp = photo.get("classifiedAt", "")
        try:
            if datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= cutoff:
                recent.append(photo)
        except ValueError:
            continue
    return "recent_recap", recent[:8]


def write_summary(kind: str, photos: list[dict], earlier: list[dict]) -> str:
    lines = [f"- {p.get('caption', '')} [{', '.join(p.get('labels', [])[:4])}]" for p in photos]
    memory = "\n".join(f"- {r.get('runAt', '')[:10]}: {r.get('summary', '')}" for r in earlier)
    intro = ("These photos are from this date in an earlier year."
             if kind == "on_this_day" else
             f"These photos were added in the last {RECENT_DAYS} days.")
    prompt = (
        "You write short retrospectives for someone's own photo library.\n"
        f"{intro}\n\nPhotos:\n" + "\n".join(lines) +
        (f"\n\nYou wrote these previously. Do not repeat them; add something new or note a change:\n{memory}"
         if memory else "") +
        "\n\nWrite 2 to 3 warm, plain sentences addressed to the owner. No headings, no lists, "
        "no invented details: mention only what the photos show."
    )
    response = _brt.converse(
        modelId=CHAT_MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 300, "temperature": 0.4},
    )
    text = "".join(b.get("text", "") for b in response["output"]["message"]["content"])
    # Gemma occasionally emits a stray brace; strip them from the prose.
    return re.sub(r"[{}]", "", re.sub(r"\s+", " ", text)).strip()


def active_users() -> list[str]:
    """Users who have classified photos. Fine at course scale; a production
    version would page the users table instead of scanning."""
    table = _ddb.Table(CLASSIFICATIONS_TABLE)
    counts: dict[str, int] = defaultdict(int)
    kwargs = {"ProjectionExpression": "userId"}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            counts[item["userId"]] += 1
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    ranked = sorted(counts, key=lambda u: counts[u], reverse=True)
    return ranked[:MAX_USERS]


def run_for_user(user_id: str, now: datetime) -> dict:
    photos = photos_for(user_id)
    kind, chosen = pick_subject(photos, now)
    earlier = previous_runs(user_id)

    record = {
        "userId": user_id,
        "runAt": now.isoformat().replace("+00:00", "Z"),
        "runId": str(uuid.uuid4()),
        "type": kind,
        "trigger": "schedule",
        "modelId": CHAT_MODEL_ID,
        "inputRunIds": [r["runId"] for r in earlier if "runId" in r],
        "imageIds": [p["imageId"] for p in chosen],
        "postIds": sorted({p["postId"] for p in chosen if p.get("postId")}),
    }

    if not chosen:
        record["status"] = "skipped"
        record["summary"] = "Nothing new to look back on."
    else:
        try:
            record["summary"] = write_summary(kind, chosen, earlier)
            record["status"] = "ok"
        except Exception as e:                      # a bad run is still a run
            log.exception("summary failed for %s", user_id)
            record["status"] = "failed"
            record["summary"] = f"Could not generate a recap: {e}"

    _ddb.Table(AGENT_RUNS_TABLE).put_item(Item=record)
    log.info(json.dumps({
        "msg": "agent_run", "userId": user_id, "type": kind, "status": record["status"],
        "photos": len(chosen), "builtOn": len(record["inputRunIds"]),
    }))
    return record


def handler(event: dict, _context) -> dict:
    now = datetime.now(timezone.utc)
    # A manual invoke may name one user, which is how the demo shows a run on demand.
    users = [event["userId"]] if isinstance(event, dict) and event.get("userId") else active_users()
    results = [run_for_user(user_id, now) for user_id in users]
    return {
        "ranAt": now.isoformat(),
        "users": len(results),
        "summaries": [{"userId": r["userId"], "type": r["type"], "summary": r["summary"]} for r in results],
    }
