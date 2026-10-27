import hashlib
from pathlib import Path

import pytest

from xmcp.challenge import (
    AnubisChallenge,
    UnsupportedChallenge,
    detect_challenge,
    is_anubis_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_anubis_fixture_is_detected():
    html = _fixture("anubis_challenge.html")
    assert is_anubis_page(html)
    challenge = detect_challenge(html)
    assert challenge is not None
    assert challenge.algorithm == "fast"
    assert challenge.difficulty == 4
    assert challenge.challenge_id == "01a17f2c-2c4b-7e6a-b2fa-303fa303f709"
    assert challenge.random_data.startswith("9f1259f993c6ac00")
    assert challenge.base_prefix == ""


def test_anubis_solver_meets_difficulty():
    challenge = detect_challenge(_fixture("anubis_challenge.html"))
    nonce, digest = challenge.solve()
    assert digest.startswith("0000")
    # Independently re-verify the hash the server would check.
    assert digest == hashlib.sha256((challenge.random_data + str(nonce)).encode()).hexdigest()
    assert nonce >= 0


def test_odd_difficulty_requires_zero_nibble():
    challenge = AnubisChallenge(
        algorithm="slow",
        difficulty=5,
        challenge_id="abc",
        random_data="deadbeef",
        base_prefix="",
    )
    nonce, digest = challenge.solve()
    assert digest.startswith("00000")
    assert nonce >= 0


def test_unknown_algorithm_raises():
    challenge = AnubisChallenge(
        algorithm="argon2id",
        difficulty=4,
        challenge_id="abc",
        random_data="deadbeef",
        base_prefix="",
    )
    with pytest.raises(UnsupportedChallenge):
        challenge.solve()


def test_pass_challenge_url_is_exact():
    challenge = AnubisChallenge(
        algorithm="fast",
        difficulty=1,
        challenge_id="01a17f2c-2c4b-7e6a-b2fa-303fa303f709",
        random_data="9f1259f993c6ac00",
        base_prefix="",
    )
    nonce, digest = challenge.solve()
    url = challenge.pass_challenge_url("/NASA")
    prefix, elapsed = url.rsplit("&elapsedTime=", 1)
    assert prefix == (
        "/.within.website/x/cmd/anubis/api/pass-challenge"
        f"?id=01a17f2c-2c4b-7e6a-b2fa-303fa303f709"
        f"&response={digest}&nonce={nonce}&redir=%2FNASA"
    )
    assert elapsed.isdigit()
    assert digest.startswith("0")


def test_base_prefix_is_honoured():
    challenge = AnubisChallenge(
        algorithm="fast",
        difficulty=1,
        challenge_id="x",
        random_data="abc",
        base_prefix="https://mirror.example.com/anubis",
    )
    url = challenge.pass_challenge_url("/foo")
    assert url.startswith(
        "https://mirror.example.com/anubis/.within.website/x/cmd/anubis/api/pass-challenge"
    )
    assert "&redir=%2Ffoo&elapsedTime=" in url


def test_gandalf_page_is_not_anubis():
    html = _fixture("gandalf.html")
    assert not is_anubis_page(html)
    assert detect_challenge(html) is None


def test_cloudflare_page_is_not_anubis():
    html = _fixture("cloudflare.html")
    assert not is_anubis_page(html)
    assert detect_challenge(html) is None


def test_content_pages_have_no_challenge():
    for name in (
        "profile_elonmusk.html",
        "status_thread.html",
        "search.html",
        "profile_empty.html",
    ):
        html = _fixture(name)
        assert not is_anubis_page(html)
        assert detect_challenge(html) is None
