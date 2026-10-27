"""HTTP layer: fetch Nitter paths with automatic failover across instances."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .challenge import UnsupportedChallenge, is_anubis_page, solve_and_pass
from .instances import InstanceStatus, ProbedInstance
from .parse import is_empty_timeline, parse_timeline

logger = logging.getLogger("xmcp")

# HTML that indicates the mirror is not serving us real content.
_BLOCK_MARKERS = (
    "gandalf",
    "just a moment",
    "challenges.cloudflare.com",
    "making sure you're not a bot",
)


@dataclass
class FetchResult:
    html: str
    instance: ProbedInstance
    url: str
    status_code: int


def normalize_tweet_path(id_or_url: str) -> str:
    """Accept a bare id, an x.com/twitter.com URL or a Nitter URL.

    Returns the Nitter path to request. A bare id uses /i/status/<id>, which
    works without knowing the author.
    """
    text = id_or_url.strip()
    if not text:
        raise ValueError("empty tweet id or URL")
    if re.fullmatch(r"\d+", text):
        return f"/i/status/{text}"

    parsed = urlparse(text if "//" in text else f"https://{text}")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[-2] == "status":
        return "/" + "/".join(parts)
    if len(parts) >= 1 and parts[-1] == "status":
        return "/" + "/".join(parts) + "/" + parts[-1]
    raise ValueError(f"cannot interpret {id_or_url!r} as a tweet id or URL")


class AttemptFailure(Exception):
    """One instance's failure, collected for the aggregate error message."""

    def __init__(self, host: str, reason: str) -> None:
        self.host = host
        self.reason = reason
        super().__init__(f"{host}: {reason}")


class FetchError(Exception):
    """All candidate instances failed; the message lists each attempt."""

    def __init__(self, path: str, failures: list[AttemptFailure]) -> None:
        self.path = path
        self.failures = failures
        lines = "; ".join(f"{f.host}: {f.reason}" for f in failures) or "no instances available"
        super().__init__(f"could not fetch {path!r} — tried: {lines}")


class NitterClient:
    def __init__(self, pool, *, timeout: float = 15.0) -> None:
        self.pool = pool
        self.timeout = timeout

    def _candidates(self, *, search: bool, instance: str | None) -> list[ProbedInstance]:
        if instance:
            pinned = self.pool.get(instance)
            if pinned is not None:
                return [pinned]
            host = instance.replace("https://", "").replace("http://", "")
            return [
                ProbedInstance(
                    host=host,
                    url=f"https://{host}",
                    status=InstanceStatus.OK,
                    latency_ms=None,
                    search_enabled=True,
                    points=1000,
                    healthy=True,
                    nitter_version="",
                    source="pinned",
                )
            ]
        pool = self.pool.searchable() if search else self.pool.usable()
        if not pool:
            # Nothing probed as usable: fall back to everything so the caller
            # still gets a diagnostic list of attempts instead of silence.
            pool = self.pool.instances
        return pool

    def _looks_blocked(self, html: str) -> str | None:
        lowered = html.lower()
        for marker in _BLOCK_MARKERS:
            if marker in lowered:
                return marker
        return None

    def fetch(
        self,
        path: str,
        *,
        search: bool = False,
        instance: str | None = None,
    ) -> FetchResult:
        """Fetch ``path`` trying instances in ranked order until one works."""
        failures: list[AttemptFailure] = []
        for inst in self._candidates(search=search, instance=instance):
            url = inst.url + path
            logger.debug("GET %s", url)
            try:
                resp = self.pool.client.get(url, timeout=self.timeout)
            except httpx.TimeoutException:
                failures.append(AttemptFailure(inst.host, "timeout"))
                self.pool.demote(inst.host, "timeout")
                continue
            except httpx.HTTPError as exc:
                failures.append(AttemptFailure(inst.host, f"network error: {exc}"))
                self.pool.demote(inst.host, str(exc))
                continue

            if resp.status_code == 403:
                failures.append(AttemptFailure(inst.host, "403 forbidden"))
                self.pool.demote(inst.host, "403")
                continue
            if resp.status_code == 429:
                failures.append(AttemptFailure(inst.host, "429 rate limited"))
                self.pool.demote(inst.host, "429")
                continue
            if resp.status_code >= 500:
                failures.append(AttemptFailure(inst.host, f"HTTP {resp.status_code}"))
                self.pool.demote(inst.host, f"HTTP {resp.status_code}")
                continue

            html = resp.text

            if is_anubis_page(html):
                try:
                    solved = solve_and_pass(self.pool.client, resp, path)
                    html = solved.text
                    logger.debug("solved Anubis challenge on %s", inst.host)
                except (UnsupportedChallenge, httpx.HTTPError) as exc:
                    failures.append(AttemptFailure(inst.host, f"Anubis challenge: {exc}"))
                    self.pool.demote(inst.host, "anubis challenge failed")
                    continue

            # A mirror with search turned off is not broken: report it as a
            # failure for this request without demoting it for others.
            if search and "Search disabled" in html:
                failures.append(AttemptFailure(inst.host, "search disabled on this mirror"))
                continue

            blocked = self._looks_blocked(html)
            if blocked:
                failures.append(AttemptFailure(inst.host, f"blocked ({blocked})"))
                self.pool.demote(inst.host, f"blocked ({blocked})")
                continue

            # An empty timeline is a legitimate answer, not a failure.
            if is_empty_timeline(html):
                logger.debug("%s returned an empty timeline (valid)", inst.host)
                self.pool.save_cookies()
                return FetchResult(html=html, instance=inst, url=url, status_code=resp.status_code)

            if (
                "timeline-item" not in html
                and "profile-card" not in html
                and ".main-tweet" not in html
            ):
                failures.append(AttemptFailure(inst.host, "page had no Nitter content"))
                self.pool.demote(inst.host, "no content")
                continue

            self.pool.save_cookies()
            return FetchResult(html=html, instance=inst, url=url, status_code=resp.status_code)

        raise FetchError(path, failures)

    # ------------------------------------------------------------ high level
    def user_timeline(
        self,
        username: str,
        *,
        kind: str = "tweets",
        cursor: str | None = None,
        pages: int = 1,
        limit: int | None = None,
        instance: str | None = None,
    ):
        """Fetch a profile and its timeline (tweets/replies/media)."""
        from .parse import parse_profile

        suffix = {"tweets": "", "replies": "/replies", "media": "/media"}[kind]
        path = f"/{username}{suffix}"
        if cursor:
            path += f"?cursor={cursor}"

        result = self.fetch(path, instance=instance)
        profile = parse_profile(result.html, result.instance.url)
        tweets = profile.timeline.tweets
        next_cursor = profile.timeline.next_cursor

        while pages > 1 and next_cursor and (limit is None or len(tweets) < limit):
            result = self.fetch(f"/{username}{suffix}?cursor={next_cursor}", instance=instance)
            timeline = parse_timeline(result.html, result.instance.url)
            if not timeline.tweets:
                break
            tweets.extend(timeline.tweets)
            next_cursor = timeline.next_cursor
            pages -= 1

        if limit is not None:
            tweets = tweets[:limit]
        return profile, tweets, next_cursor, result.instance

    def tweet(
        self, id_or_url: str, *, include_replies: bool = True, instance: str | None = None
    ):
        """Fetch a tweet page; Nitter has no query to omit replies, so drop
        them from the parsed page when they are not wanted."""
        from .parse import parse_tweet_page

        path = normalize_tweet_path(id_or_url)
        result = self.fetch(path, instance=instance)
        page = parse_tweet_page(result.html, result.instance.url)
        if not include_replies:
            page.replies = []
        return page, result.instance

    def search(
        self,
        query: str,
        *,
        kind: str = "tweets",
        cursor: str | None = None,
        pages: int = 1,
        limit: int | None = None,
        instance: str | None = None,
    ):
        """Search tweets or users. Returns (results, next_cursor, instance)."""
        from .parse import parse_timeline, parse_user_search

        f = "users" if kind == "users" else "tweets"
        path = f"/search?f={f}&q={query}"
        if cursor:
            path += f"&cursor={cursor}"

        result = self.fetch(path, search=True, instance=instance)
        if kind == "users":
            results, next_cursor = parse_user_search(result.html, result.instance.url)
        else:
            timeline = parse_timeline(result.html, result.instance.url)
            results, next_cursor = timeline.tweets, timeline.next_cursor

        while pages > 1 and next_cursor and (limit is None or len(results) < limit):
            result = self.fetch(
                f"/search?f={f}&q={query}&cursor={next_cursor}",
                search=True,
                instance=instance,
            )
            if kind == "users":
                more, next_cursor = parse_user_search(result.html, result.instance.url)
            else:
                timeline = parse_timeline(result.html, result.instance.url)
                more, next_cursor = timeline.tweets, timeline.next_cursor
            if not more:
                break
            results.extend(more)
            pages -= 1

        if limit is not None:
            results = results[:limit]
        return results, next_cursor, result.instance
