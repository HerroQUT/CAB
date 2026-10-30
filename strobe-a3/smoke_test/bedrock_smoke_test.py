"""Bedrock smoke test for CAB432 A2 (Track A).

Run with CAB432 credentials in ap-southeast-2:

    pip install boto3 pillow
    python bedrock_smoke_test.py

It answers the questions that decide how the agent is built:
  1. Does Gemma respond at all via the Converse API?
  2. Does Gemma accept a system prompt?
  3. Does Gemma support native Converse tool use (toolConfig)?
  4. If not, can Gemma reliably emit a JSON "tool request" we parse ourselves?
  5. Can Gemma see an image (vision via Converse)?
  6. Does Nemotron support native tool use (fallback "brain")?
  7. Do Titan Multimodal text and image embeddings land in the same space?

Nothing is written to your account; it only makes model calls.
"""
from __future__ import annotations

import base64
import io
import json
import math
import re
import sys

import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageDraw

REGION = "ap-southeast-2"
GEMMA = "google.gemma-3-12b-it"
NEMOTRON = "nvidia.nemotron-super-3-120b"
TITAN_MM = "amazon.titan-embed-image-v1"

brt = boto3.client("bedrock-runtime", region_name=REGION)
results: dict[str, str] = {}

WEATHER_TOOL = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_weather",
                "description": "Get the current weather for a city.",
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    }
                },
            }
        }
    ]
}


def record(name: str, ok: bool, detail: str) -> None:
    results[name] = ("PASS" if ok else "FAIL") + f" - {detail}"
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def text_of(response: dict) -> str:
    blocks = response["output"]["message"]["content"]
    return "".join(b.get("text", "") for b in blocks).strip()


def test_image_bytes() -> bytes:
    """A red circle on a white background, as JPEG."""
    image = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(image).ellipse((48, 48, 208, 208), fill="red")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def extract_json(text: str) -> dict | None:
    """Pull the first {...} object out of a model reply (tolerates ```json fences)."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


# 1. Plain text ----------------------------------------------------------------
def t1_gemma_text() -> None:
    try:
        r = brt.converse(
            modelId=GEMMA,
            messages=[{"role": "user", "content": [{"text": "Reply with exactly: pong"}]}],
            inferenceConfig={"maxTokens": 20, "temperature": 0},
        )
        reply = text_of(r)
        record("gemma_text", "pong" in reply.lower(), repr(reply))
    except ClientError as e:
        record("gemma_text", False, str(e))


# 2. System prompt ---------------------------------------------------------------
def t2_gemma_system() -> None:
    try:
        r = brt.converse(
            modelId=GEMMA,
            system=[{"text": "You are a pirate. Always start replies with 'Arr'."}],
            messages=[{"role": "user", "content": [{"text": "Say hello."}]}],
            inferenceConfig={"maxTokens": 40, "temperature": 0},
        )
        reply = text_of(r)
        record("gemma_system_prompt", reply.lower().startswith("arr"), repr(reply[:80]))
    except ClientError as e:
        record("gemma_system_prompt", False, f"system prompts rejected: {e}")


# 3. Native tool use -------------------------------------------------------------
def native_tool_test(model_id: str, name: str) -> None:
    try:
        r = brt.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": "What's the weather in Brisbane?"}]}],
            toolConfig=WEATHER_TOOL,
            inferenceConfig={"maxTokens": 200, "temperature": 0},
        )
        calls = [b["toolUse"] for b in r["output"]["message"]["content"] if "toolUse" in b]
        ok = r.get("stopReason") == "tool_use" and bool(calls)
        detail = f"stopReason={r.get('stopReason')} calls={json.dumps(calls)}" if calls else \
            f"stopReason={r.get('stopReason')} text={text_of(r)[:120]!r}"
        record(name, ok, detail)
    except ClientError as e:
        record(name, False, f"toolConfig rejected: {e}")


# 4. JSON tool fallback ----------------------------------------------------------
JSON_TOOL_PROMPT = """You can call tools. Available tools:
- get_weather(city: string): current weather for a city.
- search_my_photos(query: string): semantic search over the user's photos.

If a tool is needed, reply with ONLY a JSON object and nothing else:
{"tool": "<name>", "arguments": {...}}
If no tool is needed, reply with ONLY: {"tool": null, "answer": "<your reply>"}

User: """

JSON_CASES = [
    ("What's the weather in Brisbane?", "get_weather"),
    ("find me the photos of my dogs", "search_my_photos"),
    ("Show my beach pictures from last summer", "search_my_photos"),
    ("What is 2 + 2?", None),
    ("Is it raining in Sydney right now?", "get_weather"),
]


def t4_gemma_json_tools() -> None:
    passed = 0
    for question, expected in JSON_CASES:
        try:
            r = brt.converse(
                modelId=GEMMA,
                messages=[{"role": "user", "content": [{"text": JSON_TOOL_PROMPT + question}]}],
                inferenceConfig={"maxTokens": 200, "temperature": 0},
            )
            parsed = extract_json(text_of(r))
            if parsed is not None and parsed.get("tool") == expected:
                passed += 1
            else:
                print(f"    miss: {question!r} -> {text_of(r)[:120]!r}")
        except ClientError as e:
            print(f"    error: {e}")
    record("gemma_json_tools", passed >= 4, f"{passed}/{len(JSON_CASES)} correct")


# 5. Vision ----------------------------------------------------------------------
def t5_gemma_vision() -> None:
    try:
        r = brt.converse(
            modelId=GEMMA,
            messages=[{
                "role": "user",
                "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": test_image_bytes()}}},
                    {"text": "What shape and colour is in this image? Answer in five words or fewer."},
                ],
            }],
            inferenceConfig={"maxTokens": 30, "temperature": 0},
        )
        reply = text_of(r).lower()
        record("gemma_vision", "red" in reply and "circle" in reply, repr(reply))
    except ClientError as e:
        record("gemma_vision", False, str(e))


# 7. Titan multimodal embeddings -------------------------------------------------
def titan_embed(*, text: str | None = None, image: bytes | None = None) -> list[float]:
    body: dict = {"embeddingConfig": {"outputEmbeddingLength": 1024}}
    if text is not None:
        body["inputText"] = text
    if image is not None:
        body["inputImage"] = base64.b64encode(image).decode()
    r = brt.invoke_model(modelId=TITAN_MM, body=json.dumps(body),
                         contentType="application/json", accept="application/json")
    return json.loads(r["body"].read())["embedding"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def t7_titan_shared_space() -> None:
    try:
        img = titan_embed(image=test_image_bytes())
        match = cosine(img, titan_embed(text="a red circle"))
        miss = cosine(img, titan_embed(text="a golden retriever puppy"))
        record("titan_mm_shared_space", len(img) == 1024 and match > miss,
               f"dim={len(img)} sim('a red circle')={match:.3f} sim('a puppy')={miss:.3f}")
    except ClientError as e:
        record("titan_mm_shared_space", False, str(e))


def main() -> int:
    print(f"Region {REGION}\n")
    t1_gemma_text()
    t2_gemma_system()
    native_tool_test(GEMMA, "gemma_native_tools")
    t4_gemma_json_tools()
    t5_gemma_vision()
    native_tool_test(NEMOTRON, "nemotron_native_tools")
    t7_titan_shared_space()

    print("\n=== Summary ===")
    for name, outcome in results.items():
        print(f"{name:26s} {outcome}")

    def ok(name: str) -> bool:
        return results.get(name, "").startswith("PASS")

    print("\n=== Recommendation ===")
    if ok("gemma_native_tools"):
        print("Option A: Gemma for everything, native Converse tool use.")
    elif ok("gemma_json_tools"):
        print("Option B: Gemma for everything; agent parses JSON tool requests itself.")
    elif ok("nemotron_native_tools"):
        print("Option C: Nemotron picks tools (text); Gemma only for vision.")
    else:
        print("No tool strategy passed - paste this output back for a rethink.")
    if not ok("gemma_system_prompt"):
        print("Note: put instructions in the first user message; Gemma ignored/rejected system prompts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
