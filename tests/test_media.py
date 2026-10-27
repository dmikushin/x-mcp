from pathlib import Path

import httpx

from xmcp.media import MediaDownloader
from xmcp.parse import Media, parse_profile

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://nitter.example.com"

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 200
MP4 = b"\x00\x00\x00\x18ftypmp4" + b"x" * 200


def test_photo_download_uses_mirror_proxy_then_pbs():
    profile = parse_profile((FIXTURES / "profile_elonmusk.html").read_text(), BASE)
    photos = [m for t in profile.timeline.tweets for m in t.media if m.kind == "photo"]
    assert photos

    tried = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(str(request.url))
        if request.url.path.startswith("/pic/orig/media/"):
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
        return httpx.Response(404, text="nope")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = MediaDownloader(client, instance_url=BASE)
    out = Path("/tmp/x-mcp-media-test")
    result = downloader.download(photos[0], "2114765150431682772", out, 1)
    assert len(result) == 1 and "path" in result[0]
    path = Path(result[0]["path"])
    assert path.name == "2114765150431682772_1.jpg"
    assert path.read_bytes().startswith(b"\xff\xd8")
    assert tried[0] == photos[0].url


def test_photo_falls_back_to_pbs_when_proxy_fails():
    media = Media(
        kind="photo",
        url="https://nitter.example.com/pic/orig/media%2Fabc.jpg",
        original="https://pbs.twimg.com/media/abc.jpg?name=orig",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "pbs.twimg.com" in request.url.host:
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = MediaDownloader(client, instance_url=BASE)
    out = Path("/tmp/x-mcp-media-test-fallback")
    result = downloader.download(media, "123", out, 1)
    assert "path" in result[0]
    assert Path(result[0]["path"]).read_bytes().startswith(b"\x89PNG")
    assert result[0]["path"].endswith(".jpg")  # extension comes from the URL


def test_html_response_is_not_saved_as_media():
    media = Media(
        kind="gif",
        url="https://nitter.example.com/pic/video.twimg.com%2Ftweet_video%2Fabc.mp4",
        original="https://video.twimg.com/tweet_video/abc.mp4",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Both candidates answer with an Anubis challenge page.
        return httpx.Response(200, text="<html><title>Making sure you're not a bot!</title></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = MediaDownloader(client, instance_url=BASE)
    result = downloader.download(media, "123", Path("/tmp/x-mcp-media-test-html"), 1)
    assert "path" not in result[0]
    assert "skipped_reason" in result[0]


def test_video_download_prefers_direct_mp4():
    media = Media(
        kind="video",
        url="https://nitter.example.com/video/ABC/https%3A%2F%2Fvideo.twimg.com%2Fv.mp4",
        original="https://video.twimg.com/v.mp4",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/video/"):
            return httpx.Response(403, text="blocked")
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = MediaDownloader(client, instance_url=BASE)
    result = downloader.download(media, "123", Path("/tmp/x-mcp-media-test-video"), 1)
    assert "path" in result[0]
    assert result[0]["path"].endswith(".mp4")
    assert Path(result[0]["path"]).read_bytes().startswith(b"\x00\x00\x00\x18ftyp")


def test_existing_file_of_same_size_is_not_refetched():
    media = Media(
        kind="photo",
        url="https://nitter.example.com/pic/orig/media%2Fabc.jpg",
        original=None,
    )
    out = Path("/tmp/x-mcp-media-test-existing")
    out.mkdir(parents=True, exist_ok=True)
    target = out / "123_1.jpg"
    target.write_bytes(JPEG)

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, headers={"content-length": str(len(JPEG))}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    downloader = MediaDownloader(client, instance_url=BASE)
    result = downloader.download(media, "123", out, 1)
    assert result[0]["path"] == str(target)
    assert result[0]["note"] == "already downloaded"
    assert len(calls) == 1  # only a HEAD, no re-download


def test_card_media_is_skipped():
    media = Media(kind="card", url="https://example.com/article")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    downloader = MediaDownloader(client, instance_url=BASE)
    result = downloader.download(media, "123", Path("/tmp/x-mcp-media-test-card"), 1)
    assert "skipped_reason" in result[0]
