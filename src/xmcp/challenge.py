"""Anubis proof-of-work challenge solver.

Anubis (https://github.com/TecharoHQ/anubis) gates some Nitter mirrors. The
challenge page embeds the rules and the challenge as JSON script tags:

    <script id="anubis_challenge" type="application/json">
      {"rules": {"algorithm": "fast", "difficulty": 4},
       "challenge": {"id": "...", "randomData": "<hex>", ...}}
    </script>
    <script id="anubis_base_prefix" type="application/json">""</script>

For the sha256-based algorithms ("fast" and "slow") the server accepts a nonce
whose sha256 over ``randomData + str(nonce)`` has ``difficulty`` leading zero
bits: floor(difficulty/2) zero bytes plus, when difficulty is odd, a zero high
nibble. We brute force the nonce and hit the pass-challenge endpoint with the
same cookie jar and User-Agent (the cookie is bound to the UA).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

PASS_CHALLENGE_PATH = "/.within.website/x/cmd/anubis/api/pass-challenge"
SUPPORTED_ALGORITHMS = {"fast", "slow"}  # both are plain sha256


class UnsupportedChallenge(Exception):
    """Raised when a challenge cannot be solved with this solver."""


@dataclass
class AnubisChallenge:
    algorithm: str
    difficulty: int
    challenge_id: str
    random_data: str
    base_prefix: str

    def solve(self) -> tuple[int, str]:
        """Return (nonce, hex hash) satisfying the difficulty."""
        if self.algorithm not in SUPPORTED_ALGORITHMS:
            raise UnsupportedChallenge(
                f"Anubis algorithm {self.algorithm!r} is not supported "
                "(only sha256-based 'fast' and 'slow'); a browser is required"
            )
        # Anubis wants `difficulty` leading zero hex chars, which the JS
        # expresses as floor(difficulty/2) zero bytes plus a zero high nibble
        # when the difficulty is odd.
        zero_bytes = self.difficulty // 2
        prefix = "00" * zero_bytes
        odd = self.difficulty % 2 == 1
        nonce = 0
        while True:
            digest = hashlib.sha256(
                (self.random_data + str(nonce)).encode()
            ).hexdigest()
            if digest.startswith(prefix) and (not odd or digest[len(prefix)] == "0"):
                return nonce, digest
            nonce += 1
            if nonce > 10_000_000:
                raise UnsupportedChallenge(
                    f"nonce search exhausted at difficulty {self.difficulty}"
                )

    def pass_challenge_url(self, redir: str, elapsed_ms: int) -> str:
        nonce, digest = self.solve()
        # Anubis's own JS concatenates the base prefix with the endpoint path;
        # urljoin would drop a non-empty prefix path, so concatenate directly.
        base = self.base_prefix.rstrip("/") + PASS_CHALLENGE_PATH
        return (
            f"{base}?id={quote(self.challenge_id)}"
            f"&response={digest}&nonce={nonce}"
            f"&redir={quote(redir, safe='')}&elapsedTime={elapsed_ms}"
        )


def detect_challenge(html: str) -> AnubisChallenge | None:
    """Return the embedded Anubis challenge, or None if the page has none."""
    soup = BeautifulSoup(html, "lxml")
    script = soup.select_one("script#anubis_challenge")
    if script is None:
        return None
    try:
        data = json.loads(script.get_text())
    except json.JSONDecodeError as exc:
        raise UnsupportedChallenge(f"malformed anubis_challenge JSON: {exc}") from exc

    rules = data.get("rules", {})
    challenge = data.get("challenge", {})
    algorithm = (challenge.get("method") or rules.get("algorithm") or "").strip()
    difficulty = int(challenge.get("difficulty") or rules.get("difficulty") or 0)
    random_data = challenge.get("randomData") or ""
    challenge_id = challenge.get("id") or ""
    if not algorithm or not random_data or not challenge_id:
        raise UnsupportedChallenge("anubis challenge JSON is incomplete")

    base_prefix = ""
    prefix_script = soup.select_one("script#anubis_base_prefix")
    if prefix_script is not None:
        try:
            base_prefix = json.loads(prefix_script.get_text()) or ""
        except json.JSONDecodeError:
            base_prefix = ""

    return AnubisChallenge(
        algorithm=algorithm,
        difficulty=difficulty,
        challenge_id=challenge_id,
        random_data=random_data,
        base_prefix=base_prefix,
    )


def is_anubis_page(html: str) -> bool:
    """Cheap check: does this HTML look like an Anubis challenge page?"""
    return "anubis_challenge" in html or "Making sure you're not a bot" in html


def solve_and_pass(
    client: httpx.Client,
    challenge_page: httpx.Response,
    original_path: str,
) -> httpx.Response:
    """Solve the challenge in ``challenge_page`` and re-request ``original_path``.

    Uses the same client (cookie jar + headers), so the auth cookies Anubis
    sets are carried into the retry.
    """
    started = time.monotonic()
    challenge = detect_challenge(challenge_page.text)
    if challenge is None:
        raise UnsupportedChallenge("page looks like a challenge but has no JSON payload")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    url = challenge.pass_challenge_url(original_path, elapsed_ms)
    # An empty base_prefix means the endpoint lives on the same host, so
    # resolve the relative path against the challenge page URL.
    if url.startswith("/"):
        url = urljoin(str(challenge_page.url), url)
    resp = client.get(url)
    resp.raise_for_status()
    return resp
