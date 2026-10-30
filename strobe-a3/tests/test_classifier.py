"""Offline unit tests for the classifier's pure helpers (no AWS calls)."""
import io
import json
import os
import sys

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "classifier"))

import pytest
from PIL import Image

import handler as h


def s3_event(key, bucket="n1234567-strobe-media", source="aws.s3", detail_type="Object Created"):
    return json.dumps({
        "source": source, "detail-type": detail_type,
        "detail": {"bucket": {"name": bucket}, "object": {"key": key, "size": 123}},
    })


def test_parse_event_valid_key():
    ref = h.parse_event_body(s3_event("user-1/post-2/abc123"))
    assert (ref.user_id, ref.post_id, ref.file_id, ref.key) == ("user-1", "post-2", "abc123", "user-1/post-2/abc123")


def test_parse_event_strips_extension():
    assert h.parse_event_body(s3_event("u/p/abc123.jpg")).file_id == "abc123"


@pytest.mark.parametrize("key", ["u/p", "u/p/f/extra", "u//f", "loose.jpg"])
def test_parse_event_rejects_bad_keys(key):
    assert h.parse_event_body(s3_event(key)) is None


def test_parse_event_ignores_other_events():
    assert h.parse_event_body(s3_event("u/p/f", detail_type="Object Deleted")) is None


def test_normalise_downsizes_and_converts():
    buf = io.BytesIO()
    Image.new("RGBA", (4000, 1000), (255, 0, 0, 128)).save(buf, format="PNG")
    jpeg, w, hgt = h.normalise_image(buf.getvalue())
    assert (w, hgt) == (2048, 512)
    assert jpeg[:2] == b"\xff\xd8"


def test_normalise_keeps_small_images():
    buf = io.BytesIO()
    Image.new("RGB", (300, 200)).save(buf, format="WEBP")
    _, w, hgt = h.normalise_image(buf.getvalue())
    assert (w, hgt) == (300, 200)


def test_parse_classification_clean():
    out = h.parse_classification('{"labels": ["Dog", "beach", "dog"], "caption": " A dog on sand. "}')
    assert out == {"labels": ["dog", "beach"], "caption": "A dog on sand."}


def test_parse_classification_fenced_with_prose():
    out = h.parse_classification('Sure!\n```json\n{"labels": ["cake"], "caption": "Cake."}\n```')
    assert out["labels"] == ["cake"]


@pytest.mark.parametrize("text", ["no json here", '{"caption": "x"}', '{"labels": [1, 2]}'])
def test_parse_classification_rejects(text):
    with pytest.raises(ValueError):
        h.parse_classification(text)


def test_handler_reports_partial_failures(monkeypatch):
    def fake_process(ref):
        if ref.file_id == "bad":
            raise RuntimeError("boom")
    monkeypatch.setattr(h, "process", fake_process)
    event = {"Records": [
        {"messageId": "1", "body": s3_event("u/p/good")},
        {"messageId": "2", "body": s3_event("u/p/bad")},
        {"messageId": "3", "body": s3_event("not-a-post-key")},
    ]}
    assert h.handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "2"}]}
