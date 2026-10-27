"""Nitter HTML -> dataclasses.

Class names are taken from the fixtures in tests/fixtures, which are the
authoritative reference for the markup Nitter emits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

STAT_ICONS = {
    "icon-comment": "replies",
    "icon-retweet": "retweets",
    "icon-heart": "likes",
    "icon-views": "views",
}


@dataclass
class Media:
    kind: str  # photo | gif | video | card
    url: str  # absolute URL on the serving instance
    original: str | None = None  # pbs.twimg.com / video.twimg.com URL when derivable
    poster: str | None = None
    title: str | None = None
    description: str | None = None
    hls_referer: str | None = None  # Nitter /enablehls form target, if present


@dataclass
class Stats:
    replies: int | None = None
    retweets: int | None = None
    likes: int | None = None
    views: int | None = None


@dataclass
class Quote:
    id: str
    url: str
    author: str
    author_name: str
    date: str | None
    text: str


@dataclass
class Tweet:
    id: str
    url: str
    author: str
    author_name: str
    text: str
    date: str | None
    pinned: bool
    retweeted_by: str | None
    quote: Quote | None
    media: list[Media] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    replying_to: str | None = None


@dataclass
class ProfileCard:
    fullname: str
    username: str
    bio: str
    location: str
    website: str
    joined: str
    tweets: int | None
    following: int | None
    followers: int | None
    likes: int | None
    avatar: str
    banner: str


@dataclass
class Timeline:
    tweets: list[Tweet]
    next_cursor: str | None


@dataclass
class UserResult:
    username: str
    fullname: str
    bio: str
    avatar: str
    url: str


def parse_user_search(html: str, base_url: str) -> tuple[list[UserResult], str | None]:
    """Nitter renders f=users results as .timeline-item with .tweet-body.profile-result."""
    soup = BeautifulSoup(html, "lxml")
    users: list[UserResult] = []
    for item in soup.select(".timeline-item"):
        username_node = item.select_one(".username")
        if username_node is None:
            continue
        fullname_node = item.select_one(".fullname")
        bio_node = item.select_one(".tweet-content")
        avatar = item.select_one(".tweet-avatar img[src]")
        username = username_node.get_text(strip=True).lstrip("@")
        users.append(
            UserResult(
                username=username,
                fullname=fullname_node.get_text(strip=True) if fullname_node else "",
                bio=bio_node.get_text(" ", strip=True) if bio_node else "",
                avatar=_abs(base_url, avatar["src"]) if avatar else "",
                url=f"https://x.com{username_node.get('href', '')}",
            )
        )
    return users, _next_cursor(soup, base_url)


@dataclass
class Profile:
    card: ProfileCard | None
    timeline: Timeline


@dataclass
class TweetPage:
    main: Tweet | None
    before: list[Tweet]
    after: list[Tweet]
    replies: list[Tweet]
    next_cursor: str | None


def _abs(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


def _parse_date(title: str | None) -> str | None:
    """Nitter date titles look like "Oct 26, 2026 · 5:04 PM UTC"."""
    if not title:
        return None
    text = title.strip()
    if "·" in text:
        date_part, time_part = text.split("·", 1)
    else:
        date_part, time_part = text, ""
    time_part = time_part.strip().upper().removesuffix(" UTC")
    try:
        dt = datetime.strptime(f"{date_part.strip()} {time_part}", "%b %d, %Y %I:%M %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _parse_int(text: str) -> int | None:
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _tweet_text(node: Tag) -> str:
    """Extract text, turning <a> into their visible text and keeping newlines."""
    for link in node.find_all("a"):
        link.replace_with(link.get_text())
    text = node.get_text()
    return text.strip()


def _twimg_original(href: str) -> str | None:
    """Derive the origin URL from a Nitter /pic/... proxy path."""
    decoded = unquote(href)
    if "/pic/orig/media/" in decoded:
        name = decoded.split("/pic/orig/media/", 1)[1]
        return f"https://pbs.twimg.com/media/{name}?name=orig"
    if decoded.startswith("/pic/"):
        # /pic/video.twimg.com/tweet_video/X.mp4 and /pic/https://... both
        # carry the origin URL verbatim after the proxy prefix.
        return "https://" + decoded[len("/pic/"):]
    return None


def _parse_media(container: Tag, base_url: str) -> list[Media]:
    media: list[Media] = []

    for link in container.select("a.still-image"):
        href = link.get("href", "")
        img = link.find("img", src=True)
        media.append(
            Media(
                kind="photo",
                url=_abs(base_url, href),
                original=_twimg_original(href),
                poster=_abs(base_url, img["src"]) if img else None,
            )
        )

    for video in container.select("video"):
        source = video.find("source", src=True)
        href = source["src"] if source else ""
        poster = video.get("poster")
        kind = "gif" if "gif" in (video.get("class") or []) else "video"
        # Mirrors differ: some emit "/pic/video.twimg.com/...", others a bare
        # "video.twimg.com/..." without scheme or leading slash.
        if href and not href.startswith("/") and not href.startswith("http"):
            original = "https://" + href
            url = base_url.rstrip("/") + "/pic/" + href
        else:
            url = _abs(base_url, href) if href else ""
            original = _twimg_original(href) if href else None
        media.append(
            Media(
                kind=kind,
                url=url,
                original=original,
                poster=_abs(base_url, poster) if poster else None,
            )
        )

    for link in container.select("a.video-download"):
        href = link.get("href", "")
        # /video/<hash>/<urlencoded direct mp4>
        decoded = unquote(href)
        original = "https://" + decoded.split("https://", 1)[1] if "https://" in decoded else None
        poster_img = container.select_one(".attachment img[src]")
        media.append(
            Media(
                kind="video",
                url=_abs(base_url, href),
                original=original,
                poster=_abs(base_url, poster_img["src"]) if poster_img else None,
            )
        )

    # Videos Nitter only plays through HLS: no download link and no inline
    # <source>, just an "Enable hls playback" form.
    for gallery in container.select(".gallery-video"):
        if gallery.select_one("a.video-download") or gallery.select_one("video source[src]"):
            continue
        form = gallery.select_one("form[action='/enablehls']")
        referer = form.select_one("input[name='referer']") if form else None
        poster_img = gallery.select_one("img[src]")
        media.append(
            Media(
                kind="video",
                url="",
                original=None,
                poster=_abs(base_url, poster_img["src"]) if poster_img else None,
                hls_referer=referer.get("value") if referer else None,
            )
        )

    for card in container.select(".card-container, a.card-container"):
        href = card.get("href")
        if not href:
            continue
        title_node = card.select_one(".card-title")
        desc_node = card.select_one(".card-description")
        img = card.select_one("img[src]")
        media.append(
            Media(
                kind="card",
                url=href,
                original=None,
                poster=_abs(base_url, img["src"]) if img else None,
                title=title_node.get_text(strip=True) if title_node else None,
                description=desc_node.get_text(strip=True) if desc_node else None,
            )
        )

    return media


def _parse_stats(node: Tag) -> Stats:
    stats = Stats()
    for stat in node.select(".tweet-stat"):
        # The number sits next to the icon span; "icon-container" also starts
        # with "icon-", so match the known icon classes explicitly.
        icon_class = next(
            (cls for cls in STAT_ICONS if stat.select_one(f".{cls}")), None
        )
        if icon_class is None:
            continue
        setattr(stats, STAT_ICONS[icon_class], _parse_int(stat.get_text()))
    return stats


def _parse_quote(node: Tag, base_url: str) -> Quote | None:
    link = node.select_one(".quote-link")
    if link is None:
        return None
    href = link.get("href", "")
    tweet_id = _tweet_id(href)
    author = node.select_one(".username")
    name = node.select_one(".fullname")
    date = node.select_one(".tweet-date a")
    text = node.select_one(".quote-text")
    return Quote(
        id=tweet_id,
        url=f"https://x.com{href.split('#')[0]}" if tweet_id else _abs(base_url, href),
        author=author.get_text(strip=True).lstrip("@") if author else "",
        author_name=name.get_text(strip=True) if name else "",
        date=_parse_date(date.get("title") if date else None),
        text=_parse_text_with_links(text) if text else "",
    )


def _parse_text_with_links(node: Tag) -> str:
    for link in node.find_all("a"):
        link.replace_with(link.get_text())
    return node.get_text().strip()


def _tweet_id(href: str) -> str | None:
    path = urlparse(href).path
    parts = path.split("/")
    if len(parts) >= 3 and parts[2] == "status":
        return parts[3].split("#")[0]
    return None


def parse_tweet(item: Tag, base_url: str) -> Tweet | None:
    link = item.select_one("a.tweet-link")
    date_link = item.select_one(".tweet-date a")
    source_href = (link.get("href") if link else None) or (
        date_link.get("href") if date_link else None
    )
    if not source_href:
        return None
    tweet_id = _tweet_id(source_href)
    if not tweet_id:
        return None

    username_node = item.select_one(".username")
    fullname_node = item.select_one(".fullname")
    content = item.select_one(".tweet-content")
    retweet = item.select_one(".retweet-header")
    replying = item.select_one(".replying-to a")

    quote_node = item.select_one(".quote")
    return Tweet(
        id=tweet_id,
        url=f"https://x.com{source_href.split('#')[0]}",
        author=username_node.get_text(strip=True).lstrip("@") if username_node else "",
        author_name=fullname_node.get_text(strip=True) if fullname_node else "",
        text=_parse_text_with_links(content) if content else "",
        date=_parse_date(date_link.get("title") if date_link else None),
        pinned=item.select_one(".pinned") is not None,
        retweeted_by=retweet.get_text(strip=True).removesuffix(" retweeted") if retweet else None,
        quote=_parse_quote(quote_node, base_url) if quote_node else None,
        media=_parse_media(item, base_url),
        stats=_parse_stats(item.select_one(".tweet-stats") or item),
        replying_to=replying.get_text(strip=True).lstrip("@") if replying else None,
    )


def _next_cursor(soup: BeautifulSoup, base_url: str) -> str | None:
    show_more = soup.select_one(".show-more a")
    if show_more is None:
        return None
    query = urlparse(urljoin(base_url, show_more.get("href", ""))).query
    values = parse_qs(query).get("cursor")
    return values[0] if values else None


def parse_timeline(html: str, base_url: str) -> Timeline:
    soup = BeautifulSoup(html, "lxml")
    tweets = [t for item in soup.select(".timeline-item") if (t := parse_tweet(item, base_url))]
    return Timeline(tweets=tweets, next_cursor=_next_cursor(soup, base_url))


def is_empty_timeline(html: str) -> bool:
    """A genuinely empty timeline ("No items found") is a valid result."""
    soup = BeautifulSoup(html, "lxml")
    timeline = soup.select_one(".timeline")
    if timeline is None:
        return False
    return "No items found" in timeline.get_text(" ", strip=True)


def parse_profile(html: str, base_url: str) -> Profile:
    soup = BeautifulSoup(html, "lxml")
    card = soup.select_one(".profile-card")
    profile_card = None
    if card is not None:
        def stat(name: str) -> int | None:
            node = card.select_one(f".profile-statlist .{name} .profile-stat-num")
            return _parse_int(node.get_text()) if node else None

        banner = card.select_one(".profile-banner img[src]") or soup.select_one(
            ".profile-banner img[src]"
        )
        profile_card = ProfileCard(
            fullname=card.select_one(".profile-card-fullname").get_text(strip=True)
            if card.select_one(".profile-card-fullname")
            else "",
            username=card.select_one(".profile-card-username").get_text(strip=True).lstrip("@")
            if card.select_one(".profile-card-username")
            else "",
            bio=(
                card.select_one(".profile-bio").get_text(" ", strip=True)
                if card.select_one(".profile-bio")
                else ""
            ),
            location=card.select_one(".profile-location").get_text(strip=True)
            if card.select_one(".profile-location")
            else "",
            website=card.select_one(".profile-website a").get("href", "")
            if card.select_one(".profile-website a")
            else "",
            joined=card.select_one(".profile-joindate a").get("title", "")
            if card.select_one(".profile-joindate a")
            else "",
            tweets=stat("posts"),
            following=stat("following"),
            followers=stat("followers"),
            likes=stat("likes"),
            avatar=_abs(base_url, card.select_one(".profile-card-avatar img[src]")["src"])
            if card.select_one(".profile-card-avatar img[src]")
            else "",
            banner=_abs(base_url, banner["src"]) if banner else "",
        )

    return Profile(
        card=profile_card,
        timeline=parse_timeline(html, base_url),
    )


def parse_tweet_page(html: str, base_url: str) -> TweetPage:
    soup = BeautifulSoup(html, "lxml")

    def items(selector: str) -> list[Tweet]:
        out = []
        for item in soup.select(selector + " .timeline-item"):
            tweet = parse_tweet(item, base_url)
            if tweet:
                out.append(tweet)
        return out

    main_node = soup.select_one(".main-tweet .timeline-item")
    return TweetPage(
        main=parse_tweet(main_node, base_url) if main_node else None,
        before=items(".before-tweet"),
        after=items(".after-tweet"),
        replies=items(".replies"),
        next_cursor=_next_cursor(soup, base_url),
    )
