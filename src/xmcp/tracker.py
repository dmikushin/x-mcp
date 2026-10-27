"""Discover live Nitter instances from the community status tracker.

The tracker page (https://status.d420.de/) is plain HTML with a sortable table
``#status-tbl``. Its JSON API rate-limits with 429, so we parse the HTML.
Every row is parsed, healthy or not, so callers can decide what to use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

TRACKER_URL = "https://status.d420.de/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
)

_LATENCY_RE = re.compile(r"(\d+)\s*ms")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")


@dataclass
class TrackerEntry:
    """One row of the tracker table."""

    host: str
    url: str
    country: str
    healthy: bool
    health_label: str
    latency_ms: int | None
    all_time_percent: float | None
    rss: bool
    nitter_version: str
    version_badge: str
    connectivity: str
    points: int

    @property
    def is_latest(self) -> bool:
        return self.version_badge == "latest"


def _parse_int(text: str) -> int | None:
    m = _LATENCY_RE.search(text)
    return int(m.group(1)) if m else None


def _parse_percent(text: str) -> float | None:
    m = _PERCENT_RE.search(text)
    return float(m.group(1)) if m else None


def parse_tracker_html(html: str) -> list[TrackerEntry]:
    """Parse every row of ``#status-tbl`` into TrackerEntry objects."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("#status-tbl")
    if table is None:
        raise ValueError("tracker page has no #status-tbl table")

    entries: list[TrackerEntry] = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 10:
            # Malformed or column-hidden row: skip rather than misalign fields.
            continue

        link = cells[0].find("a", href=True)
        if link is None:
            continue
        url = link["href"].strip()
        host = link.get_text(strip=True) or re.sub(r"^https?://", "", url)

        health_span = cells[2].find("span")
        health_label = (health_span.get("aria-label") or "").strip() if health_span else ""
        healthy = health_label == "healthy"

        version_cell = cells[7]
        badge = version_cell.select_one(".badge")
        version_badge = badge.get_text(strip=True) if badge else ""
        version_text = version_cell.get_text(" ", strip=True)

        try:
            points = int(cells[9].get_text(strip=True))
        except ValueError:
            points = 0

        entries.append(
            TrackerEntry(
                host=host,
                url=url,
                country=cells[1].get_text(strip=True),
                healthy=healthy,
                health_label=health_label,
                latency_ms=_parse_int(cells[4].get_text(strip=True)),
                all_time_percent=_parse_percent(cells[5].get_text(strip=True)),
                rss=cells[6].get_text(strip=True) == "✅",
                nitter_version=version_text,
                version_badge=version_badge,
                connectivity=cells[8].get_text(strip=True),
                points=points,
            )
        )
    return entries


def fetch_tracker(client: httpx.Client, url: str = TRACKER_URL) -> list[TrackerEntry]:
    """Download the tracker page and parse it."""
    resp = client.get(url)
    resp.raise_for_status()
    return parse_tracker_html(resp.text)
