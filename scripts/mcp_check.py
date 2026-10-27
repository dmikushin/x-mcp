#!/usr/bin/env python3
"""Start x-mcp over stdio with the official MCP client SDK and exercise it.

Not part of pytest. Run with: uv run python scripts/mcp_check.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"OK: {message}")


def text_of(result) -> dict:
    for block in result.content:
        if block.type == "text":
            return json.loads(block.text)
    return {}


async def run() -> int:
    params = StdioServerParameters(command="uv", args=["run", "x-mcp"])

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        info = init.server_info
        print(f"server: {info.name} v{info.version}")

        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("tools:", names)
        expected = {
            "x_instances",
            "x_user_timeline",
            "x_tweet",
            "x_search",
            "x_download_media",
        }
        check(set(names) == expected, f"all five tools exposed ({names})")
        for tool in tools.tools:
            first_line = (tool.description or "").splitlines()[0]
            print(f"  {tool.name}: {first_line}")

        print("\n=== x_instances ===")
        instances = text_of(await session.call_tool("x_instances", {"refresh": True}))
        check(instances.get("usable"), f"usable instances: {instances['usable']}")
        check(instances.get("searchable"), f"searchable: {instances['searchable']}")

        print("\n=== x_user_timeline NASA ===")
        timeline = text_of(
            await session.call_tool("x_user_timeline", {"username": "NASA", "limit": 5})
        )
        check("error" not in timeline, "no error returned")
        check(len(timeline["tweets"]) >= 1, f"{len(timeline['tweets'])} tweets")
        check(timeline["profile"]["username"] == "NASA", "profile parsed")
        check(bool(timeline["instance"]), f"served by {timeline['instance']}")
        check(timeline["next_cursor"] is not None, "next_cursor present")
        first = timeline["tweets"][0]
        print(f"  first: @{first['author']} {first['date']} :: {first['text'][:60]}")
        print(f"  stats: {first['stats']}")

        tweet_id = first["id"]
        media_tweet = next(
            (
                t
                for t in timeline["tweets"]
                if any(m["kind"] == "photo" for m in t["media"])
            ),
            None,
        )

        print("\n=== x_tweet (with replies) ===")
        tweet = text_of(await session.call_tool("x_tweet", {"id_or_url": tweet_id}))
        check(tweet.get("main") is not None, f"main tweet {tweet_id} fetched")
        thread_parts = len(tweet["before"]) + len(tweet["after"])
        print(
            f"  replies: {len(tweet['replies'])}, thread parts: {thread_parts}, "
            f"instance: {tweet['instance']}"
        )

        print("\n=== x_search ===")
        search = text_of(
            await session.call_tool("x_search", {"query": "artemis", "limit": 5})
        )
        check("error" not in search, "no error returned")
        check(
            len(search["results"]) >= 1,
            f"{len(search['results'])} results from {search['instance']}",
        )

        print("\n=== x_download_media with return_images ===")
        out_dir = tempfile.mkdtemp(prefix="x-mcp-mcp-check-")
        media_id = media_tweet["id"] if media_tweet else tweet_id
        result = await session.call_tool(
            "x_download_media",
            {"id_or_url": media_id, "directory": out_dir, "return_images": True},
        )
        payload = text_of(result)
        check("error" not in payload, "no error returned")
        check(len(payload["saved"]) >= 1, f"saved {len(payload['saved'])} file(s)")
        images = [block for block in result.content if block.type == "image"]
        check(len(images) >= 1, f"returned {len(images)} MCP ImageContent block(s)")
        for image in images:
            raw = base64.b64decode(image.data)
            check(len(raw) > 1000, f"image {image.mime_type} decodes to {len(raw)} bytes")
        for path in payload["saved"]:
            print(f"  {path} ({Path(path).stat().st_size} bytes)")
        if payload["skipped"]:
            print(f"  skipped: {payload['skipped']}")

        print("\nALL MCP CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
