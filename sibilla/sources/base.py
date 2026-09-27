"""SourcePlugin contract and the JudgeConfig dataclass (docs/02).

A source is a plugin exposing ``fetch()`` plus its own JudgeConfig: an
arXiv abstract and a tweet are different species of content, so scales,
question wording, thresholds and quadrant cuts are all per-source. The
state stays shared and unique.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sibilla.judge.base import Question


@dataclass(frozen=True)
class JudgeConfig:
    """Per-source question set, budget and quadrant cuts (docs/02)."""

    source: str
    version: str
    char_budget: int  # max chars per item sent to the judge; truncation is deterministic
    questions: tuple[Question, ...]
    quadrant_thresholds: dict[str, float] = field(default_factory=dict)  # read/skim cuts on composite

    @property
    def read_threshold(self) -> float:
        return self.quadrant_thresholds.get("read", 7.0)

    @property
    def skim_threshold(self) -> float:
        return self.quadrant_thresholds.get("skim", 4.0)


class SourceFetchError(RuntimeError):
    """A source API is unreachable/hard-failing — the pipeline isolates it
    and the map renders with a missing-source note (docs/01 failure table)."""


@dataclass(frozen=True)
class RawItem:
    """Heterogeneous source output, pre-normalization."""

    source: str
    native_id: str
    title: str
    url: str | None = None
    author: str | None = None
    published: datetime | None = None
    body: str = ""
    meta: dict[str, object] = field(default_factory=dict)


class SourcePlugin(Protocol):
    """Every source implements this (docs/02); adding one never touches the core."""

    name: str
    judge_config: JudgeConfig

    def fetch(self, since: datetime) -> list[RawItem]: ...


def truncate_to_budget(text: str, char_budget: int) -> tuple[str, bool]:
    """Deterministic truncation before the item ever reaches the backend (docs/03)."""
    if len(text) <= char_budget:
        return text, False
    return text[:char_budget].rstrip(), True


def item_text(title: str | None, body: str, url_domain: str | None = None) -> str:
    """Canonical item text sent to the judge: title + optional domain + body."""
    parts: list[str] = []
    if title:
        parts.append(title.strip())
    if url_domain:
        parts.append(f"source: {url_domain}")
    if body:
        parts.append(body.strip())
    return "\n".join(p for p in parts if p)


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        return host.removeprefix("www.") or None
    except ValueError:
        return None


def sources_registry() -> dict[str, type]:
    """Lazy import map name → plugin class (avoids import cycles at package load)."""
    from sibilla.sources.arxiv import ArxivSource
    from sibilla.sources.hackernews import HackerNewsSource
    from sibilla.sources.reddit import RedditSource
    from sibilla.sources.twitter_x import XSource

    return {s.name: s for s in (ArxivSource, HackerNewsSource, RedditSource, XSource)}
