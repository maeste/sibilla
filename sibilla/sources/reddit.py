"""Reddit source — v1 (docs/02).

Public JSON endpoints (/.json suffix), subreddit list from config (default
r/LocalLLaMA, r/MachineLearning, r/agents), min-upvote pre-filter.
Item body: title + selftext excerpt. JudgeConfig focus: community signal —
practitioner experience (gotchas, regressions, "don't bother with X").
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from sibilla.config import RedditConfig
from sibilla.judge.configs import REDDIT_CONFIG
from sibilla.sources.base import RawItem, SourceFetchError

USER_AGENT = "sibilla-research-radar/0.1 (personal; contact via GitHub)"
SELFTEXT_CHARS = 2400


class RedditSource:
    name = "reddit"
    judge_config = REDDIT_CONFIG

    def __init__(self, cfg: RedditConfig | None = None, session: requests.Session | None = None):
        self.cfg = cfg or RedditConfig()
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def fetch(self, since: datetime) -> list[RawItem]:
        since = since.astimezone(timezone.utc)
        out: list[RawItem] = []
        saw_any = False
        for sub in self.cfg.subreddits:
            try:
                resp = self.session.get(
                    f"https://www.reddit.com/r/{sub}/new.json",
                    params={"limit": 100},
                    timeout=20,
                )
            except requests.RequestException as exc:
                raise SourceFetchError(f"reddit unreachable: {exc}") from exc
            if resp.status_code != 200:
                raise SourceFetchError(f"reddit {resp.status_code} for r/{sub}")
            saw_any = True
            children = (resp.json().get("data") or {}).get("children") or []
            for child in children:
                post = child.get("data") or {}
                item = _to_raw_item(sub, post)
                if item.published is None or item.published < since:
                    continue
                if (post.get("ups") or 0) < self.cfg.min_upvotes:
                    continue  # deterministic pre-cut, docs/02
                out.append(item)
        if not saw_any:
            raise SourceFetchError("no subreddits configured")
        return out


def _to_raw_item(sub: str, post: dict[str, Any]) -> RawItem:
    created = datetime.fromtimestamp(int(post.get("created_utc") or 0), tz=timezone.utc)
    permalink = post.get("permalink") or ""
    url = f"https://www.reddit.com{permalink}" if permalink else (post.get("url") or None)
    selftext = (post.get("selftext") or "").strip()
    body = f"{post.get('title', '')}\n\n{selftext[:SELFTEXT_CHARS]}" if selftext else post.get("title", "")
    return RawItem(
        source="reddit",
        native_id=str(post.get("id") or ""),
        title=post.get("title") or "(untitled)",
        url=url,
        author=post.get("author"),
        published=created,
        body=body,
        meta={
            "subreddit": sub or post.get("subreddit"),
            "ups": post.get("ups"),
            "num_comments": post.get("num_comments"),
            "link_flair": post.get("link_flair_text"),
        },
    )
