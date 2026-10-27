"""Download media attached to tweets.

Photos go through the serving instance's /pic/ proxy (no challenge needed),
falling back to the derived pbs.twimg.com URL. Videos are fetched from the
direct mp4 URL Nitter exposes via the download link; m3u8-only streams are
handled with ffmpeg when it is installed, otherwise reported as skipped.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import httpx

from .parse import Media

DEFAULT_DIR = Path("x-media")
CHUNK = 1 << 16
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _safe_name(tweet_id: str, index: int, ext: str) -> str:
    return f"{tweet_id}_{index}.{ext}"


def _extension(media: Media) -> str:
    if media.kind == "photo":
        for candidate in (media.original or media.url, media.url):
            for known in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                if candidate.lower().endswith(known):
                    return known.lstrip(".")
        return "jpg"
    return "mp4"


def _download(client: httpx.Client, url: str, target: Path) -> Path | None:
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    # A mirror can answer a media URL with an Anubis/Cloudflare HTML page;
    # saving that as an image or mp4 would be silently wrong.
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type or "application/json" in content_type:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("wb") as fh:
        for chunk in resp.iter_bytes(chunk_size=CHUNK):
            if written == 0 and chunk.lstrip()[:10].startswith((b"<!doctype", b"<html", b"<?xml")):
                return None
            fh.write(chunk)
            written += len(chunk)
    if written == 0:
        return None
    return target


class MediaDownloader:
    def __init__(self, client: httpx.Client, *, instance_url: str) -> None:
        self.client = client
        self.instance_url = instance_url

    def _candidates(self, media: Media) -> list[str]:
        """URLs to try, in order: mirror proxy first, origin second."""
        urls: list[str] = []
        if media.url:
            urls.append(
                media.url if media.url.startswith("http") else self.instance_url + media.url
            )
        if media.original:
            urls.append(media.original)
        return urls

    def download(self, media: Media, tweet_id: str, directory: Path, index: int = 1) -> list[dict]:
        """Return [{kind, path}] for saved files or [{kind, skipped_reason}]."""
        directory = Path(directory)

        if media.kind == "card":
            return [{"kind": "card", "skipped_reason": "link cards have no downloadable media"}]
        if media.kind not in ("photo", "gif", "video"):
            return [{"kind": media.kind, "skipped_reason": "unknown media kind"}]

        candidates = self._candidates(media)
        if not candidates and media.hls_referer:
            # Nitter hides the mp4 behind an "Enable hls playback" form; the
            # resulting page contains an m3u8 playlist which ffmpeg can turn
            # into an mp4.
            m3u8 = self._discover_m3u8(media.hls_referer)
            if m3u8:
                return [self.download_m3u8(m3u8, directory / _safe_name(tweet_id, index, "mp4"))]
        if not candidates:
            return [{"kind": media.kind, "skipped_reason": "no direct media URL on the mirror"}]

        target = directory / _safe_name(tweet_id, index, _extension(media))
        if target.exists():
            remote_size = None
            for candidate in candidates:
                try:
                    head = self.client.head(candidate)
                    remote_size = int(head.headers.get("content-length", 0))
                except (httpx.HTTPError, ValueError):
                    continue
                if remote_size and remote_size == target.stat().st_size:
                    break
            else:
                # Size differs or is unknown: re-fetch to keep the file correct.
                saved = None
                for candidate in candidates:
                    saved = _download(self.client, candidate, target)
                    if saved is not None:
                        break
                if saved is None:
                    return [{"kind": media.kind, "skipped_reason": "download failed"}]
            return [{"kind": media.kind, "path": str(target), "note": "already downloaded"}]

        saved = None
        for candidate in candidates:
            saved = _download(self.client, candidate, target)
            if saved is not None:
                break
        if saved is None:
            return [{"kind": media.kind, "skipped_reason": f"could not download {candidates}"}]
        return [{"kind": media.kind, "path": str(saved)}]

    def _discover_m3u8(self, referer: str) -> str | None:
        """POST /enablehls and look for the m3u8 playlist in the result page."""
        try:
            resp = self.client.post(self.instance_url + "/enablehls", data={"referer": referer})
        except httpx.HTTPError:
            return None
        for match in re.findall(r'"([^"]+\.m3u8[^"]*)"', resp.text):
            return match if match.startswith("http") else self.instance_url + match
        return None

    def download_m3u8(self, m3u8_url: str, target: Path) -> dict:
        """Download an HLS stream with ffmpeg when it is available."""
        if shutil.which("ffmpeg") is None:
            return {
                "kind": "video",
                "skipped_reason": "only an m3u8 stream is available and ffmpeg is not installed",
            }
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                m3u8_url,
                "-c",
                "copy",
                str(target),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return {
                "kind": "video",
                "skipped_reason": f"ffmpeg failed: {proc.stderr.strip()[:200]}",
            }
        return {"kind": "video", "path": str(target)}
