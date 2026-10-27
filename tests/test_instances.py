import json
from pathlib import Path

import httpx

from xmcp.instances import InstancePool, InstanceStatus, ProbedInstance

FIXTURES = Path(__file__).parent / "fixtures"
TRACKER = (FIXTURES / "tracker_d420.html").read_text()
ANUBIS = (FIXTURES / "anubis_challenge.html").read_text()
GANDALF = (FIXTURES / "gandalf.html").read_text()
CLOUDFLARE = (FIXTURES / "cloudflare.html").read_text()
PROFILE = (FIXTURES / "profile_elonmusk.html").read_text()
SEARCH = (FIXTURES / "search.html").read_text()

HOSTS = [
    "plain.example.com",
    "gandalf.example.com",
    "cloudflare.example.com",
    "forbidden.example.com",
    "anubis.example.com",
    "broken.example.com",
    "timeout.example.com",
    "search-disabled.example.com",
]


def handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    path = request.url.path
    if host == "status.d420.de":
        return httpx.Response(200, text=TRACKER)
    if path == "/NASA":
        if host in ("plain.example.com", "search-disabled.example.com"):
            return httpx.Response(200, text=PROFILE)
        if host == "gandalf.example.com":
            return httpx.Response(200, text=GANDALF)
        if host == "cloudflare.example.com":
            return httpx.Response(200, text=CLOUDFLARE)
        if host == "forbidden.example.com":
            return httpx.Response(403, text="no")
        if host == "anubis.example.com":
            return httpx.Response(401, text=ANUBIS)
        if host == "broken.example.com":
            return httpx.Response(200, text="<html>nothing here</html>")
        raise httpx.ConnectTimeout("timed out")
    if path == "/search":
        if "disabled" in host:
            return httpx.Response(200, text="<title>Search disabled</title>")
        return httpx.Response(200, text=SEARCH)
    return httpx.Response(404, text="nope")


def make_pool(cache_path: Path) -> InstancePool:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return InstancePool(client, pinned=HOSTS, cache_path=cache_path)


def test_probe_classifies_every_instance():
    pool = make_pool(Path("/tmp/x-mcp-pool-cache.json"))
    pool.refresh()

    statuses = {i.host: i.status for i in pool.instances}
    assert statuses["plain.example.com"] is InstanceStatus.OK
    assert statuses["gandalf.example.com"] is InstanceStatus.GANDALF
    assert statuses["cloudflare.example.com"] is InstanceStatus.CLOUDFLARE
    assert statuses["forbidden.example.com"] is InstanceStatus.FORBIDDEN
    assert statuses["broken.example.com"] is InstanceStatus.ERROR
    assert statuses["timeout.example.com"] is InstanceStatus.TIMEOUT
    # The solver either passes the challenge (OK) or reports it as unsolved.
    assert statuses["anubis.example.com"] in (InstanceStatus.OK, InstanceStatus.ANUBIS)
    assert statuses["search-disabled.example.com"] is InstanceStatus.OK

    assert pool.get("search-disabled.example.com").search_enabled is False
    assert pool.get("plain.example.com").search_enabled is True
    assert pool.usable()
    assert pool.searchable()


def test_probe_records_failure_reasons():
    pool = make_pool(Path("/tmp/x-mcp-pool-cache2.json"))
    pool.refresh()
    assert "no recognizable Nitter content" in pool.get("broken.example.com").failure_reason
    assert pool.get("timeout.example.com").failure_reason == "request timed out"


def test_cache_round_trip_and_ttl():
    path = Path("/tmp/x-mcp-cache-roundtrip.json")
    if path.exists():
        path.unlink()

    pool = make_pool(path)
    pool.refresh()
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["instances"]
    assert payload["instances"][0]["status"] in {s.value for s in InstanceStatus}

    def never_probe(request: httpx.Request) -> httpx.Response:
        if request.url.host == "status.d420.de":
            return httpx.Response(200, text=TRACKER)
        raise AssertionError("a fresh cache must not trigger a probe")

    second = InstancePool(
        httpx.Client(transport=httpx.MockTransport(never_probe)), cache_path=path
    )
    assert second.ensure()

    # Forcing a refresh does probe again.
    probing = InstancePool(
        httpx.Client(transport=httpx.MockTransport(handler)), cache_path=path
    )
    assert probing.ensure(force=True)


def test_demote_sinks_an_instance():
    pool = InstancePool(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        cache_path=Path("/tmp/x-mcp-demote-cache.json"),
    )
    for host, points in [("a.example.com", 100), ("b.example.com", 90)]:
        pool.instances.append(
            ProbedInstance(
                host=host,
                url=f"https://{host}",
                status=InstanceStatus.OK,
                latency_ms=100,
                search_enabled=False,
                points=points,
                healthy=True,
                nitter_version="",
                source="tracker",
            )
        )
    pool.demote("a.example.com", "403")
    assert pool.instances[0].host == "b.example.com"
    assert pool.get("a.example.com").status is InstanceStatus.ERROR
    assert pool.get("a.example.com").failure_reason == "403"


def test_concurrency_is_bounded():
    pool = make_pool(Path("/tmp/x-mcp-concurrency.json"))
    assert pool.concurrency <= 8
