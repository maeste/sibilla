"""Source plugins: fetchers parsed from recorded payloads, no network."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sibilla.config import ArxivConfig, HackerNewsConfig, RedditConfig, XConfig
from sibilla.sources.arxiv import ArxivSource
from sibilla.sources.base import SourceFetchError, truncate_to_budget
from sibilla.sources.hackernews import HackerNewsSource
from sibilla.sources.reddit import RedditSource
from sibilla.sources.twitter_x import XSource

SINCE = datetime(2026, 9, 26, 6, 0, tzinfo=timezone.utc)

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2609.12345v1</id>
    <title>  Sparse Attention
      Revisited </title>
    <summary>We propose a sparse attention variant cutting memory by 10x.</summary>
    <published>2026-09-27T00:30:00Z</published>
    <updated>2026-09-27T00:30:00Z</updated>
    <author><name>Ada Lovelace</name></author>
    <author><name>Grace Hopper</name></author>
    <author><name>Third Author</name></author>
    <author><name>Fourth</name></author>
    <author><name>Fifth</name></author>
    <arxiv:primary_category xmlns:arxiv="http://arxiv.org/schemas/atom" term="cs.LG"/>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <title>Old paper</title>
    <summary>From 2024.</summary>
    <published>2024-01-01T00:00:00Z</published>
    <updated>2024-01-01T00:00:00Z</updated>
    <author><name>Old Author</name></author>
  </entry>
</feed>
"""


class _Resp:
    def __init__(self, payload=None, status=200, text=None):
        self._payload = payload
        self.status_code = status
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, responder):
        self.responder = responder
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        return self.responder(url, params)


def test_arxiv_fetch_window_and_parsing():
    session = _Session(lambda url, params: _Resp(text=ATOM))
    src = ArxivSource(ArxivConfig(categories=["cs.LG"]), session=session)
    items = src.fetch(SINCE)
    assert len(items) == 1  # 2024 paper is outside the 24h window (sorted descending)
    item = items[0]
    assert item.native_id == "2609.12345v1"
    assert item.title == "Sparse Attention Revisited"  # whitespace collapsed
    assert item.author == "Ada Lovelace, Grace Hopper, Third Author, Fourth et al."
    assert item.meta["arxiv_base_id"] == "2609.12345"
    assert "cs.LG" in str(item.meta["primary_category"])
    assert item.body.startswith("Sparse Attention Revisited\n\nWe propose")


def test_arxiv_http_error_raises_source_fetch_error():
    session = _Session(lambda url, params: _Resp(status=503, text="slow down"))
    with pytest.raises(SourceFetchError):
        ArxivSource(ArxivConfig(), session=session).fetch(SINCE)


HN_HITS = {
    "hits": [
        {
            "objectID": "1",
            "title": "Sparse attention paper discussion",
            "url": "https://arxiv.org/abs/2609.12345",
            "author": "hnuser",
            "created_at_i": int(SINCE.timestamp()) + 3600,
            "points": 120,
            "num_comments": 44,
        },
        {
            "objectID": "2",
            "title": "Too old",
            "url": None,
            "author": "x",
            "created_at_i": int(SINCE.timestamp()) - 99999,
            "points": 500,
            "num_comments": 9,
        },
        {
            "objectID": "3",
            "title": "Low points, pre-cut",
            "url": None,
            "author": "y",
            "created_at_i": int(SINCE.timestamp()) + 7200,
            "points": 3,
            "num_comments": 1,
        },
    ]
}


def test_hn_min_points_threshold_and_window():
    def responder(url, params):
        assert "hn.algolia.com" in url
        if "search_by_date" in url:  # emulate Algolia numericFilters server-side
            points_min = int(params["numericFilters"].split("points>=")[1].split(",")[0])
            hits = [h for h in HN_HITS["hits"] if (h.get("points") or 0) >= points_min]
            return _Resp({"hits": hits})
        return _Resp({"hits": []})  # front_page: empty snapshot

    items = HackerNewsSource(HackerNewsConfig(min_points=40), _Session(responder)).fetch(SINCE)
    ids = {i.native_id for i in items}
    assert ids == {"1"}  # old → window cut; low points → deterministic pre-cut


def test_hn_top_comments_from_session():
    """Comment excerpts ride the same session — no hidden network, no real calls."""

    def responder(url, params):
        if "/items/" in url:
            return _Resp({"children": [{"text": "<p>the benchmark is real</p>"}]})
        return _Resp({"hits": [HN_HITS["hits"][0]]})

    session = _Session(responder)
    items = HackerNewsSource(HackerNewsConfig(min_points=40), session).fetch(SINCE)
    assert "top comments:" in items[0].body
    assert "the benchmark is real" in items[0].body  # HTML stripped to prose


def test_hn_error_raises():
    with pytest.raises(SourceFetchError):
        HackerNewsSource(HackerNewsConfig(), _Session(lambda u, p: _Resp(status=500))).fetch(SINCE)


REDDIT_PAYLOAD = {
    "data": {
        "children": [
            {
                "data": {
                    "id": "abc",
                    "title": "Sparse attention in practice",
                    "selftext": "Tried it, works.",
                    "author": "ruser",
                    "created_utc": int(SINCE.timestamp()) + 60,
                    "ups": 80,
                    "permalink": "/r/LocalLLaMA/comments/abc/",
                    "num_comments": 12,
                }
            },
            {
                "data": {
                    "id": "low",
                    "title": "upvote pre-filter",
                    "selftext": "",
                    "author": "l",
                    "created_utc": int(SINCE.timestamp()) + 60,
                    "ups": 5,
                    "permalink": "/r/LocalLLaMA/comments/low/",
                }
            },
        ]
    }
}


def test_reddit_min_upvotes():
    session = _Session(lambda url, params: _Resp(REDDIT_PAYLOAD))
    items = RedditSource(RedditConfig(subreddits=["LocalLLaMA"], min_upvotes=50), session).fetch(SINCE)
    assert [i.native_id for i in items] == ["abc"]
    assert items[0].url.endswith("/comments/abc/")
    assert items[0].meta["subreddit"] == "LocalLLaMA"


def test_reddit_error_raises():
    with pytest.raises(SourceFetchError):
        RedditSource(RedditConfig(), _Session(lambda u, p: _Resp(status=403))).fetch(SINCE)


def test_x_export_transport(tmp_path):
    export = tmp_path / "tweets.json"
    export.write_text(
        json.dumps(
            [
                {
                    "id": "100",
                    "author": "karpathy",
                    "created_at": "2026-09-27T01:00:00Z",
                    "text": "released a new repo: same-marker attention",
                },
                {"id": "101", "author": "someoneelse", "created_at": "2026-09-27T01:00:00Z", "text": "not monitored"},
                {"id": "102", "author": "karpathy", "created_at": "2020-01-01T00:00:00Z", "text": "ancient"},
            ]
        ),
        encoding="utf-8",
    )
    src = XSource(XConfig(profiles=["karpathy"], transport="export", export_path=str(export)))
    items = src.fetch(SINCE)
    assert [i.native_id for i in items] == ["100"]  # profile filter + window filter
    assert items[0].url == "https://x.com/karpathy/status/100"


def test_x_bridge_rss_transport():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>new model dropped: same-marker v2</title>
        <link>https://x.com/karpathy/status/200</link>
        <pubDate>Sat, 26 Sep 2026 23:00:00 GMT</pubDate></item>
    </channel></rss>"""

    session = _Session(lambda url, params: _Resp(text=rss))
    src = XSource(XConfig(profiles=["karpathy"], transport="bridge", bridge_url="https://bridge.example"), session)
    items = src.fetch(SINCE)
    assert len(items) == 1
    assert items[0].native_id == "200"


def test_x_transport_gate_defaults_closed(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    src = XSource(XConfig(profiles=["karpathy"], transport="auto"))
    assert src.fetch(SINCE) == []  # no transport configured: the v2 gate stays closed


def test_truncation_is_deterministic():
    cut, flag = truncate_to_budget("a" * 50, 10)
    assert cut == "a" * 10 and flag is True
    intact, flag = truncate_to_budget("short", 10)
    assert intact == "short" and flag is False
