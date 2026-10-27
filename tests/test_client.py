import hashlib
import json
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from xmcp.client import FetchError, NitterClient, normalize_tweet_path
from xmcp.instances import InstancePool, InstanceStatus, ProbedInstance

FIXTURES = Path(__file__).parent / "fixtures"
ANUBIS_PAGE = (FIXTURES / "anubis_challenge.html").read_text()
PROFILE_PAGE = (FIXTURES / "profile_elonmusk.html").read_text()
EMPTY_PAGE = (FIXTURES / "profile_empty.html").read_text()
GANDALF_PAGE = (FIXTURES / "gandalf.html").read_text()
CLOUDFLARE_PAGE = (FIXTURES / "cloudflare.html").read_text()

# host -> (search_enabled, points), in tracker ranking order.
HOSTS = [
    ("bad.example.com", False, 90),
    ("blocked.example.com", True, 80),
    ("empty.example.com", False, 70),
    ("good.example.com", True, 60),
]


def make_pool(responses: dict[str, object]) -> InstancePool:
    """Pool with seeded instances and mocked HTTP.

    ``responses`` maps a host to a body string, an (status, body) tuple or an
    exception to raise. Hosts missing from the map raise a connection error.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        behaviour = responses.get(host)
        if behaviour is None:
            raise httpx.ConnectError(f"no route for {host}")
        if isinstance(behaviour, Exception):
            raise behaviour
        if isinstance(behaviour, tuple):
            status, body = behaviour
            return httpx.Response(status, text=body)
        return httpx.Response(200, text=behaviour)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pool = InstancePool(client, cache_path=Path("/tmp/x-mcp-test-cache.json"))
    pool.instances = [
        ProbedInstance(
            host=host,
            url=f"https://{host}",
            status=InstanceStatus.OK,
            latency_ms=100,
            search_enabled=search,
            points=points,
            healthy=True,
            nitter_version="",
            source="tracker",
        )
        for host, search, points in HOSTS
    ]
    pool.probed_at = 0.0
    return pool


def test_failover_order_and_winner():
    pool = make_pool(
        {
            "bad.example.com": httpx.ConnectError("connection refused"),
            "blocked.example.com": (403, "forbidden"),
            "good.example.com": PROFILE_PAGE,
        }
    )
    client = NitterClient(pool)
    result = client.fetch("/elonmusk")
    assert result.instance.host == "good.example.com"
    assert result.status_code == 200
    assert "timeline-item" in result.html
    # The failing hosts were demoted so the ranking sinks them.
    assert pool.get("bad.example.com").status is InstanceStatus.ERROR
    assert pool.get("blocked.example.com").status is InstanceStatus.ERROR


def test_all_instances_fail_with_diagnostic_error():
    pool = make_pool(
        {
            "bad.example.com": httpx.ConnectError("connection refused"),
            "blocked.example.com": (403, "forbidden"),
            "empty.example.com": (503, "unavailable"),
            "good.example.com": (500, "boom"),
        }
    )
    client = NitterClient(pool)
    try:
        client.fetch("/elonmusk")
    except FetchError as exc:
        hosts = {f.host for f in exc.failures}
        assert hosts == {host for host, _, _ in HOSTS}
        assert "connection refused" in str(exc)
        assert "403 forbidden" in str(exc)
        assert "HTTP 500" in str(exc)
    else:
        raise AssertionError("FetchError was not raised")


def test_gandalf_and_cloudflare_are_not_used():
    pool = make_pool(
        {
            "bad.example.com": GANDALF_PAGE,
            "blocked.example.com": CLOUDFLARE_PAGE,
            "empty.example.com": EMPTY_PAGE,
            "good.example.com": PROFILE_PAGE,
        }
    )
    client = NitterClient(pool)
    result = client.fetch("/elonmusk")
    assert result.instance.host == "empty.example.com"
    assert pool.get("bad.example.com").status is InstanceStatus.ERROR
    assert pool.get("blocked.example.com").status is InstanceStatus.ERROR


def test_empty_timeline_is_a_valid_result_not_a_failover():
    pool = make_pool({"empty.example.com": EMPTY_PAGE})
    client = NitterClient(pool)
    result = client.fetch("/jack")
    assert result.instance.host == "empty.example.com"
    assert pool.get("empty.example.com").status is InstanceStatus.OK


def test_search_only_uses_search_enabled_instances():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host)
        if request.url.host == "good.example.com":
            return httpx.Response(200, text=(FIXTURES / "search.html").read_text())
        return httpx.Response(403, text="forbidden")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pool = InstancePool(client, cache_path=Path("/tmp/x-mcp-test-cache-search.json"))
    pool.instances = [
        ProbedInstance(
            host=host,
            url=f"https://{host}",
            status=InstanceStatus.OK,
            latency_ms=100,
            search_enabled=(host == "good.example.com"),
            points=points,
            healthy=True,
            nitter_version="",
            source="tracker",
        )
        for host, _search, points in HOSTS
    ]
    pool.probed_at = 0.0

    nitter = NitterClient(pool)
    result = nitter.fetch("/search?f=tweets&q=archlinux", search=True)
    assert result.instance.host == "good.example.com"
    assert "bad.example.com" not in requested
    assert "blocked.example.com" not in requested
    assert "empty.example.com" not in requested


def test_search_disabled_mirror_is_reported_but_not_demoted():
    pool = make_pool(
        {
            "blocked.example.com": "<html><title>Search disabled | nitter</title></html>",
        }
    )
    # Only the "blocked" host is flagged as searchable, and it has search off.
    pool.instances[1].search_enabled = True

    client = NitterClient(pool)
    try:
        client.fetch("/search?f=tweets&q=x", search=True)
    except FetchError as exc:
        assert "search disabled on this mirror" in str(exc)
    else:
        raise AssertionError("expected the search to fail")
    assert pool.get("blocked.example.com").status is InstanceStatus.OK


def test_pinned_instance_is_used_directly():
    pool = make_pool({"good.example.com": PROFILE_PAGE})
    client = NitterClient(pool)
    result = client.fetch("/elonmusk", instance="good.example.com")
    assert result.instance.host == "good.example.com"


def test_anubis_challenge_solved_end_to_end():
    """Challenge page -> PoW -> pass-challenge sets cookies -> content."""
    soup = BeautifulSoup(ANUBIS_PAGE, "lxml")
    payload = json.loads(soup.select_one("#anubis_challenge").get_text())
    random_data = payload["challenge"]["randomData"]
    difficulty = payload["rules"]["difficulty"]

    state = {"passed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.within.website/x/cmd/anubis/api/pass-challenge":
            nonce = int(request.url.params["nonce"])
            response = request.url.params["response"]
            expected = hashlib.sha256((random_data + str(nonce)).encode()).hexdigest()
            if response != expected:
                return httpx.Response(400, text="wrong answer")
            if not expected.startswith("00" * (difficulty // 2)):
                return httpx.Response(400, text="insufficient zeros")
            state["passed"] = True
            return httpx.Response(
                303,
                headers={
                    "Set-Cookie": "anubis_auth=xyz; Path=/",
                    "Location": request.url.params.get("redir", "/elonmusk"),
                },
            )
        if not state["passed"]:
            return httpx.Response(401, text=ANUBIS_PAGE)
        return httpx.Response(200, text=PROFILE_PAGE)

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
            )
        },
    )
    pool = InstancePool(client, cache_path=Path("/tmp/x-mcp-test-cache-anubis.json"))
    pool.instances = [
        ProbedInstance(
            host="anubis.example.com",
            url="https://anubis.example.com",
            status=InstanceStatus.OK,
            latency_ms=50,
            search_enabled=False,
            points=50,
            healthy=True,
            nitter_version="",
            source="tracker",
        )
    ]

    nitter = NitterClient(pool)
    result = nitter.fetch("/elonmusk")
    assert state["passed"] is True
    assert result.instance.host == "anubis.example.com"
    assert "timeline-item" in result.html
    assert client.cookies.get("anubis_auth") == "xyz"


def test_tweet_path_normalization():
    assert normalize_tweet_path("2114765150431682772") == "/i/status/2114765150431682772"
    assert (
        normalize_tweet_path("https://x.com/elonmusk/status/2114765150431682772")
        == "/elonmusk/status/2114765150431682772"
    )
    assert (
        normalize_tweet_path("https://twitter.com/elonmusk/status/2114765150431682772")
        == "/elonmusk/status/2114765150431682772"
    )
    assert (
        normalize_tweet_path("https://nitter.net/elonmusk/status/2114765150431682772#m")
        == "/elonmusk/status/2114765150431682772"
    )
    try:
        normalize_tweet_path("not a tweet")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid input should raise ValueError")
