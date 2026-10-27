"""MCP stdio server exposing the xmcp core library to AI agents.

Every tool calls the same functions the CLI uses; nothing is shelled out.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from pathlib import Path

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent

from .client import FetchError, NitterClient
from .instances import USER_AGENT, InstancePool
from .media import DEFAULT_DIR, MediaDownloader

# Built once per server process and reused across tool calls; the probe only
# happens on the first tool call so importing the module does no network I/O.
_pool: InstancePool | None = None
_client: NitterClient | None = None


def get_client() -> NitterClient:
    global _pool, _client
    if _client is None:
        http = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
            timeout=15.0,
        )
        _pool = InstancePool(http)
        _pool.load_cookies()
        _pool.ensure()
        _client = NitterClient(_pool)
    return _client


def _dump(obj):
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return obj


def _json(payload) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, default=_dump, indent=2))]


def _error(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": message}, indent=2))]


mcp = MCPServer(
    name="x-mcp",
    description=(
        "Read X/Twitter without an account. All data is fetched through public "
        "Nitter mirrors; pagination uses opaque cursor strings returned by each "
        "previous call."
    ),
)


@mcp.tool()
def x_instances(refresh: bool = False) -> list[TextContent]:
    """List the Nitter mirror instances available for fetching.

    Returns host, status (ok/anubis/gandalf/cloudflare/forbidden/error/timeout),
    latency in ms, whether search is enabled, points and source. Instances are
    ranked by usability. Set refresh=true to re-probe them live.
    """
    client = get_client()
    client.pool.ensure(force=refresh)
    return _json(
        {
            "instances": [_dump(inst) for inst in client.pool.instances],
            "usable": [inst.host for inst in client.pool.usable()],
            "searchable": [inst.host for inst in client.pool.searchable()],
        }
    )


@mcp.tool()
def x_user_timeline(
    username: str,
    kind: str = "tweets",
    cursor: str | None = None,
    limit: int = 20,
    instance: str | None = None,
) -> list[TextContent]:
    """Fetch a user's timeline from a public Nitter mirror.

    kind is one of "tweets", "replies" or "media". Pass the `next_cursor`
    value from a previous call to paginate. Returns the profile card, the
    tweets, the next cursor and the instance that served the request.
    """
    try:
        profile, tweets, next_cursor, served = get_client().user_timeline(
            username, kind=kind, cursor=cursor, limit=limit, instance=instance
        )
    except FetchError as exc:
        return _error(str(exc))
    return _json(
        {
            "profile": _dump(profile.card),
            "tweets": [_dump(t) for t in tweets],
            "next_cursor": next_cursor,
            "instance": served.host,
        }
    )


@mcp.tool()
def x_tweet(id_or_url: str, include_replies: bool = True) -> list[TextContent]:
    """Fetch one tweet plus its thread and replies.

    Accepts a bare numeric tweet id, an x.com/twitter.com URL or a Nitter URL.
    """
    try:
        page, served = get_client().tweet(id_or_url, include_replies=include_replies)
    except FetchError as exc:
        return _error(str(exc))
    return _json(
        {
            "main": _dump(page.main),
            "before": [_dump(t) for t in page.before],
            "after": [_dump(t) for t in page.after],
            "replies": [_dump(t) for t in page.replies],
            "next_cursor": page.next_cursor,
            "instance": served.host,
        }
    )


@mcp.tool()
def x_search(
    query: str,
    kind: str = "tweets",
    cursor: str | None = None,
    limit: int = 20,
    instance: str | None = None,
) -> list[TextContent]:
    """Search tweets or users via a Nitter mirror that has search enabled.

    kind is "tweets" or "users". Use the returned next_cursor to paginate.
    """
    try:
        results, next_cursor, served = get_client().search(
            query, kind=kind, cursor=cursor, limit=limit, instance=instance
        )
    except FetchError as exc:
        return _error(str(exc))
    return _json(
        {
            "results": [_dump(r) for r in results],
            "kind": kind,
            "next_cursor": next_cursor,
            "instance": served.host,
        }
    )


@mcp.tool()
def x_download_media(
    id_or_url: str,
    directory: str | None = None,
    return_images: bool = False,
) -> list[TextContent | ImageContent]:
    """Download the media attached to a tweet and return the saved file paths.

    Photos are fetched through the mirror's /pic/ proxy (falling back to the
    original pbs.twimg.com URL). Videos are downloaded when the mirror exposes
    a direct mp4; m3u8-only streams use ffmpeg if installed, otherwise they are
    reported as skipped. Set return_images=true to also receive the photos as
    MCP image content so a model can view them.
    """
    try:
        page, served = get_client().tweet(id_or_url, include_replies=False)
    except FetchError as exc:
        return _error(str(exc))

    tweets = ([page.main] if page.main else []) + page.before + page.after + page.replies
    # Absolute, so the paths returned to the agent do not depend on the
    # server process's working directory.
    target = (Path(directory).expanduser() if directory else DEFAULT_DIR).resolve()
    downloader = MediaDownloader(get_client().pool.client, instance_url=served.url)

    saved: list[str] = []
    skipped: list[dict] = []
    for tweet in tweets:
        for index, media in enumerate(tweet.media, start=1):
            for record in downloader.download(media, tweet.id, target, index):
                if "path" in record:
                    saved.append(record["path"])
                else:
                    skipped.append(
                        {
                            "tweet_id": tweet.id,
                            "kind": record["kind"],
                            "reason": record["skipped_reason"],
                        }
                    )

    result: list[TextContent | ImageContent] = [
        TextContent(
            type="text",
            text=json.dumps(
                {"saved": saved, "skipped": skipped, "instance": served.host},
                indent=2,
            ),
        )
    ]

    if return_images:
        import mimetypes

        for path in saved:
            p = Path(path)
            if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
                continue
            mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
            result.append(
                ImageContent(
                    type="image",
                    # MCP ImageContent carries base64-encoded bytes.
                    data=base64.b64encode(p.read_bytes()).decode(),
                    mime_type=mime,
                )
            )
    return result


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
