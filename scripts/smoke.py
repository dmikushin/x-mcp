#!/usr/bin/env python3
"""Live smoke test: exercise the whole stack against real Nitter mirrors.

Not part of pytest. Run with: uv run python scripts/smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import httpx

from xmcp.client import NitterClient
from xmcp.instances import USER_AGENT, InstancePool
from xmcp.media import MediaDownloader

MAGIC = {
    b"\xff\xd8": "jpeg",
    b"\x89PNG": "png",
    b"RIFF": "webp/gif-container",
    b"GIF8": "gif",
    b"\x00\x00\x00\x18ftyp": "mp4",
    b"\x00\x00\x00\x1cftyp": "mp4",
}


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"OK: {message}")


def main() -> int:
    http = httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=15.0,
    )
    pool = InstancePool(http)
    pool.load_cookies()

    print("=== 1. refresh instance pool ===")
    instances = pool.refresh()
    for inst in instances:
        print(
            f"  {inst.host:24} {inst.status.value:10} "
            f"{(str(inst.latency_ms) + 'ms') if inst.latency_ms else '-':>7} "
            f"search={'yes' if inst.search_enabled else 'no':3} points={inst.points} "
            f"{inst.failure_reason}"
        )
    usable = pool.usable()
    check(len(usable) >= 1, f"at least one usable instance ({len(usable)})")
    check(
        any(i.search_enabled for i in pool.searchable()),
        f"at least one instance with search ({len(pool.searchable())})",
    )

    client = NitterClient(pool)

    print("\n=== 2. user NASA ===")
    profile, tweets, cursor, served = client.user_timeline("NASA", limit=10)
    check(profile.card is not None, "NASA profile card parsed")
    check(profile.card.username == "NASA", f"username is {profile.card.username}")
    check(len(tweets) >= 1, f"got {len(tweets)} tweets from {served.host}")
    check(tweets[0].id and tweets[0].url.startswith("https://x.com/"), "first tweet has id/url")
    check(tweets[0].date is not None, f"first tweet date = {tweets[0].date}")
    print(f"  served by {served.host}; next cursor: {cursor!r}")
    for tweet in tweets[:3]:
        text = tweet.text.replace("\n", " ")[:80]
        print(f"  @{tweet.author} {tweet.date} :: {text}")

    print("\n=== 3. one tweet with replies ===")
    tweet_id = tweets[0].id
    page, served = client.tweet(tweet_id)
    check(page.main is not None, f"main tweet {tweet_id} found")
    check(page.main.id == tweet_id, "main tweet id matches request")
    thread_parts = len(page.before) + len(page.after)
    print(
        f"  served by {served.host}; replies={len(page.replies)} "
        f"thread parts={thread_parts}"
    )
    for reply in page.replies[:3]:
        text = reply.text.replace("\n", " ")[:60]
        print(f"  reply @{reply.author}: {text}")

    print("\n=== 4. search ===")
    results, cursor, served = client.search("nasa", limit=10)
    check(len(results) >= 1, f"search returned {len(results)} tweets from {served.host}")
    check(all(t.author for t in results), "all search results have an author")
    print(f"  served by {served.host}; next cursor: {cursor!r}")

    print("\n=== 5. download media of a tweet with photos ===")
    photo_tweet = next(
        (t for t in tweets if any(m.kind == "photo" for m in t.media)),
        next((t for t in results if any(m.kind == "photo" for m in t.media)), None),
    )
    check(photo_tweet is not None, "found a tweet with photos")
    page, served = client.tweet(photo_tweet.id, include_replies=False)
    tweets_for_media = ([page.main] if page.main else []) + page.before + page.after
    out_dir = Path(tempfile.mkdtemp(prefix="x-mcp-smoke-"))
    downloader = MediaDownloader(pool.client, instance_url=served.url)
    saved = []
    for tweet in tweets_for_media:
        for index, media in enumerate(tweet.media, start=1):
            records = downloader.download(media, tweet.id, out_dir, index)
            saved.extend(records)
            print(f"  {tweet.id} media {index}: {records}")
    downloaded = [r["path"] for r in saved if "path" in r]
    check(len(downloaded) >= 1, f"downloaded {len(downloaded)} file(s)")
    for path in downloaded:
        data = Path(path).read_bytes()
        kind = next((name for magic, name in MAGIC.items() if data.startswith(magic)), None)
        check(kind is not None, f"{path} is a valid {kind} ({len(data)} bytes)")

    print("\n=== 6. cache ===")
    print(f"  instance cache: {pool.cache_path} ({pool.cache_path.stat().st_size} bytes)")
    print(f"  cookie cache:   {pool.cookie_file()}")
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
