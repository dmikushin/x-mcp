from pathlib import Path

import pytest

from xmcp.tracker import parse_tracker_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_tracker_parses_all_rows():
    html = (FIXTURES / "tracker_d420.html").read_text()
    entries = parse_tracker_html(html)
    assert len(entries) >= 10


def test_tracker_jaydenha_is_healthy():
    entries = parse_tracker_html((FIXTURES / "tracker_d420.html").read_text())
    jaydenha = [e for e in entries if e.host == "nitter.jaydenha.uk"]
    assert len(jaydenha) == 1
    entry = jaydenha[0]
    assert entry.healthy is True
    assert entry.health_label == "healthy"
    assert entry.url == "https://nitter.jaydenha.uk"
    assert entry.latency_ms == 351
    assert entry.all_time_percent == 90.0
    assert entry.rss is True
    assert entry.version_badge == "fork"
    assert entry.connectivity == "IPv4"
    assert entry.points == 74


def test_tracker_points_are_ints():
    entries = parse_tracker_html((FIXTURES / "tracker_d420.html").read_text())
    assert all(isinstance(e.points, int) for e in entries)
    assert sum(e.points for e in entries) > 0


def test_tracker_version_badges():
    entries = parse_tracker_html((FIXTURES / "tracker_d420.html").read_text())
    badges = {e.host: e.version_badge for e in entries}
    assert badges["nitter.tiekoetter.com"] == "latest"
    assert badges["nitter.mie.foundation"] == "missing"
    assert badges["nitter.click"] == "outdated"
    assert badges["nitter.netbub.com"] == "fork"


def test_tracker_missing_table_raises():
    with pytest.raises(ValueError):
        parse_tracker_html("<html><body>no table here</body></html>")
