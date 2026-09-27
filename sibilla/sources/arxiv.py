"""arXiv source — v0 (docs/02).

Atom API, categories from config (default cs.AI, cs.CL, cs.LG), window =
last 24h, 1 req / 3s etiquette. Item body: title + abstract (~350 tokens).
JudgeConfig focus: substance — long-form, low-noise input.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import requests

from sibilla.config import ArxivConfig
from sibilla.judge.configs import ARXIV_CONFIG
from sibilla.sources.base import RawItem, SourceFetchError

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}
ARXIV_API = "https://export.arxiv.org/api/query"
PAGE_SIZE = 100
MAX_PAGES = 10
ETIQUETTE_S = 3.0  # arXiv asks for 1 request / 3 seconds


class ArxivSource:
    name = "arxiv"
    judge_config = ARXIV_CONFIG

    def __init__(self, cfg: ArxivConfig | None = None, session: requests.Session | None = None):
        self.cfg = cfg or ArxivConfig()
        self.session = session or requests.Session()

    def fetch(self, since: datetime) -> list[RawItem]:
        since = since.astimezone(timezone.utc)
        query = " OR ".join(f"cat:{c}" for c in self.cfg.categories)
        items: list[RawItem] = []
        for page in range(MAX_PAGES):
            params: dict[str, Any] = {
                "search_query": query,
                "start": page * PAGE_SIZE,
                "max_results": PAGE_SIZE,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            try:
                resp = self.session.get(ARXIV_API, params=params, timeout=30)
            except requests.RequestException as exc:
                raise SourceFetchError(f"arXiv Atom API unreachable: {exc}") from exc
            if resp.status_code != 200:
                raise SourceFetchError(f"arXiv Atom API {resp.status_code}")
            entries = _parse_atom(resp.text)
            if not entries:
                break
            for entry in entries:
                published = entry.get("published")
                if published is not None and published < since:
                    return items  # sorted descending — nothing older is in window
                items.append(_to_raw_item(entry))
            if len(entries) < PAGE_SIZE:
                break
            time.sleep(ETIQUETTE_S)
        return items


def _parse_atom(text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(text)
    out: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", ATOM_NS):
        arxiv_id = (entry.findtext("a:id", default="", namespaces=ATOM_NS) or "").rsplit("/", 1)[-1]
        out.append(
            {
                "id": arxiv_id,
                "title": " ".join((entry.findtext("a:title", default="", namespaces=ATOM_NS) or "").split()),
                "summary": (entry.findtext("a:summary", default="", namespaces=ATOM_NS) or "").strip(),
                "published": _parse_ts(entry.findtext("a:published", namespaces=ATOM_NS)),
                "updated": _parse_ts(entry.findtext("a:updated", namespaces=ATOM_NS)),
                "authors": [
                    a.findtext("a:name", default="", namespaces=ATOM_NS) for a in entry.findall("a:author", ATOM_NS)
                ],
                "primary_category": _category(entry),
            }
        )
    return out


def _category(entry: ET.Element) -> str | None:
    primary = entry.find("a:primary_category", ATOM_NS)
    if primary is not None:
        return primary.get("term")
    cat = entry.find("a:category", ATOM_NS)
    return cat.get("term") if cat is not None else None


def _parse_ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_raw_item(entry: dict[str, Any]) -> RawItem:
    arxiv_id = entry["id"]
    base_id = arxiv_id.split("v")[0] if "v" in arxiv_id else arxiv_id
    authors = [a for a in entry["authors"] if a]
    return RawItem(
        source="arxiv",
        native_id=arxiv_id,
        title=entry["title"],
        url=f"https://arxiv.org/abs/{arxiv_id}",
        author=", ".join(authors[:4]) + (" et al." if len(authors) > 4 else ""),
        published=entry["published"],
        body=f"{entry['title']}\n\n{entry['summary']}",
        meta={"primary_category": entry.get("primary_category"), "arxiv_base_id": base_id},
    )
