"""Console frontend: x-cli."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

from .client import FetchError, NitterClient
from .instances import USER_AGENT, InstancePool
from .media import MediaDownloader
from .parse import Tweet


def _color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def wrap(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def dim(self, text: str) -> str:
        return self.wrap(text, "2")

    def bold(self, text: str) -> str:
        return self.wrap(text, "1")

    def cyan(self, text: str) -> str:
        return self.wrap(text, "36")

    def yellow(self, text: str) -> str:
        return self.wrap(text, "33")


def _to_json(obj) -> str:
    def default(value):
        if dataclasses.is_dataclass(value):
            return dataclasses.asdict(value)
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    return json.dumps(obj, default=default, indent=2)


def make_http_client() -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        follow_redirects=True,
        timeout=15.0,
    )


def _first_instance(args) -> str | None:
    """The --instance flag is repeatable; requests target the first one."""
    return args.instance[0] if args.instance else None


def build_pool(args) -> InstancePool:
    pinned = args.instance or [
        h.strip() for h in os.environ.get("X_MCP_INSTANCES", "").split(",") if h.strip()
    ]
    client = make_http_client()
    pool = InstancePool(client, pinned=pinned)
    pool.load_cookies()
    pool.ensure(force=args.refresh if hasattr(args, "refresh") else False)
    return pool


def format_tweet(tweet: Tweet, style: Style, *, indent: str = "") -> list[str]:
    lines = []
    header = f"{style.cyan('@' + tweet.author)}"
    if tweet.author_name and tweet.author_name != tweet.author:
        header = f"{style.bold(tweet.author_name)} {style.dim('(@' + tweet.author + ')')}"
    if tweet.date:
        header += style.dim(f" · {tweet.date}")
    flags = []
    if tweet.pinned:
        flags.append("pinned")
    if tweet.retweeted_by:
        flags.append(f"retweeted by {tweet.retweeted_by}")
    if flags:
        header += style.yellow(" [" + ", ".join(flags) + "]")
    lines.append(indent + header)

    for line in tweet.text.splitlines():
        lines.append(indent + "  " + line)

    if tweet.quote:
        lines.append(
            indent
            + style.dim("  quote @")
            + style.cyan(tweet.quote.author)
            + style.dim(f" · {tweet.quote.date or ''}")
        )
        for line in tweet.quote.text.splitlines():
            lines.append(indent + "    " + line)

    for media in tweet.media:
        if media.kind == "card":
            lines.append(indent + style.dim(f"  card: {media.title or ''} {media.url}"))
        else:
            url = media.original or media.url
            lines.append(indent + style.dim(f"  {media.kind}: {url}"))

    stats = tweet.stats
    parts = []
    if stats.replies is not None:
        parts.append(f"replies {stats.replies}")
    if stats.retweets is not None:
        parts.append(f"retweets {stats.retweets}")
    if stats.likes is not None:
        parts.append(f"likes {stats.likes}")
    if stats.views is not None:
        parts.append(f"views {stats.views}")
    if parts:
        lines.append(indent + style.dim("  " + " · ".join(parts)))
    lines.append(indent + style.dim("  " + tweet.url))
    return lines


def print_instances(pool: InstancePool, *, show_all: bool, style: Style) -> None:
    rows = pool.instances if show_all else pool.usable()
    header = (
        f"{'HOST'.ljust(28)} {'STATUS'.ljust(12)} {'LATENCY'.ljust(9)} "
        f"{'SEARCH'.ljust(7)} {'POINTS'.ljust(7)} SOURCE"
    )
    print(style.bold(header))
    print("-" * len(header))
    for inst in rows:
        latency = f"{inst.latency_ms}ms" if inst.latency_ms is not None else "-"
        search = "yes" if inst.search_enabled else "no"
        print(
            f"{inst.host.ljust(28)} {inst.status.value.ljust(12)} "
            f"{latency.ljust(9)} {search.ljust(7)} {str(inst.points).ljust(7)} {inst.source}"
        )
        if inst.failure_reason:
            print("  " + style.dim(inst.failure_reason))


def cmd_instances(args, pool, style) -> int:
    print_instances(pool, show_all=args.all, style=style)
    if args.json:
        print(_to_json([inst.to_dict() for inst in pool.instances]))
    return 0


def cmd_user(args, pool, style) -> int:
    client = NitterClient(pool)
    try:
        profile, tweets, cursor, instance = client.user_timeline(
            args.username,
            kind="media" if args.media else "replies" if args.replies else "tweets",
            cursor=args.cursor,
            pages=args.pages,
            limit=args.limit,
            instance=_first_instance(args),
        )
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.search_in:
        # Nitter supports the Twitter-style "from:user" operator in search.
        search_tweets, search_cursor, search_instance = client.search(
            f"{args.search_in} from:{args.username}",
            pages=args.pages,
            limit=args.limit,
        )
        tweets = search_tweets
        cursor = search_cursor
        instance = search_instance

    if args.json:
        payload = {
            "profile": profile.card,
            "tweets": tweets,
            "next_cursor": cursor,
            "instance": instance.host,
        }
        if args.download_media:
            payload["saved_media"] = download_media_for_tweets(
                pool, tweets, instance.url, args.output, style
            )
        print(_to_json(payload))
        return 0

    if profile.card:
        card = profile.card
        print(style.bold(f"{card.fullname} (@{card.username})"))
        detail = []
        if card.location:
            detail.append(card.location)
        if card.website:
            detail.append(card.website)
        if card.joined:
            detail.append(f"joined {card.joined}")
        if detail:
            print(style.dim("  " + " · ".join(detail)))
        if card.bio:
            print("  " + card.bio)
        print(
            style.dim(
                f"  tweets {card.tweets} · following {card.following} · "
                f"followers {card.followers} · likes {card.likes}"
            )
        )
        print(style.dim(f"  served by {instance.host}"))
        print()

    if not tweets:
        print(style.dim("no tweets found (timeline is empty)"))
        return 0

    for tweet in tweets:
        print("\n".join(format_tweet(tweet, style)))
        print()

    if cursor:
        print(style.dim(f"next cursor: {cursor}"))

    if args.download_media:
        saved = download_media_for_tweets(pool, tweets, instance.url, args.output, style)
        print(style.dim(f"saved {len(saved)} file(s) to {args.output}"))
    return 0


def cmd_tweet(args, pool, style) -> int:
    client = NitterClient(pool)
    try:
        page, instance = client.tweet(
            args.id, include_replies=not args.no_replies, instance=_first_instance(args)
        )
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "main": page.main,
            "before": page.before,
            "after": page.after,
            "replies": page.replies,
            "next_cursor": page.next_cursor,
            "instance": instance.host,
        }
        if args.download_media:
            tweets = ([page.main] if page.main else []) + page.before + page.after + page.replies
            payload["saved_media"] = download_media_for_tweets(
                pool, tweets, instance.url, args.output, style
            )
        print(_to_json(payload))
        return 0

    if page.main is None:
        print("error: no main tweet found on the page", file=sys.stderr)
        return 1

    print(style.dim(f"served by {instance.host}"))
    print("\n".join(format_tweet(page.main, style)))
    for group, tweets in (
        ("context before", page.before),
        ("context after", page.after),
        ("replies", page.replies),
    ):
        if tweets:
            print()
            print(style.bold(group))
            for tweet in tweets:
                print("\n".join(format_tweet(tweet, style, indent="  ")))
                print()
    if page.next_cursor:
        print(style.dim(f"next cursor: {page.next_cursor}"))

    if args.download_media:
        tweets = [page.main] if page.main else []
        tweets += page.before + page.after + page.replies
        saved = download_media_for_tweets(pool, tweets, instance.url, args.output, style)
        print(style.dim(f"saved {len(saved)} file(s) to {args.output}"))
    return 0


def cmd_search(args, pool, style) -> int:
    client = NitterClient(pool)
    kind = "users" if args.users else "tweets"
    try:
        results, cursor, instance = client.search(
            args.query,
            kind=kind,
            cursor=args.cursor,
            pages=args.pages,
            limit=args.limit,
            instance=_first_instance(args),
        )
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "results": results,
            "kind": kind,
            "next_cursor": cursor,
            "instance": instance.host,
        }
        if kind == "tweets" and args.download_media:
            payload["saved_media"] = download_media_for_tweets(
                pool, results, instance.url, args.output, style
            )
        print(_to_json(payload))
        return 0

    if not results:
        print(style.dim("no results"))
        return 0
    print(style.dim(f"served by {instance.host}"))

    if kind == "users":
        for user in results:
            line = f"{style.bold(user.fullname)} {style.cyan('@' + user.username)}"
            if user.bio:
                line += style.dim(f" — {user.bio}")
            print(line)
            print(style.dim(f"  {user.url}"))
            print()
    else:
        for tweet in results:
            print("\n".join(format_tweet(tweet, style)))
            print()

    if cursor:
        print(style.dim(f"next cursor: {cursor}"))
    if kind == "tweets" and args.download_media:
        saved = download_media_for_tweets(pool, results, instance.url, args.output, style)
        print(style.dim(f"saved {len(saved)} file(s) to {args.output}"))
    return 0


def cmd_media(args, pool, style) -> int:
    client = NitterClient(pool)
    try:
        page, instance = client.tweet(args.id, include_replies=False)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    tweets = ([page.main] if page.main else []) + page.before + page.after + page.replies
    saved = download_media_for_tweets(pool, tweets, instance.url, args.output, style)
    if args.json:
        print(_to_json({"saved": saved, "instance": instance.host}))
        return 0
    if not saved:
        print(style.dim("no media downloaded"))
        return 0
    for path in saved:
        print(path)
    return 0


def download_media_for_tweets(
    pool, tweets, instance_url: str, directory: str, style: Style
) -> list[str]:
    """Shared implementation of the --download-media flag."""
    downloader = MediaDownloader(pool.client, instance_url=instance_url)
    saved: list[str] = []
    for tweet in tweets:
        for index, media in enumerate(tweet.media, start=1):
            for record in downloader.download(media, tweet.id, Path(directory), index):
                if "path" in record:
                    saved.append(record["path"])
                else:
                    print(
                        style.dim(
                            f"skipped {tweet.id} {record['kind']}: {record['skipped_reason']}"
                        ),
                        file=sys.stderr,
                    )
    return saved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x-cli",
        description="Read X/Twitter without an account through public Nitter mirrors.",
    )
    # Global flags live in a parent parser so they are accepted both before and
    # after the subcommand; SUPPRESS keeps a value set before the subcommand
    # from being overwritten by the subparser default.
    parser.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    parser.add_argument(
        "--instance", action="append", help="pin a specific instance host (repeatable)"
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log every request and instance decision to stderr",
    )

    parent = argparse.ArgumentParser(add_help=False)
    # SUPPRESS so a flag given before the subcommand is not overwritten by the
    # subparser's default when it is not repeated after the subcommand.
    parent.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="machine-readable JSON output",
    )
    parent.add_argument(
        "--instance", action="append", default=argparse.SUPPRESS,
        help="pin a specific instance host (repeatable)",
    )
    parent.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="log every request and instance decision to stderr",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_inst = sub.add_parser("instances", parents=[parent], help="show the instance pool")
    p_inst.add_argument("--refresh", action="store_true", help="force a re-probe of all instances")
    p_inst.add_argument("--all", action="store_true", help="include unusable instances")
    p_inst.set_defaults(handler=cmd_instances)

    p_user = sub.add_parser("user", parents=[parent], help="profile timeline of a user")
    p_user.add_argument("username")
    p_user.add_argument("--replies", action="store_true", help="show the replies timeline")
    p_user.add_argument("--media", action="store_true", help="show the media timeline")
    p_user.add_argument("--search-in", metavar="Q", help="search within this user's tweets")
    p_user.add_argument("--cursor", help="pagination cursor from a previous page")
    p_user.add_argument("--pages", type=int, default=1, help="number of pages to fetch")
    p_user.add_argument("--limit", type=int, help="stop after this many tweets")
    p_user.add_argument(
        "--download-media", action="store_true", help="save media of the shown tweets"
    )
    p_user.add_argument("-o", "--output", default="x-media", help="directory for downloaded media")
    p_user.set_defaults(handler=cmd_user)

    p_tweet = sub.add_parser(
        "tweet", parents=[parent], help="one tweet with its thread and replies"
    )
    p_tweet.add_argument("id", help="tweet id, x.com/twitter.com/nitter URL")
    p_tweet.add_argument("--no-replies", action="store_true", help="omit the replies section")
    p_tweet.add_argument("--download-media", action="store_true", help="save media of the tweet")
    p_tweet.add_argument("-o", "--output", default="x-media", help="directory for downloaded media")
    p_tweet.set_defaults(handler=cmd_tweet)

    p_search = sub.add_parser("search", parents=[parent], help="search tweets or users")
    p_search.add_argument("query")
    p_search.add_argument("--users", action="store_true", help="search users instead of tweets")
    p_search.add_argument("--cursor", help="pagination cursor")
    p_search.add_argument("--pages", type=int, default=1)
    p_search.add_argument("--limit", type=int)
    p_search.add_argument("--download-media", action="store_true", help="save media of the results")
    p_search.add_argument(
        "-o", "--output", default="x-media", help="directory for downloaded media"
    )
    p_search.set_defaults(handler=cmd_search)

    p_media = sub.add_parser("media", parents=[parent], help="download media of one tweet")
    p_media.add_argument("id", help="tweet id or URL")
    p_media.add_argument("-o", "--output", default="x-media", help="directory for downloaded media")
    p_media.set_defaults(handler=cmd_media)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    style = Style(_color_enabled())
    pool = build_pool(args)

    return args.handler(args, pool, style)


if __name__ == "__main__":
    raise SystemExit(main())
