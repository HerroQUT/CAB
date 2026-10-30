"""Exercise the MCP server the way the agent will.

Logs in to Strobe for a real Cognito token, connects over streamable HTTP,
lists the tools and calls the important ones.

    # terminal 1
    cd mcp_server && python server.py

    # terminal 2
    python scripts/mcp_smoke.py --email you@example.com --password '...'
    python scripts/mcp_smoke.py --email ... --password ... --url http://<ecs-ip>:8000/mcp
"""
from __future__ import annotations

import argparse
import asyncio
import json

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

API = "https://zu3y6kfvnh.execute-api.ap-southeast-2.amazonaws.com"


def login(email: str, password: str) -> str:
    r = httpx.post(f"{API}/v1/auth/login", json={"email": email, "password": password}, timeout=20)
    r.raise_for_status()
    data = r.json()
    # A1 returns the token under one of a few shapes depending on the handler.
    for key in ("accessToken", "token", "idToken"):
        if isinstance(data.get(key), str):
            return data[key]
        if isinstance(data.get("tokens"), dict) and key in data["tokens"]:
            return data["tokens"][key]
    raise SystemExit(f"could not find a token in the login response: {json.dumps(data)[:300]}")


def show(title: str, result) -> None:
    print(f"\n--- {title}")
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            print(text[:1200])


async def run(url: str, token: str, query: str) -> None:
    # mcp 2.x transports run on httpx2, not httpx
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=60)
    async with streamable_http_client(url, http_client=client) as (read, write):
        async with ClientSession(read, write) as mcp:
            await mcp.initialize()

            tools = await mcp.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            search = await mcp.call_tool("search_my_content", {"query": query, "top_k": 5})
            show(f"search_my_content({query!r})", search)

            image_id = None
            for block in search.content:
                try:
                    hits = json.loads(getattr(block, "text", "") or "[]")
                    if isinstance(hits, list) and hits:
                        image_id = hits[0].get("imageId")
                except json.JSONDecodeError:
                    continue

            if image_id:
                show("get_classification", await mcp.call_tool("get_classification", {"image_id": image_id}))
                show("get_image_urls", await mcp.call_tool("get_image_urls", {"image_ids": [image_id]}))

            show("list_my_posts", await mcp.call_tool("list_my_posts", {"limit": 3}))
            show("get_agent_runs", await mcp.call_tool("get_agent_runs", {"limit": 3}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    ap.add_argument("--query", default="photos of my dogs")
    args = ap.parse_args()

    token = login(args.email, args.password)
    print(f"got a token ({len(token)} chars)")
    asyncio.run(run(args.url, token, args.query))


if __name__ == "__main__":
    main()
