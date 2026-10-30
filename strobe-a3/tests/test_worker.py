"""Offline tests for the A3 worker loop and thumbnail helpers (no AWS calls)."""
import io
import json
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "classifier"))
sys.path.insert(0, os.path.join(HERE, "..", "worker"))

import pytest
from PIL import Image

import handler as h
import worker as w


def s3_event(key):
    return json.dumps({"source": "aws.s3", "detail-type": "Object Created",
                       "detail": {"bucket": {"name": "b"}, "object": {"key": key}}})


def jpeg(width, height):
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 50, 50)).save(out, format="JPEG")
    return out.getvalue()


class FakeSQS:
    def __init__(self):
        self.deleted, self.visibility = [], []

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):
        self.visibility.append((ReceiptHandle, VisibilityTimeout))


def message(key, receives="1"):
    return {"ReceiptHandle": "r1", "Body": s3_event(key), "Attributes": {"ApproximateReceiveCount": receives}}


# ---------------------------------------------------------------- thumbnails
def test_thumbnail_is_bounded_and_keeps_aspect():
    with Image.open(io.BytesIO(h.make_thumbnail(jpeg(2000, 1000)))) as img:
        assert img.size == (640, 320) and img.format == "JPEG"


def test_thumbnail_never_upscales():
    with Image.open(io.BytesIO(h.make_thumbnail(jpeg(300, 200)))) as img:
        assert img.size == (300, 200)


def test_thumb_key_layout():
    ref = h.ImageRef("b", "u/p/f1", "u", "p", "f1")
    assert h.thumb_key(ref) == "thumbs/u/p/f1.jpg"


def test_thumbnails_are_not_reclassified():
    assert h.parse_event_body(s3_event("thumbs/u/p/f1.jpg")) is None


# ---------------------------------------------------------------- worker loop
def test_success_deletes_message(monkeypatch):
    seen = []
    monkeypatch.setattr(h, "process", lambda ref: seen.append(ref.file_id) or {"latencyMs": 5})
    sqs = FakeSQS()
    assert w.handle(sqs, "q", message("u/p/f1")) == "processed"
    assert seen == ["f1"] and sqs.deleted == ["r1"] and not sqs.visibility


def test_ignored_message_is_deleted_without_processing(monkeypatch):
    monkeypatch.setattr(h, "process", lambda ref: pytest.fail("should not process"))
    sqs = FakeSQS()
    assert w.handle(sqs, "q", message("thumbs/u/p/f1.jpg")) == "ignored"
    assert sqs.deleted == ["r1"]


def test_failure_keeps_message_and_backs_off(monkeypatch):
    def boom(ref):
        raise RuntimeError("bedrock throttled")
    monkeypatch.setattr(h, "process", boom)
    sqs = FakeSQS()
    assert w.handle(sqs, "q", message("u/p/f1", receives="2")) == "retry"
    assert sqs.deleted == [] and sqs.visibility == [("r1", 60)]


@pytest.mark.parametrize("receives,delay", [(1, 15), (2, 60), (3, 180), (9, 180)])
def test_backoff_grows_then_caps(receives, delay):
    assert w.backoff_for(receives) == delay
