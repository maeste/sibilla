"""Hacker News source — v1 (docs/02).

Algolia Search API (hn.algolia.com/api/v1), front-page snapshot +
by-date search with a min-points threshold to pre-cut noise
deterministically. Item body: title + URL domain + top comments excerpt
(char-budgeted). JudgeConfig focus: signal vs. discourse.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from sibilla.config import HackerNewsConfig
from sibilla.judge.configs import HACKERNEWS_CONFIG
from sibilla.sources.base import RawItem, SourceFetchError

ALGOLIA = "https://hn.algolia.com/api/v1"
COMMENT_EXCERPT_CHARS = 1200


class HackerNewsSource:
    name = "hackernews"
    judge_config = HACKERNEWS_CONFIG

    def __init__(self, cfg: HackerNewsConfig | None = None, session: requests.Session | None = None):
        self.cfg = cfg or HackerNewsConfig()
        self.session = session or requests.Session()

    def fetch(self, since: datetime) -> list[RawItem]:
        since = since.astimezone(timezone.utc)
        numeric = f"points>={self.cfg.min_points},created_at_i>{int(since.timestamp())}"
        stories: dict[str, dict[str, Any]] = {}

        front = self._search("search", {"tags": "front_page"})
        for hit in front:
            stories[hit["objectID"]] = hit

        by_date = self._search("search_by_date", {"tags": "story", "numericFilters": numeric, "hitsPerPage": 200})
        for hit in by_date:
            stories.setdefault(hit["objectID"], hit)

        out: list[RawItem] = []
        for hit in stories.values():
            created = _hit_ts(hit)
            if created is None or created < since:
                continue
            out.append(_to_raw_item(hit, created, self.session))
        return out

    def _search(self, endpoint: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            resp = self.session.get(f"{ALGOLIA}/{endpoint}", params=params, timeout=30)
        except requests.RequestException as exc:
            raise SourceFetchError(f"Algolia API unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise SourceFetchError(f"Algolia API {resp.status_code}")
        return list(resp.json().get("hits") or [])


def _hit_ts(hit: dict[str, Any]) -> datetime | None:
    ts = hit.get("created_at_i")
    return datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None


def _to_raw_item(hit: dict[str, Any], created: datetime, session: requests.Session | None = None) -> RawItem:
    object_id = hit["objectID"]
    title = hit.get("title") or hit.get("story_title") or "(untitled)"
    url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
    body_parts = []
    text = (hit.get("story_text") or hit.get("comment_text") or "").strip()
    if text:
        body_parts.append(_strip_html(text))
    comments = _top_comments(object_id, session)
    if comments:
        body_parts.append("top comments: " + " | ".join(comments))
    return RawItem(
        source="hackernews",
        native_id=object_id,
        title=title,
        url=url,
        author=hit.get("author"),
        published=created,
        body="\n\n".join(body_parts)[: (COMMENT_EXCERPT_CHARS * 2)],
        meta={"points": hit.get("points") or hit.get("num_comments") or 0, "num_comments": hit.get("num_comments")},
    )


def _top_comments(object_id: str, session: requests.Session | None = None) -> list[str]:
    """Best comment excerpts (deterministic: Algolia relevance order, first 3)."""
    getter = session.get if session is not None else requests.get
    try:
        resp = getter(f"{ALGOLIA}/items/{object_id}", timeout=10)
    except requests.RequestException:
        return []
    if resp.status_code != 200:
        return []
    data = resp.json()
    out: list[str] = []
    for child in (data.get("children") or [])[:3]:
        text = _strip_html((child.get("text") or "").strip())
        if text:
            out.append(text[: COMMENT_EXCERPT_CHARS // 3])
    return out


def _strip_html(text: str) -> str:
    """Algolia text carries HTML entities/tags; strip to plain prose."""
    import html
    import re

    text = html.unescape(text)
    return re.sub(r"<[^>]+>", " ", text).strip()
