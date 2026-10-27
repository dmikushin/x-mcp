"""Instance pool: discover, probe, cache and rank Nitter mirrors.

The pool is built from the tracker (healthy first, points descending), then
each instance is probed concurrently with a known profile page and classified.
Results are cached as JSON under ``$XDG_CACHE_HOME/x-mcp/instances.json``.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from .challenge import UnsupportedChallenge, is_anubis_page, solve_and_pass
from .tracker import TrackerEntry

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
)
DEFAULT_TIMEOUT = 15.0
DEFAULT_CONCURRENCY = 8
DEFAULT_TTL = 3600.0
PROBE_PATH = "/NASA"
SEARCH_PROBE_PATH = "/search?f=tweets&q=test"


class InstanceStatus(StrEnum):
    OK = "ok"
    ANUBIS = "anubis"
    GANDALF = "gandalf"
    CLOUDFLARE = "cloudflare"
    FORBIDDEN = "forbidden"
    REDIRECT = "redirect"
    TIMEOUT = "timeout"
    ERROR = "error"

    @property
    def usable(self) -> bool:
        return self is InstanceStatus.OK


@dataclass
class ProbedInstance:
    host: str
    url: str
    status: InstanceStatus
    latency_ms: int | None
    search_enabled: bool
    points: int
    healthy: bool
    nitter_version: str
    source: str  # "tracker" or "pinned"
    solved_challenge: bool = False
    failure_reason: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ProbedInstance:
        return cls(
            host=data["host"],
            url=data["url"],
            status=InstanceStatus(data["status"]),
            latency_ms=data.get("latency_ms"),
            search_enabled=data.get("search_enabled", False),
            points=data.get("points", 0),
            healthy=data.get("healthy", False),
            nitter_version=data.get("nitter_version", ""),
            source=data.get("source", "tracker"),
            solved_challenge=data.get("solved_challenge", False),
            failure_reason=data.get("failure_reason", ""),
        )


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "x-mcp"


class InstancePool:
    """Ranked, cached pool of usable Nitter instances."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        pinned: list[str] | None = None,
        ttl: float = DEFAULT_TTL,
        concurrency: int = DEFAULT_CONCURRENCY,
        cache_path: Path | None = None,
    ) -> None:
        self.client = client
        self.ttl = ttl
        self.concurrency = min(concurrency, DEFAULT_CONCURRENCY)
        self.pinned = [h.strip() for h in (pinned or []) if h.strip()]
        self.cache_path = cache_path or cache_dir() / "instances.json"
        self.instances: list[ProbedInstance] = []
        self.probed_at: float = 0.0

    # ---------------------------------------------------------------- cache
    def _write_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "probed_at": self.probed_at,
            "instances": [inst.to_dict() for inst in self.instances],
        }
        self.cache_path.write_text(json.dumps(payload, indent=2))

    def _read_cache(self) -> bool:
        if not self.cache_path.exists():
            return False
        try:
            payload = json.loads(self.cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        if time.time() - payload.get("probed_at", 0) > self.ttl:
            return False
        self.probed_at = payload["probed_at"]
        self.instances = [ProbedInstance.from_dict(d) for d in payload["instances"]]
        return bool(self.instances)

    # -------------------------------------------------------------- probing
    def _probe(self, entry: TrackerEntry) -> ProbedInstance:
        host = entry.host
        url = entry.url if entry.url.startswith("http") else f"https://{host}"
        inst = ProbedInstance(
            host=host,
            url=url,
            status=InstanceStatus.ERROR,
            latency_ms=None,
            search_enabled=False,
            points=entry.points,
            healthy=entry.healthy,
            nitter_version=entry.nitter_version,
            source="pinned" if host in self.pinned else "tracker",
        )
        started = time.monotonic()
        try:
            resp = self.client.get(url + PROBE_PATH)
            inst.latency_ms = int((time.monotonic() - started) * 1000)
            inst.status = self._classify(resp, inst, url)
        except httpx.TimeoutException:
            inst.status = InstanceStatus.TIMEOUT
            inst.failure_reason = "request timed out"
        except httpx.HTTPStatusError as exc:
            inst.status = (
                InstanceStatus.FORBIDDEN
                if exc.response.status_code == 403
                else InstanceStatus.ERROR
            )
            inst.failure_reason = f"HTTP {exc.response.status_code}"
        except httpx.HTTPError as exc:
            inst.status = InstanceStatus.ERROR
            inst.failure_reason = str(exc)

        if inst.status is InstanceStatus.OK:
            inst.search_enabled = self._probe_search(url)
        return inst

    def _classify(
        self, resp: httpx.Response, inst: ProbedInstance, base_url: str
    ) -> InstanceStatus:
        if resp.status_code == 403:
            return InstanceStatus.FORBIDDEN
        if resp.status_code in (301, 302, 303, 307):
            return InstanceStatus.REDIRECT
        if resp.status_code >= 500:
            return InstanceStatus.ERROR
        text = resp.text
        lowered = text.lower()
        if "<title>gandalf" in lowered:
            return InstanceStatus.GANDALF
        if (
            "just a moment" in lowered
            or "cf-challenge" in lowered
            or "challenges.cloudflare.com" in lowered
        ):
            return InstanceStatus.CLOUDFLARE
        if is_anubis_page(text):
            try:
                solved = solve_and_pass(self.client, resp, PROBE_PATH)
                inst.solved_challenge = True
                if "timeline-item" in solved.text or "profile-card" in solved.text:
                    return InstanceStatus.OK
                inst.failure_reason = "Anubis solved but page had no content"
                return InstanceStatus.ERROR
            except (UnsupportedChallenge, httpx.HTTPError) as exc:
                inst.failure_reason = f"Anubis challenge: {exc}"
                return InstanceStatus.ANUBIS
        if "timeline-item" in text or "profile-card" in text:
            return InstanceStatus.OK
        inst.failure_reason = "probe page had no recognizable Nitter content"
        return InstanceStatus.ERROR

    def _probe_search(self, base_url: str) -> bool:
        try:
            resp = self.client.get(base_url + SEARCH_PROBE_PATH)
        except httpx.HTTPError:
            return False
        text = resp.text
        if "Search disabled" in text or "search disabled" in text.lower():
            return False
        return "timeline-item" in text

    # --------------------------------------------------------------- public
    def refresh(self) -> list[ProbedInstance]:
        """Discover from the tracker and probe every instance concurrently."""
        from .tracker import fetch_tracker

        entries: list[TrackerEntry] = []
        try:
            entries = fetch_tracker(self.client)
        except (httpx.HTTPError, ValueError):
            entries = []

        seen: set[str] = set()
        candidates: list[TrackerEntry] = []
        for host in self.pinned:
            candidates.append(
                TrackerEntry(
                    host=host,
                    url=f"https://{host}",
                    country="",
                    healthy=True,
                    health_label="pinned",
                    latency_ms=None,
                    all_time_percent=None,
                    rss=False,
                    nitter_version="",
                    version_badge="",
                    connectivity="",
                    points=1000,
                )
            )
            seen.add(host)
        for entry in entries:
            if entry.host not in seen:
                candidates.append(entry)
                seen.add(entry.host)

        # Healthy first, then points descending.
        candidates.sort(key=lambda e: (not e.healthy, -e.points))

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            self.instances = list(pool.map(self._probe, candidates))

        self.instances.sort(
            key=lambda i: (not i.status.usable, -i.points, i.latency_ms or 10**9)
        )
        self.probed_at = time.time()
        self._write_cache()
        return self.instances

    def ensure(self, *, force: bool = False) -> list[ProbedInstance]:
        """Return the pool, using the cache when fresh enough."""
        if not force and self._read_cache():
            return self.instances
        return self.refresh()

    def usable(self) -> list[ProbedInstance]:
        return [i for i in self.instances if i.status.usable]

    def searchable(self) -> list[ProbedInstance]:
        return [i for i in self.instances if i.status.usable and i.search_enabled]

    def demote(self, host: str, reason: str) -> None:
        """Record a live failure so the instance sinks in the ranking."""
        for inst in self.instances:
            if inst.host == host:
                inst.status = InstanceStatus.ERROR
                inst.failure_reason = reason
                inst.points = max(0, inst.points - 50)
        self.instances.sort(
            key=lambda i: (not i.status.usable, -i.points, i.latency_ms or 10**9)
        )
        self._write_cache()

    def get(self, host: str) -> ProbedInstance | None:
        for inst in self.instances:
            if inst.host == host:
                return inst
        return None

    def cookie_file(self) -> Path:
        # Cookies live next to the instance cache so a custom cache_path also
        # relocates them (important for the offline tests).
        return self.cache_path.parent / "cookies.json"

    def load_cookies(self) -> None:
        path = self.cookie_file()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for entry in data:
            self.client.cookies.set(
                entry["name"], entry["value"], domain=entry["domain"], path=entry["path"]
            )

    def save_cookies(self) -> None:
        path = self.cookie_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.client.cookies.jar
        ]
        path.write_text(json.dumps(entries, indent=2))
