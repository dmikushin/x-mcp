from pathlib import Path

from xmcp.parse import (
    is_empty_timeline,
    parse_profile,
    parse_timeline,
    parse_tweet_page,
    parse_user_search,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://nitter.example.com"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_profile_timeline_has_about_20_tweets():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    assert len(profile.timeline.tweets) == 21


def test_first_tweet_is_pinned_with_quote():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    first = profile.timeline.tweets[0]
    assert first.id == "2114765150431682772"
    assert first.pinned is True
    assert first.url == "https://x.com/elonmusk/status/2114765150431682772"
    assert first.author == "elonmusk"
    assert first.author_name == "Elon Musk"
    assert first.date == "2026-10-26T17:04:00Z"
    assert "That was dumb." in first.text
    assert first.quote is not None
    assert first.quote.author == "XFreeze"
    assert first.quote.id == "2114725666537283669"
    assert first.quote.url == "https://x.com/XFreeze/status/2114725666537283669"
    assert "woke mind virus" in first.quote.text


def test_first_tweet_stats_are_numeric():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    stats = profile.timeline.tweets[0].stats
    assert stats.likes == 113952
    assert stats.replies == 2848
    assert stats.retweets == 10431
    assert stats.views == 4813141


def test_retweet_has_retweeted_by():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    retweets = [t for t in profile.timeline.tweets if t.retweeted_by]
    assert retweets
    assert retweets[0].author == "SawyerMerritt"
    assert retweets[0].retweeted_by == "Elon Musk"
    assert all(t.pinned is False for t in retweets)


def test_photos_map_to_pbs_twimg_com():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    photos = [m for t in profile.timeline.tweets for m in t.media if m.kind == "photo"]
    assert len(photos) >= 3
    first = photos[0]
    assert first.url == "https://nitter.example.com/pic/orig/media%2FHVpSbFiWMAAIy4y.jpg"
    assert first.original == "https://pbs.twimg.com/media/HVpSbFiWMAAIy4y.jpg?name=orig"
    for photo in photos:
        assert photo.original.startswith("https://pbs.twimg.com/media/")


def test_gif_and_video_are_classified():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    kinds = {m.kind for t in profile.timeline.tweets for m in t.media}
    assert "gif" in kinds
    assert "video" in kinds
    assert "card" in kinds

    gifs = [m for t in profile.timeline.tweets for m in t.media if m.kind == "gif"]
    assert gifs[0].original.startswith("https://video.twimg.com/tweet_video/")
    assert gifs[0].poster.startswith(
        "https://nitter.example.com/pic/tweet_video_thumb%2F"
    )

    videos = [
        m
        for t in profile.timeline.tweets
        for m in t.media
        if m.kind == "video" and m.original
    ]
    assert videos[0].original.startswith("https://video.twimg.com/amplify_video/")
    assert videos[0].original.endswith(".mp4?tag=28")

    # A gallery-video without a download link only offers HLS.
    hls_only = [m for m in profile.timeline.tweets[-1].media if m.kind == "video" and m.hls_referer]
    assert hls_only


def test_next_cursor_is_present():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    cursor = profile.timeline.next_cursor
    assert cursor and cursor.startswith("DAAHCgABHVqayFi")


def test_profile_card_fields():
    profile = parse_profile(_fixture("profile_elonmusk.html"), BASE)
    card = profile.card
    assert card.fullname == "Elon Musk"
    assert card.username == "elonmusk"
    assert card.bio == "Terafab.AI"
    assert card.tweets == 111048
    assert card.following == 1422
    assert card.followers == 241539699
    assert card.likes == 255723
    assert card.joined == "8:12 PM - 2 Jun 2009"
    assert card.avatar.startswith("https://nitter.example.com/pic/")
    assert card.location == ""


def test_empty_profile_is_not_an_error():
    html = _fixture("profile_empty.html")
    assert is_empty_timeline(html) is True
    profile = parse_profile(html, BASE)
    assert profile.timeline.tweets == []
    assert profile.timeline.next_cursor is None
    assert profile.card.username == "jack"
    assert profile.card.bio == "no state is the best state"
    assert profile.card.website == "http://primal.net/jack"


def test_search_results():
    timeline = parse_timeline(_fixture("search.html"), BASE)
    assert len(timeline.tweets) == 20
    assert timeline.next_cursor
    assert timeline.tweets[0].author
    assert timeline.tweets[0].url.startswith("https://x.com/")


def test_tweet_page_has_main_and_replies():
    page = parse_tweet_page(_fixture("status_thread.html"), BASE)
    assert page.main is not None
    assert page.main.id == "2114765150431682772"
    assert page.main.quote is not None
    assert len(page.replies) >= 1
    assert page.replies[0].replying_to == "elonmusk"
    assert page.before == []
    assert page.after == []
    # The main tweet must not be duplicated inside the replies list.
    reply_ids = {r.id for r in page.replies}
    assert page.main.id not in reply_ids


def test_user_search_results():
    html = """
    <html><body>
      <div class="timeline">
        <div class="timeline-item" data-username="NASA">
          <a class="tweet-link" href="/NASA"></a>
          <div class="tweet-body profile-result">
            <div class="tweet-header">
              <a class="tweet-avatar" href="/NASA">
                <img class="avatar round" src="/pic/profile_images%2F1%2Fa_bigger.jpg"/>
              </a>
              <div class="tweet-name-row">
                <a class="fullname" href="/NASA" title="NASA">NASA</a>
                <a class="username" href="/NASA" title="@NASA">@NASA</a>
              </div>
            </div>
            <div class="tweet-content media-body">Making the seemingly possible</div>
          </div>
        </div>
        <div class="show-more"><a href="?cursor=abc123">Load more</a></div>
      </div>
    </body></html>
    """
    users, cursor = parse_user_search(html, BASE)
    assert len(users) == 1
    assert users[0].username == "NASA"
    assert users[0].fullname == "NASA"
    assert users[0].bio == "Making the seemingly possible"
    assert users[0].url == "https://x.com/NASA"
    assert users[0].avatar.startswith("https://nitter.example.com/pic/")
    assert cursor == "abc123"
